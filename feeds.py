"""
Real-time price feeds.

BTC, ETH, SOL spot prices are polled from Kraken REST every CHAINLINK_POLL_SEC seconds.
- Binance (WS + REST): HTTP 451 geo-blocked from Render Oregon (US IPs)
- CoinCap: DNS resolution failure on Render
- Kraken: US-based, no geo-block, no API key, 1 req/sec public limit
Bayse market YES/NO prices come from the Bayse WebSocket.
"""

import asyncio
import json
import logging
import aiohttp
import websockets
from config import WS_MARKETS_URL, CHAINLINK_POLL_SEC

log = logging.getLogger(__name__)

# Live spot prices — {"BTC": 77800.12, "ETH": 1520.50, "SOL": 86.72}
spot: dict[str, float] = {}

# Previous Bayse YES prices — used to detect BTC moves for correlation signals
prev_yes: dict[str, float] = {}

# Current Bayse market prices — {market_id: {"yes": float, "no": float}}
market_prices: dict[str, dict] = {}

# Bayse WebSocket task (restarted after each market scan)
_bayse_task: asyncio.Task | None = None
_subscribed_ids: set[str] = set()

def _parse_frames(raw: str):
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


# ── Kraken REST feed (BTC + ETH + SOL) ───────────────────────────────────────
# Kraken is a US-based exchange — no geo-block from Render Oregon, no API key needed.
# Binance (WS + REST) returns HTTP 451 from US IPs; CoinCap has DNS failures on Render.

_KRAKEN_PAIRS = {
    "XXBTZUSD": "BTC",
    "XETHZUSD": "ETH",
    "SOLUSD":   "SOL",
}

async def kraken_feed(on_price=None):
    url = "https://api.kraken.com/0/public/Ticker"
    params = {"pair": "XBTUSD,ETHUSD,SOLUSD"}
    backoff = 1

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                log.info("Kraken price feed started (BTC, ETH, SOL)")
                backoff = 1
                while True:
                    try:
                        async with session.get(
                            url, params=params,
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as r:
                            if r.status == 200:
                                data = await r.json()
                                errors = data.get("error", [])
                                if errors:
                                    log.warning(f"Kraken API error: {errors}")
                                else:
                                    for pair, asset in _KRAKEN_PAIRS.items():
                                        info = data.get("result", {}).get(pair)
                                        if info:
                                            price = float(info["c"][0])
                                            spot[asset] = price
                                            log.debug(f"Kraken {asset}: {price:,.4f}")
                                            if on_price:
                                                on_price(asset, price)
                            elif r.status == 429:
                                log.warning("Kraken rate limited — waiting 60s")
                                await asyncio.sleep(60)
                            else:
                                log.warning(f"Kraken HTTP {r.status}")
                    except Exception as e:
                        log.warning(f"Kraken fetch error: {e}")
                    await asyncio.sleep(CHAINLINK_POLL_SEC)
        except Exception as e:
            log.warning(f"Kraken feed crashed: {e}. Restarting in {backoff}s")
            spot.clear()  # don't trade on stale prices while feed is down
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── Bayse market feed ────────────────────────────────────────────────────────

async def bayse_feed(event_ids: list[str], on_update=None):
    if not event_ids:
        return
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_MARKETS_URL, ping_interval=20) as ws:
                log.info(f"Bayse market feed connected ({len(event_ids)} events)")
                backoff = 1
                for eid in event_ids:
                    await ws.send(json.dumps({
                        "type": "subscribe", "channel": "prices", "eventId": eid
                    }))
                async for raw in ws:
                    for msg in _parse_frames(raw):
                        _handle_market(msg, on_update)
        except Exception as e:
            log.warning(f"Bayse market feed error: {e}. Reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _handle_market(msg: dict, on_update=None):
    data = msg.get("data", {})
    mid = data.get("marketId") or data.get("id")
    if not mid:
        return

    # outcome1 = UP/YES, outcome2 = DOWN/NO
    yes_p = (data.get("yesPrice") or data.get("yes")
             or data.get("outcome1Price") or data.get("upPrice"))
    no_p = (data.get("noPrice") or data.get("no")
            or data.get("outcome2Price") or data.get("downPrice"))

    if yes_p is not None:
        prev_yes[mid] = market_prices.get(mid, {}).get("yes", float(yes_p))
        market_prices[mid] = {
            "yes": float(yes_p),
            "no": float(no_p) if no_p is not None else round(1.0 - float(yes_p), 4),
        }
        if on_update:
            on_update(mid, market_prices[mid])


def restart_bayse_feed(markets: list[dict], on_update=None):
    """Call after each market scan to keep the WS subscriptions current.
    markets: full market dicts from the scanner (need both event_id and market_id).
    Subscribes by event_id (what the Bayse WS endpoint expects).
    """
    global _bayse_task, _subscribed_ids
    new_market_ids = {m["market_id"] for m in markets}
    if new_market_ids == _subscribed_ids and _bayse_task and not _bayse_task.done():
        return  # no change — avoid unnecessary reconnects
    _subscribed_ids = new_market_ids
    if _bayse_task and not _bayse_task.done():
        _bayse_task.cancel()
    if markets:
        # Deduplicate event_ids — each event has one market but avoid duplicate subscribes
        event_ids = list({m["event_id"] for m in markets})
        _bayse_task = asyncio.create_task(bayse_feed(event_ids, on_update))
        log.info(f"Bayse WS (re)started — {len(event_ids)} events / {len(markets)} markets")


async def start_feeds(on_price=None):
    """Start only the Kraken spot feed. Bayse WS is managed via restart_bayse_feed()."""
    await kraken_feed(on_price)
