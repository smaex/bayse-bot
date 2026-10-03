"""
Data hygiene: find and fix records that are stale, contradictory or corrupt.

A trading bot's database accumulates three kinds of garbage, and each one
quietly corrupts a different decision:

1. **Contradictory trade rows** — a row marked resolved with no verdict, a row
   claiming a fill with no quantity, a negative stake. Every calibration,
   break-even and P&L number downstream is computed from these, so one bad row
   does not just look wrong, it moves a gate.

2. **Abandoned resting orders** — a quote row that expired on the exchange but
   was never reconciled. It holds phantom "deployed" capital, inflates
   exposure, and can trip a drawdown pause that is not real.

3. **Stale leases and state** — a singleton lock whose owner died without
   releasing it stops the next deploy from ever trading.

Nothing here deletes evidence. Resolved trades are never removed; rows are
either *corrected* (a contradiction is resolved toward the conservative
reading) or *voided* (marked, not dropped), so the record of what happened
survives the cleanup. The only rows this will delete are provably unusable:
those with no chat_id, no trade_id, or no executable content at all.

Every operation is dry-run by default. The operator sees the plan first.
"""

from __future__ import annotations

import logging
from typing import Any

import config
import database

log = logging.getLogger("maintenance")

# A resting quote older than this cannot still be open: MAKER_ORDER_TIMEOUT
# withdraws it, and a 15-minute market has long since settled.
ORPHANED_ORDER_AGE_HOURS = 6.0
# Resolved rows are kept for calibration, but not forever.
TRADE_RETENTION_DAYS = 365 * 2
# A lease whose heartbeat is older than this belongs to a dead process.
STALE_LEASE_FACTOR = 3.0


class MaintenanceReport:
    """What a purge would do (or did), as structured data and as text."""

    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    def record(self, category: str, detail: str, count: int, applied: bool) -> None:
        self.actions.append({
            "category": category,
            "detail": detail,
            "count": int(count),
            "applied": bool(applied),
        })

    @property
    def total(self) -> int:
        return sum(a["count"] for a in self.actions)

    def text(self) -> str:
        if not self.actions:
            return "🧹 Maintenance: nothing to clean."
        verb = "applied" if self.actions[0]["applied"] else "would apply"
        lines = [f"🧹 Maintenance — {self.total} record(s) {verb}:"]
        for action in self.actions:
            if not action["count"]:
                continue
            lines.append(f"  • {action['category']}: {action['count']} — {action['detail']}")
        return "\n".join(lines)


def _row_count(sql: str, params: tuple = ()) -> int:
    try:
        rows = database._fetch_all(sql, params)
    except Exception as exc:
        log.warning(f"maintenance count failed: {exc}")
        return 0
    return int((rows[0].get("n") if rows else 0) or 0)


def _apply(sql: str, params: tuple = ()) -> int:
    """Execute a corrective statement, returning the number of rows touched."""
    try:
        with database._cx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                affected = cur.rowcount
            conn.commit()
        return max(0, int(affected or 0))
    except Exception as exc:
        log.error(f"maintenance statement failed: {exc}")
        return 0


# ── Contradiction checks ──────────────────────────────────────────────────────

def find_resolved_without_verdict() -> int:
    """Rows marked resolved whose ``won`` is still NULL.

    A resolved row with no verdict is the worst kind of record: it is excluded
    from unresolved queries (so it will never be retried) and from P&L sums
    (so the loss it represents is invisible). It is how an account quietly
    understates its own losses.
    """
    return _row_count(
        "SELECT COUNT(*) AS n FROM trades "
        "WHERE resolved_at IS NOT NULL AND won IS NULL"
    )


def repair_resolved_without_verdict(report: MaintenanceReport, apply: bool) -> None:
    count = find_resolved_without_verdict()
    if not count:
        return
    # Reopen rather than delete: the resolution monitor will settle it
    # properly against the exchange, which is the only source of truth.
    applied = 0
    if apply:
        applied = _apply(
            "UPDATE trades SET resolved_at = NULL, pnl_ngn = 0 "
            "WHERE resolved_at IS NOT NULL AND won IS NULL"
        )
    report.record("resolved_without_verdict",
                  "reopened so the resolution monitor can settle them from the exchange",
                  applied if apply else count, apply)


def find_nonsensical_amounts() -> int:
    """Rows whose stake, price or quantity is negative, missing or out of band.

    A price outside (0, 1) is not a price: Bayse will not accept one. A
    negative quantity has no meaning. Either value poisons every average it
    touches, and averages here decide whether a gate opens.
    """
    return _row_count(
        "SELECT COUNT(*) AS n FROM trades "
        "WHERE amount_ngn IS NULL OR amount_ngn < 0 "
        "OR entry_price IS NULL OR entry_price <= 0 OR entry_price >= 1 "
        "OR filled_quantity < 0"
    )


