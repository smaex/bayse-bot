"""
Market scanner — fetches active markets, enriches with full event details.
Always requests prices in NGN.
Caches last known good markets — survives Bayse API 500 outages.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from client import BayseClient
from config import SERIES, ALL_TIMEFRAMES, ASSET_ORACLE, ALL_ASSETS, CURRENCY

log = logging.getLogger(__name__)

# Cache of last successful scan — used when Bayse returns 500s
_last_known_markets: list[dict] = []
_last_known_time:    float      = 0.0
_CACHE_MAX_AGE_SEC               = 120  # use cache for up to 2 min during outages


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


def _parse_threshold_from_title(title: str) -> Optional[float]:
    patterns = [
        r'(?:above|below|at)\s+\$?([\d,]+\.?\d*)',
        r'\$?([\d,]{4,}\.?\d*)',
    ]
    for pattern in patterns:
        m = re.search(pattern, title, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0.01:
                    return val
            except (ValueError, AttributeError):
                continue
    return None


async def _enrich(client: BayseClient, lean_event: dict, asset: str, timeframe: str) -> Optional[dict]:
    event_id     = lean_event.get("id")
    closing_date = lean_event.get("closingDate", "")
    opening_date = lean_event.get("openingDate", lean_event.get("startDate", ""))

    if not event_id:
        return None

    secs_to_close = _seconds_to_close(closing_date)
    secs_to_open  = _seconds_to_open(opening_date)

    if secs_to_close < 0 or secs_to_open > 0:
        return None

    try:
        full = await client.get_event(event_id, currency=CURRENCY)
    except Exception as e:
        log.debug(f"Failed to enrich {event_id}: {e}")
        return None

    markets = full.get("markets", [])
    if not markets:
        return None
    market = markets[0]

    threshold = (
        full.get("eventThreshold")
        or full.get("threshold")
        or full.get("openingPrice")
        or market.get("marketThreshold")
        or market.get("threshold")
        or market.get("openingPrice")
        or _parse_threshold_from_title(full.get("title", ""))
    )
    if threshold is not None:
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            threshold = None

    market_id = market.get("id")
    yes_id    = market.get("outcome1Id")
    no_id     = market.get("outcome2Id")

    if not market_id or not yes_id or not no_id:
        return None

    raw_yes = market.get("outcome1Price")
    raw_no  = market.get("outcome2Price")
    if raw_yes is None or raw_no is None:
        return None

    yes_price = float(raw_yes)
    no_price  = float(raw_no)

    if yes_price <= 0 or no_price <= 0:
        return None

    # Data-quality guard: YES+NO should sum close to 1.0 in any valid,
    # liquid binary market. A market priced at e.g. yes=0.030 no=0.020
    # (sum=0.05) is broken or has effectively zero real liquidity behind
    # those numbers. Trusting it produces a mathematically "huge edge"
    # that isn't real — observed directly in production (a strategy
    # computed 65% true probability vs a 3% "market" price and burned
    # 50+ evaluation cycles on a market that could never have filled).
    # Filtering here means every strategy is protected automatically,
    # not just whichever ones remember to check it themselves.
    price_sum = yes_price + no_price
    if not (0.90 <= price_sum <= 1.05):
        log.debug(
            f"Skipping {asset} {timeframe} — bad price data "
            f"(yes={yes_price:.3f} no={no_price:.3f} sum={price_sum:.3f})"
        )
        return None

    fee_pct = float(market.get("feePercentage", 2)) / 100

    return {
        "event_id":    event_id,
        "market_id":   market_id,
        "asset":       asset,
        "timeframe":   timeframe,
        "title":       full.get("title", ""),
        "threshold":   threshold,
        "yes_id":      yes_id,
        "no_id":       no_id,
        "yes_label":   market.get("outcome1Label", "YES"),
        "no_label":    market.get("outcome2Label", "NO"),
        "yes_price":   yes_price,
        "no_price":    no_price,
        "fee_rate":    fee_pct,
        "opening_date": opening_date,
        "closing_date": closing_date,
        "secs_to_close": secs_to_close,
        "status":      full.get("status", "open"),
        "engine":      full.get("engine") or market.get("engine", "AMM"),
        "oracle":      ASSET_ORACLE.get(asset, "BINANCE"),
    }


async def scan_all(client: BayseClient) -> list[dict]:
    global _last_known_markets, _last_known_time

    tasks = []
    for asset, timeframes in SERIES.items():
        if asset not in ALL_ASSETS:
            continue
        for tf, slug in timeframes.items():
            if tf not in ALL_TIMEFRAMES:
                continue
            tasks.append(_scan_series(client, asset, tf, slug))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    markets    = []
    all_failed = True
    for r in results:
        if isinstance(r, list):
            markets.extend(r)
            if r:
                all_failed = False
        elif isinstance(r, Exception):
            pass  # individual series errors logged inside _scan_series
        else:
            all_failed = False

    if markets:
        # Good scan — update cache
        _last_known_markets = markets
        _last_known_time    = time.time()
        log.info(f"Scan complete: {len(markets)} active markets")
        return markets

    # No markets found — check if it's a Bayse outage
    cache_age = time.time() - _last_known_time
    if _last_known_markets and cache_age < _CACHE_MAX_AGE_SEC:
        # Refresh secs_to_close in cached markets so timers stay accurate
        for m in _last_known_markets:
            m["secs_to_close"] = _seconds_to_close(m.get("closing_date", ""))
        valid = [m for m in _last_known_markets if m["secs_to_close"] > 0]
        if valid:
            log.warning(
                f"Bayse API returned 0 markets — using {len(valid)} cached "
                f"(age {cache_age:.0f}s)"
            )
            return valid

    log.info("Scan complete: 0 active markets")
    return []


async def _scan_series(client: BayseClient, asset: str, tf: str, slug: str) -> list[dict]:
    try:
        lean_events = await client.get_series_events(slug)
    except Exception as e:
        log.warning(f"Series {slug} fetch failed: {e}")
        return []
    tasks    = [_enrich(client, ev, asset, tf) for ev in lean_events]
    enriched = await asyncio.gather(*tasks, return_exceptions=True)
    return [m for m in enriched if isinstance(m, dict)]


async def discover_series(client: BayseClient) -> None:
    slugs: dict[str, int] = {}
    page = 1
    while True:
        try:
            data   = await client.list_events(page=page, limit=50)
            events = data if isinstance(data, list) else data.get("events", data.get("data", []))
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
        log.info("Available Bayse series slugs:")
        for slug, count in sorted(slugs.items()):
            tracked = any(slug in tf.values() for tf in SERIES.values())
            log.info(f"  {'✅' if tracked else '❌'} {slug} ({count} open)")
    else:
        log.warning("discover_series: no slugs found")
