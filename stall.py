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
import threading
import time
from typing import Any

log = logging.getLogger("stall")

# Distinct reject codes kept per user. A long-running process evaluates a
# 15-minute market thousands of times a day; bounded keys keep memory flat.
_MAX_CODES = 48
_MAX_RECENT = 12

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
        "last_trade": 0.0,
        "trades": 0,
        "markets_total": 0,
        "markets_in_scope": 0,
        "markets_evaluated": 0,
        "skips": {},
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
                    detail: str = "") -> None:
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
            state["markets_in_scope"] = int(in_scope)
            state["markets_evaluated"] = int(evaluated)
            state["last_detail"] = str(detail or "")[:300]
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


def note_order(chat_id: str | None, strategy: str, *, placed: bool, reason: str = "") -> None:
    """Records an execution attempt that reached the executor, and its outcome."""
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
                _remember(state, f"ORDER PLACED {label}")
            else:
                _bump(state["rejects"], f"exec:{reason or 'rejected'}", label)
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
    """Minutes since the last placed order for this account.

    Falls back to process start when nothing has been placed yet, so a restart
    does not reset an ongoing drought to zero.
    """
    now = now if now is not None else time.time()
    with _lock:
        state = _bucket(chat_id)
        if state is None:
            return 0.0
        last = float(state.get("last_trade", 0.0)) or float(state.get("last_order_placed", 0.0))
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

def _top(counters: dict[str, Any], limit: int = 4) -> list[tuple[str, int, float, str]]:
    rows = [
        (code, int(entry.get("count", 0)), float(entry.get("last", 0.0)), str(entry.get("detail", "")))
        for code, entry in counters.items()
    ]
    rows.sort(key=lambda row: (-row[1], -row[2]))
    return rows[:limit]


def verdict(chat_id: str | None, *, now: float | None = None,
             equity: float = 0.0, min_viable: float = 0.0,
             feed_age_sec: float | None = None,
             stale_feed_skip: int = 0,
             eval_max_age_sec: float = 180.0) -> dict[str, Any]:
    """Classify why the account is not trading, most-actionable first.

    Returns ``{"code", "headline", "detail", "action", "severity"}``.
    """
    now = now if now is not None else time.time()
    with _lock:
        state = _bucket(chat_id)
        snapshot = dict(state) if state else _blank()
    last_eval = float(snapshot.get("last_evaluation", 0.0))
    last_trade = float(snapshot.get("last_trade", 0.0)) or float(snapshot.get("last_order_placed", 0.0))
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
            "trade_gap_min": round(trade_gap_min, 1) if trade_gap_min is not None else None,
        }

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
        return out(
            "SCOPE_EMPTY",
            f"None of the {int(snapshot['markets_total'])} open markets match this account's scope.",
            str(snapshot.get("last_detail") or ""),
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
    if int(snapshot.get("markets_evaluated", 0)) > 0 and int(snapshot.get("signals", 0)) <= 0:
        top = _top(rejects)
        detail = "; ".join(f"{code}×{count}" for code, count, _, _ in top) or "no gate counters recorded"
        return out(
            "NO_EDGE",
            f"{int(snapshot['markets_evaluated'])} market(s) evaluated per cycle; no candidate cleared its gates.",
            f"Dominant rejections: {detail}",
            "Absence of qualifying edge is the correct outcome on a quiet tape. Lowering a gate "
            "to manufacture activity is not a fix.",
            "info",
        )
    if int(snapshot.get("signals", 0)) > 0 and int(snapshot.get("order_attempts", 0)) > 0 and int(
            snapshot.get("orders_placed", 0)) <= 0:
        top = _top(rejects)
        detail = "; ".join(f"{code}×{count}" for code, count, _, _ in top) or "no executor rejections recorded"
        return out(
            "EXECUTION_BLOCKED",
            "Signals reach the executor but no order is ever placed.",
            f"Executor outcomes: {detail}",
            "Inspect the top reason: quote not confirming completeFill, book depth below the "
            "market minimum, fee-adjusted EV, or the risk budget not covering the platform minimum.",
            "critical",
        )
    if float(snapshot.get("last_order_placed", 0.0)) and int(snapshot.get("trades", 0)) <= 0:
        return out(
            "NO_CONFIRMED_FILL",
            "Orders are submitted but no exchange-confirmed fill has been recorded.",
            "Maker quotes may be resting unfilled, or fills are not being reconciled from the order object.",
            "Check /trades and the fill reconciliation log; unfilled maker orders are cancelled by design.",
            "warn",
        )
    return out(
        "HEALTHY",
        "The trading pipeline is evaluating and has placed orders.",
        f"{int(snapshot.get('evaluations', 0))} evaluation(s), {int(snapshot.get('signals', 0))} signal(s), "
        f"{int(snapshot.get('orders_placed', 0))} order(s) placed in this process.",
        "",
        "info",
    )


def report(chat_id: str | None, **context: Any) -> dict[str, Any]:
    """Full structured diagnosis for one account (safe for logs and /api/stats)."""
    now = time.time()
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
        "markets_in_scope": int(snapshot.get("markets_in_scope", 0)),
        "markets_evaluated": int(snapshot.get("markets_evaluated", 0)),
        "evaluations": int(snapshot.get("evaluations", 0)),
        "signals": int(snapshot.get("signals", 0)),
        "order_attempts": int(snapshot.get("order_attempts", 0)),
        "orders_placed": int(snapshot.get("orders_placed", 0)),
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
        "recent": list(snapshot["recent"])[-6:],
        "scan": {
            "age_sec": round(now - float(_global.get("last_scan_ok", 0.0)), 1)
            if _global.get("last_scan_ok") else None,
            "markets": int(_global.get("scan_markets", 0)),
            "last_error": str(_global.get("last_scan_error", "")),
        },
        "verdict": verdict_data,
    }


