"""
CORRELATE — BTC lead-lag signal.

BTC often reprices before ETH/SOL adjust. When BTC's market price moves
>=0.35%, we trade ETH or SOL in the same direction within 3 minutes.
"""
import time
import logging
from typing import Optional
import config
import feeds
from strategies.base import BaseStrategy, TradeSignal
from strategies.utils import (
    btc_spot_move_pct, momentum_score, regime_score, certainty_to_prob,
)
from strategies.manager import kelly_size, max_ev_price

log = logging.getLogger("strat.correlate")


class CorrelateStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("CORRELATE")

    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float = None) -> Optional[TradeSignal]:
        asset = market["asset"]
        if asset not in {"ETH", "SOL"}:
            return None

        tf        = market["timeframe"]
        threshold = config.CORRELATION_THRESHOLD

        spot_move, spot_dir = btc_spot_move_pct(config.CORRELATION_WINDOW_SEC, state=state)

        if spot_move < threshold:
            sig_time = getattr(state, "btc_signal_time", {}).get(tf)
            if not sig_time:
                return None
            age = time.time() - sig_time
            if age > config.CORRELATION_WINDOW_SEC:
                return None
            if getattr(state, "btc_signal_move", {}).get(tf, 0.0) < threshold:
                return None
            direction = getattr(state, "btc_signal_direction", {}).get(tf)
            freshness = 1.0 - (age / config.CORRELATION_WINDOW_SEC)
        else:
            direction = spot_dir
            freshness = 1.0

        if not direction:
            return None

        secs = market.get("secs_to_close", 0)
        if secs < 300:
            return None

        tgt_thresh = market.get("threshold")
        tgt_spot   = spot_price if spot_price is not None else feeds.spot.get(asset)
        if tgt_thresh and tgt_spot:
            if direction == "UP"   and tgt_spot < tgt_thresh: return None
            if direction == "DOWN" and tgt_spot > tgt_thresh: return None

        outcome    = "YES" if direction == "UP" else "NO"
        outcome_id = market["yes_id"] if outcome == "YES" else market["no_id"]
        mkt_price  = market["yes_price"] if outcome == "YES" else market["no_price"]

        if mkt_price > config.CORRELATE_MAX_MARKET_PRICE:
            return None

        regime = regime_score(asset, state)
        if regime < config.CORRELATE_MIN_REGIME:
            return None

        mom = momentum_score(asset, outcome, state)
        if mom < -0.4:
            return None

        certainty = min(
            config.CORRELATE_BASE_CERTAINTY * freshness * (1.0 + 0.20 * mom), 0.99
        )
        w_est    = certainty_to_prob(certainty)
        fee_rate = market.get("fee_rate", 0.02)
        ev_ceil  = max_ev_price(w_est, mkt_price, fee_rate)
        if mkt_price >= ev_ceil:
            return None

        raw_edge = w_est - mkt_price
        size     = kelly_size(w_est, mkt_price, fee_rate,
                              asset=asset, state=state, learned=learned,
                              strategy_name="CORRELATE")

        return TradeSignal(
            strategy="CORRELATE",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=tf,
            outcome=outcome,
            outcome_id=outcome_id,
            certainty=certainty,
            win_prob=w_est,
            market_price=mkt_price,
            size_pct=size,
            reason=f"BTC {direction} {spot_move:.2%} freshness={freshness:.2f} mom={mom:+.2f}",
            title=market.get("title", ""),
            momentum_at_entry=mom,
            regime_at_entry=regime,
            edge_at_entry=raw_edge,
        )
