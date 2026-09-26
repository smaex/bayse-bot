"""Read-only MAKER quote-ladder audit.

Records only MAKER signals skipped because the current cap cannot compete with
an otherwise fresh book. It compares a bounded set of hypothetical price caps
against the signal's model FV and the same post-only/top-of-book constraints as
the executor. It never places, cancels, or simulates a confirmed fill.

The snapshot measures *price feasibility*, not profitability or fill rate. A
competitive post-only quote can still sit in queue, never fill, or be adversely
selected. Results are local diagnostic data under ``data/`` and are not sent to
any trading or sizing decision.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

import config
from strategies.maker import HALF_SPREAD

log = logging.getLogger("maker_shadow")

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_LOG_FILE = os.path.join(_DATA_DIR, "maker_quote_shadow.jsonl")

# Diagnostic counterfactuals only; none of these values affect live orders.
_BASE_CAP_LEVELS = (0.58, 0.62, 0.66, 0.70, 0.74)
_DEDUPE_SEC = 60.0
_MAX_FILE_BYTES = 5_000_000
_MAX_REPORT_ROWS = 5_000

_lock = threading.Lock()
_file_lock = threading.Lock()
_last_recorded: dict[tuple[str, str], float] = {}


def _top_price(book: dict, side: str) -> float | None:
    prices = []
    for level in book.get(side) or []:
        try:
            raw = level.get("price") if isinstance(level, dict) else level[0]
            price = float(raw)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            continue
        if math.isfinite(price) and 0.0 < price < 1.0:
            prices.append(price)
    if not prices:
        return None
    return min(prices) if side == "asks" else max(prices)


def _cap_observation(
    cap: float,
    *,
    fair_value: float,
    best_bid: float | None,
    best_ask: float | None,
) -> dict[str, Any]:
    """Evaluate a hypothetical cap with the live executor's passive-price rules."""
    quote_price = round(float(cap), 3)
    post_only_price_exists = True
    if best_ask is not None and quote_price >= best_ask - 1e-9:
        quote_price = round(best_ask - config.MAKER_TICK, 3)
        if quote_price < config.MAKER_MIN_BID - 1e-9:
            post_only_price_exists = False

    if not post_only_price_exists:
        return {
            "cap": round(float(cap), 3),
            "quote_price": None,
            "edge": None,
            "gross_roi": None,
            "edge_ok": False,
            "book_competitive": False,
            "both_ok": False,
        }

    quote_price = max(0.0, round(quote_price, 3))
    edge = fair_value - quote_price
    gross_roi = fair_value / quote_price - 1.0 if quote_price > 0 else None
    post_only = best_ask is None or quote_price < best_ask - 1e-9
    ticks_behind = (
        (best_bid - quote_price) / config.MAKER_TICK
        if best_bid is not None else 0.0
    )
    book_competitive = (
        post_only
        and ticks_behind <= config.MAKER_MAX_TICKS_BEHIND_BEST_BID + 1e-9
    )
    edge_ok = (
        quote_price >= config.MAKER_MIN_BID - 1e-9
        and edge >= HALF_SPREAD - 1e-9
    )
    return {
        "cap": round(float(cap), 3),
        "quote_price": quote_price,
        "edge": round(edge, 6),
        "gross_roi": round(gross_roi, 6) if gross_roi is not None else None,
        "edge_ok": edge_ok,
        "book_competitive": book_competitive,
        "both_ok": edge_ok and book_competitive,
    }


def build_snapshot(sig, book: dict, *, now: float | None = None) -> dict | None:
    """Build the diagnostic record without I/O or mutation of the signal/book."""
    if str(getattr(sig, "strategy", "")).upper() != "MAKER":
        return None
    try:
        fair_value = float(sig.win_prob)
        signal_cap = float(sig.market_price)
    except (AttributeError, TypeError, ValueError):
        return None
    if not math.isfinite(fair_value) or not 0.0 < fair_value < 1.0:
        return None
    if not math.isfinite(signal_cap) or not 0.0 < signal_cap < 1.0:
        return None
    if not isinstance(book, dict):
        return None

    best_bid = _top_price(book, "bids")
    best_ask = _top_price(book, "asks")
    levels = sorted({round(float(config.MAKER_MAX_BID), 3), *_BASE_CAP_LEVELS})
    observations = [
        _cap_observation(
            cap,
            fair_value=fair_value,
            best_bid=best_bid,
            best_ask=best_ask,
        )
        for cap in levels
        if config.MAKER_MIN_BID <= cap <= 0.75
    ]
    now = time.time() if now is None else float(now)
    return {
        "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "market_id": str(getattr(sig, "market_id", "") or ""),
        "asset": str(getattr(sig, "asset", "") or ""),
        "timeframe": str(getattr(sig, "timeframe", "") or ""),
        "outcome": str(getattr(sig, "outcome", "") or ""),
        "outcome_id": str(getattr(sig, "outcome_id", "") or ""),
        "fair_value": round(fair_value, 6),
        "signal_cap": round(signal_cap, 3),
        "configured_cap": round(float(config.MAKER_MAX_BID), 3),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "levels": observations,
        "interpretation": "snapshot_only_no_fill_or_settlement_observed",
    }


