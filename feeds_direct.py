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
    Compares the relay price against the direct Binance truth.
    Returns a status dict with lag metrics.
    """
    direct_p, direct_t = get_direct_price(asset)
    if not direct_p:
        return {"status": "ok", "reason": "no_direct_data"}
        
    price_diff_pct = abs(direct_p - relay_price) / relay_price
    time_diff = time.time() - direct_t
    
    # 0.08% is a massive move for a single second in crypto
    # If the relay is off by more than this, it's likely stale.
    is_stale_price = price_diff_pct > 0.0008 
    is_stale_time = time_diff > 3.0
    
    if is_stale_price or is_stale_time:
        return {
            "status": "stale",
            "diff_pct": price_diff_pct,
            "lag_sec": time_diff,
            "direct": direct_p,
            "relay": relay_price
        }
        
    return {"status": "ok", "diff_pct": price_diff_pct, "lag_sec": time_diff}
