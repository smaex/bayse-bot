import os
import json
import time
import asyncio
import logging
import websockets
from typing import Tuple
import config
import aiohttp

log = logging.getLogger("feeds_direct")

# Ground Truth Storage: { Asset: {"price": float, "time": float} }
direct_spot: dict[str, dict] = {}
macro_bias: dict[str, dict] = {} # { "USD_SPIKE": {"active": bool, "strength": float, "expires": float} }
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

    # Asset-specific stale threshold: Crypto is fast, FX is slow.
    is_fx = asset in _FX_SYMBOLS.values()
    max_lag = config.INFRA_STALE_LAG_SEC if is_fx else 90.0 # Strict 90s for Crypto
    
    if price_diff_pct > config.INFRA_STALE_DIFF_PCT or time_diff > max_lag:
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

def get_macro_bias() -> dict:
    """
    Aggregation layer for macro signals.
    """
    now = time.time()
    active = {}
    for k, v in macro_bias.items():
        if v["active"] and v["expires"] > now:
            active[k] = v
    return active

def get_latency_bias(asset: str, bayse_price: float) -> float:
    """
    Returns the synthetic bias (oracle vs bayse).
    Positive = Oracle > Bayse (Bullish pressure/Lag)
    Negative = Oracle < Bayse (Bearish pressure/Lag)
    """
    direct_p, direct_t = get_direct_price(asset)
    if not direct_p or (time.time() - direct_t > 30):
        return 0.0
    
    return (direct_p - bayse_price) / bayse_price

# ── Oracle 1: Binance (Crypto) ────────────────────────────────────────────────

