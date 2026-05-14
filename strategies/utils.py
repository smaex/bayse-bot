import math
import logging
import config
from datetime import datetime, timezone

log = logging.getLogger("strategies.utils")

def realized_vol_hourly(asset: str, state: any) -> float:
    """Blended hourly realized volatility using GARCH and config baseline."""
    config_vol = config.ASSET_HOURLY_VOL.get(asset, 0.022)
    if not hasattr(state, 'garch_state'):
        return config_vol
        
    garch = state.garch_state.get(asset)
    if not garch:
        return config_vol
        
    hourly_garch_vol = math.sqrt(garch["var"] * 720.0)
    return max(config_vol, hourly_garch_vol)

def momentum_score(asset: str, direction: str, state: any) -> float:
    """±1.0 score indicating price movement conviction."""
    if not hasattr(state, 'kalman_state'):
        return 0.0
    kalman = state.kalman_state.get(asset)
    if not kalman:
        return 0.0
        
    price, velocity = kalman["x"]
    if price <= 0: return 0.0
    
    projected_change = velocity * 90.0
    fractional_change = projected_change / price
    
    signed = fractional_change if direction == "YES" else -fractional_change
    return min(max(signed / 0.001, -1.0), 1.0)

def velocity_score(asset: str, threshold: float, direction: str, state: any) -> float:
    """Measures 'crash velocity' toward the threshold."""
    if not hasattr(state, 'kalman_state'):
        return 0.0
    kalman = state.kalman_state.get(asset)
    if not kalman:
        return 0.0
        
    price, velocity = kalman["x"]
    if price <= 0: return 0.0
    
    now_gap = abs(price - threshold)
    if (direction == "YES" and price < threshold) or (direction == "NO" and price > threshold):
        return -1.0
        
    projected_move = velocity * config.SNIPE_VELOCITY_WINDOW
    gap_change = projected_move if direction == "YES" else -projected_move
    return gap_change / max(now_gap, 1e-9)

def regime_score(asset: str, state: any) -> float:
    """0-1 efficiency ratio indicating trend vs noise."""
    if not hasattr(state, 'price_history'):
        return 0.5
    hist = list(state.price_history.get(asset, []))
    n = min(len(hist), 60)
    if n < 10:
        return 0.5
    prices = [p for _, p in hist[-n:]]
    net  = abs(prices[-1] - prices[0])
    path = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
    if path < 1e-10:
        return 0.5
    return min(net / path / 0.5, 1.0)

def fx_distance_trend(asset: str, threshold: float, direction: str, state: any) -> float:
    """How the price-to-threshold distance has changed over the last 10 minutes (FX)."""
    if not hasattr(state, 'price_history'):
        return 0.0
    hist = list(state.price_history.get(asset, []))
    if len(hist) < 12:
        return 0.0
    now_price  = hist[-1][1]
    past_price = hist[max(0, len(hist) - 120)][1] # ~10 min
    if direction == "YES":
        return (now_price - past_price) / threshold
    else:
        return (past_price - now_price) / threshold

def win_probability(dist_pct: float, secs: float, asset: str, sigma_override: float = None) -> float:
    """
    Standard Brownian Diffusion for win probability estimation.
    dist_pct: (spot - threshold) / threshold
    """
    if secs <= 0: return 1.0 if dist_pct > 0 else 0.0
    sigma = sigma_override if sigma_override is not None else 0.02
    t = secs / 3600.0
    # Standard normal CDF
    def norm_cdf(x):
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0
    
    return norm_cdf(dist_pct / (sigma * math.sqrt(t)))

def btc_spot_move_pct(window_sec: float = 300, state: any = None) -> tuple[float, str]:
    """Returns (move_pct, direction) of BTC spot price over the last window_sec."""
    if not hasattr(state, 'price_history'):
        return 0.0, ""
    hist = list(state.price_history.get("BTC", []))
    if len(hist) < 6:
        return 0.0, ""
    import time
    now     = time.time()
    cutoff  = now - window_sec
    past    = next(((t, p) for t, p in hist if t >= cutoff), None)
    if past is None:
        return 0.0, ""
    move = (hist[-1][1] - past[1]) / past[1]
    return abs(move), ("UP" if move > 0 else "DOWN")

def record_btc_move(market: dict, yes_price_new: float, state: any):
    """Record BTC market price moves for lead-lag detection."""
    if market["asset"] != "BTC":
        return
    tf = market["timeframe"]
    prev_p = market.get("yes_price", 0.5)
    if prev_p <= 0: return
    
    move = (yes_price_new - prev_p) / prev_p
    if abs(move) >= 0.01: # 1% threshold
        import time
        if not hasattr(state, 'btc_signal_time'): return
        state.btc_signal_time[tf] = time.time()
        state.btc_signal_direction[tf] = "UP" if move > 0 else "DOWN"
        state.btc_signal_move[tf] = abs(move)


def certainty_to_prob(certainty: float) -> float:
    """Map certainty [0–1] → estimated win probability [0.50–0.95]."""
    return 0.50 + 0.45 * min(max(certainty, 0.0), 1.0)

def probability_to_certainty(win_prob: float) -> float:
    """Inverse of certainty_to_prob."""
    return max(0.0, min((win_prob - 0.50) / 0.45, 1.0))

