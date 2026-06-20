import math
import logging
import config
from datetime import datetime, timezone

log = logging.getLogger("strategies.utils")


def realized_vol_hourly(asset: str, state) -> float:
    """Blended hourly vol: max(GARCH estimate, config baseline)."""
    base = config.ASSET_HOURLY_VOL.get(asset, 0.022)
    if not hasattr(state, "garch_state"):
        return base
    g = state.garch_state.get(asset)
    if not g:
        return base
    return max(base, math.sqrt(g["var"] * 720.0))


def momentum_score(asset: str, direction: str, state) -> float:
    """±1.0 — Kalman velocity projected 90s forward, normalised to price movement."""
    if not hasattr(state, "kalman_state"):
        return 0.0
    k = state.kalman_state.get(asset)
    if not k:
        return 0.0
    price, velocity = k["x"]
    if price <= 0:
        return 0.0
    proj  = velocity * 90.0 / price
    signed = proj if direction == "YES" else -proj
    return min(max(signed / 0.001, -1.0), 1.0)


def projected_drift_pct(asset: str, secs: float, state) -> float:
    """
    Kalman-filter projected price drift over the next `secs` seconds,
    expressed as a fraction of current price — i.e. the same units as
    distance_pct in win_probability().

    This is a real drift term for a GBM-with-drift probability estimate,
    NOT a normalised [-1,1] heuristic score (that's what momentum_score is
    for, used by CORRELATE). SNIPE uses this raw value to fold momentum
    directly into its diffusion model rather than bolting it on afterward
    as a separate additive bonus — the textbook-correct way to incorporate
    drift into a boundary-crossing probability estimate.

    Returns 0.0 if no Kalman state exists yet for this asset.
    """
    if not hasattr(state, "kalman_state"):
        return 0.0
    k = state.kalman_state.get(asset)
    if not k:
        return 0.0
    price, velocity = k["x"]
    if price <= 0:
        return 0.0
    return (velocity / price) * secs


def velocity_score(asset: str, threshold: float, direction: str, state) -> float:
    """Measures how fast price is heading toward (negative) or away from (positive) threshold."""
    if not hasattr(state, "kalman_state"):
        return 0.0
    k = state.kalman_state.get(asset)
    if not k:
        return 0.0
    price, velocity = k["x"]
    if price <= 0:
        return 0.0
    gap = abs(price - threshold)
    if (direction == "YES" and price < threshold) or (direction == "NO" and price > threshold):
        return -1.0
    move = velocity * config.SNIPE_VELOCITY_WINDOW
    change = move if direction == "YES" else -move
    return change / max(gap, 1e-9)


def regime_score(asset: str, state) -> float:
    """Bayesian HMM proxy: probability that the asset is in a trending regime (0–1)."""
    if not hasattr(state, "price_history"):
        return 0.5
    hist = list(state.price_history.get(asset, []))
    n = min(len(hist), 60)
    if n < 10:
        return 0.5

    prices  = [p for _, p in hist[-n:]]
    returns = [math.log(prices[i] / prices[i-1])
               for i in range(1, len(prices)) if prices[i-1] > 0]
    if not returns:
        return 0.5

    mean = sum(returns) / len(returns)
    var  = sum((r - mean)**2 for r in returns) / len(returns)
    std  = math.sqrt(var) if var > 0 else 1e-9

    p = 0.5
    for r in returns:
        z = abs(r) / std
        lk_trend = min(0.95, max(0.05, z / 2.0))
        lk_chop  = 1.0 - lk_trend
        marg = lk_trend * p + lk_chop * (1.0 - p)
        if marg > 0:
            p = lk_trend * p / marg
    return p


def win_probability(dist_pct: float, secs: float, asset: str,
                    sigma_override: float = None) -> float:
    """Brownian diffusion: P(price stays on correct side until close)."""
    if secs <= 0:
        return 1.0 if dist_pct > 0 else 0.0
    sigma = sigma_override if sigma_override is not None else config.ASSET_HOURLY_VOL.get(asset, 0.022)
    t     = secs / 3600.0

    def norm_cdf(x):
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

    return norm_cdf(dist_pct / (sigma * math.sqrt(t)))


def btc_spot_move_pct(window_sec: float = 300, state=None) -> tuple:
    """Returns (move_pct, direction) of BTC over last window_sec."""
    if not hasattr(state, "price_history"):
        return 0.0, ""
    import time
    hist   = list(state.price_history.get("BTC", []))
    if len(hist) < 6:
        return 0.0, ""
    cutoff = time.time() - window_sec
    past   = next(((t, p) for t, p in hist if t >= cutoff), None)
    if past is None:
        return 0.0, ""
    move = (hist[-1][1] - past[1]) / past[1]
    return abs(move), ("UP" if move > 0 else "DOWN")


def record_btc_move(market: dict, yes_price_new: float, state=None):
    """Record BTC market repricing for CORRELATE lead-lag detection."""
    if market.get("asset") != "BTC":
        return
    import time
    tf     = market["timeframe"]
    prev_p = market.get("yes_price", 0.5)
    if prev_p <= 0:
        return
    move = (yes_price_new - prev_p) / prev_p
    if abs(move) >= 0.01 and state:
        state.btc_signal_time[tf]      = time.time()
        state.btc_signal_direction[tf] = "UP" if move > 0 else "DOWN"
        state.btc_signal_move[tf]      = abs(move)


def certainty_to_prob(certainty: float) -> float:
    return 0.50 + 0.45 * min(max(certainty, 0.0), 1.0)


def probability_to_certainty(win_prob: float) -> float:
    return max(0.0, min((win_prob - 0.50) / 0.45, 1.0))


def realized_correlation(asset1: str, asset2: str, state) -> float:
    """Pearson correlation between two assets over recent price history."""
    if not hasattr(state, "price_history"):
        return 0.0
    h1 = list(state.price_history.get(asset1, []))
    h2 = list(state.price_history.get(asset2, []))
    n  = min(len(h1), len(h2), 60)
    if n < 10:
        return 0.0
    p1  = [p for _, p in h1[-n:]]
    p2  = [p for _, p in h2[-n:]]
    m1, m2 = sum(p1)/n, sum(p2)/n
    num    = sum((a-m1)*(b-m2) for a, b in zip(p1, p2))
    d1     = sum((a-m1)**2 for a in p1)
    d2     = sum((b-m2)**2 for b in p2)
    if d1 <= 1e-9 or d2 <= 1e-9:
        return 0.0
    return num / math.sqrt(d1 * d2)
