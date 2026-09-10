"""Read-only complete-set arbitrage monitor.

The monitor evaluates two structural patterns supported by Bayse's documented
CLOB, mint, and burn APIs:

* BUY_BURN: buy equal YES/NO shares when fee-adjusted asks sum below one,
  then burn the complete set back to wallet currency.
* MINT_SELL: mint equal YES/NO shares, then sell both when fee-adjusted bids
  sum above one.

It never places an order. Batch placement is best-effort rather than atomic, so
live promotion requires observed opportunities plus an orphan-leg state machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import config

log = logging.getLogger("complete_set_shadow")

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_LOG_FILE = os.path.join(_DATA_DIR, "complete_set_shadow.jsonl")
_last_scan_by_market: dict[str, float] = {}
_scan_failures = 0


@dataclass(frozen=True)
class CompleteSetSnapshot:
    timestamp: str
    market_id: str
    asset: str
    timeframe: str
    buy_burn_edge: float
    mint_sell_edge: float
    buy_burn_pair_quantity: float
    mint_sell_pair_quantity: float
    buy_burn_opportunity: bool
    mint_sell_opportunity: bool


def clob_fee_fraction(price: float, fee_rate: float) -> float:
    return max(0.0, fee_rate) * max(1.0 - price, config.FEE_FLOOR)


def clob_buy_cost_per_net_share(price: float, fee_rate: float) -> float:
    """Wallet cost for one net share when BUY fees reduce shares received."""
    return price / max(1.0 - clob_fee_fraction(price, fee_rate), 1e-9)


def clob_sell_proceeds_per_share(price: float, fee_rate: float) -> float:
    """Net wallet proceeds for one sold share after the CLOB taker fee."""
    return price * (1.0 - clob_fee_fraction(price, fee_rate))


def _top_level(book: dict, side: str) -> tuple[float, float] | None:
    levels = book.get(side) or []
    if not levels:
        return None
    # Do not trust transport ordering when a malformed fixture/API response can
    # be handled deterministically.
    chooser = min if side == "asks" else max
    try:
        level = chooser(levels, key=lambda row: float(row.get("price") or 0.0))
        price = float(level.get("price") or 0.0)
        quantity = float(level.get("quantity") or 0.0)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (0.0 < price < 1.0) or quantity <= 0:
        return None
    return price, quantity


def evaluate_books(
    *, market_id: str, asset: str, timeframe: str,
    yes_book: dict, no_book: dict, fee_rate: float,
    min_edge: float | None = None,
) -> CompleteSetSnapshot | None:
    """Evaluate executable top-of-book complete-set economics."""
    yes_ask = _top_level(yes_book, "asks")
    no_ask = _top_level(no_book, "asks")
    yes_bid = _top_level(yes_book, "bids")
    no_bid = _top_level(no_book, "bids")
    if not all((yes_ask, no_ask, yes_bid, no_bid)):
        return None

    threshold = config.COMPLETE_SET_MIN_EDGE if min_edge is None else min_edge
    buy_cost = (
        clob_buy_cost_per_net_share(yes_ask[0], fee_rate)
        + clob_buy_cost_per_net_share(no_ask[0], fee_rate)
    )
    sell_proceeds = (
        clob_sell_proceeds_per_share(yes_bid[0], fee_rate)
        + clob_sell_proceeds_per_share(no_bid[0], fee_rate)
    )
    buy_edge = 1.0 - buy_cost
    sell_edge = sell_proceeds - 1.0

    return CompleteSetSnapshot(
        timestamp=datetime.now(timezone.utc).isoformat(),
        market_id=market_id,
        asset=asset,
        timeframe=timeframe,
        buy_burn_edge=buy_edge,
        mint_sell_edge=sell_edge,
        buy_burn_pair_quantity=min(yes_ask[1], no_ask[1]),
        mint_sell_pair_quantity=min(yes_bid[1], no_bid[1]),
        buy_burn_opportunity=buy_edge >= threshold,
        mint_sell_opportunity=sell_edge >= threshold,
    )


async def _scan_one(client, market: dict) -> CompleteSetSnapshot | None:
    yes_id = market.get("yes_id")
    no_id = market.get("no_id")
    if not yes_id or not no_id:
        return None
    yes_book, no_book = await asyncio.gather(
        client.get_orderbook(yes_id, depth=5),
        client.get_orderbook(no_id, depth=5),
    )
    return evaluate_books(
        market_id=market.get("market_id", ""),
        asset=market.get("asset", ""),
        timeframe=market.get("timeframe", ""),
        yes_book=yes_book,
        no_book=no_book,
        fee_rate=float(market.get("fee_rate") or 0.0),
    )


def _append(snapshot: CompleteSetSnapshot) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        # Bound local runtime growth. Keep the most recent half if the file
        # exceeds 5 MiB; this data is diagnostic and is ignored by Git.
        if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > 5_000_000:
            with open(_LOG_FILE, "rb") as source:
                source.seek(max(0, os.path.getsize(_LOG_FILE) // 2))
                source.readline()
                tail = source.read()
            with open(_LOG_FILE, "wb") as target:
                target.write(tail)
        with open(_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(snapshot), sort_keys=True) + "\n")
    except OSError as exc:
        log.warning("Complete-set shadow persistence failed: %s", exc)


async def scan_markets(client, markets: list[dict]) -> list[CompleteSetSnapshot]:
    """Scan eligible CLOB books at most once per market per 30 seconds."""
    global _scan_failures
    now = time.time()
    eligible = []
    for market in markets:
        market_id = market.get("market_id", "")
        if str(market.get("engine") or "").upper() != "CLOB":
            continue
        if market.get("status") != "open" or not market_id:
            continue
        if now - _last_scan_by_market.get(market_id, 0.0) < 30.0:
            continue
        _last_scan_by_market[market_id] = now
        eligible.append(market)

    if not eligible:
        return []

    results = await asyncio.gather(
        *(_scan_one(client, market) for market in eligible),
        return_exceptions=True,
    )
    snapshots = []
    for result in results:
        if isinstance(result, CompleteSetSnapshot):
            snapshots.append(result)
            _append(result)
            if result.buy_burn_opportunity or result.mint_sell_opportunity:
                log.warning(
                    "COMPLETE-SET SHADOW opportunity %s %s: buy/burn=%+.2f%% "
                    "mint/sell=%+.2f%%",
                    result.asset,
                    result.timeframe,
                    result.buy_burn_edge * 100.0,
                    result.mint_sell_edge * 100.0,
                )
        elif isinstance(result, Exception):
            _scan_failures += 1
            log.debug("Complete-set shadow scan failed: %s", result)
    return snapshots


def _load_records(limit: int = 5000) -> list[dict]:
    if not os.path.exists(_LOG_FILE):
        return []
    try:
        with open(_LOG_FILE, encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def get_summary_report() -> str:
    records = _load_records()
    if not records:
        return (
            "🔬 *Complete-Set Arbitrage Shadow*\n\n"
            "No CLOB book snapshots recorded yet. This monitor is read-only; "
            "it never places orders."
        )

    buy = [row for row in records if row.get("buy_burn_opportunity")]
    sell = [row for row in records if row.get("mint_sell_opportunity")]
    best_buy = max(float(row.get("buy_burn_edge") or 0.0) for row in records)
    best_sell = max(float(row.get("mint_sell_edge") or 0.0) for row in records)
    return "\n".join([
        "🔬 *Complete-Set Arbitrage Shadow*",
        f"Snapshots: {len(records)}",
        f"BUY + BURN opportunities: {len(buy)}",
        f"MINT + SELL opportunities: {len(sell)}",
        f"Best fee-adjusted BUY/BURN edge: {best_buy:+.2%}",
        f"Best fee-adjusted MINT/SELL edge: {best_sell:+.2%}",
        f"Book/API failures this process: {_scan_failures}",
        "",
        (
            "⚠️ Read-only observation. Bayse batches are not atomic, so a "
            "quoted spread is not locked until every leg and conversion is "
            "confirmed."
        ),
    ])
