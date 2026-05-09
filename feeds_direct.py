import os
import json
import time
import asyncio
import logging
import websockets
from typing import Tuple
import config

log = logging.getLogger("feeds_direct")

# Ground Truth Storage: { Asset: {"price": float, "time": float} }
direct_spot: dict[str, dict] = {}
startup_time: float = time.time()

def is_warming_up(grace_period=60) -> bool:
    """Returns True if the bot started less than grace_period seconds ago."""
    return (time.time() - startup_time) < grace_period

# ── Crypto Config ─────────────────────────────────────────────────────────────
_CRYPTO_SYMBOLS = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "BNBUSDT": "BNB",
    "SOLUSDT": "SOL",
    "ADAUSDT": "ADA",
}

# ── FX Config (Tiingo) ────────────────────────────────────────────────────────
_FX_SYMBOLS = {
    "eurusd": "EURUSD",
    "gbpusd": "GBPUSD",
    "usdjpy": "USDJPY",
    "eurjpy": "EURJPY",
    "gbpjpy": "GBPJPY",
    "eurgbp": "EURGBP",
    "xauusd": "XAUUSD", # Gold
}

TIINGO_API_KEY = os.getenv("TIINGO_API_KEY", "")

def get_direct_price(asset: str) -> Tuple[float, float]:
    """Returns (price, local_arrival_time) from the direct oracles."""
    data = direct_spot.get(asset)
    if data:
        return data["price"], data["time"]
    return 0.0, 0.0

def check_lag(asset: str, relay_price: float) -> dict:
    """
    Trench-Hardened Lag Logic:
    1. Returns 'ok' for fresh data with tight price alignment.
    2. Returns 'degraded' for moderate lag/diff (suggests using a safety spread).
    3. Returns 'stale' for excessive lag/diff (blocks entry).
    """
    if is_warming_up(60):
        # During first 60s, don't block entry while oracles are connecting
        return {"status": "ok", "price": relay_price, "reason": "startup_grace"}

    direct_p, direct_t = get_direct_price(asset)
    if not direct_p:
        # Fallback if oracle is down or not tracking this asset
        return {"status": "ok", "price": relay_price, "reason": "no_direct_feed"}
        
    price_diff_pct = abs(direct_p - relay_price) / relay_price
    time_diff = time.time() - direct_t
    
    # Use direct price as ground truth if it's fresh (last 2s)
    best_price = direct_p if time_diff < 2.0 else relay_price

    if price_diff_pct > config.INFRA_STALE_DIFF_PCT or time_diff > config.INFRA_STALE_LAG_SEC:
        return {
            "status": "stale",
            "price": best_price,
            "diff_pct": price_diff_pct,
            "lag_sec": time_diff
        }
    
    if time_diff > config.INFRA_DEGRADED_LAG_SEC or price_diff_pct > config.INFRA_DEGRADED_DIFF_PCT:
        return {
            "status": "degraded",
            "price": best_price,
            "diff_pct": price_diff_pct,
            "lag_sec": time_diff
        }
        
    return {
        "status": "ok", 
        "price": best_price, 
        "diff_pct": price_diff_pct, 
        "lag_sec": time_diff
    }

# ── Oracle 1: Binance (Crypto) ────────────────────────────────────────────────

