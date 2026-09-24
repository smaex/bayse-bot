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
import math
from collections import deque
import aiohttp
import websockets

import health

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

# Microstructure and Lead-Lag Trackers
_btc_ticks: deque = deque(maxlen=60)
_ewma_vol: dict[str, float] = {"BTC": 0.025, "ETH": 0.035, "SOL": 0.045}
_last_ewma_price: dict[str, tuple[float, float]] = {}
_book_imbalances: dict[str, float] = {}


def get_btc_velocity(seconds: float = 5.0) -> float:
    """Return BTC % return over the last `seconds` (lead-lag predictor for ETH & SOL)."""
    now = time.time()
    if len(_btc_ticks) < 2:
        return 0.0
    latest_t, latest_p = _btc_ticks[-1]
    if (now - latest_t) > 15.0 or latest_p <= 0:
        return 0.0
    target_t = now - seconds
    for t, p in _btc_ticks:
        if t >= target_t and p > 0:
            return (latest_p - p) / p
    oldest_t, oldest_p = _btc_ticks[0]
    if oldest_p > 0 and (now - oldest_t) >= 1.0:
        return (latest_p - oldest_p) / oldest_p
    return 0.0


def get_imbalance(asset: str) -> float:
    """Return top-of-book depth imbalance: (bid_qty - ask_qty) / (bid_qty + ask_qty)."""
    return _book_imbalances.get(asset, 0.0)


def get_ewma_hourly_vol(asset: str, default: float = 0.025) -> float:
    """Return instantaneous EWMA hourly volatility for dynamic risk gating."""
    return _ewma_vol.get(asset, default)
def get_direct_price(asset: str) -> Tuple[float, float]:
    """Return the independent oracle sample and its real timestamp.

    Never substitute the Bayse relay here. The old fallback stamped relay data
    with ``time.time()``, making a dead Binance feed look permanently fresh to
    every strategy and to the watchdog.
    """
    data = direct_spot.get(asset)
    if not data:
        return 0.0, 0.0
    try:
        return float(data.get("price") or 0.0), float(data.get("time") or 0.0)
    except (TypeError, ValueError):
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
        # FX/commodities intentionally have no Binance oracle.
        if asset not in _CRYPTO_SYMBOLS.values():
            return {"status": "ok", "price": relay_price, "reason": "relay_only_asset"}
        return {
            "status": "stale", "price": relay_price,
            "reason": "independent_oracle_missing", "lag_sec": float("inf"),
        }
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
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20, open_timeout=10, close_timeout=5
            ) as ws:
                log.info("Binance oracle connected")
                backoff = 1
                async for raw in ws:
                    msg  = json.loads(raw)
                    data = msg.get("data", msg)
                    sym  = data.get("s", "").upper()
                    bid  = data.get("b")
                    ask  = data.get("a")
                    bid_qty = data.get("B")
                    ask_qty = data.get("A")
                    asset = _CRYPTO_SYMBOLS.get(sym)
                    if asset and bid and ask:
                        mid = (float(bid) + float(ask)) / 2
                        now = time.time()
                        direct_spot[asset] = {"price": mid, "time": now}

                        # 1. Update Orderbook Imbalance
                        try:
                            b_q = float(bid_qty or 0.0)
                            a_q = float(ask_qty or 0.0)
                            if (b_q + a_q) > 0:
                                _book_imbalances[asset] = (b_q - a_q) / (b_q + a_q)
                        except (TypeError, ValueError):
                            pass

                        # 2. Update BTC tick history for Lead-Lag
                        if asset == "BTC":
                            _btc_ticks.append((now, mid))

                        # 3. Update instantaneous EWMA volatility (RiskMetrics lambda=0.94)
                        last_info = _last_ewma_price.get(asset)
                        if last_info:
                            last_t, last_p = last_info
                            dt = now - last_t
                            if 0.2 <= dt <= 30.0 and last_p > 0 and mid > 0:
                                ret = math.log(mid / last_p)
                                ret_hourly = ret * math.sqrt(3600.0 / dt)
                                prev_vol = _ewma_vol.get(asset, 0.025)
                                new_var = 0.94 * (prev_vol ** 2) + 0.06 * (ret_hourly ** 2)
                                _ewma_vol[asset] = math.sqrt(max(0.0001, min(new_var, 0.25)))
                        _last_ewma_price[asset] = (now, mid)

                        health.touch("direct_feed", asset=asset)
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
                                        health.touch("direct_feed", asset=asset, source="rest")
                            break
                except Exception as e:
                    log.debug(f"Binance REST fallback {url}: {e}")
