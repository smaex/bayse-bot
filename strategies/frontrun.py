"""
FRONTRUN — latency arbitrage between the Binance oracle and the Bayse AMM.
When the oracle has moved >0.20% and Bayse hasn't caught up yet, we enter
before the AMM reprices.
"""
import logging
import time
from typing import Optional
import feeds_direct
import feeds
import config
from strategies.base import BaseStrategy, TradeSignal
from strategies.utils import win_probability

log = logging.getLogger("strat.frontrun")

# Minimum trade in NGN — Bayse platform minimum is ₦100
MIN_TRADE_NGN = 100.0


class FrontrunStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("FRONTRUN")

    async def evaluate(self, market: dict, learned: dict, state, spot_price: float = None) -> Optional[TradeSignal]:
        asset = market["asset"]
        tf    = market.get("timeframe", "")
        secs  = market.get("secs_to_close", 0)

        # Only short candles where latency edge hasn't decayed
        if config.FRONTRUN_ALLOWED_TFS and tf not in config.FRONTRUN_ALLOWED_TFS:
            return None
        if secs < 60:
            return None

        # Oracle price must be fresh (< 10 seconds old)
        oracle_p, oracle_t = feeds_direct.get_direct_price(asset)
        if not oracle_p or (time.time() - oracle_t > 10.0):
            return None

        bayse_spot = feeds.spot.get(asset)
        if not bayse_spot:
            return None

        # BUG FIX: compute live_spot here so the latency-bias comparison uses
        # the freshest available price, not the (potentially older) cached feed
        # value. Previously live_spot was defined below and bias was computed
        # with bayse_spot — defeating the purpose of passing spot_price in.
        live_spot = spot_price if spot_price is not None else bayse_spot
        bias = (oracle_p - live_spot) / live_spot

        trigger = config.FRONTRUN_BIAS_TRIGGER
        if abs(bias) < trigger:
            return None

        outcome = "YES" if bias > 0 else "NO"

        # Market data-quality guard — same fix as SNIPE. YES+NO should sum
        # close to 1.0 in any valid, liquid binary market.
        price_sum = market.get("yes_price", 0) + market.get("no_price", 0)
        if not (0.90 <= price_sum <= 1.05):
            log.info(
                f"FRONTRUN {asset} — bad market data "
                f"(yes={market.get('yes_price',0):.3f} no={market.get('no_price',0):.3f})"
            )
            return None

        # Don't enter if the market is already doomed
        threshold = market.get("threshold")
        if not threshold:
            return None

        # Strict Directional Alignment with Strike Threshold:
        # NEVER buy YES if spot or oracle is below the strike threshold!
        # NEVER buy NO if spot or oracle is above the strike threshold!
        # This completely prevents buying into underdog traps where an oracle lag
        # occurs while the asset is still on the losing side of the threshold.
        dist_pct = (live_spot - threshold) / threshold
        oracle_dist = (oracle_p - threshold) / threshold

        if outcome == "YES" and (dist_pct < 0.0000 or oracle_dist < 0.0000):
            log.info(
                f"FRONTRUN SKIP {asset} YES — spot/oracle below threshold "
                f"(dist={dist_pct:+.4%}, oracle_dist={oracle_dist:+.4%})"
            )
            return None

        if outcome == "NO" and (dist_pct > 0.0000 or oracle_dist > 0.0000):
            log.info(
                f"FRONTRUN SKIP {asset} NO — spot/oracle above threshold "
                f"(dist={dist_pct:+.4%}, oracle_dist={oracle_dist:+.4%})"
            )
            return None

        market_price = market["yes_price"] if outcome == "YES" else market["no_price"]
        if market_price < 0.40 or market_price > 0.65:
            return None   # must be in actionable 0.40 - 0.65 window

        # Calculate dynamic win probability based on fresh oracle price
        w_oracle = win_probability(oracle_dist, secs, asset)
        win_prob = w_oracle if outcome == "YES" else 1.0 - w_oracle

        # High-Conviction Probability Floor:
        # A directional frontrun bet MUST have at least 55% statistical win probability.
        if win_prob < 0.55:
            log.info(
                f"FRONTRUN SKIP {asset} {outcome} — win_prob {win_prob:.1%} < 55% floor"
            )
            return None

        # Import manager utilities
        from strategies.utils import probability_to_certainty
        from strategies.manager import kelly_size, max_ev_price

        certainty = probability_to_certainty(win_prob)
        if certainty < 0.50:
            log.info(
                f"FRONTRUN SKIP {asset} {outcome} — certainty {certainty:.2f} < 0.50 floor"
            )
            return None

        # BUG FIX: explicit positive-EV gate before committing to a Kelly bet.
        # kelly_size returns 0 for negative-EV signals, but max_ev_price also
        # catches borderline cases where fees exactly consume the edge.
        fee_rate = market.get("fee_rate", 0.02)
        ev_ceil = max_ev_price(win_prob, market_price, fee_rate)
        if market_price >= ev_ceil:
            log.info(
                f"FRONTRUN SKIP {asset} {outcome} — market_price {market_price:.3f} "
                f">= ev_ceil {ev_ceil:.3f} (win_prob={win_prob:.1%})"
            )
            return None

        # Compute Kelly size
        size_pct = kelly_size(
            win_prob, market_price, fee_rate,
            asset=asset, state=state, learned=learned,
            strategy_name="FRONTRUN"
        )
        if size_pct <= 0:
            return None

        log.info(f"FRONTRUN | {asset} bias={bias:+.3%} → {outcome} | w_oracle={win_prob:.1%}")

        return TradeSignal(
            strategy="FRONTRUN",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=tf,
            outcome=outcome,
            outcome_id=market["yes_id"] if outcome == "YES" else market["no_id"],
            certainty=certainty,
            win_prob=win_prob,
            market_price=market_price,
            size_pct=size_pct,
            reason=f"Oracle lag {bias:+.3%}",
            title=market.get("title", ""),
        )
