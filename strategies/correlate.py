import time
import logging
from typing import Optional
import config
import feeds
from strategies.base import BaseStrategy, TradeSignal
from strategies.utils import (
    btc_spot_move_pct, momentum_score, regime_score, certainty_to_prob
)
from strategies.manager import kelly_size, max_ev_price

log = logging.getLogger("strat.correlate")

class CorrelateStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("CORRELATE")

    async def evaluate(self, market: dict, learned: dict, state: any, spot_price: float = None) -> Optional[TradeSignal]:
        asset = market["asset"]
        # CORRELATE trades ETH/SOL when BTC moves — skip BTC itself (it's the leader)
        if asset not in {"ETH", "SOL"}:
            return None

        tf = market["timeframe"]
        threshold = config.CORRELATION_THRESHOLD
        
        # 1. BTC Spot Move Detection
        spot_move, spot_dir = btc_spot_move_pct(config.CORRELATION_WINDOW_SEC, state=state)
        
        if spot_move < threshold:
            # Fallback: check market-price signal
            signal_time = getattr(state, 'btc_signal_time', {}).get(tf)
            if not signal_time: return None
            age = time.time() - signal_time
            if age > config.CORRELATION_WINDOW_SEC or state.btc_signal_move.get(tf, 0.0) < threshold:
                return None
            direction = state.btc_signal_direction.get(tf)
            freshness = 1.0 - (age / config.CORRELATION_WINDOW_SEC)
        else:
            direction = spot_dir
            freshness = 1.0

        # 2. Time Guard
        secs = market.get("secs_to_close", 0)
        if secs < 300: return None

        # 3. Target Alignment Guard
        target_threshold = market.get("threshold")
        target_spot = spot_price if spot_price is not None else feeds.spot.get(asset)
        if target_threshold and target_spot:
            if direction == "UP" and target_spot < target_threshold: return None
            if direction == "DOWN" and target_spot > target_threshold: return None

        outcome = "YES" if direction == "UP" else "NO"
        outcome_id = market["yes_id"] if direction == "UP" else market["no_id"]
        market_price = market["yes_price"] if direction == "UP" else market["no_price"]

        # 4. Market Repricing Guard
        if market_price > config.CORRELATE_MAX_MARKET_PRICE: return None

        # 5. Regime & Momentum
        regime = regime_score(asset, state)
        if regime < config.CORRELATE_MIN_REGIME: return None

        mom_dir = "YES" if direction == "UP" else "NO"
        target_mom = momentum_score(asset, mom_dir, state)
        if target_mom < -0.4: return None

        # 6. Certainty & EV
        certainty = min(config.CORRELATE_BASE_CERTAINTY * freshness * (1.0 + 0.20 * target_mom), 0.99)
        w_est = certainty_to_prob(certainty)
        fee_rate = market.get("fee_rate", 0.04)
        ev_ceiling = max_ev_price(w_est, market_price, fee_rate)

        if market_price >= ev_ceiling: return None

        # 7. Sizing
        size = kelly_size(w_est, market_price, fee_rate, asset=asset, state=state, learned=learned, strategy_name="CORRELATE")

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
            market_price=market_price,
            size_pct=size,
            reason=f"BTC {direction} {spot_move:.2%} | freshness={freshness:.2f} mom={target_mom:+.2f}",
            title=market["title"]
        )
