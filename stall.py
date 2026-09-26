"""Trading-stall telemetry: turn "the bot is quiet" into a specific, checkable reason.

A live trading process can be silent for very different reasons, and they have
opposite remedies:

* the process or an asyncio task is dead               → restart / supervisor
* the account is paused by a safety stop                → wait for rollover or /resume
* ``LIVE_TRADING=false``                                → dry-run by design, never orders
* no markets were discovered / feeds are stale          → upstream data outage
* the user's scope (assets/timeframes/strategies) is empty → configuration
* every candidate is rejected by a risk/edge gate       → *correct* behaviour, no trade
* signals fire but no order is confirmed                → execution/quote problem

Before this module existed, the first five looked identical from the outside: a
quiet log. That is how a deployment that stopped the service, or one losing-day
auto-pause, can stay invisible for days.

This module is pure observation. It never changes sizing, gating, or order
routing, and it must never be allowed to raise into a trading path — every
public entry point swallows its own errors.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

log = logging.getLogger("stall")

# Distinct reject codes kept per user. A long-running process evaluates a
# 15-minute market thousands of times a day; bounded keys keep memory flat.
_MAX_CODES = 48
_MAX_RECENT = 12

# Mirrors executor.TRADE_COOLDOWN_SEC for the wording of the COOLDOWN_BLOCKED
# verdict. Referenced rather than imported: stall.py is deliberately free of
# imports from the trading path so it can never fail a trade.
TRADE_COOLDOWN_SEC_REFERENCE = 60

# Counters are process-lifetime, but a safety valve that fired once hours ago
# is not why the account is quiet *now*. Valve verdicts (exposure cap,
# cooldown, MAKER book position) only consider hits inside this window.
RECENT_WINDOW_SEC = 900

# An empty scope has to *persist* before it is called a configuration error.
# The scanner only lists a short-term market between its opening and closing
# timestamps (scanner._enrich drops ``secs_to_open > 0`` and
# ``secs_to_close < 0``), so in the minutes around every series boundary an
# account scoped to 15-minute markets legitimately has nothing in scope: the
# round just closed and the next one has not opened. In production that gap
# produced a SCOPE_EMPTY alert every 15 minutes telling the operator to
# "check /settings" — and because a changed verdict code bypasses the alert
# rate limit, it also produced a second alert one minute later when the scope
# came back. One full cycle of the longest supported timeframe is long enough
# to separate "between rounds" from "this scope can never match".
SCOPE_EMPTY_CONFIRM_SEC = 900.0

# Reject codes that say "this market/strategy is excluded by configuration",
# recorded on every pass before any candidate is weighed. Counted alongside real
# gates they dominate the report by construction: in production the top two
# "gates that stopped candidates" were scope:blocked_by_policy and
# SNIPE:asset_not_in_allowed_scope while the actual problem was that MAKER's
# quotes never filled. They are reported on their own line instead.
# scope:no_enabled_strategies is deliberately NOT here: it stops everything.
_STRUCTURAL_CODES = frozenset({"scope:blocked_by_policy", "scope:suspended_by_learner"})
_STRUCTURAL_SUFFIXES = (":engine_not_clob", "_not_in_allowed_scope")

# Executor outcomes produced by MAKER's live-book check (executor.py).
_MAKER_BOOK_CODES = frozenset({"exec:maker_quote_behind_book", "exec:maker_would_cross_book"})


def is_structural(code: str) -> bool:
    """True for configuration/scope exclusions rather than candidate gates."""
    code = str(code or "")
    return code in _STRUCTURAL_CODES or code.endswith(_STRUCTURAL_SUFFIXES)

_lock = threading.RLock()

# Scanner/oracle state is process-wide, not per user.
_global: dict[str, Any] = {
    "last_scan_ok": 0.0,
    "scan_markets": 0,
    "last_scan_error": "",
    "started_at": time.time(),
}

_users: dict[str, dict[str, Any]] = {}


def _blank() -> dict[str, Any]:
    now = time.time()
    return {
        "first_seen": now,
        "last_evaluation": 0.0,
        "evaluations": 0,
        "last_signal": 0.0,
        "signals": 0,
        "last_order_attempt": 0.0,
        "order_attempts": 0,
        "last_order_placed": 0.0,
        "orders_placed": 0,
        "orders_resting": 0,
        "last_trade": 0.0,
        "trades": 0,
        "markets_total": 0,
        # Open markets inside the last pass, when the caller can tell us.
        # ``markets_total`` counts every market the scanner is holding, which
        # includes ones that have closed or not opened yet, so it must not be
        # labelled "open" on its own.
        "markets_open": None,
        "markets_in_scope": 0,
        "markets_evaluated": 0,
        "skips": {},
        # Skip counts of the *last* pass only. The lifetime counters answer
        # "what has this process spent its time on", never "why did this pass
        # match nothing", which is the question SCOPE_EMPTY has to answer.
        "last_pass_skips": {},
        "scope_empty_since": 0.0,
        "rejects": {},
        "recent": [],
        "paused": False,
        "paused_reason": "",
        "manual_pause": False,
        "dry_run": False,
        "last_detail": "",
        "last_alert_at": 0.0,
        "last_alert_code": "",
        "alert_count": 0,
    }


def _bucket(chat_id: str | None) -> dict[str, Any] | None:
    if not chat_id:
        return None
    key = str(chat_id)
    state = _users.get(key)
    if state is None:
        state = _blank()
        _users[key] = state
    return state


def _bump(counters: dict[str, Any], code: str, detail: str = "") -> None:
    entry = counters.get(code)
    if entry is None:
        if len(counters) >= _MAX_CODES:
            # Drop the oldest-seen code so new problems stay visible.
            oldest = min(counters.items(), key=lambda kv: kv[1].get("first", 0.0))[0]
            counters.pop(oldest, None)
        counters[code] = {"count": 0, "first": time.time(), "last": time.time(), "detail": detail}
        entry = counters[code]
    entry["count"] += 1
    entry["last"] = time.time()
    if detail:
        entry["detail"] = detail


# ── Recording hooks ───────────────────────────────────────────────────────────

def note_scan(markets: int, error: str = "") -> None:
    """Called by the scanner loop once per cycle."""
    try:
        with _lock:
            _global["scan_markets"] = int(markets)
            if not error:
                _global["last_scan_ok"] = time.time()
            _global["last_scan_error"] = str(error or "")[:300]
            for state in _users.values():
                state["markets_total"] = int(markets)
    except Exception:  # telemetry must never break trading
        log.debug("note_scan telemetry error", exc_info=True)


def note_state(chat_id: str | None, *, paused: bool | None = None,
               paused_reason: str | None = None, dry_run: bool | None = None,
               manual_pause: bool | None = None) -> None:
    try:
        with _lock:
            state = _bucket(chat_id)
            if state is None:
                return
            if paused is not None:
                state["paused"] = bool(paused)
            if paused_reason is not None:
                state["paused_reason"] = str(paused_reason or "")
                state["manual_pause"] = str(paused_reason or "") in ("manual", "operator")
            if manual_pause is not None:
                state["manual_pause"] = bool(manual_pause)
            if dry_run is not None:
                state["dry_run"] = bool(dry_run)
    except Exception:
        log.debug("note_state telemetry error", exc_info=True)


def note_evaluation(chat_id: str | None, *, markets_total: int, in_scope: int,
                    evaluated: int, signals: int, skips: dict[str, int] | None = None,
                    detail: str = "", open_markets: int | None = None) -> None:
    """One completed evaluation pass for a user."""
    try:
        with _lock:
            state = _bucket(chat_id)
            if state is None:
                return
            now = time.time()
            state["last_evaluation"] = now
            state["evaluations"] += 1
            state["markets_total"] = int(markets_total)
            if open_markets is not None:
                state["markets_open"] = int(open_markets)
            state["markets_in_scope"] = int(in_scope)
            state["markets_evaluated"] = int(evaluated)
            state["last_detail"] = str(detail or "")[:300]
            # Last pass only: a scope that matched nothing has to be explained
            # by *this* pass, not by counters accumulated over the process.
            state["last_pass_skips"] = {
                str(code): int(count) for code, count in (skips or {}).items() if count
            }
            # A consecutive run of empty-scope passes is what separates a
            # configuration error from the gap between two rounds of a series.
            if int(in_scope) <= 0:
                if not float(state.get("scope_empty_since", 0.0)):
                    state["scope_empty_since"] = now
            else:
                state["scope_empty_since"] = 0.0
            for code, count in (skips or {}).items():
                if count:
                    entry = state["skips"].setdefault(
                        code, {"count": 0, "first": now, "last": now, "detail": ""}
                    )
                    entry["count"] += int(count)
                    entry["last"] = now
    except Exception:
        log.debug("note_evaluation telemetry error", exc_info=True)


def note_signal(chat_id: str | None, strategy: str, asset: str) -> None:
    try:
        with _lock:
            state = _bucket(chat_id)
            if state is None:
                return
            state["last_signal"] = time.time()
            state["signals"] += 1
            _remember(state, f"SIGNAL {strategy}/{asset}")
    except Exception:
        log.debug("note_signal telemetry error", exc_info=True)


def note_order(chat_id: str | None, strategy: str, *, placed: bool, reason: str = "",
               detail: str = "") -> None:
    """Records an execution attempt that reached the executor, and its outcome.

    A skipped attempt is counted once, here. Callers must not also call
    :func:`reject` for the same ``exec:`` code: the executor did both, so every
    number the report printed for an execution outcome — including the
    "×37" in a MAKER_QUOTE_UNCOMPETITIVE detail — was twice the real count.
    """
    try:
        with _lock:
            state = _bucket(chat_id)
            if state is None:
                return
            now = time.time()
            state["last_order_attempt"] = now
            state["order_attempts"] += 1
            label = f"{strategy}: {reason}" if reason else strategy
            if placed:
                state["last_order_placed"] = now
                state["orders_placed"] += 1
                # A placed MAKER order is a resting quote: the exchange has NOT
                # executed anything. Counting it separately keeps /why able to
                # distinguish "we are quoting" from "we are getting filled".
                if reason and reason != "filled" and "resting" in reason:
                    state["orders_resting"] += 1
                _remember(state, f"ORDER PLACED {label}")
            else:
                _bump(state["rejects"], f"exec:{reason or 'rejected'}", detail or label)
                _remember(state, f"ORDER SKIPPED {label}")
    except Exception:
        log.debug("note_order telemetry error", exc_info=True)


def note_trade(chat_id: str | None, *, market_id: str = "") -> None:
    """Called when an exchange-confirmed order is live (a fill or a resting order)."""
    try:
        with _lock:
            state = _bucket(chat_id)
            if state is None:
                return
            state["last_trade"] = time.time()
            state["trades"] += 1
            _remember(state, f"TRADE {market_id[:8]}" if market_id else "TRADE")
            # A fresh trade clears the alert cooldown bookkeeping.
            state["last_alert_code"] = ""
    except Exception:
        log.debug("note_trade telemetry error", exc_info=True)


def seed_last_trade(chat_id: str | None, epoch: float) -> None:
    """Backdate the drought clock from the database after a restart.

    A process that just restarted has no in-memory trade history, which would
    otherwise hide a drought that began before the restart.
    """
    try:
        with _lock:
            state = _bucket(chat_id)
            if state is None or not epoch:
                return
            if float(state.get("last_trade", 0.0)) > 0:
                return
            state["last_trade"] = float(epoch)
    except Exception:
        log.debug("seed_last_trade telemetry error", exc_info=True)


def trade_gap_minutes(chat_id: str | None, now: float | None = None) -> float:
    """Minutes since the last exchange-confirmed fill for this account.

    Placed-but-unfilled orders do not count (see below). Falls back to process
    start when nothing has filled yet, so a restart does not reset an ongoing
    drought to zero.
    """
    now = now if now is not None else time.time()
    with _lock:
        state = _bucket(chat_id)
        if state is None:
            return 0.0
        # Deliberately NOT falling back to last_order_placed: a resting MAKER
        # quote is placed every minute and may never execute, and counting it
        # here is what kept the drought watchdog quiet through a fill-less day.
        last = float(state.get("last_trade", 0.0))
        reference = last or max(float(state.get("first_seen", 0.0)), float(_global.get("started_at", 0.0)))
    if not reference:
        return 0.0
    return max(0.0, (now - reference) / 60.0)


def reject(chat_id: str | None, stage: str, code: str, detail: str = "") -> None:
    """Record a candidate rejected by a named gate.

    ``stage`` is the strategy or subsystem (``SNIPE``, ``MAKER``, ``scan``…),
    ``code`` is a stable short identifier for the gate. Codes are counted, never
    logged at INFO — the counters are what make a drought explainable.
    """
    try:
        with _lock:
            state = _bucket(chat_id)
            if state is None:
                return
            _bump(state["rejects"], f"{stage}:{code}", detail)
    except Exception:
        log.debug("reject telemetry error", exc_info=True)


def _remember(state: dict[str, Any], line: str) -> None:
    state["recent"].append({"t": time.time(), "text": line[:200]})
    if len(state["recent"]) > _MAX_RECENT:
        del state["recent"][: len(state["recent"]) - _MAX_RECENT]


# ── Diagnosis ────────────────────────────────────────────────────────────────

# Skip reasons that explain *why a market was not in this account's scope*,
# in the order an operator would act on them. The remaining counters
# (``trigger``, ``halted``, ``stale_feed``, ``no_spot``) describe markets that
# were in scope but not evaluated this pass, so they cannot explain an empty
# scope and are left out.
_SCOPE_SKIP_LABELS = (
    ("timeframe", "excluded by timeframe"),
    ("asset", "excluded by asset"),
    ("status", "not open (closed or not started)"),
)


def _scope_gap_detail(snapshot: dict[str, Any]) -> str:
    """Why the last pass matched nothing, from that pass's own skip counts."""
    total = int(snapshot.get("markets_total", 0))
    skips = {str(k): int(v) for k, v in (snapshot.get("last_pass_skips") or {}).items()}
    parts = [
        f"{skips[code]} {label}"
        for code, label in _SCOPE_SKIP_LABELS
        if skips.get(code)
    ]
    if parts:
        detail = f"Last pass saw {total} market(s): " + ", ".join(parts) + "."
    else:
        detail = f"Last pass saw {total} market(s), none of them in scope."
    scope = str(snapshot.get("last_detail") or "").strip()
    return f"{detail} Scope: {scope}" if scope else detail