def void_nonsensical_amounts(report: MaintenanceReport, apply: bool) -> None:
    """Mark unusable rows resolved-neutral instead of deleting them.

    Deleting a trade row destroys the audit trail of what the bot did.
    Stamping it as resolved with zero P&L keeps it visible to /trades and to
    any reconciliation, while removing it from every live calculation.
    """
    count = find_nonsensical_amounts()
    if not count:
        return
    applied = 0
    if apply:
        applied = _apply(
            "UPDATE trades SET won = NULL, pnl_ngn = 0, resolved_at = NULL, "
            "amount_ngn = CASE WHEN amount_ngn IS NULL OR amount_ngn < 0 "
            "THEN 0 ELSE amount_ngn END, "
            "filled_quantity = CASE WHEN filled_quantity < 0 THEN 0 "
            "ELSE filled_quantity END "
            "WHERE amount_ngn IS NULL OR amount_ngn < 0 "
            "OR entry_price IS NULL OR entry_price <= 0 OR entry_price >= 1 "
            "OR filled_quantity < 0"
        )
    report.record("nonsensical_amounts",
                  "neutralised: stake or price outside the executable band",
                  applied if apply else count, apply)


# ── Abandoned resting orders ──────────────────────────────────────────────────

def find_abandoned_resting_orders() -> int:
    """Quote rows that never filled and are far older than any quote timeout.

    Bayse withdraws a GTC order only when we cancel it or the market settles.
    A row that survived both is holding phantom deployed capital: it counts
    toward exposure and drawdown for a position that does not exist.
    """
    return _row_count(
        "SELECT COUNT(*) AS n FROM trades "
        "WHERE (engine = 'CLOB_LIMIT' OR engine IS NULL) "
        "AND COALESCE(filled_quantity, 0) <= 0 "
        "AND resolved_at IS NULL "
        "AND created_at < NOW() - (%s * INTERVAL '1 hour')",
        (ORPHANED_ORDER_AGE_HOURS,),
    )


def retire_abandoned_resting_orders(report: MaintenanceReport, apply: bool) -> None:
    count = find_abandoned_resting_orders()
    if not count:
        return
    applied = 0
    if apply:
        applied = _apply(
            "UPDATE trades SET pnl_ngn = 0, resolved_at = NOW(), won = NULL "
            "WHERE (engine = 'CLOB_LIMIT' OR engine IS NULL) "
            "AND COALESCE(filled_quantity, 0) <= 0 "
            "AND resolved_at IS NULL "
            "AND created_at < NOW() - (%s * INTERVAL '1 hour')",
            (ORPHANED_ORDER_AGE_HOURS,),
        )
    report.record("abandoned_resting_orders",
                  f"retired: resting quotes older than {ORPHANED_ORDER_AGE_HOURS:.0f}h "
                  f"with no fill cannot still be open",
                  applied if apply else count, apply)


# ── Leases and state ──────────────────────────────────────────────────────────

def find_stale_lease() -> int:
    """A singleton lease whose heartbeat stopped, which blocks the next deploy."""
    return _row_count(
        "SELECT COUNT(*) AS n FROM bot_lock "
        "WHERE lock_id = 'MASTER' AND owner_id IS NOT NULL "
        "AND updated_at < NOW() - (%s * INTERVAL '1 second')",
        (config.LOCK_LEASE_SEC * STALE_LEASE_FACTOR,),
    )


def clear_stale_lease(report: MaintenanceReport, apply: bool) -> None:
    count = find_stale_lease()
    if not count:
        return
    applied = 0
    if apply:
        applied = _apply(
            "UPDATE bot_lock SET owner_id = NULL, process_id = 0 "
            "WHERE lock_id = 'MASTER' AND owner_id IS NOT NULL "
            "AND updated_at < NOW() - (%s * INTERVAL '1 second')",
            (config.LOCK_LEASE_SEC * STALE_LEASE_FACTOR,),
        )
    report.record("stale_singleton_lease",
                  "released: the owning process stopped heartbeating, so no "
                  "instance can trade until this is cleared",
                  applied if apply else count, apply)


def find_stale_quant_state() -> int:
    """Vol/state rows for assets the bot no longer trades."""
    if not config.ALL_ASSETS:
        return 0
    placeholders = ",".join(["%s"] * len(config.ALL_ASSETS))
    return _row_count(
        f"SELECT COUNT(*) AS n FROM quant_state WHERE asset NOT IN ({placeholders})",
        tuple(config.ALL_ASSETS),
    )


