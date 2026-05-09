import asyncio
import json
import logging
import time
import websockets

log = logging.getLogger("feeds_direct")

# Shared state — {"BTC": {"price": float, "time": float}, ...}
direct_spot: dict[str, dict] = {}

# Mapping Binance symbols to our internal keys
_SYMBOLS = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "BNBUSDT": "BNB"
}

async def binance_feed():
    """
    Direct high-speed WebSocket feed from Binance.
    Provides the 'Ground Truth' for crypto prices to detect relay lag.
    Automatically switches to Binance.US if Binance.com is geoblocked.
    """
    # Try .com first, then .us if geoblocked (HTTP 451)
    endpoints = [
        {"url": "wss://stream.binance.com:9443", "suffix": "usdt"},
        {"url": "wss://stream.binance.us:9443",  "suffix": "usd"}
    ]
    
    current_idx = 0
    backoff = 1
    
    while True:
        endpoint = endpoints[current_idx]
        streams = "/".join([f"{s.lower().replace('usdt', endpoint['suffix'])}@ticker" for s in _SYMBOLS.keys()])
        full_url = f"{endpoint['url']}/stream?streams={streams}"
        
        try:
            async with websockets.connect(full_url, ping_interval=20) as ws:
                log.info(f"Direct Binance feed connected ({endpoint['url']})")
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    symbol = data.get("s", "").replace("USD", "USDT") # normalize back to USDT
                    if symbol.endswith("T"): # e.g. BTCUSDT
                         pass
                    else:
                         symbol = symbol + "T" # normalize BTCUSD -> BTCUSDT

                    price = data.get("c")
                    event_time = data.get("E")
                    
                    asset = _SYMBOLS.get(symbol)
                    if asset and price:
                        direct_spot[asset] = {
                            "price": float(price),
                            "time": event_time / 1000.0 if event_time else time.time()
                        }
        except Exception as e:
            if "451" in str(e) and current_idx == 0:
                log.warning("Binance.com is geoblocked (451). Switching to Binance.US oracle...")
                current_idx = 1
                backoff = 1
                continue
                
            log.warning(f"Direct Binance feed error ({endpoint['url']}): {e}. Reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

def get_direct_price(asset: str) -> tuple[float, float]:
    """Returns (price, timestamp) for an asset from the direct feed."""
    entry = direct_spot.get(asset)
    if not entry:
        return 0.0, 0.0
    return entry["price"], entry["time"]

def check_lag(asset: str, relay_price: float) -> dict:
    """
    Trench-Hardened Lag Logic:
    1. Returns 'ok' for < 5s lag.
    2. Returns 'degraded' for 5-15s lag (suggests using a safety spread).
    3. Returns 'stale' for > 15s lag or > 0.15% price diff.
    """
    direct_p, direct_t = get_direct_price(asset)
    if not direct_p:
        # Fallback if Binance feed is down — trust relay but log it
        return {"status": "ok", "price": relay_price, "reason": "no_direct_feed"}
        
    price_diff_pct = abs(direct_p - relay_price) / relay_price
    time_diff = time.time() - direct_t
    
    # ── Tiered Logic ──────────────────────────────────────────────────────────
    # If Binance has a newer price, we always prefer it as the 'Ground Truth'
    best_price = direct_p if direct_t > (time.time() - 2.0) else relay_price

    if price_diff_pct > 0.0015 or time_diff > 15.0:
        return {
            "status": "stale",
            "price": best_price,
            "diff_pct": price_diff_pct,
            "lag_sec": time_diff
        }
    
    if time_diff > 5.0 or price_diff_pct > 0.0005:
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
