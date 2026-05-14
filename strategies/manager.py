import math
import logging
import config

log = logging.getLogger("strategies.manager")

def kelly_size(win_prob: float, market_price: float, fee_rate: float = 0.04,
               fraction: float = 0.25, cap: float = 0.05, asset: str = None,
               state: any = None, learned: dict = None,
               strategy_name: str = None) -> float:
    """
    Quarter-Kelly position size with Bayesian Sample Size Penalty and Volatility Scaling.
    """
    b = (1.0 - fee_rate) / market_price - 1.0
    if b <= 0:
        return 0.0
        
    # ── Bayesian Sample Size Penalty ──
    if learned and strategy_name:
        counts = learned.get("trade_counts", {})
        total = counts.get(strategy_name, 0)
        # Bayesian shrink: penalty scales from 0.1x at 0 trades to 1.0x at 20 trades.
        sample_penalty = min(1.0, 0.1 + (0.9 * (total / 20.0)))
        fraction *= sample_penalty

    # ── Dynamic Kelly Scaling (Volatility) ──
    if asset and state and hasattr(state, 'garch_state') and asset in state.garch_state:
        garch_var = state.garch_state[asset]["var"]
        current_vol = math.sqrt(garch_var * 720.0) # approx hourly
        base_vol = config.ASSET_HOURLY_VOL.get(asset, 0.022)
        
        vol_ratio = current_vol / base_vol
        dynamic_fraction = min(max(fraction / vol_ratio, config.DYNAMIC_KELLY_MIN), config.DYNAMIC_KELLY_MAX)
        fraction = dynamic_fraction

    raw_kelly = (win_prob * b - (1.0 - win_prob)) / b
    return min(max(raw_kelly * fraction, 0.0), cap)

def max_ev_price(win_prob: float, fee_rate: float = 0.04, min_margin: float = 0.06) -> float:
    """Calculates the maximum price we can pay to maintain a specific EV margin."""
    return win_prob * (1.0 - fee_rate) / (1.0 + min_margin)

def certainty_to_prob(certainty: float) -> float:
    """Map certainty [0–1] → estimated win probability [0.50–0.95]."""
    return 0.50 + 0.45 * min(certainty, 1.0)

def probability_to_certainty(win_prob: float) -> float:
    """Inverse of certainty_to_prob."""
    return max(0.0, min((win_prob - 0.50) / 0.45, 1.0))