def format_report(chat_id: str | None, **context: Any) -> str:
    """Telegram-shaped rendering of :func:`report`."""
    data = report(chat_id, **context)
    verdict_data = data["verdict"]
    icon = {"critical": "🔴", "warn": "🟠", "config": "⚙️", "info": "🟢"}.get(
        verdict_data.get("severity", "info"), "🟢"
    )
    lines = [
        f"{icon} {verdict_data['headline']}",
        "",
        f"Code: `{verdict_data['code']}`",
    ]
    if verdict_data.get("trade_gap_min") is not None:
        lines.append(f"Last confirmed trade: {verdict_data['trade_gap_min']:.0f} min ago")
    if verdict_data.get("detail"):
        lines += ["", verdict_data["detail"]]
    if verdict_data.get("action"):
        lines += ["", f"→ {verdict_data['action']}"]
    lines += [
        "",
        f"Markets: {data['markets_total']} open, {data['markets_in_scope']} in scope, "
        f"{data['markets_evaluated']} evaluated per cycle",
        f"Process totals: {data['evaluations']} evaluations | {data['signals']} signals | "
        f"{data['orders_placed']} orders placed",
    ]
    if data["top_rejects"]:
        lines += ["", "🚪 Gates that stopped candidates:"]
        for row in data["top_rejects"]:
            lines.append(f"  {row['code']}: {row['count']}× (last {row['age_sec']:.0f}s ago)")
    if data["skip_counts"]:
        skips = sorted(data["skip_counts"].items(), key=lambda kv: -kv[1])[:5]
        lines += ["", "⏭ Skips: " + ", ".join(f"{k}={v}" for k, v in skips)]
    if data["recent"]:
        lines += ["", "🕒 Recent:"]
        for row in data["recent"][-4:]:
            lines.append(f"  {time.strftime('%H:%M:%S', time.localtime(row['t']))} {row['text']}")
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