def _top(counters: dict[str, Any], limit: int = 4, *, structural: bool | None = False,
         since: float | None = None) -> list[tuple[str, int, float, str]]:
    """Most frequent codes. By default candidate gates only (``structural=False``);
    ``structural=True`` selects configuration exclusions, ``None`` selects all.
    ``since`` keeps only codes hit at or after that epoch."""
    rows = []
    for code, entry in counters.items():
        if structural is not None and is_structural(code) != structural:
            continue
        last = float(entry.get("last", 0.0))
        if since is not None and last < since:
            continue
        rows.append((code, int(entry.get("count", 0)), last, str(entry.get("detail", ""))))
    rows.sort(key=lambda row: (-row[1], -row[2]))
    return rows[:limit]


def verdict(chat_id: str | None, *, now: float | None = None,
             equity: float = 0.0, min_viable: float = 0.0,
             feed_age_sec: float | None = None,
             stale_feed_skip: int = 0,
             eval_max_age_sec: float = 180.0,
             resting_now: int | None = None) -> dict[str, Any]:
    """Classify why the account is not trading, most-actionable first.

    ``resting_now`` is the live count of unfilled resting orders from the risk
    book, when the caller has it; the lifetime placement counter is not that.

    Returns ``{"code", "headline", "detail", "action", "severity"}``.
    """
    now = now if now is not None else time.time()
    with _lock:
        state = _bucket(chat_id)
        snapshot = dict(state) if state else _blank()
    last_eval = float(snapshot.get("last_evaluation", 0.0))
    last_trade = float(snapshot.get("last_trade", 0.0))
    trade_gap_min = (now - last_trade) / 60.0 if last_trade else None
    skips = snapshot.get("skips", {})
    rejects = snapshot.get("rejects", {})
    skip_counts = {code: int(entry.get("count", 0)) for code, entry in skips.items()}
    last_skip = max((float(e.get("last", 0.0)) for e in skips.values()), default=0.0)

    def out(code, headline, detail, action, severity="info"):
        extras = []
        if snapshot.get("dry_run") and code != "DRY_RUN":
            extras.append("LIVE_TRADING=false, so no order would be sent even if this cleared")
        if snapshot.get("paused") and code not in ("PAUSED_MANUAL", "PAUSED_SESSION"):
            why = str(snapshot.get("paused_reason") or "manual")
            extras.append(f"entries are currently paused ({why})")
        combined = detail + (f"  Also notable: {'; '.join(extras)}." if extras else "")
        return {
            "code": code,
            "headline": headline,
            "detail": combined.strip(),
            "action": action,
            "severity": severity,
            "secondary": extras,
            # Unrounded: format_gap_minutes is the only thing allowed to round
            # it, and it must round the same value the header rounds.
            "trade_gap_min": trade_gap_min if trade_gap_min is not None else None,
        }

    # ── Paused / dry-run checks FIRST ──────────────────────────────────
    # When the account is paused (drawdown, daily target, manual), the
    # evaluation loop intentionally stops running.  Checking NO_EVALUATION
    # first would fire a critical "dead process" alert for what is really
    # a known, safe pause — exactly the false alarm users reported.
    if snapshot.get("dry_run"):
        return out(
            "DRY_RUN",
            "LIVE_TRADING is false — the bot evaluates and logs signals but never sends orders.",
            f"{int(snapshot.get('signals', 0))} signal(s) have been produced in this process so far; "
            "each one ends in a 'DRY RUN' log line instead of an order.",
            "Deliberately set LIVE_TRADING=true after validating feeds and reconciliation.",
            "config",
        )
    if snapshot.get("paused"):
        reason = str(snapshot.get("paused_reason") or "") or "unknown"
        if snapshot.get("manual_pause"):
            return out(
                "PAUSED_MANUAL",
                "Trading is paused by an operator (/pause).",
                f"paused_reason={reason}. Position monitoring continues; new entries are blocked. "
                "A manual pause is never cleared automatically.",
                "Send /resume when the account should trade again.",
                "warn",
            )
        return out(
            "PAUSED_SESSION",
            f"Trading is paused by the '{reason}' safety stop.",
            "This clears itself at the start of the next configured trading day. "
            "Existing positions remain monitored.",
            f"Wait for the trading-day rollover, or /resume to override {reason} explicitly.",
            "warn",
        )

    # ── NO_EVALUATION — only reachable when the account is NOT paused ──
    if last_eval <= 0 and float(snapshot.get("first_seen", 0.0)) and (
        now - float(snapshot["first_seen"])) > eval_max_age_sec:
        return out(
            "NO_EVALUATION",
            "The trading loop has never completed an evaluation for this account.",
            "The user loop was started but no evaluation finished — the process is "
            "wedged before evaluation, or this account was never resumed by the supervisor.",
            "Check /ready and the service log for a crashed background task.",
            "critical",
        )
    if last_eval > 0 and (now - last_eval) > eval_max_age_sec:
        return out(
            "NO_EVALUATION",
            f"No evaluation has completed in {(now - last_eval) / 60:.1f} minutes.",
            "The scanner, feed tasks, or the per-user loop stopped progressing. A process "
            "can answer /live while its trading tasks are dead.",
            "Restart the service; check /ready component ages.",
            "critical",
        )
    if int(snapshot.get("markets_total", 0)) <= 0 and float(_global.get("last_scan_ok", 0.0)):
        return out(
            "NO_MARKETS",
            "Market discovery returned 0 open markets.",
            f"Scanner runs without markets (last error: {_global.get('last_scan_error') or 'none'}). "
            "Either Bayse has no open short-term events for the tracked series, or the "
            "public series/quote endpoints are failing.",
            "Verify series slugs and relay availability; nothing can trade without markets.",
            "critical",
        )
    # (paused and dry_run checks moved above NO_EVALUATION — see top of function)
    if min_viable and equity and equity < min_viable:
        return out(
            "LOW_BALANCE",
            f"Equity ₦{equity:,.0f} is below the ₦{min_viable:,.0f} minimum for safe operation.",
            "Entries are blocked while the wallet cannot cover the platform minimum order "
            "plus fee and slippage buffer.",
            "Fund the account; no strategy change will make this trade.",
            "warn",
        )
    if int(snapshot.get("markets_in_scope", 0)) <= 0 and int(snapshot.get("markets_total", 0)) > 0:
        scope_empty_since = float(snapshot.get("scope_empty_since", 0.0))
        scope_empty_sec = (now - scope_empty_since) if scope_empty_since else 0.0
        # Only a *persistent* empty scope is a configuration finding. Around
        # every series boundary the in-scope markets are briefly absent from
        # discovery, which is the exchange's calendar, not the account's
        # settings; alerting on it told the operator to fix /settings every
        # 15 minutes and hid the verdict that was actually true.
        if scope_empty_sec >= SCOPE_EMPTY_CONFIRM_SEC:
            return out(
                "SCOPE_EMPTY",
                f"None of the {int(snapshot['markets_total'])} discovered markets match this "
                f"account's scope, and none has for {scope_empty_sec / 60:.0f} min.",
                _scope_gap_detail(snapshot),
                "Check /settings assets, timeframes and strategies; quarantined strategies are "
                "removed by global policy and can require ALLOW_EXPERIMENTAL_STRATEGIES.",
                "warn",
            )

    stale = int(skip_counts.get("stale_feed", 0))
    if (feed_age_sec is not None and feed_age_sec > 60) or (
        stale and int(snapshot.get("markets_evaluated", 0)) == 0 and stale >= last_skip > 0
    ):
        age_text = f"{feed_age_sec:.0f}s" if feed_age_sec is not None else "unknown"
        return out(
            "FEEDS_STALE",
            f"Market data is too old to trade on (oracle age {age_text}).",
            "Independent-oracle or relay staleness fails closed by design: a stale feed can "
            "look like a huge edge that is only data lag.",
            "Check the Binance/relay feeds and network egress; /debug shows per-asset age.",
            "critical",
        )
    reject_counts = {code: int(entry.get("count", 0)) for code, entry in rejects.items()}
    recent_since = now - RECENT_WINDOW_SEC
    last_placed = float(snapshot.get("last_order_placed", 0.0))
    placed_recently = last_placed >= recent_since
    # Executor outcomes that explain a missing order, most frequent first. The
    # per-market cooldown is left out: every placement *and* every skip stamps
    # it, so it is always a consequence of something else, never a root cause.
    exec_causes = [
        row for row in _top(rejects, _MAX_CODES, structural=None)
        if row[0].startswith("exec:") and row[0] != "exec:market_cooldown"
    ]
    recent_exec_causes = [row for row in exec_causes if row[2] >= recent_since]

    def _hit_recently(code: str) -> bool:
        entry = rejects.get(code) or {}
        return int(entry.get("count", 0)) > 0 and float(entry.get("last", 0.0)) >= recent_since

    if int(snapshot.get("markets_evaluated", 0)) > 0 and int(snapshot.get("signals", 0)) <= 0:
        top = _top(rejects) or _top(rejects, structural=True)
        detail = "; ".join(f"{code}×{count}" for code, count, _, _ in top) or "no gate counters recorded"
        return out(
            "NO_EDGE",
            f"{int(snapshot['markets_evaluated'])} market(s) evaluated in the last pass; "
            "no candidate cleared its gates.",
            f"Dominant rejections: {detail}",
            "Absence of qualifying edge is the correct outcome on a quiet tape. Lowering a gate "
            "to manufacture activity is not a fix.",
            "info",
        )
    # MAKER found a signal but its maximum price cannot reach the live book, so
    # it declined to rest a quote that could not fill. Checked before the
    # generic execution verdict: this is a pricing-policy boundary, not a fault.
    if recent_exec_causes and recent_exec_causes[0][0] in _MAKER_BOOK_CODES and not placed_recently:
        code, count, _, detail = recent_exec_causes[0]
        return out(
            "MAKER_QUOTE_UNCOMPETITIVE",
            "MAKER has signals, but its risk-capped bid is too far below the live book to "
            "compete for a fill.",
            f"{code} ×{count} — latest: {detail}. A post-only bid below the best bid fills "
            "only if higher bids are removed or consumed first.",
            "Do not raise MAKER_MAX_BID just to force activity. Prob is a model estimate, not "
            "a confirmed win rate. A fill at the live best bid is positive EV only if the "
            "calibrated win probability clears that price by a margin after execution costs "
            "and adverse selection. Keep the cap until out-of-sample fill results support "
            "changing it.",
            "warn",
        )
    if int(snapshot.get("signals", 0)) > 0 and int(snapshot.get("order_attempts", 0)) > 0 and int(
            snapshot.get("orders_placed", 0)) <= 0:
        top = exec_causes[:4] or _top(rejects)
        detail = "; ".join(f"{code}×{count}" for code, count, _, _ in top) or "no executor rejections recorded"
        return out(
            "EXECUTION_BLOCKED",
            "Signals reach the executor but no order is ever placed.",
            f"Executor outcomes: {detail}",
            "Inspect the top reason: quote not confirming completeFill, book depth below the "
            "market minimum, fee-adjusted EV, or the risk budget not covering the platform minimum.",
            "critical",
        )
    # Bounded safety valves deserve their own verdict: both were silent INFO
    # logs before, which is how "MAKER resting quotes froze every entry" read
    # as a healthy account with no edge. Only a *recent* hit explains a
    # current drought — one refusal hours ago must not name the cause forever.
    if _hit_recently("exec:exposure_cap"):
        return out(
            "EXPOSURE_CAPPED",
            "Entries are being refused by the portfolio exposure ceiling.",
            f"exec:exposure_cap ×{reject_counts['exec:exposure_cap']} "
            f"(filled positions count toward the cap; resting orders no longer do).",
            "Wait for open positions to resolve, or raise MAX_PORTFOLIO_EXPOSURE deliberately. "
            "At ₦1,600 equity and a 15% ceiling the account can only hold ₦240 of filled exposure.",
            "warn",
        )
    # Orders were placed and none filled. This outranks the cooldown: a
    # cooldown hit is the expected echo of every placement, and checking it
    # first let a single cooldown skip mask "zero fills" for the whole process.
    if last_placed and int(snapshot.get("trades", 0)) <= 0:
        placed = int(snapshot.get("orders_placed", 0))
        passive = int(snapshot.get("orders_resting", 0))
        if resting_now is not None:
            resting_text = f"; {int(resting_now)} resting on the book now"
        else:
            resting_text = ""
        gap_text = (
            f" No confirmed fill for {format_gap_minutes(trade_gap_min)} min."
            if trade_gap_min is not None else " No confirmed fill yet in this process."
        )
        book_text = ""
        maker_book = [row for row in recent_exec_causes if row[0] in _MAKER_BOOK_CODES]
        if maker_book:
            code, count, _, detail = maker_book[0]
            book_text = f" Recent MAKER quotes skipped as unfillable: {code} ×{count} ({detail})."
        return out(
            "NO_CONFIRMED_FILL",
            "Orders are submitted but no exchange-confirmed fill has been recorded.",
            f"{placed} order(s) placed"
            + (f" ({passive} as passive resting quotes)" if passive else "")
            + f"; {int(snapshot.get('trades', 0))} confirmed fill(s){resting_text}."
            + gap_text + book_text,
            "Passive quotes often expire unfilled by design. Check /trades and the unfilled-order "
            "notices. A longer MAKER_ORDER_TIMEOUT will not make a buried quote competitive; "
            "do not raise MAKER_MAX_BID unless out-of-sample results show the model probability "
            "clears actual fill prices. Prob is a model estimate, not an observed win rate "
            "or a guarantee.",
            "warn",
        )
    if _hit_recently("exec:market_cooldown") and not placed_recently and not recent_exec_causes:
        return out(
            "COOLDOWN_BLOCKED",
            "A per-strategy cooldown on the same market is refusing entries.",
            f"exec:market_cooldown ×{reject_counts['exec:market_cooldown']} "
            f"({TRADE_COOLDOWN_SEC_REFERENCE}s window per strategy).",
            "Normal after any placement or rejection on that market; if the counter dominates, "
            "check whether one strategy is churning the market with re-quotes.",
            "info",
        )
    # Everything below is reached only with no order ever placed: an order
    # with no fill returns NO_CONFIRMED_FILL above, and signals that reached
    # the executor without an order return EXECUTION_BLOCKED.
    signals_total = int(snapshot.get("signals", 0))
    placed_total = int(snapshot.get("orders_placed", 0))
    attempts_total = int(snapshot.get("order_attempts", 0))
    if placed_total <= 0 and signals_total <= 0:
        # The process evaluated a lot and never produced a candidate. This is
        # the same finding as the last-pass NO_EDGE above, so it must not be
        # reported as "healthy" just because the *last* pass happened to
        # evaluate nothing: a scanner-triggered pass covers no market, and the
        # account then alternated between NO_EDGE and HEALTHY every minute —
        # with an alert on each flip, because a changed verdict code bypasses
        # the rate limit.
        top = _top(rejects) or _top(rejects, structural=True)
        detail = "; ".join(f"{code}×{count}" for code, count, _, _ in top) or "no gate counters recorded"
        return out(
            "NO_EDGE",
            f"{int(snapshot.get('evaluations', 0))} evaluation(s) in this process, "
            f"{int(snapshot.get('markets_evaluated', 0))} market(s) in the last pass; "
            "no candidate has cleared its gates.",
            f"Dominant rejections: {detail}",
            "Absence of qualifying edge is the correct outcome on a quiet tape. Lowering a gate "
            "to manufacture activity is not a fix.",
            "info",
        )
    if placed_total <= 0 and attempts_total <= 0:
        # Signals exist but the executor was never asked: merge_signals keeps
        # one signal per market/strategy and drops the weaker of two opposing
        # sides, so a burst of raw signals can legitimately collapse to nothing.
        # What must never happen is reporting that as a healthy pipeline.
        return out(
            "SIGNALS_NOT_EXECUTED",
            f"{signals_total} signal(s) were produced but none reached the executor.",
            "No execution attempt was recorded for any of them. Signals are merged before "
            "execution (one per market/strategy, and the weaker of two opposing sides is "
            "dropped), so some collapse is expected — but every signal that survives the "
            "merge stamps an executor outcome, and none did.",
            "Check the service log for a crashed user loop or an exception in signal merging; "
            "/why's 'Executor outcomes' line should list an outcome for each surviving signal.",
            "warn",
        )
    return out(
        "HEALTHY",
        "The trading pipeline is evaluating, placing orders and recording fills.",
        f"{int(snapshot.get('evaluations', 0))} evaluation(s), {signals_total} signal(s), "
        f"{placed_total} order(s) placed, {int(snapshot.get('trades', 0))} confirmed fill(s) "
        "in this process"
        + (f"; last confirmed fill {format_gap_minutes(trade_gap_min)} min ago."
           if trade_gap_min is not None else "."),
        "",
        "info",
    )

