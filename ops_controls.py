"""Pure operational controls: position drift, stale-order sweep, mark-to-market.

Kept free of I/O so the decisions can be unit-tested. The bot wires these to
the exchange client and to the pause mechanism.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Iterable

from risk import _position_amount, position_is_filled, share_quantities_match

CANCEL_BATCH_LIMIT = 100  # Bayse DELETE /v1/pm/orders/batch maximum


class MarkUnavailable(Exception):
    """A tracked position cannot be priced; equity must not be computed from it."""


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _held_by_outcome(portfolio_rows) -> dict[str, dict]:
    held: dict[str, dict] = {}
    for row in portfolio_rows or []:
        if isinstance(row, dict) and row.get("outcomeId") is not None:
            held[str(row["outcomeId"])] = row
    return held


def _tracked_shares(pos: dict) -> float | None:
    return _finite(pos.get("filled_quantity"))


# ── Position drift ───────────────────────────────────────────────────────────

def find_share_drift(positions: dict[str, dict], portfolio_rows) -> list[dict]:
    """Tracked filled shares that do not match the exchange balance, either way."""
    held = _held_by_outcome(portfolio_rows)
    drift = []
    for market_id, pos in positions.items():
        if not position_is_filled(pos) or not pos.get("outcome_id"):
            continue
        tracked = _tracked_shares(pos)
        if tracked is None:
            continue
        row = held.get(str(pos["outcome_id"]))
        balance = _finite(row.get("balance")) if row else 0.0
        if balance is None:
            balance = 0.0
        if not share_quantities_match(tracked, balance):
            drift.append({
                "market_id": market_id,
                "outcome_id": str(pos["outcome_id"]),
                "tracked": tracked,
                "held": balance,
            })
    return drift


def untracked_holdings(positions: dict[str, dict], portfolio_rows) -> list[dict]:
    """Exchange holdings the bot is not tracking. Reported, never auto-acted on:
    the account may hold tokens bought outside the bot."""
    tracked = {str(p.get("outcome_id")) for p in positions.values() if p.get("outcome_id")}
    out = []
    for outcome_id, row in _held_by_outcome(portfolio_rows).items():
        balance = _finite(row.get("balance"))
        if outcome_id not in tracked and balance is not None and balance > 1e-6:
            out.append({"outcome_id": outcome_id, "held": balance})
    return out


def split_drift_by_ledger(drift: list[dict], positions: dict[str, dict],
                          ledger_fill_by_order: dict[str, float]) -> tuple[list[dict], list[dict]]:
    """Separate drift the exchange and the ledger agree on from real disagreement.

    ``auto``: the ledger's confirmed fill for the position's order equals the
    exchange balance. The exchange and the ledger agree, so the bot's in-memory
    copy is the stale value and can be corrected without an operator.

    ``manual``: no ledger row, or the ledger and exchange disagree. Only then is
    the cause unknown and the account paused for reconciliation.
    """
    auto: list[dict] = []
    manual: list[dict] = []
    for d in drift:
        pos = positions.get(d["market_id"]) or {}
        ledger = _finite(ledger_fill_by_order.get(str(pos.get("order_id") or "")))
        if ledger is not None and share_quantities_match(ledger, d["held"]):
            auto.append(d)
        else:
            manual.append(d)
    return auto, manual


class DriftGate:
    """Confirms drift only when the same mismatch is seen on two consecutive checks.

    A single mismatch can be a fill or sell that landed between the position
    update and the portfolio read. Pausing on it would be a false alarm.
    """

    def __init__(self):
        self._previous: set = set()

    def observe(self, current: set) -> set:
        confirmed = set(current) & self._previous
        self._previous = set(current)
        return confirmed


# ── Startup stale-order sweep ────────────────────────────────────────────────

def known_order_ids(positions: Iterable[dict], trade_rows: Iterable[dict]) -> set[str]:
    known: set[str] = set()
    for pos in positions:
        for key, value in (pos or {}).items():
            if key.endswith("order_id") and value:
                known.add(str(value))
    for row in trade_rows:
        if row and row.get("order_id"):
            known.add(str(row["order_id"]))
    return known


def orders_to_cancel(open_orders: Iterable[dict], known: set[str]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for order in open_orders:
        oid = str((order or {}).get("id") or (order or {}).get("orderId") or "")
        if oid and oid not in known and oid not in seen:
            seen.add(oid)
            ids.append(oid)
    return ids


def chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ── Mark-to-market ───────────────────────────────────────────────────────────

def mark_positions(positions: dict[str, dict], portfolio_rows, multiplier: float) -> float:
    """Account value of the tracked book, in NGN.

    Filled positions are valued at the exit (bid) price times shares held, which
    is what selling them now would realise. Positions with no confirmed shares
    keep their cost, because the funds are still reserved on a resting order.
    Anything that cannot be priced raises MarkUnavailable so the caller fails
    closed instead of falling back to cost.
    """
    held = _held_by_outcome(portfolio_rows)
    total = 0.0
    for market_id, pos in positions.items():
        if not position_is_filled(pos):
            total += _position_amount(pos)
            continue
        row = held.get(str(pos.get("outcome_id")))
        if row is None:
            raise MarkUnavailable(f"{market_id}: filled position missing from portfolio")
        bid = _finite(row.get("sellPrice"))
        if bid is None or not 0.0 < bid < 1.0:
            raise MarkUnavailable(f"{market_id}: portfolio sell price missing or outside (0, 1)")
        balance = _finite(row.get("balance"))
        if balance is None or balance < 0.0:
            raise MarkUnavailable(f"{market_id}: portfolio balance malformed")
        total += balance * bid * multiplier
    if not math.isfinite(total):
        raise MarkUnavailable("marked value is not finite")
    return total


def gain_lock_state(session_profit: float, peak_profit: float, start_balance: float,
                    arm_fraction: float, keep_fraction: float) -> tuple[float, bool, float | None]:
    """Track the day's peak session gain and decide whether the giveback lock fires.

    Returns ``(new_peak, locked, floor)``. The lock arms once the peak reaches
    ``arm_fraction`` of the day's start balance. From then on, the floor is
    ``keep_fraction`` of the peak, and falling to or below it locks new entries.
    Losses count the same way: a session below the floor is locked.
    """
    peak = max(float(peak_profit or 0.0), float(session_profit))
    if not (arm_fraction > 0.0 and math.isfinite(start_balance) and start_balance > 0.0):
        return peak, False, None
    if peak < start_balance * arm_fraction:
        return peak, False, None
    floor = peak * keep_fraction
    return peak, float(session_profit) <= floor, floor


class PortfolioCache:
    """Short-lived portfolio read shared by the mark and the drift check."""

    def __init__(self, ttl_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self.clock = clock
        self._entries: dict[str, tuple[float, list]] = {}

    async def rows(self, key: str, client, *, max_age: float | None = None) -> list:
        ttl = self.ttl if max_age is None else max_age
        now = self.clock()
        hit = self._entries.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        portfolio = await client.get_portfolio()
        rows = portfolio.get("outcomeBalances", []) if isinstance(portfolio, dict) else []
        if not isinstance(rows, list):
            raise MarkUnavailable("portfolio outcomeBalances is malformed")
        self._entries[key] = (now, rows)
        return rows

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)