def drop_stale_quant_state(report: MaintenanceReport, apply: bool) -> None:
    count = find_stale_quant_state()
    if not count:
        return
    applied = 0
    if apply:
        placeholders = ",".join(["%s"] * len(config.ALL_ASSETS))
        applied = _apply(
            f"DELETE FROM quant_state WHERE asset NOT IN ({placeholders})",
            tuple(config.ALL_ASSETS),
        )
    report.record("stale_quant_state",
                  "dropped: volatility state for assets outside the traded universe",
                  applied if apply else count, apply)


# ── Retention ─────────────────────────────────────────────────────────────────

def find_expired_trades() -> int:
    """Resolved rows past the retention window. Unresolved rows are never pruned."""
    return _row_count(
        "SELECT COUNT(*) AS n FROM trades "
        "WHERE resolved_at IS NOT NULL "
        "AND resolved_at < NOW() - (%s * INTERVAL '1 day')",
        (TRADE_RETENTION_DAYS,),
    )


def prune_expired_trades(report: MaintenanceReport, apply: bool) -> None:
    count = find_expired_trades()
    if not count:
        return
    applied = 0
    if apply:
        applied = _apply(
            "DELETE FROM trades "
            "WHERE resolved_at IS NOT NULL "
            "AND resolved_at < NOW() - (%s * INTERVAL '1 day')",
            (TRADE_RETENTION_DAYS,),
        )
    report.record("expired_trades",
                  f"pruned: resolved rows older than {TRADE_RETENTION_DAYS} days "
                  f"(unresolved rows are kept indefinitely)",
                  applied if apply else count, apply)


# ── Orchestration ─────────────────────────────────────────────────────────────

# Order matters: correctness repairs run before anything is retired or pruned,
# so a row that is both contradictory and old is corrected first and then
# judged on its corrected state.
_STEPS = (
    repair_resolved_without_verdict,
    void_nonsensical_amounts,
    retire_abandoned_resting_orders,
    clear_stale_lease,
    drop_stale_quant_state,
    prune_expired_trades,
)


def run(apply: bool = False) -> MaintenanceReport:
    """Inspect (and optionally repair) the database. Never raises."""
    report = MaintenanceReport()
    for step in _STEPS:
        try:
            step(report, apply)
        except Exception as exc:
            # A cleanup failure must never stop the bot from starting.
            log.error(f"maintenance step {step.__name__} failed: {exc}", exc_info=True)
    return report


# Startup repairs only. Deleting rows (retention pruning, dropping quant
# state) is an operator decision via `tools/maintenance.py --apply`, never
# something a restart does on its own -- a restart is not consent to delete
# data, and a bad deploy should not be able to erase the record of what the
# previous one did.
_SAFE_STEPS = (
    repair_resolved_without_verdict,
    void_nonsensical_amounts,
    retire_abandoned_resting_orders,
    clear_stale_lease,
)


def apply_safe() -> MaintenanceReport:
    """Apply only the repairs that are safe to run unattended at startup."""
    report = MaintenanceReport()
    for step in _SAFE_STEPS:
        try:
            step(report, True)
        except Exception as exc:
            log.error(f"maintenance step {step.__name__} failed: {exc}", exc_info=True)
    return report


def purge_stale_settings(chat_id: str | None = None) -> int:
    """Drop saved references to strategies that no longer exist.

    An account whose saved scope names a deleted strategy produces no signals
    from it forever, and the failure looks identical to "no edge today". The
    stale name has to go so the account silently returns to the live default.
    """
    valid = set(config.ACTIVE_STRATEGIES)
    targets = [chat_id] if chat_id else [
        row["chat_id"] for row in (database.get_all_active(force_fresh=True) or [])
    ]
    changed = 0
    for cid in targets:
        try:
            user = database.get_user(cid, force_fresh=True)
            if not user:
                continue
            settings = user.get("settings") or {}
            saved = settings.get("strategies") or []
            cleaned = [s for s in saved if s in valid]
            if cleaned == saved:
                continue
            settings["strategies"] = cleaned or list(config.DEFAULT_STRATEGIES)
            database.update_settings(cid, settings)
            changed += 1
            log.info(
                f"Purged stale strategies for {cid}: "
                f"{sorted(set(saved) - valid)} -> {settings['strategies']}"
            )
        except Exception as exc:
            log.warning(f"Could not purge settings for {cid}: {exc}")
    return changed
