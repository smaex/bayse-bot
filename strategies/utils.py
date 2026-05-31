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
    """Continuous Bayesian Hidden Markov Model (HMM) proxy indicating trend probability (0.0 to 1.0)."""
    if not hasattr(state, 'price_history'):
        return 0.5
    hist = list(state.price_history.get(asset, []))  # deque → list so [-n:] slice works
    n = min(len(hist), 60)
    if n < 10:
        return 0.5
        
    prices = [p for _, p in hist[-n:]]
    
    returns = []
    for i in range(1, len(prices)):
        if prices[i-1] <= 0: continue
        returns.append(math.log(prices[i] / prices[i-1]))
        
    if not returns:
        return 0.5
        
    # Local volatility
    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret)**2 for r in returns) / len(returns)
    std_dev = math.sqrt(variance)
    
    if std_dev < 1e-9:
        return 0.5
        
    # Bayesian Update: Prior P(Trend)
    p_trend = 0.5
    
    for r in returns:
        z = abs(r) / std_dev
        # Likelihood proxy: Z > 1.0 favors trend, Z < 1.0 favors chop
        likelihood_trend = min(0.95, max(0.05, z / 2.0)) 
        likelihood_chop = 1.0 - likelihood_trend
        
        marginal = (likelihood_trend * p_trend) + (likelihood_chop * (1.0 - p_trend))
        if marginal > 0:
            p_trend = (likelihood_trend * p_trend) / marginal
            
    return p_trend

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

def realized_correlation(asset1: str, asset2: str, state: any) -> float:
    """Calculates Pearson correlation coefficient between two assets over recent history."""
    if not hasattr(state, 'price_history'):
        return 0.0
    # Convert to list — price_history values may be deque which doesn't support [-n:] slicing
    hist1 = list(state.price_history.get(asset1, []))
    hist2 = list(state.price_history.get(asset2, []))
    
    n = min(len(hist1), len(hist2), 60)
    if n < 10:
        return 0.0
        
    prices1 = [p for _, p in hist1[-n:]]
    prices2 = [p for _, p in hist2[-n:]]
    
    mean1 = sum(prices1) / n
    mean2 = sum(prices2) / n
    
    num = sum((x - mean1) * (y - mean2) for x, y in zip(prices1, prices2))
    den1 = sum((x - mean1)**2 for x in prices1)
    den2 = sum((y - mean2)**2 for y in prices2)
    
    if den1 <= 1e-9 or den2 <= 1e-9:
        return 0.0
        
    return num / math.sqrt(den1 * den2)
