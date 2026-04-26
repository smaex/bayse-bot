"""
Risk manager: position sizing, drawdown control, exposure limits.
"""

import logging
from config import MAX_DRAWDOWN_STOP, MAX_PORTFOLIO_EXPOSURE

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self.peak_balance: float = 0.0
        self.paused: bool = False
        self.open_positions: dict[str, dict] = {}  # market_id → position

    def update_peak(self, balance: float):
        if balance > self.peak_balance:
            self.peak_balance = balance

    def check_drawdown(self, balance: float) -> bool:
        if self.peak_balance <= 0:
            self.peak_balance = balance
            return True
        dd = (self.peak_balance - balance) / self.peak_balance
        if dd >= MAX_DRAWDOWN_STOP:
            if not self.paused:
                log.warning(
                    f"DRAWDOWN STOP hit: {dd:.1%} from peak ₦{self.peak_balance:,.0f}. "
                    "All trading paused."
                )
            self.paused = True
            return False
        if self.paused and dd < MAX_DRAWDOWN_STOP * 0.25:
            log.info(f"Drawdown recovered to {dd:.1%} — resuming trading")
            self.paused = False
        return not self.paused

    def deployed(self) -> float:
        return sum(p["amount_ngn"] for p in self.open_positions.values())

    def can_trade(self, balance: float, amount: float, max_exposure: float = 0.30) -> bool:
        if (self.deployed() + amount) > balance * max_exposure:
            log.debug(
                f"Exposure cap: deployed=₦{self.deployed():,.0f} + "
                f"₦{amount:,.0f} > {max_exposure:.0%} of ₦{balance:,.0f}"
            )
            return False
        return True

    def add_position(self, market_id: str, pos: dict):
        self.open_positions[market_id] = pos
        log.info(
            f"Position opened [{pos['strategy']}] "
            f"{pos['outcome']} on {market_id} @ {pos['entry_price']:.3f} | "
            f"₦{pos['amount_ngn']:,.0f}"
        )

    def remove_position(self, market_id: str):
        self.open_positions.pop(market_id, None)

    def already_in(self, market_id: str) -> bool:
        return market_id in self.open_positions

    def summary(self, balance: float) -> str:
        dd = 0.0
        if self.peak_balance > 0:
            dd = (self.peak_balance - balance) / self.peak_balance
        return (
            f"Balance: ₦{balance:,.0f} | "
            f"Peak: ₦{self.peak_balance:,.0f} | "
            f"Drawdown: {dd:.1%} | "
            f"Open positions: {len(self.open_positions)} | "
            f"Deployed: ₦{self.deployed():,.0f} | "
            f"{'⛔ PAUSED' if self.paused else '✅ ACTIVE'}"
        )
