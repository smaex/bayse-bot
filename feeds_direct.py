import os
import json
import time
import asyncio
import logging
import websockets
from typing import Tuple
import config
import aiohttp
import feeds_hardened

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

# ── Dedup Keys and Msg Handlers for Hardened WS ───────────────────────────────

def binance_dedup_key(msg):
    data = msg.get("data", msg)
    symbol = data.get("s")
    event_time = data.get("E")
    if symbol and event_time:
        return (symbol.upper(), event_time)
    return None

def binance_msg_handler(msg):
    data = msg.get("data", msg)
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
        
        # Layer 3 delta limit validation (if we have old data and it is fresh)
        old_data = direct_spot.get(asset)
        if old_data and (time.time() - old_data["time"] <= 5.0):
            if not feeds_hardened.check_delta_limit(asset, mid_price, old_data["price"]):
                return
                
        direct_spot[asset] = {
            "price": mid_price, 
            "time": time.time(),
            "bid_sz": bid_sz,
            "ask_sz": ask_sz
        }

def tiingo_dedup_key(msg):
    if msg.get("messageType") == "A":
        data = msg.get("data", [])
        if len(data) >= 6:
            ticker = data[1]
            timestamp = data[2]
            return (ticker.lower(), timestamp)
    return None

def tiingo_msg_handler(msg):
    if msg.get("messageType") == "A":
        data = msg.get("data", [])
        if len(data) >= 6:
            ticker = data[1].lower()
            mid_price = data[5]
            asset = _FX_SYMBOLS.get(ticker)
            if asset:
                old_data = direct_spot.get(asset)
                new_price = float(mid_price)
                
                # Layer 3 delta limit validation (if we have old data and it is fresh)
                if old_data and (time.time() - old_data["time"] <= 10.0):
                    if not feeds_hardened.check_delta_limit(asset, new_price, old_data["price"]):
                        return
                        
                direct_spot[asset] = {"price": new_price, "time": time.time()}
                
                # Macro Lead Detection (EURUSD as USD proxy)
                if old_data and asset == "EURUSD":
                    move = (new_price - old_data["price"]) / old_data["price"]
                    if move < -0.0010:
                        macro_bias["USD_SPIKE"] = {"active": True, "strength": abs(move), "expires": time.time() + 300}
                        log.info(f"🚨 MACRO BIAS: USD Spike detected (EURUSD {move:+.3%})")
                    elif move > 0.0010:
                        macro_bias["USD_CRASH"] = {"active": True, "strength": abs(move), "expires": time.time() + 300}
                        log.info(f"🚀 MACRO BIAS: USD Crash detected (EURUSD {move:+.3%})")
                        
                # Gold Lead
                if old_data and asset == "XAUUSD":
                    move = (new_price - old_data["price"]) / old_data["price"]
                    if move > 0.0020:
                        macro_bias["GOLD_BREAKOUT"] = {"active": True, "strength": abs(move), "expires": time.time() + 600}
                        log.info(f"✨ MACRO BIAS: Gold Breakout detected ({move:+.2%})")

# ── Oracle 1: Binance (Crypto) ────────────────────────────────────────────────

async def binance_feed():
    """High-speed WebSocket feed for Crypto Ground Truth."""
    endpoints = [
        {"url": "wss://stream.binance.com:9443", "suffix": "usdt"},
        {"url": "wss://stream.binance.us:9443", "suffix": "usd"}
    ]
    
    if not config.USE_HARDENED_WS:
        # Legacy loop
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
                        binance_msg_handler(msg)
                        if msg_count % 500 == 0:
                            log.debug(f"Oracle update Binance tick received (count={msg_count})")
                                
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
    else:
        # Hardened WS Pool
        urls = []
        for endpoint in endpoints:
            suffix = endpoint["suffix"]
            stream_list = []
            for s in _CRYPTO_SYMBOLS.keys():
                stream_list.append(f"{s.lower()}@ticker")
                if suffix == "usd":
                    stream_list.append(f"{s.lower().replace('usdt', 'usd')}@ticker")
            full_url = f"{endpoint['url']}/stream?streams={'/'.join(stream_list)}"
            urls.append(full_url)
            
        pool = feeds_hardened.HardenedWebsocketPool(
            url=urls,
            pool_size=config.WS_POOL_SIZE,
            sub_payload=None,
            message_handler=binance_msg_handler,
            dedup_key_fn=binance_dedup_key
        )
        log.info("Starting HardenedWS Pool for Binance direct feed...")
        await pool.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await pool.stop()
            raise

# ── Oracle 2: Tiingo (FX & Gold) ──────────────────────────────────────────────

