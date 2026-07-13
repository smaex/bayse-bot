"""
ARB strategy — detects when YES + NO prices sum below 1.00.
Risk-free: buy both sides and burn pairs for guaranteed profit.
Only fires on CLOB markets where the two outcomes can be independently priced.
"""
import logging
from typing import Optional
import config
from strategies.base import BaseStrategy, TradeSignal

log = logging.getLogger("strat.arb")


class ArbStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("ARB")

    async def evaluate(self, market: dict, learned: dict, state, spot_price: float = None) -> Optional[TradeSignal]:
        yes_p = market.get("yes_price", 0.5)
        no_p  = market.get("no_price",  0.5)
        total = yes_p + no_p

        if total >= config.ARB_TRIGGER:
            return None

        # Both legs must fill before close; 120s gives time for two orders + partial-fill
        # recovery. 30s was too short — partial fills at entry price 0.91–0.97 produced
        # 100% losses on 6 of 13 ARB trades in production.
        if market.get("secs_to_close", 0) < config.ARB_MIN_TIME_SECS:
            return None

        edge = 1.0 - total

        return TradeSignal(
            strategy="ARB",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=market["asset"],
            timeframe=market["timeframe"],
            outcome="ARB",
            outcome_id=market["yes_id"],   # executor handles both sides
            certainty=1.0,
            win_prob=1.0,
            market_price=total,
            size_pct=0.02,                 # sized inside execute_arb
            reason=f"YES({yes_p:.3f})+NO({no_p:.3f})={total:.3f} | edge={edge:.3f}",
            title=market.get("title", ""),
        )
