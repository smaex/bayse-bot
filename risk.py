"""
Risk manager: position sizing, drawdown control, exposure limits.
"""

import logging
import time
from config import MAX_DRAWDOWN_STOP, MAX_PORTFOLIO_EXPOSURE

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self.peak_balance: float = 0.0
        self.daily_target: float = 0.0
        self.mode: str = "balanced"
        self.paused: bool = False
        self.current_free_cash: float = 0.0
        self.open_positions: dict[str, dict] = {}  # market_id → position
        self.daily_realized_pnl: float = 0.0
        self.last_reset_date: str = ""
        self.probation_trades_left: int = 0
        self.pending_markets: set[str] = set()  # market_id lock during execution
        self._dd_breach_since: float = 0.0  # debounce: when the current drawdown breach started

    @property
    def target_hit(self) -> bool:
        # BUG FIX: this previously compared self.peak_balance (absolute
        # account balance, e.g. ₦857) against self.daily_target (a PROFIT
        # target, e.g. ₦85.70 — 10% of starting balance). Absolute balance
        # will almost always exceed a modest profit target for any funded
        # account, so this returned True almost immediately once
        # daily_target was ever set to a nonzero value — silently blocking
        # every future evaluation for the rest of the day. Compare PROFIT
        # against the PROFIT target instead.
        return self.daily_target > 0 and self.daily_realized_pnl >= self.daily_target

    @property
    def max_drawdown_hit(self) -> bool:
        return self.paused

    def update_peak(self, balance: float):
        if balance > self.peak_balance:
            self.peak_balance = balance

    def reset_daily_if_needed(self):
        import datetime
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        if self.last_reset_date != today:
            log.info(f"Daily risk reset: profit was ₦{self.daily_realized_pnl:,.0f}")
            self.daily_realized_pnl = 0.0
            self.last_reset_date = today

    def update_balance(self, balance: float):
        self.update_peak(balance)
        self.check_drawdown(balance)

    def check_drawdown(self, balance: float) -> bool:
        if self.peak_balance <= 0:
            self.peak_balance = balance
            return True
        dd = (self.peak_balance - balance) / self.peak_balance
        if dd >= MAX_DRAWDOWN_STOP:
            if self._dd_breach_since == 0.0:
                # First time seeing this breach — start the clock but don't
                # act yet. A single noisy/transient balance reading (seen
                # repeatedly in production, not fully explained by resolved
                # trades) can no longer trigger a false pause on its own.
                self._dd_breach_since = time.time()
                return not self.paused
            if time.time() - self._dd_breach_since >= 25:
                # Breach has persisted across at least one extra check cycle
                # — this is a real, sustained drawdown, not a blip.
                if not self.paused:
                    log.warning(
                        f"DRAWDOWN STOP hit: {dd:.1%} from peak ₦{self.peak_balance:,.0f}. "
                        "All trading paused."
                    )
                self.paused = True
            return not self.paused
        # Drawdown condition no longer true — reset the debounce clock.
        self._dd_breach_since = 0.0
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

    def is_in_strict_mode(self) -> bool:
        """Returns True if we have hit 80% of our daily target — only take high-conviction signals."""
        self.reset_daily_if_needed()
        if self.daily_target > 0:
            if self.daily_realized_pnl >= self.daily_target * 0.8:
                return True
        return False

    def is_on_probation(self) -> bool:
        return self.probation_trades_left > 0

    def add_pnl(self, pnl: float):
        self.daily_realized_pnl += pnl
        if pnl < 0:
            self.probation_trades_left = 2
            log.warning(f"Risk Manager: Entering PROBATION for next 2 trades after loss of ₦{abs(pnl):,.0f}")
        elif pnl > 0 and self.probation_trades_left > 0:
            self.probation_trades_left -= 1
            if self.probation_trades_left == 0:
                log.info("Risk Manager: Probation cleared! Returning to full position sizes.")

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
        return market_id in self.open_positions or market_id in self.pending_markets

    def lock_market(self, market_id: str):
        self.pending_markets.add(market_id)

    def unlock_market(self, market_id: str):
        self.pending_markets.discard(market_id)

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