async def binance_feed():
    """High-speed WebSocket feed for Crypto Ground Truth."""
    endpoints = [
        {"url": "wss://stream.binance.com:9443", "suffix": "usdt"},
        {"url": "wss://stream.binance.us:9443",  "suffix": "usd"}
    ]
    current_idx = 0
    backoff = 1
    
    while True:
        endpoint = endpoints[current_idx]
        suffix = endpoint["suffix"]
        stream_list = []
        for s in _CRYPTO_SYMBOLS.keys():
            # bookTicker provides the fastest possible heartbeat (every bid/ask change)
            stream_list.append(f"{s.lower()}@bookTicker")
            if suffix == "usd":
                # For Binance.US, also try the fiat USD pair: btcusd@bookTicker
                stream_list.append(f"{s.lower().replace('usdt', 'usd')}@bookTicker")
            
        full_url = f"{endpoint['url']}/stream?streams={'/'.join(stream_list)}"
        
        try:
            log.info(f"Connecting to Binance Direct feed ({endpoint['url']})...")
            async with websockets.connect(full_url, ping_interval=10, ping_timeout=10) as ws:
                log.info(f"✅ Direct Binance feed connected ({endpoint['url']})")
                backoff = 1
                msg_count = 0
                while True:
                    try:
                        # Shorten timeout from 60s to 20s to detect zombie connections faster
                        raw = await asyncio.wait_for(ws.recv(), timeout=20)
                    except asyncio.TimeoutError:
                        log.warning(f"⚠️ Direct feed stall detected ({endpoint['url']}). Reconnecting...")
                        break

                    msg_count += 1
                    if msg_count % 500 == 0:
                        log.debug(f"Direct Binance feed heartbeat ({endpoint['url']})")

                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    raw_symbol = data.get("s", "").upper()
                    
                    # Normalize back to USDT key
                    lookup_key = raw_symbol
                    if not lookup_key.endswith("USDT"):
                        lookup_key = lookup_key.replace("USD", "USDT")
                    if not lookup_key.endswith("T"):
                        lookup_key += "T"

                    asset = _CRYPTO_SYMBOLS.get(lookup_key)
                    # bookTicker uses 'b' for bid and 'a' for ask
                    bid = data.get("b")
                    ask = data.get("a")
                    
                    if asset and bid and ask:
                        # Use mid-price for the most accurate spot representation
                        mid_price = (float(bid) + float(ask)) / 2
                        direct_spot[asset] = {"price": mid_price, "time": time.time()}
        except Exception as e:
            if "451" in str(e) and current_idx == 0:
                log.warning("Geoblocked by Binance.com. Switching to Binance.US oracle...")
                current_idx = 1
                backoff = 1
                continue
            else:
                log.error(f"Binance feed error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

# ── Oracle 2: Tiingo (FX & Gold) ──────────────────────────────────────────────

async def tiingo_fx_feed():
    """High-speed WebSocket feed for Forex and Gold Ground Truth."""
    if not TIINGO_API_KEY:
        log.warning("❌ No TIINGO_API_KEY found. FX Infra Guard is DISABLED.")
        return

    url = "wss://api.tiingo.com/fx"
    backoff = 1
    
    while True:
        try:
            async with websockets.connect(url) as ws:
                # Tiingo Auth
                subscribe = {
                    "eventName": "subscribe",
                    "authorization": TIINGO_API_KEY,
                    "eventData": { "thresholdLevel": 2 } # Filter noise but stay responsive (2 pips)
                }
                await ws.send(json.dumps(subscribe))
                log.info("✅ Tiingo FX Oracle connected")
                backoff = 1
                
                while True:
                    try:
                        # Increase timeout from 40s to 300s. FX can be quiet.
                        raw = await asyncio.wait_for(ws.recv(), timeout=300)
                    except asyncio.TimeoutError:
                        log.debug("Tiingo FX heartbeat timeout (300s). Reconnecting to ensure fresh session...")
                        break
                        
                    msg = json.loads(raw)
                    if msg.get("messageType") == "A":
                        data = msg.get("data", [])
                        # Tiingo format: [ 'A', ticker, date, bid_size, bid, mid, ask, ask_size ]
                        if len(data) >= 6:
                            ticker = data[1].lower()
                            mid_price = data[5]
                            asset = _FX_SYMBOLS.get(ticker)
                            if asset:
                                direct_spot[asset] = {"price": float(mid_price), "time": time.time()}
                                
        except Exception as e:
            log.error(f"Tiingo FX error: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
