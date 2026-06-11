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

        # Need enough time to place both legs before market closes
        if market.get("secs_to_close", 0) < 30:
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