async def binance_feed():
    """High-speed WebSocket feed for Crypto Ground Truth."""
    endpoints = [
        {"url": "wss://stream.binance.com:9443", "suffix": "usdt"},
        {"url": "wss://stream.binance.us:9443", "suffix": "usd"}
    ]
    current_idx = 0
    backoff = 1
    
    while True:
        endpoint = endpoints[current_idx]
        suffix = endpoint["suffix"]
        stream_list = []
        for s in _CRYPTO_SYMBOLS.keys():
            stream_list.append(f"{s.lower()}@ticker")
            if suffix == "usd":
                stream_list.append(f"{s.lower().replace('usdt', 'usd')}@ticker")
            
        full_url = f"{endpoint['url']}/stream?streams={'/'.join(stream_list)}"
        
        try:
            log.info(f"Connecting to Binance Direct feed ({endpoint['url']})...")
            async with websockets.connect(full_url, ping_interval=10, ping_timeout=10) as ws:
                log.info(f"✅ Direct Binance feed connected ({endpoint['url']})")
                backoff = 1
                msg_count = 0
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=20)
                    except asyncio.TimeoutError:
                        log.warning(f"⚠️ Direct feed stall detected ({endpoint['url']}). Reconnecting...")
                        break

                    msg_count += 1
                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    raw_symbol = data.get("s", "").upper()
                    
                    lookup_key = raw_symbol
                    if not lookup_key.endswith("USDT"):
                        lookup_key = lookup_key.replace("USD", "USDT")
                    if not lookup_key.endswith("T"):
                        lookup_key += "T"

                    asset = _CRYPTO_SYMBOLS.get(lookup_key)
                    bid = data.get("b")
                    ask = data.get("a")
                    
                    if asset and bid and ask:
                        mid_price = (float(bid) + float(ask)) / 2
                        bid_sz = float(data.get("B", 0))
                        ask_sz = float(data.get("A", 0))
                        
                        direct_spot[asset] = {
                            "price": mid_price, 
                            "time": time.time(),
                            "bid_sz": bid_sz,
                            "ask_sz": ask_sz
                        }
                        
                        if msg_count % 500 == 0:
                            log.debug(f"Oracle update [{asset}]: {mid_price:,.2f} | B:{bid_sz:.1f} A:{ask_sz:.1f}")
                            
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
    connection_errors = 0
    last_error_time = 0
    
    while True:
        try:
            # Circuit Breaker: If we've had > 5 errors in the last 10 mins, 
            # wait longer before trying WebSocket again.
            now = time.time()
            if connection_errors > 5 and (now - last_error_time < 600):
                log.warning("Tiingo FX WS Circuit Breaker active. Relying on REST fallback for 10 mins.")
                await asyncio.sleep(60)
                continue

            # Jitter to prevent connection storms between ghost instances
            import random
            await asyncio.sleep(random.uniform(10, 30)) # Increased jitter
            
            async with websockets.connect(url, ping_interval=10, ping_timeout=10) as ws:
                subscribe = {
                    "eventName": "subscribe",
                    "authorization": TIINGO_API_KEY,
                    "eventData": { "tickers": list(_FX_SYMBOLS.keys()), "thresholdLevel": 5 } 
                }
                await ws.send(json.dumps(subscribe))
                log.info("✅ Tiingo FX WebSocket connected.")
                backoff = 1
                connection_errors = 0 # Reset on success
                
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=300)
                    except asyncio.TimeoutError:
                        break
                        
                    msg = json.loads(raw)
                    if msg.get("messageType") == "A":
                        data = msg.get("data", [])
                        if len(data) >= 6:
                            ticker = data[1].lower()
                            mid_price = data[5]
                            asset = _FX_SYMBOLS.get(ticker)
                            if asset:
                                old_data = direct_spot.get(asset)
                                direct_spot[asset] = {"price": float(mid_price), "time": time.time()}
                                
                                # Macro Lead Detection (EURUSD as USD proxy)
                                if old_data and asset == "EURUSD":
                                    move = (float(mid_price) - old_data["price"]) / old_data["price"]
                                    if move < -0.0010:
                                        macro_bias["USD_SPIKE"] = {"active": True, "strength": abs(move), "expires": time.time() + 300}
                                        log.info(f"🚨 MACRO BIAS: USD Spike detected (EURUSD {move:+.3%})")
                                    elif move > 0.0010:
                                        macro_bias["USD_CRASH"] = {"active": True, "strength": abs(move), "expires": time.time() + 300}
                                        log.info(f"🚀 MACRO BIAS: USD Crash detected (EURUSD {move:+.3%})")
                                        
                                # Gold Lead
                                if old_data and asset == "XAUUSD":
                                    move = (float(mid_price) - old_data["price"]) / old_data["price"]
                                    if move > 0.0020:
                                        macro_bias["GOLD_BREAKOUT"] = {"active": True, "strength": abs(move), "expires": time.time() + 600}
                                        log.info(f"✨ MACRO BIAS: Gold Breakout detected ({move:+.2%})")
                                
        except websockets.exceptions.ConnectionClosed as e:
            connection_errors += 1
            last_error_time = time.time()
            if e.code == 1005:
                log.debug(f"Tiingo FX WS closed (1005, count={connection_errors}). Reconnecting quietly...")
            else:
                log.warning(f"Tiingo FX WS closed ({e.code}, count={connection_errors}): {e.reason}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            connection_errors += 1
            last_error_time = time.time()
            log.error(f"Tiingo FX error (count={connection_errors}): {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def tiingo_fx_rest_fallback():
    """
    Fallback loop that polls Tiingo REST API every 15s.
    Only updates direct_spot if the WebSocket has stalled (>30s).
    """
    if not TIINGO_API_KEY:
        return
        
    log.info("Starting Tiingo FX REST Fallback loop...")
    tickers = ",".join(_FX_SYMBOLS.keys())
    url = f"https://api.tiingo.com/tiingo/fx/top?tickers={tickers}&token={TIINGO_API_KEY}"
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Check if WS is stalled
                stalled = False
                for asset in _FX_SYMBOLS.values():
                    _, last_t = get_direct_price(asset)
                    if time.time() - last_t > 30:
                        stalled = True
                        break
                
                if stalled:
                    async with session.get(url, timeout=10) as r:
                        if r.status == 200:
                            data = await r.json()
                            for item in data:
                                ticker = item.get("ticker", "").lower()
                                asset = _FX_SYMBOLS.get(ticker)
                                if asset:
                                    # Tiingo Top API returns bid/ask
                                    mid = (float(item["bid"]) + float(item["ask"])) / 2
                                    _, last_t = get_direct_price(asset)
                                    if time.time() - last_t > 15:
                                        direct_spot[asset] = {"price": mid, "time": time.time()}
                                        log.debug(f"REST Fallback update [{asset}]: {mid:,.4f}")
            except Exception as e:
                log.error(f"Tiingo REST fallback error: {e}")
                
            await asyncio.sleep(15)