def report(chat_id: str | None, *, now: float | None = None, **context: Any) -> dict[str, Any]:
    """Full structured diagnosis for one account (safe for logs and /api/stats).

    ``now`` pins every age in the report — and the verdict inside it — to one
    instant. Callers that also print the drought clock (the watchdog header)
    should pass the same value to :func:`trade_gap_minutes` so a sub-second
    drift cannot straddle a minute boundary; the rounding itself is centralised
    in :func:`format_gap_minutes`.
    """
    now = time.time() if now is None else float(now)
    with _lock:
        state = _bucket(chat_id)
        snapshot = dict(state) if state else _blank()
        snapshot["rejects"] = dict(state["rejects"]) if state else {}
        snapshot["skips"] = dict(state["skips"]) if state else {}
        snapshot["recent"] = list(state["recent"]) if state else []
    verdict_data = verdict(chat_id, now=now, **context)
    return {
        "chat_id": str(chat_id),
        "markets_total": int(snapshot.get("markets_total", 0)),
        # ``None`` when no pass has reported it; never guess from markets_total.
        "markets_open": (
            int(snapshot["markets_open"]) if snapshot.get("markets_open") is not None else None
        ),
        "markets_in_scope": int(snapshot.get("markets_in_scope", 0)),
        # How long the scope has been empty, for "between rounds" vs "broken
        # settings". 0 when the last pass had something in scope.
        # Unrounded: a gap that started this instant must still read as a gap.
        "scope_empty_sec": (
            now - float(snapshot["scope_empty_since"])
            if float(snapshot.get("scope_empty_since", 0.0)) else 0.0
        ),
        "markets_evaluated": int(snapshot.get("markets_evaluated", 0)),
        "evaluations": int(snapshot.get("evaluations", 0)),
        "signals": int(snapshot.get("signals", 0)),
        "order_attempts": int(snapshot.get("order_attempts", 0)),
        "orders_placed": int(snapshot.get("orders_placed", 0)),
        # Lifetime count of orders placed as passive resting quotes. NOT the
        # number resting now — that is ``resting_now`` (from the risk book).
        "orders_resting": int(snapshot.get("orders_resting", 0)),
        "resting_now": (
            int(context["resting_now"]) if context.get("resting_now") is not None else None
        ),
        "trades": int(snapshot.get("trades", 0)),
        "age_evaluation_sec": round(now - float(snapshot.get("last_evaluation", 0.0)), 1)
        if snapshot.get("last_evaluation") else None,
        "age_trade_sec": round(now - float(snapshot.get("last_trade", 0.0)), 1)
        if snapshot.get("last_trade") else None,
        "age_signal_sec": round(now - float(snapshot.get("last_signal", 0.0)), 1)
        if snapshot.get("last_signal") else None,
        "paused": bool(snapshot.get("paused")),
        "paused_reason": str(snapshot.get("paused_reason") or ""),
        "dry_run": bool(snapshot.get("dry_run")),
        "skip_counts": {code: int(entry.get("count", 0)) for code, entry in snapshot["skips"].items()},
        "top_rejects": [
            {"code": code, "count": count, "age_sec": round(now - last, 1), "detail": detail}
            for code, count, last, detail in _top(snapshot["rejects"], 6)
        ],
        # Configuration/scope exclusions (see is_structural), kept apart from gates.
        "structural_rejects": [
            {"code": code, "count": count, "age_sec": round(now - last, 1), "detail": detail}
            for code, count, last, detail in _top(snapshot["rejects"], 4, structural=True)
        ],
        # Executor outcomes inside the recency window, market_cooldown included.
        # The gate counters above are process-lifetime and dominated by strategy
        # gates counted thousands of times, which is why a report reading
        # "12 signals | 2 orders placed" explained neither the 10 that never
        # became orders nor the quotes the executor refused as unfillable.
        "recent_exec": [
            {"code": code, "count": count, "age_sec": round(now - last, 1), "detail": detail}
            for code, count, last, detail in sorted(
                (
                    (str(code), int(entry.get("count", 0)), float(entry.get("last", 0.0)),
                     str(entry.get("detail", "")))
                    for code, entry in snapshot["rejects"].items()
                    if str(code).startswith("exec:")
                    and float(entry.get("last", 0.0)) >= now - RECENT_WINDOW_SEC
                ),
                key=lambda row: (-row[1], -row[2]),
            )[:6]
        ],
        "recent": list(snapshot["recent"])[-6:],
        "scan": {
            "age_sec": round(now - float(_global.get("last_scan_ok", 0.0)), 1)
            if _global.get("last_scan_ok") else None,
            "markets": int(_global.get("scan_markets", 0)),
            "last_error": str(_global.get("last_scan_error", "")),
        },
        "verdict": verdict_data,
    }


