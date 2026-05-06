"""
Market scanner.

Fetches all active BTC/ETH/SOL markets across all timeframes,
enriches them with full event details (market IDs, outcome IDs, threshold),
and returns them ready for the strategy engine.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from client import BayseClient
from config import SERIES, ALL_TIMEFRAMES, ASSET_ORACLE, ALL_ASSETS

log = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _seconds_to_close(closing_date: str) -> float:
    dt = _parse_dt(closing_date)
    if not dt:
        return -1
    return (dt - _now_utc()).total_seconds()


def _seconds_to_open(opening_date: str) -> float:
    dt = _parse_dt(opening_date)
    if not dt:
        return -1
    return (dt - _now_utc()).total_seconds()


async def _enrich(client: BayseClient, lean_event: dict, asset: str, timeframe: str) -> Optional[dict]:
    """Fetch full event details to get marketId, outcomeIds, threshold, prices."""
    event_id = lean_event.get("id")
    if not event_id:
        return None

    closing_date = lean_event.get("closingDate", "")
    opening_date = lean_event.get("openingDate", lean_event.get("startDate", ""))

    secs_to_close = _seconds_to_close(closing_date)
    secs_to_open = _seconds_to_open(opening_date)

    # Skip already closed or not yet open (beyond 5 windows ahead)
    if secs_to_close < 0:
        return None
    if secs_to_open > 0:
        return None  # not open yet

    try:
        full = await client.get_event(event_id)
    except Exception as e:
        log.debug(f"Failed to enrich {event_id}: {e}")
        return None

    markets = full.get("markets", [])
    if not markets:
        return None

    market = markets[0]
    threshold = full.get("eventThreshold") or market.get("marketThreshold")
    yes_id = market.get("outcome1Id")    # "Up" or "Yes" outcome
    no_id = market.get("outcome2Id")     # "Down" or "No" outcome
    yes_label = market.get("outcome1Label", "Up")   # "Up" or "Yes"
    no_label = market.get("outcome2Label", "Down")  # "Down" or "No"
    market_id = market.get("id")
    fee_pct = float(market.get("feePercentage", 4)) / 100

    if not market_id or not yes_id or not no_id:
        return None

    # Reject markets with missing prices — 0.5/0.5 default would cause false ARB signals
    raw_yes = market.get("outcome1Price")
    raw_no  = market.get("outcome2Price")
    if raw_yes is None or raw_no is None:
        return None
    yes_price = float(raw_yes)
    no_price  = float(raw_no)

    # Reject markets with zero prices or missing threshold — likely broken or closed
    if yes_price <= 0 or no_price <= 0 or not threshold:
        return None

    return {
        "event_id": event_id,
        "market_id": market_id,
        "asset": asset,
        "timeframe": timeframe,
        "title": full.get("title", ""),
        "threshold": threshold,         # opening Binance price = resolution reference
        "yes_id": yes_id,
        "no_id": no_id,
        "yes_label": yes_label,
        "no_label": no_label,
        "yes_price": yes_price,
        "no_price": no_price,
        "fee_rate": fee_pct,            # base fee rate (e.g. 0.04)
        "opening_date": opening_date,
        "closing_date": closing_date,
        "resolution_date": full.get("resolutionDate", ""),
        "secs_to_close": secs_to_close,
        "status": full.get("status", "open"),
        "engine": full.get("engine", "AMM"),
        "oracle": ASSET_ORACLE.get(asset, "BINANCE"),
        "series_slug": full.get("seriesSlug", ""),
    }


async def discover_series(client: BayseClient) -> None:
    """
    Fetch all open events from the Bayse API and log every unique series slug found.
    Run once on startup to discover FX/commodity slugs for config.py.
    """
    slugs: dict[str, int] = {}  # slug → count of open events
    page = 1
    while True:
        try:
            data    = await client.list_events(page=page, limit=50)
            events  = data if isinstance(data, list) else data.get("events", data.get("data", []))
            if not events:
                break
            for ev in events:
                slug = ev.get("seriesSlug") or ev.get("series") or ""
                if slug:
                    slugs[slug] = slugs.get(slug, 0) + 1
            pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
            if page >= pagination.get("lastPage", 1):
                break
            page += 1
        except Exception as e:
            log.warning(f"discover_series page {page}: {e}")
            break

    if slugs:
        log.info("═══ Available series slugs on Bayse ══════════════════════")
        for slug, count in sorted(slugs.items()):
            log.info(f"  {slug}  ({count} open events)")
        log.info("══════════════════════════════════════════════════════════")
    else:
        log.warning("discover_series: no slugs found — check API connectivity")


async def scan_all(client: BayseClient) -> list[dict]:
    """Fetch all currently open markets across all assets and timeframes."""
    tasks = []
    for asset, timeframes in SERIES.items():
        if asset not in ALL_ASSETS:
            continue
        for tf, slug in timeframes.items():
            if tf not in ALL_TIMEFRAMES:
                continue
            tasks.append(_scan_series(client, asset, tf, slug))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    markets = []
    for r in results:
        if isinstance(r, list):
            markets.extend(r)
    log.info(f"Scan complete: {len(markets)} active markets")
    return markets


async def _scan_series(client: BayseClient, asset: str, timeframe: str, slug: str) -> list[dict]:
    try:
        lean_events = await client.get_series_events(slug)
    except Exception as e:
        log.warning(f"Failed to fetch series {slug}: {e}")
        return []

    enrich_tasks = [_enrich(client, ev, asset, timeframe) for ev in lean_events]
    enriched = await asyncio.gather(*enrich_tasks, return_exceptions=True)
    return [m for m in enriched if isinstance(m, dict) and m is not None]