async def tiingo_fx_feed():
    """High-speed WebSocket feed for Forex and Gold Ground Truth."""
    if not TIINGO_API_KEY:
        log.warning("❌ No TIINGO_API_KEY found. FX Infra Guard is DISABLED.")
        return

    url = "wss://api.tiingo.com/fx"
    subscribe = {
        "eventName": "subscribe",
        "authorization": TIINGO_API_KEY,
        "eventData": { "tickers": list(_FX_SYMBOLS.keys()), "thresholdLevel": 5 } 
    }

    if not config.USE_HARDENED_WS:
        # Legacy loop
        backoff = 1
        connection_errors = 0
        last_error_time = 0
        while True:
            try:
                now = time.time()
                if connection_errors > 5 and (now - last_error_time < 600):
                    log.warning("Tiingo FX WS Circuit Breaker active. Relying on REST fallback for 10 mins.")
                    await asyncio.sleep(60)
                    continue

                import random
                await asyncio.sleep(random.uniform(10, 30))
                
                async with websockets.connect(url, ping_interval=10, ping_timeout=10) as ws:
                    await ws.send(json.dumps(subscribe))
                    log.info("✅ Tiingo FX WebSocket connected.")
                    backoff = 1
                    connection_errors = 0
                    
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=300)
                        except asyncio.TimeoutError:
                            break
                            
                        msg = json.loads(raw)
                        tiingo_msg_handler(msg)
                                    
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
    else:
        # Hardened WS Pool
        pool = feeds_hardened.HardenedWebsocketPool(
            url=url,
            pool_size=1,  # Hardcoded to 1 because Tiingo strictly allows only 1 concurrent WS connection per key
            sub_payload=subscribe,
            message_handler=tiingo_msg_handler,
            dedup_key_fn=tiingo_dedup_key
        )
        log.info("Starting HardenedWS Pool for Tiingo FX feed...")
        await pool.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await pool.stop()
            raise

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
                                    # Tiingo /tiingo/fx/top REST API returns bidPrice/askPrice
                                    # (NOT bid/ask — that was the WS format)
                                    bid_p = item.get("bidPrice") or item.get("bid")
                                    ask_p = item.get("askPrice") or item.get("ask")
                                    if not bid_p or not ask_p:
                                        # mid price fallback if bid/ask absent
                                        mid = item.get("midPrice") or item.get("mid")
                                        if not mid:
                                            continue
                                        mid = float(mid)
                                    else:
                                        mid = (float(bid_p) + float(ask_p)) / 2
                                    _, last_t = get_direct_price(asset)
                                    if time.time() - last_t > 15:
                                        direct_spot[asset] = {"price": mid, "time": time.time()}
                                        log.debug(f"REST Fallback update [{asset}]: {mid:,.4f}")
            except Exception as e:
                log.error(f"Tiingo REST fallback error: {e}")
                
            await asyncio.sleep(15)


async def binance_rest_fallback():
    """
    Fallback loop that polls Binance REST API every 15s if WS has stalled (>30s).
    Alternates between Binance.com and Binance.US to bypass geoblocks.
    """
    log.info("Starting Binance REST Fallback loop...")
    urls = [
        "https://api.binance.com/api/v3/ticker/bookTicker",
        "https://api.binance.us/api/v3/ticker/bookTicker"
    ]
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Check if WS is stalled
                stalled = False
                for asset in _CRYPTO_SYMBOLS.values():
                    _, last_t = get_direct_price(asset)
                    if time.time() - last_t > 30:
                        stalled = True
                        break
                
                if stalled:
                    success = False
                    for url in urls:
                        try:
                            async with session.get(url, timeout=10) as r:
                                if r.status == 200:
                                    data = await r.json()
                                    # bookTicker returns a list of symbols
                                    for item in data:
                                        sym = item.get("symbol", "").upper()
                                        asset = _CRYPTO_SYMBOLS.get(sym)
                                        if not asset:
                                            # handle US pairs e.g. BTCUSD -> BTCUSDT lookup
                                            lookup_key = sym
                                            if not lookup_key.endswith("USDT"):
                                                lookup_key = lookup_key.replace("USD", "USDT")
                                            if not lookup_key.endswith("T"):
                                                lookup_key += "T"
                                            asset = _CRYPTO_SYMBOLS.get(lookup_key)
                                            
                                        if asset and item.get("bidPrice") and item.get("askPrice"):
                                            bid = float(item["bidPrice"])
                                            ask = float(item["askPrice"])
                                            mid = (bid + ask) / 2
                                            bid_sz = float(item.get("bidQty", 0))
                                            ask_sz = float(item.get("askQty", 0))
                                            
                                            # Only update if the REST price is newer than the last WS tick
                                            _, last_t = get_direct_price(asset)
                                            if time.time() - last_t > 15:
                                                direct_spot[asset] = {
                                                    "price": mid, 
                                                    "time": time.time(),
                                                    "bid_sz": bid_sz,
                                                    "ask_sz": ask_sz
                                                }
                                                log.debug(f"Binance REST Fallback update [{asset}]: {mid:,.2f}")
                                    success = True
                                    break # success, don't try other url
                        except Exception as e:
                            log.debug(f"Binance REST fallback failed on {url}: {e}")
                            
            except Exception as e:
                log.error(f"Binance REST fallback general error: {e}")
                
            await asyncio.sleep(15)

