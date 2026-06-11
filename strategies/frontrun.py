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

        # Oracle price must be fresh (< 5 seconds old)
        oracle_p, oracle_t = feeds_direct.get_direct_price(asset)
        if not oracle_p or (time.time() - oracle_t > 5.0):
            return None

        bayse_spot = feeds.spot.get(asset)
        if not bayse_spot:
            return None

        bias = (oracle_p - bayse_spot) / bayse_spot

        trigger = config.FRONTRUN_BIAS_TRIGGER
        if abs(bias) < trigger:
            return None

        outcome = "YES" if bias > 0 else "NO"

        # Don't enter if the market is already doomed
        if market.get("threshold") and spot_price and secs > 0:
            dist_pct = (spot_price - market["threshold"]) / market["threshold"]
            prob = win_probability(dist_pct, secs, asset)
            if outcome == "YES" and prob < 0.05:
                return None
            if outcome == "NO" and prob > 0.95:
                return None

        market_price = market["yes_price"] if outcome == "YES" else market["no_price"]
        if market_price > 0.90:
            return None   # move already priced in

        # Certainty scales with bias strength
        certainty = min(0.50 + abs(bias) * 100.0, 0.95)
        size_pct  = min(0.01 + abs(bias) * 5.0, 0.03)

        log.info(f"FRONTRUN | {asset} bias={bias:+.3%} → {outcome}")

        return TradeSignal(
            strategy="FRONTRUN",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=tf,
            outcome=outcome,
            outcome_id=market["yes_id"] if outcome == "YES" else market["no_id"],
            certainty=certainty,
            win_prob=0.78,
            market_price=market_price,
            size_pct=size_pct,
            reason=f"Oracle lag {bias:+.3%}",
            title=market.get("title", ""),
        )
