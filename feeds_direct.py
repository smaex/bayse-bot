"""
Direct oracle feed — Binance WebSocket for BTC/ETH/SOL.

Purpose: secondary ground-truth to detect lag between the Bayse relay
and the actual Binance price.  Used by FRONTRUN and the infra guard.

Removed vs previous version:
  - Tiingo FX feed (EURUSD/GBPUSD are now on the Bayse realtime WS directly)
  - Hardened WS pool (overkill for a single feed)
  - Macro bias signals (too noisy, hurt SNIPE certainty)
"""

import asyncio
import json
import logging
import time
from typing import Tuple

import aiohttp
import websockets

log = logging.getLogger("feeds_direct")

# Ground-truth prices: { "BTC": {"price": float, "time": float} }
direct_spot: dict[str, dict] = {}

_startup_time = time.time()

_CRYPTO_SYMBOLS = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
}

# Binance endpoints (US fallback if .com is geo-blocked)
_BINANCE_WS_URLS = [
    "wss://stream.binance.com:9443",
    "wss://stream.binance.us:9443",
]


def get_direct_price(asset: str) -> Tuple[float, float]:
    data = direct_spot.get(asset)
    if data:
        return data["price"], data["time"]
    return 0.0, 0.0


def get_latency_bias(asset: str, bayse_price: float) -> float:
    """
    Returns (oracle - bayse) / bayse.
    Positive = oracle ahead (bullish pressure).
    Negative = oracle below (bearish pressure).
    """
    p, t = get_direct_price(asset)
    if not p or (time.time() - t > 30):
        return 0.0
    return (p - bayse_price) / bayse_price


def check_lag(asset: str, relay_price: float) -> dict:
    """
    Compare relay price to oracle.
    Returns status: 'ok' | 'degraded' | 'stale'
    """
    import config

    # Startup grace: don't block while oracles are warming up
    if (time.time() - _startup_time) < 60:
        return {"status": "ok", "price": relay_price, "reason": "startup_grace"}

    p, t = get_direct_price(asset)
    if not p:
        return {"status": "ok", "price": relay_price, "reason": "no_direct_feed"}

    diff_pct = abs(p - relay_price) / relay_price
    lag_sec  = time.time() - t
    best     = p if lag_sec < 2.0 else relay_price

    if diff_pct > config.INFRA_STALE_DIFF_PCT or lag_sec > config.INFRA_STALE_LAG_SEC:
        return {"status": "stale",    "price": best, "diff_pct": diff_pct, "lag_sec": lag_sec}
    if diff_pct > config.INFRA_DEGRADED_DIFF_PCT or lag_sec > config.INFRA_DEGRADED_LAG_SEC:
        return {"status": "degraded", "price": best, "diff_pct": diff_pct, "lag_sec": lag_sec}
    return {"status": "ok",           "price": best, "diff_pct": diff_pct, "lag_sec": lag_sec}


# ── Binance WebSocket ─────────────────────────────────────────────────────────

async def binance_feed():
    streams = "/".join(f"{s.lower()}@bookTicker" for s in _CRYPTO_SYMBOLS)
    url_idx = 0
    backoff = 1

    while True:
        base = _BINANCE_WS_URLS[url_idx]
        url  = f"{base}/stream?streams={streams}"
        try:
            log.info(f"Binance oracle connecting ({base})…")
            async with websockets.connect(url, ping_interval=20) as ws:
                log.info("Binance oracle connected")
                backoff = 1
                async for raw in ws:
                    msg  = json.loads(raw)
                    data = msg.get("data", msg)
                    sym  = data.get("s", "").upper()
                    bid  = data.get("b")
                    ask  = data.get("a")
                    asset = _CRYPTO_SYMBOLS.get(sym)
                    if asset and bid and ask:
                        mid = (float(bid) + float(ask)) / 2
                        direct_spot[asset] = {"price": mid, "time": time.time()}
        except Exception as e:
            if "451" in str(e) and url_idx == 0:
                log.warning("Binance.com geo-blocked — switching to Binance.US")
                url_idx = 1
                backoff = 1
                continue
            log.warning(f"Binance oracle error: {e}. Retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── REST fallback (if WS stalls >30s) ────────────────────────────────────────

async def binance_rest_fallback():
    urls = [
        "https://api.binance.com/api/v3/ticker/bookTicker",
        "https://api.binance.us/api/v3/ticker/bookTicker",
    ]
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(15)
            stalled = any(
                (time.time() - direct_spot.get(a, {}).get("time", 0)) > 30
                for a in _CRYPTO_SYMBOLS.values()
            )
            if not stalled:
                continue
            for url in urls:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            for item in await r.json():
                                sym   = item.get("symbol", "").upper()
                                asset = _CRYPTO_SYMBOLS.get(sym)
                                if asset and item.get("bidPrice") and item.get("askPrice"):
                                    mid = (float(item["bidPrice"]) + float(item["askPrice"])) / 2
                                    old_t = direct_spot.get(asset, {}).get("time", 0)
                                    if (time.time() - old_t) > 15:
                                        direct_spot[asset] = {"price": mid, "time": time.time()}
                            break
                except Exception as e:
                    log.debug(f"Binance REST fallback {url}: {e}")
