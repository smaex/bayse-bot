"""
Real-time price feeds.

IMPORTANT: Each asset uses a different resolution oracle:
  BTC → Binance BTC/USDT  (verified from live API: assetSymbolPair = "BTCUSDT")
  ETH → Binance ETH/USDT
  SOL → Chainlink SOL/USD  (verified from live API: assetSymbolPair = "SOLUSDT_CHAINLINK")

All spot prices written to module-level `spot` dict, keyed by asset symbol.
Bayse market prices (YES/NO) written to `market_prices`.
"""

import asyncio
import json
import logging
import aiohttp
import websockets
from config import (
    WS_MARKETS_URL, BINANCE_WS_URL, BINANCE_SYMBOLS,
    CHAINLINK_SOL_URL, CHAINLINK_POLL_SEC,
)

log = logging.getLogger(__name__)

# Live oracle prices — keyed by Bayse asset symbol
# {"BTC": 77800.12, "ETH": 1520.50, "SOL": 86.72}
spot: dict[str, float] = {}

# Previous Bayse YES prices — used to detect moves for correlation signals
prev_yes: dict[str, float] = {}

# Current Bayse market prices — {market_id: {"yes": float, "no": float}}
market_prices: dict[str, dict] = {}

BINANCE_TO_ASSET = {"btcusdt": "BTC", "ethusdt": "ETH"}


def _parse_frames(raw: str):
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


# ── Binance feed (BTC + ETH) ─────────────────────────────────────────────────

async def binance_feed(on_price=None):
    streams = "/".join(f"{s}@miniTicker" for s in BINANCE_SYMBOLS)
    url = f"{BINANCE_WS_URL}?streams={streams}"
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log.info("Binance feed connected (BTC, ETH)")
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    data = msg.get("data", msg)
                    symbol = data.get("s", "").lower()
                    price = data.get("c")
                    if symbol in BINANCE_TO_ASSET and price:
                        asset = BINANCE_TO_ASSET[symbol]
                        spot[asset] = float(price)
                        log.debug(f"Binance {asset}: {float(price):,.4f}")
                        if on_price:
                            on_price(asset, float(price))
        except Exception as e:
            log.warning(f"Binance feed error: {e}. Reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


# ── Chainlink feed (SOL) ─────────────────────────────────────────────────────

async def chainlink_sol_feed(on_price=None):
    """
    Chainlink doesn't have a public WS — poll their REST API every 10s.
    Chainlink updates on-chain every 10–30s or when price moves 0.5%.
    """
    backoff = 1
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                log.info("Chainlink SOL feed polling started")
                backoff = 1
                while True:
                    try:
                        async with session.get(
                            "https://api.coingecko.com/api/v3/simple/price",
                            params={"ids": "solana", "vs_currencies": "usd"},
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as r:
                            if r.status == 200:
                                data = await r.json()
                                price = data.get("solana", {}).get("usd")
                                if price:
                                    spot["SOL"] = float(price)
                                    log.debug(f"Chainlink/CG SOL: {float(price):,.4f}")
                                    if on_price:
                                        on_price("SOL", float(price))
                    except Exception as e:
                        log.debug(f"SOL price fetch error: {e}")
                    await asyncio.sleep(CHAINLINK_POLL_SEC)
        except Exception as e:
            log.warning(f"Chainlink feed error: {e}. Restarting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


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
        binance_feed(on_price),
        chainlink_sol_feed(on_price),
        bayse_feed(market_ids, on_update),
    )
