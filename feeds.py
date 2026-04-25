"""
Real-time price feeds.

BTC, ETH, SOL spot prices are all fetched from CoinGecko every 10 seconds.
Binance WebSocket is not used — it geo-blocks non-US Render servers (HTTP 451).
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

_CG_IDS = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}


def _parse_frames(raw: str):
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


# ── Binance REST feed (BTC + ETH + SOL) ──────────────────────────────────────

_BINANCE_SYMBOLS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}

async def binance_rest_feed(on_price=None):
    """
    Poll Binance REST API every CHAINLINK_POLL_SEC seconds.
    REST is NOT geo-blocked (only the WebSocket was). No API key needed.
    Rate limit: 1200 req/min — polling every 10s uses ~18 req/min, well within limits.
    """
    url = "https://api.binance.com/api/v3/ticker/price"
    backoff = 1

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                log.info("Binance REST price feed started (BTC, ETH, SOL)")
                backoff = 1
                while True:
                    try:
                        for symbol, asset in _BINANCE_SYMBOLS.items():
                            async with session.get(
                                url, params={"symbol": symbol},
                                timeout=aiohttp.ClientTimeout(total=8),
                            ) as r:
                                if r.status == 200:
                                    data = await r.json()
                                    price = float(data.get("price", 0))
                                    if price:
                                        spot[asset] = price
                                        log.debug(f"Binance REST {asset}: {price:,.4f}")
                                        if on_price:
                                            on_price(asset, price)
                                elif r.status == 429:
                                    log.warning("Binance REST rate limited — waiting 30s")
                                    await asyncio.sleep(30)
                                else:
                                    log.warning(f"Binance REST {symbol}: HTTP {r.status}")
                    except Exception as e:
                        log.warning(f"Binance REST fetch error: {e}")
                    await asyncio.sleep(CHAINLINK_POLL_SEC)
        except Exception as e:
            log.warning(f"Binance REST feed crashed: {e}. Restarting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── Bayse market feed ────────────────────────────────────────────────────────

async def bayse_feed(market_ids: list[str], on_update=None):
    if not market_ids:
        return
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_MARKETS_URL, ping_interval=20) as ws:
                log.info(f"Bayse market feed connected ({len(market_ids)} markets)")
                backoff = 1
                for mid in market_ids:
                    await ws.send(json.dumps({
                        "type": "subscribe", "channel": "prices", "eventId": mid
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


async def start_feeds(market_ids: list[str], on_price=None, on_update=None):
    await asyncio.gather(
        binance_rest_feed(on_price),
        bayse_feed(market_ids, on_update),
    )