def _append(record: dict) -> None:
    try:
        with _file_lock:
            os.makedirs(_DATA_DIR, exist_ok=True)
            if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > _MAX_FILE_BYTES:
                with open(_LOG_FILE, "rb") as source:
                    source.seek(os.path.getsize(_LOG_FILE) // 2)
                    source.readline()
                    tail = source.read()
                with open(_LOG_FILE, "wb") as target:
                    target.write(tail)
            with open(_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        log.debug("MAKER shadow snapshot persistence failed: %s", exc)


def record_candidate(sig, book: dict, *, now: float | None = None) -> bool:
    """Persist a deduplicated counterfactual for a skipped MAKER quote."""
    now = time.time() if now is None else float(now)
    snapshot = build_snapshot(sig, book, now=now)
    if snapshot is None:
        return False
    key = (snapshot["market_id"], snapshot["outcome_id"] or snapshot["outcome"])
    with _lock:
        last = _last_recorded.get(key)
        if last is not None and now - last < _DEDUPE_SEC:
            return False
        _last_recorded[key] = now
        # Expire old keys to keep this cache bounded in a long-running process.
        if len(_last_recorded) > 2_000:
            cutoff = now - _DEDUPE_SEC
            for old_key, timestamp in list(_last_recorded.items()):
                if timestamp < cutoff:
                    _last_recorded.pop(old_key, None)
            while len(_last_recorded) > 2_000:
                oldest_key = min(_last_recorded, key=_last_recorded.get)
                _last_recorded.pop(oldest_key, None)
    _append(snapshot)
    return True


def _load_records() -> list[dict]:
    if not os.path.exists(_LOG_FILE):
        return []
    try:
        with open(_LOG_FILE, encoding="utf-8") as handle:
            lines = handle.readlines()[-_MAX_REPORT_ROWS:]
        records = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("levels"), list):
                records.append(row)
        return records
    except OSError as exc:
        log.debug("MAKER shadow report read failed: %s", exc)
        return []


def get_summary_report() -> str:
    """Summarize price feasibility; never present it as fills or profitability."""
    records = _load_records()
    if not records:
        return (
            "🕶️ *MAKER Quote Ladder — read-only*\n\n"
            "No skipped signal/book snapshots recorded yet. It records only "
            "fresh-book MAKER skips caused by an uncompetitive quote.\n\n"
            "No orders are placed by this monitor."
        )

    caps = sorted({
        round(float(level.get("cap")), 3)
        for row in records
        for level in row.get("levels", [])
        if isinstance(level, dict) and level.get("cap") is not None
    })
    lines = [
        "🕶️ *MAKER Quote Ladder — read-only*",
        f"Snapshots: *{len(records)}* distinct skipped signal/book observations",
        "",
        "*Cap | model-edge eligible | book-competitive | both*",
    ]
    for cap in caps:
        rows = [
            level
            for row in records
            for level in row.get("levels", [])
            if isinstance(level, dict)
            and level.get("cap") is not None
            and round(float(level["cap"]), 3) == cap
        ]
        edge_ok = sum(bool(row.get("edge_ok")) for row in rows)
        book_ok = sum(bool(row.get("book_competitive")) for row in rows)
        both_ok = sum(bool(row.get("both_ok")) for row in rows)
        marker = " ← configured" if any(
            abs(float(record.get("configured_cap", -1.0)) - cap) < 1e-9
            for record in records
        ) else ""
        lines.append(
            f"{cap:.2f} | {edge_ok}/{len(rows)} | {book_ok}/{len(rows)} | "
            f"{both_ok}/{len(rows)}{marker}"
        )
    lines.extend([
        "",
        "Edge-eligible means model FV exceeds the hypothetical quote by at "
        f"least {HALF_SPREAD:.1%}; book-competitive means post-only and within "
        f"{config.MAKER_MAX_TICKS_BEHIND_BEST_BID} tick(s) of the best bid.",
        "This measures price feasibility at signal time only. It does not infer "
        "queue position, fills, settlement, or realized profit; do not raise a "
        "live cap from this report alone.",
    ])
    return "\n".join(lines)
