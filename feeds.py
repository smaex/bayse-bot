"""
Real-time price feeds.

All spot prices come from the Bayse realtime WebSocket:
  wss://socket.bayse.markets/ws/v1/realtime

Bayse proxies Binance (crypto) and TwelveData (FX/Gold) — no geo-block,
no extra API keys, prices update ~every second per symbol.

This replaces the previous Kraken REST polling (10-second delay).
Bayse market YES/NO prices come from a separate Bayse markets WebSocket.
"""

import asyncio
import json
import logging
import websockets
from config import WS_MARKETS_URL, WS_REALTIME_URL

log = logging.getLogger(__name__)

# Live spot prices — {"BTC": 77800.12, "ETH": 1520.50, "EUR": 1.085, ...}
spot: dict[str, float] = {}

# Previous Bayse YES prices — used to detect BTC moves for correlation signals
prev_yes: dict[str, float] = {}

# Current Bayse market prices — {market_id: {"yes": float, "no": float}}
market_prices: dict[str, dict] = {}

# Bayse markets WS task (restarted after each market scan)
_bayse_task: asyncio.Task | None = None
_subscribed_ids: set[str] = set()

# Bayse realtime symbols → internal asset name (matches SERIES keys in config.py)
_REALTIME_SYMBOLS = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "BNBUSDT": "BNB",
    "EURUSD":  "EURUSD",
    "GBPUSD":  "GBPUSD",
    "USDJPY":  "USDJPY",
    "EURJPY":  "EURJPY",
    "GBPJPY":  "GBPJPY",
    "XAUUSD":  "XAUUSD",
    # USDNGN subscribed for future markets but no active series yet
    "USDNGN":  "USDNGN",
}


def _parse_frames(raw: str):
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


# ── Bayse realtime spot feed ──────────────────────────────────────────────────

async def realtime_feed(on_price=None):
    """
    Stream live asset prices from Bayse realtime WS.
    Covers BTC/ETH/SOL (Binance source) + EUR/GBP/NGN/XAU (TwelveData source).
    Auto-reconnects with exponential backoff.
    """
    symbols = list(_REALTIME_SYMBOLS.keys())
    backoff = 1

    while True:
        try:
            async with websockets.connect(WS_REALTIME_URL, ping_interval=20) as ws:
                log.info(f"Bayse realtime feed connected — {len(symbols)} symbols")
                backoff = 1
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channel": "asset_prices",
                    "symbols": symbols,
                }))
                async for raw in ws:
                    for msg in _parse_frames(raw):
                        if msg.get("type") != "asset_price":
                            continue
                        data   = msg.get("data", {})
                        symbol = data.get("symbol", "")
                        price  = data.get("price")
                        asset  = _REALTIME_SYMBOLS.get(symbol)
                        if asset and price is not None:
                            spot[asset] = float(price)
                            # Derive EURGBP from EURUSD / GBPUSD whenever either updates
                            if asset in ("EURUSD", "GBPUSD"):
                                eur = spot.get("EURUSD")
                                gbp = spot.get("GBPUSD")
                                if eur and gbp:
                                    spot["EURGBP"] = round(eur / gbp, 6)
                            log.debug(f"Spot {asset} ({symbol}): {float(price):,.4f}")
                            if on_price:
                                on_price(asset, float(price))
        except Exception as e:
            log.warning(f"Bayse realtime feed error: {e}. Reconnecting in {backoff}s")
            spot.clear()   # don't trade on stale prices while feed is down
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── Bayse market YES/NO price feed ────────────────────────────────────────────

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

    yes_p = (data.get("yesPrice") or data.get("yes")
             or data.get("outcome1Price") or data.get("upPrice"))
    no_p  = (data.get("noPrice") or data.get("no")
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
    """Call after each market scan to keep WS subscriptions current."""
    global _bayse_task, _subscribed_ids
    new_market_ids = {m["market_id"] for m in markets}
    if new_market_ids == _subscribed_ids and _bayse_task and not _bayse_task.done():
        return
    _subscribed_ids = new_market_ids
    if _bayse_task and not _bayse_task.done():
        _bayse_task.cancel()
    if markets:
        event_ids = list({m["event_id"] for m in markets})
        _bayse_task = asyncio.create_task(bayse_feed(event_ids, on_update))
        log.info(f"Bayse WS (re)started — {len(event_ids)} events / {len(markets)} markets")


async def start_feeds(on_price=None):
    """Start the Bayse realtime spot feed. Bayse WS is managed via restart_bayse_feed()."""
    await realtime_feed(on_price)