def format_gap_minutes(minutes: float) -> str:
    """Render the drought clock, half-up, from the unrounded value.

    One alert prints this number in three places: the watchdog header, the
    "Last confirmed fill" line, and the NO_CONFIRMED_FILL detail. Formatting
    each with ``f"{x:.0f}"`` disagreed inside a single message because Python
    rounds halves to *even* — and the verdict pre-rounded to one decimal
    first, which moved values onto an exact half:

    ========  =================  =========================
    raw       ``f"{raw:.0f}"``   pre-rounded, then ``.0f``
    ========  =================  =========================
    1512.5001 ``1513``           1512.5 → ``1512``
    1513.4999 ``1513``           1513.5 → ``1514``
    ========  =================  =========================

    Both patterns appeared in one operator paste ("stall — 1573 min" over
    "Last confirmed fill: 1572 min ago"). Every renderer must therefore go
    through this one function, on the unrounded gap.
    """
    return str(int(math.floor(float(minutes) + 0.5)))


def md_escape(text: Any) -> str:
    """Escape Telegram legacy-Markdown control characters in free text."""
    out = str(text)
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def format_report(chat_id: str | None, *, markdown: bool = False, now: float | None = None,
                  **context: Any) -> str:
    """Telegram-shaped rendering of :func:`report`.

    ``markdown=True`` escapes every free-text line for Telegram's legacy
    Markdown parse mode. The report quotes raw gate codes full of underscores
    (``exec:market_cooldown``, ``MAKER_ORDER_TIMEOUT``); unescaped, Telegram
    rendered them as italics ("blockedbypolicy", "MAKERORDERTIMEOUT") or, with
    an odd count, rejected the whole alert.

    ``now`` is forwarded to :func:`report` so a caller that prints the drought
    clock next to this text renders both from the same instant.
    """
    data = report(chat_id, now=now, **context)
    esc = md_escape if markdown else str
    verdict_data = data["verdict"]
    icon = {"critical": "🔴", "warn": "🟠", "config": "⚙️", "info": "🟢"}.get(
        verdict_data.get("severity", "info"), "🟢"
    )
    lines = [
        f"{icon} {esc(verdict_data['headline'])}",
        "",
        # Inside a code span underscores are literal; codes are [A-Z_] only.
        f"Code: `{verdict_data['code']}`",
    ]
    if verdict_data.get("trade_gap_min") is not None:
        lines.append(
            f"Last confirmed fill: {format_gap_minutes(verdict_data['trade_gap_min'])} min ago"
        )
    elif int(data.get("orders_placed", 0)):
        lines.append(
            "Last confirmed fill: none yet — every order so far is resting or "
            "expired unfilled"
        )
    if verdict_data.get("detail"):
        lines += ["", esc(verdict_data["detail"])]
    if verdict_data.get("action"):
        lines += ["", "→ " + esc(verdict_data["action"])]
    totals = (
        f"Process totals: {data['evaluations']} evaluations | {data['signals']} signals | "
        f"{data['orders_placed']} orders placed"
    )
    if data.get("orders_resting"):
        totals += f" ({data['orders_resting']} as passive quotes)"
    totals += f" | {data['trades']} confirmed fills"
    if data.get("resting_now") is not None:
        totals += f" | {data['resting_now']} resting now"
    # ``markets_total`` counts every market the scanner is holding, including
    # ones that are not open, so it was mislabelled "open" before. The open
    # count comes from the last pass; when no pass has reported one, say only
    # what is known.
    discovered = (
        f"{data['markets_total']} discovered ({data['markets_open']} open)"
        if data.get("markets_open") is not None else f"{data['markets_total']} discovered"
    )
    scope_note = ""
    scope_empty_sec = float(data.get("scope_empty_sec", 0.0) or 0.0)
    if int(data.get("markets_in_scope", 0)) <= 0 and scope_empty_sec > 0:
        # A short gap here is the exchange's calendar between two rounds of the
        # same series, not a settings fault; showing the duration says which.
        gap_text = (
            f"{scope_empty_sec / 60:.0f} min" if scope_empty_sec >= 60
            else f"{scope_empty_sec:.0f}s"
        )
        scope_note = f" — nothing in scope for {gap_text}"
    lines += [
        "",
        # A feed-triggered pass evaluates one asset, so this is "last pass",
        # not a per-cycle rate.
        f"Markets: {discovered}, {data['markets_in_scope']} in scope, "
        f"{data['markets_evaluated']} evaluated in the last pass{scope_note}",
        esc(totals),
    ]
    if data["top_rejects"]:
        lines += ["", "🚪 Gates that stopped candidates:"]
        for row in data["top_rejects"]:
            lines.append(esc(f"  {row['code']}: {row['count']}× (last {row['age_sec']:.0f}s ago)"))
    if data.get("recent_exec"):
        lines += ["", esc(f"🛠 Executor outcomes (last {RECENT_WINDOW_SEC / 60:.0f} min):")]
        for row in data["recent_exec"]:
            lines.append(esc(f"  {row['code']}: {row['count']}× (last {row['age_sec']:.0f}s ago)"))
    if data.get("structural_rejects"):
        parts = []
        for row in data["structural_rejects"]:
            detail = str(row.get("detail") or "")[:40]
            parts.append(f"{row['code']}" + (f" [{detail}]" if detail else "") + f" ×{row['count']}")
        lines += ["", esc("⚙️ Excluded by configuration (every pass, not a gate): " + "; ".join(parts))]
    if data["skip_counts"]:
        skips = sorted(data["skip_counts"].items(), key=lambda kv: -kv[1])[:5]
        lines += ["", esc("⏭ Skips: " + ", ".join(f"{k}={v}" for k, v in skips))]
    if data["recent"]:
        lines += ["", "🕒 Recent:"]
        for row in data["recent"][-4:]:
            lines.append(esc(f"  {time.strftime('%H:%M:%S', time.localtime(row['t']))} {row['text']}"))
    return "\n".join(lines)

def note_alert(chat_id: str | None, code: str, *, now: float | None = None) -> bool:
    """True when an alert for this (code, account) should be sent now.

    Repeats are rate limited per verdict code so a persistent condition alerts
    occasionally instead of every minute — but a *changing* condition alerts
    immediately, because the previous explanation was wrong.
    """
    now = now if now is not None else time.time()
    import config

    repeat_sec = max(300.0, float(config.TRADE_STALL_ALERT_REPEAT_MIN) * 60.0)
    with _lock:
        state = _bucket(chat_id)
        if state is None:
            return False
        if state["last_alert_code"] == code and now - float(state["last_alert_at"]) < repeat_sec:
            return False
        state["last_alert_code"] = code
        state["last_alert_at"] = now
        state["alert_count"] += 1
        return True


def reset(chat_id: str | None = None) -> None:
    """Test helper: drop recorded state (all users when ``chat_id`` is None)."""
    with _lock:
        if chat_id is None:
            _users.clear()
        else:
            _users.pop(str(chat_id), None)
