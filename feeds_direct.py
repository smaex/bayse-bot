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
    """
    streams = "/".join([f"{s.lower()}@ticker" for s in _SYMBOLS.keys()])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log.info(f"Direct Binance feed connected — {len(_SYMBOLS)} symbols")
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    symbol = data.get("s")
                    price = data.get("c")
                    event_time = data.get("E")
                    
                    asset = _SYMBOLS.get(symbol)
                    if asset and price:
                        direct_spot[asset] = {
                            "price": float(price),
                            "time": event_time / 1000.0 if event_time else time.time()
                        }
        except Exception as e:
            log.warning(f"Direct Binance feed error: {e}. Reconnecting in {backoff}s")
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
