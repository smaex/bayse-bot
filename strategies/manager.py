import math
import logging
import config

log = logging.getLogger("strategies.manager")


def _effective_fee(fee_rate: float, market_price: float) -> float:
    """
    Bayse fee formula: fee = feeRate × max(1 - price, 0.3)
    The floor is 0.3 — previous code incorrectly used 0.5, which
    over-estimated fees at high prices and killed profitable trades.
    """
    return fee_rate * max(1.0 - market_price, config.FEE_FLOOR)


def kelly_size(win_prob: float, market_price: float, fee_rate: float = 0.02,
               fraction: float = 0.25, cap: float = 0.05,
               asset: str = None, state=None, learned: dict = None,
               strategy_name: str = None) -> float:
    """
    Quarter-Kelly position size with:
      - Drawdown-adjusted fraction
      - Bayesian sample-size penalty (new strategies start small)
      - Dynamic volatility scaling via GARCH
    """
    eff_fee = _effective_fee(fee_rate, market_price)
    b = (1.0 - eff_fee) / market_price - 1.0
    if b <= 0:
        return 0.0

    # ── Drawdown adjustment ──
    if learned and "drawdown_pct" in learned:
        dd = learned["drawdown_pct"]
        if dd >= 0.10:
            fraction *= 0.5     # in drawdown — shrink to eighth-Kelly
        elif dd <= 0.01:
            fraction *= 1.5     # near ATH — can afford half-Kelly

    # ── Sample size penalty ──
    if learned and strategy_name:
        n       = learned.get("trade_counts", {}).get(strategy_name, 0)
        penalty = min(1.0, 0.1 + 0.9 * (n / 20.0))
        fraction *= penalty

    # ── Volatility scaling ──
    if asset and state and hasattr(state, "garch_state") and asset in state.garch_state:
        garch_var  = state.garch_state[asset]["var"]
        hourly_vol = math.sqrt(garch_var * 720.0)
        base_vol   = config.ASSET_HOURLY_VOL.get(asset, 0.022)
        vol_ratio  = hourly_vol / base_vol
        fraction   = min(
            max(fraction / vol_ratio, config.DYNAMIC_KELLY_MIN),
            config.DYNAMIC_KELLY_MAX,
        )

    raw_kelly = (win_prob * b - (1.0 - win_prob)) / b
    return min(max(raw_kelly * fraction, 0.0), cap)


def max_ev_price(win_prob: float, market_price: float,
                 fee_rate: float = 0.02, min_margin: float = 0.05) -> float:
    """
    Maximum price we can pay and still have positive EV after fees.
    Applies AMM convexity — margin requirement grows as price approaches 1.0.
    """
    skew             = market_price - 0.50
    convexity_factor = 1.0 + skew            # 1.4× at 0.90, 0.6× at 0.10
    dynamic_margin   = max(0.01, min_margin * convexity_factor)
    eff_fee          = _effective_fee(fee_rate, market_price)
    return win_prob * (1.0 - eff_fee) / (1.0 + dynamic_margin)
