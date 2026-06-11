"""
Real-time price feeds via Bayse WebSockets.

  Realtime WS  → spot prices (BTC/ETH/SOL from Binance, EURUSD/GBPUSD/XAUUSD from TwelveData)
  Markets WS   → live YES/NO prices per market event

Only subscribe to symbols confirmed on the Bayse realtime WS feed.
"""

import asyncio
import json
import logging
import time
import websockets
from config import WS_MARKETS_URL, WS_REALTIME_URL

log = logging.getLogger(__name__)

# Live spot prices
spot: dict[str, float] = {}

# Bayse market YES/NO prices — {market_id: {"yes": float, "no": float}}
market_prices: dict[str, dict] = {}

# Previous YES prices — used by CORRELATE to detect BTC moves
prev_yes: dict[str, float] = {}

_bayse_task: asyncio.Task | None = None
_subscribed_ids: set[str] = set()

# Confirmed symbols on the Bayse realtime WS.
# DO NOT add BNBUSDT, ADAUSDT, USDJPY, EURJPY, etc. — they are not provided.
_REALTIME_SYMBOLS = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "XAUUSD":  "XAUUSD",
    "EURUSD":  "EURUSD",
    "GBPUSD":  "GBPUSD",
    "USDNGN":  "USDNGN",
}

_SUBSCRIBE_SYMBOLS = list(_REALTIME_SYMBOLS.keys())


def _parse_frames(raw: str):
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


# ── Realtime spot feed ────────────────────────────────────────────────────────

async def realtime_feed(on_price=None):
    """Stream live asset prices from Bayse realtime WS."""
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_REALTIME_URL, ping_interval=20) as ws:
                log.info(f"Bayse realtime feed connected ({len(_SUBSCRIBE_SYMBOLS)} symbols)")
                backoff = 1
                await ws.send(json.dumps({
                    "type":    "subscribe",
                    "channel": "asset_prices",
                    "symbols": _SUBSCRIBE_SYMBOLS,
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
                            log.debug(f"Spot {asset}: {float(price):,.4f}")
                            if on_price:
                                on_price(asset, float(price))
        except Exception as e:
            log.warning(f"Realtime feed error: {e}. Reconnect in {backoff}s")
            spot.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── Market YES/NO price feed ──────────────────────────────────────────────────

async def bayse_feed(event_ids: list[str], on_update=None):
    if not event_ids:
        return
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_MARKETS_URL, ping_interval=20) as ws:
                log.info(f"Bayse markets feed connected ({len(event_ids)} events)")
                backoff = 1
                for eid in event_ids:
                    await ws.send(json.dumps({
                        "type": "subscribe", "channel": "prices", "eventId": eid
                    }))
                async for raw in ws:
                    for msg in _parse_frames(raw):
                        _handle_market(msg, on_update)
        except Exception as e:
            log.warning(f"Markets feed error: {e}. Reconnect in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _handle_market(msg: dict, on_update=None):
    """
    Parse a Bayse price_update WS message.

    Correct payload shape (per Bayse WS docs):
    {
      "type": "price_update",
      "data": {
        "id": "evt_123",
        "markets": [
          { "id": "mkt_456", "prices": { "YES": 0.65, "NO": 0.35 } }
        ]
      }
    }
    """
    if msg.get("type") != "price_update":
        return
    data = msg.get("data", {})

    for market in data.get("markets", []):
        mid = market.get("id")
        if not mid:
            continue

        prices  = market.get("prices", {})
        yes_val = prices.get("YES") or prices.get("yes")
        no_val  = prices.get("NO")  or prices.get("no")

        if yes_val is None:
            continue

        yes_val = float(yes_val)
        no_val  = float(no_val) if no_val is not None else round(1.0 - yes_val, 4)

        from strategies.base import global_state

        # Record opening prices (first time we see this market)
        if mid not in global_state.market_opening_prices:
            global_state.market_opening_prices[mid] = {
                "yes": yes_val, "no": no_val, "timestamp": time.time()
            }

        # Track favourite flips (used by SNIPE conviction boost)
        fav = "YES" if yes_val > no_val else ("NO" if no_val > yes_val else None)
        if fav:
            if mid not in global_state.market_last_fav:
                global_state.market_last_fav[mid] = fav
                global_state.market_flips[mid] = 0
            elif global_state.market_last_fav[mid] != fav:
                global_state.market_flips[mid] = global_state.market_flips.get(mid, 0) + 1
                global_state.market_last_fav[mid] = fav

        prev_yes[mid] = market_prices.get(mid, {}).get("yes", yes_val)
        market_prices[mid] = {"yes": yes_val, "no": no_val}

        if on_update:
            on_update(mid, market_prices[mid])


def restart_bayse_feed(markets: list[dict], on_update=None):
    """Restart the markets WS after each scan to stay subscribed to current events."""
    global _bayse_task, _subscribed_ids
    new_ids = {m["market_id"] for m in markets}

    # Clean up stale global state entries
    from strategies.base import global_state
    for mid in list(global_state.market_flips.keys()):
        if mid not in new_ids:
            global_state.market_flips.pop(mid, None)
            global_state.market_last_fav.pop(mid, None)
            global_state.market_opening_prices.pop(mid, None)

    if new_ids == _subscribed_ids and _bayse_task and not _bayse_task.done():
        return
    _subscribed_ids = new_ids
    if _bayse_task and not _bayse_task.done():
        _bayse_task.cancel()
    if markets:
        event_ids = list({m["event_id"] for m in markets})
        _bayse_task = asyncio.create_task(bayse_feed(event_ids, on_update))
        log.info(f"Bayse markets WS restarted — {len(event_ids)} events / {len(markets)} markets")


async def start_feeds(on_price=None):
    await realtime_feed(on_price)
