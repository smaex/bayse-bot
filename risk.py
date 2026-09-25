"""
Risk manager: position sizing, drawdown control, exposure limits.
"""

import logging
import time
from config import MAX_DRAWDOWN_STOP, MAX_PORTFOLIO_EXPOSURE, TRADING_TIMEZONE

log = logging.getLogger(__name__)


def position_is_filled(pos: dict) -> bool:
    """True only when the exchange has confirmed shares for this position.

    A resting LIMIT order that has not been matched yet is deliberately NOT a
    fill: the bot asked for it, the exchange has not executed it. Exposure and
    the drought clock must use the same definition of "we are in this trade".
    """
    if pos.get("confirmed_filled"):
        return True
    try:
        return float(pos.get("filled_quantity") or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


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
        from zoneinfo import ZoneInfo
        today = datetime.datetime.now(ZoneInfo(TRADING_TIMEZONE)).strftime("%Y-%m-%d")
        if self.last_reset_date != today:
            log.info(f"Daily risk reset: profit was ₦{self.daily_realized_pnl:,.0f}")
            self.daily_realized_pnl = 0.0
            self.last_reset_date = today
            # If the risk manager paused from yesterday's drawdown, reset the baseline for the new day
            # so the bot never stays permanently silent across trading days
            if self.paused:
                log.info("Daily risk reset: clearing drawdown pause for new trading day")
                self.paused = False
                self.peak_balance = self.current_free_cash
                self._dd_breach_since = 0.0

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
        """Sum of capital in all open positions and resting maker orders.

        Bayse reserves order funds from availableBalance immediately upon placing limit orders.
        Including all open positions and resting orders in deployed capital ensures that equity
        (free_cash + deployed) reflects true account net worth and prevents false drawdown
        halts when maker orders are resting.

        This is the *balance-sheet* view. It is NOT the number the entry-exposure
        cap uses — see ``deployed_filled`` for why.
        """
        return sum(
            p.get("amount_ngn", 0.0)
            for p in self.open_positions.values()
        )

    def deployed_filled(self) -> float:
        """Capital in positions the exchange has actually executed.

        The README's rule is that exposure is created only from exchange-
        confirmed filled quantity. A resting, unmatched MAKER quote has no
        directional risk (and its cash is already excluded from free_cash), so
        charging it against MAX_PORTFOLIO_EXPOSURE let a single unfilled quote
        exhaust the whole budget for every other strategy on the account —
        observed in production as "MAKER resting orders freeze SNIPE", logged
        at INFO with no gate counter and no notification.
        """
        return sum(
            p.get("amount_ngn", 0.0)
            for p in self.open_positions.values()
            if position_is_filled(p)
        )

    def deployed_resting(self) -> float:
        """Capital committed to orders that are still waiting to be matched."""
        return max(0.0, self.deployed() - self.deployed_filled())

    def can_trade(self, balance: float, amount: float, max_exposure: float = 0.30) -> bool:
        max_exposure = min(max_exposure, MAX_PORTFOLIO_EXPOSURE)
        if (self.deployed_filled() + amount) > balance * max_exposure:
            log.info(
                f"Exposure cap: filled=₦{self.deployed_filled():,.0f} + "
                f"₦{amount:,.0f} > {max_exposure:.0%} of ₦{balance:,.0f} "
                f"(resting orders ₦{self.deployed_resting():,.0f} excluded)"
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
            self.probation_trades_left = 1
            log.warning(f"Risk Manager: Entering PROBATION for next 1 trade after loss of ₦{abs(pnl):,.0f}")
        elif pnl > 0 and self.probation_trades_left > 0:
            self.probation_trades_left -= 1
            if self.probation_trades_left == 0:
                log.info("Risk Manager: Probation cleared! Returning to full position sizes.")

    def add_position(self, market_id: str, pos: dict):
        pos.setdefault("placed_at", time.time())  # always stamp entry time
        pos.setdefault("market_id", market_id)
        self.open_positions[market_id] = pos
        log.info(
            f"Position opened [{pos['strategy']}] "
            f"{pos['outcome']} on {market_id} @ {pos['entry_price']:.3f} | "
            f"₦{pos['amount_ngn']:,.0f}"
        )

    def remove_position(self, market_id: str, *, order_id: str = "", outcome_id: str = ""):
        """Remove one tracked position without deleting sibling hedge legs."""
        if order_id or outcome_id:
            for key, pos in list(self.open_positions.items()):
                if pos.get("market_id", key) != market_id:
                    continue
                if order_id and pos.get("order_id") != order_id:
                    continue
                if outcome_id and pos.get("outcome_id") != outcome_id:
                    continue
                self.open_positions.pop(key, None)
                return
        self.open_positions.pop(market_id, None)

    def has_correlated_open_position(self, asset: str, outcome: str, timeframe: str = "15min", certainty: float = 0.0, strategy: str = "") -> bool:
        """
        Prevents stacking weak correlated bets on BTC, ETH, and SOL.
        Macro Consensus Exception: If certainty >= 0.65 (strong macro breakout where
        the model has high mathematical edge and conviction), all 3 assets are allowed
        to trade to capture the multi-asset winning sweep!
        Only checks correlation against positions of the same strategy family (directional vs directional).
        """
        if certainty >= 0.65:
            return False  # High macro conviction — allow the multi-asset sweep!

        crypto_assets = {"BTC", "ETH", "SOL"}
        if asset not in crypto_assets:
            return False
        
        is_maker_strat = strategy.upper() in {"MAKER", "MIDMARKET_MAKER"}

        for pos in self.open_positions.values():
            pos_strat = pos.get("strategy", "").upper()
            pos_is_maker = pos_strat in {"MAKER", "MIDMARKET_MAKER"}
            # Only correlate directional taker positions against directional taker positions
            if strategy and (is_maker_strat != pos_is_maker):
                continue

            if (pos.get("asset") in crypto_assets
                    and pos.get("outcome") == outcome
                    and pos.get("timeframe") == timeframe):
                return True
        return False

    def already_in(self, market_id: str, asset: str = "", is_hedge: bool = False,
                   strategy: str = "", outcome: str = "") -> bool:
        if is_hedge:
            # Matched-pair hedge explicitly acquires the opposite side to lock in redemption spread
            return False
        if market_id in self.pending_markets:
            return True
        makers = {"MAKER", "MIDMARKET_MAKER"}
        incoming_strat = (strategy or "").upper()
        # A market can hold more than one tracked entry: the executor keys a
        # second position as "<market_id>:<outcome>:<order_id>". Looking up the
        # bare key alone made those entries invisible to this check.
        for key, pos in self.open_positions.items():
            if key != market_id and not str(key).startswith(f"{market_id}:"):
                continue
            existing_strat = str(pos.get("strategy") or "").upper()
            is_maker_pair = bool(incoming_strat and existing_strat) and (
                (incoming_strat in makers) != (existing_strat in makers)
            )
            if not is_maker_pair:
                # Active position or pending limit order already exists for this
                # exact market and strategy family.
                return True
            # A passive MAKER quote and a directional taker may share a market
            # only on the SAME outcome. Opposite sides of one binary cost more
            # than the 1.00 they can ever pay out together, so one leg is a
            # guaranteed loss. An unknown side is treated as a conflict.
            existing_outcome = str(pos.get("outcome") or "").upper()
            if not outcome or existing_outcome != str(outcome).upper():
                log.info(
                    f"BLOCK opposite-side entry on {market_id}: {incoming_strat} "
                    f"{outcome or '?'} vs open {existing_strat} {existing_outcome or '?'}"
                )
                return True

        # Asset-level deduplication:
        # Directional takers (SNIPE, FRONTRUN, CORRELATE) deduplicate against each other.
        # Passive liquidity makers (MAKER) deduplicate against MAKER.
        # MAKER and SNIPE do NOT block each other on the asset level.
        if asset:
            incoming_is_maker = strategy.upper() in {"MAKER", "MIDMARKET_MAKER"}
            for existing_pos in self.open_positions.values():
                if existing_pos.get("asset") == asset:
                    existing_is_maker = existing_pos.get("strategy", "").upper() in {"MAKER", "MIDMARKET_MAKER"}
                    if strategy and (incoming_is_maker != existing_is_maker):
                        # Different execution nature: MAKER spread capture vs SNIPE directional take.
                        # Do not block across the boundary!
                        continue

                    log.info(
                        f"BLOCK duplicate asset entry: already holding {asset} "
                        f"({existing_pos.get('outcome')} @ {existing_pos.get('entry_price', 0):.3f}, "
                        f"strategy={existing_pos.get('strategy', 'UNKNOWN')})"
                    )
                    return True
        return False

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
