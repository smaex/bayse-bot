"""
Quant state management — Kalman filter (price + velocity) and GARCH(1,1) variance.

These power momentum_score, regime_score, velocity_score, and realized_vol_hourly
in strategies/utils.py.  Without these updates those functions return defaults forever.
"""

import logging
import time
import math
from collections import deque

import config
import strategies
from strategies.base import TradeSignal, MarketState, global_state

log = logging.getLogger("strategy")

# ── Kalman filter ─────────────────────────────────────────────────────────────

def _kalman_update(asset: str, price: float, state: MarketState):
    """
    Constant-velocity Kalman filter: state = [price, velocity].
    Gives a smooth price estimate and a live velocity (trend direction).
    """
    now = time.time()

    if asset not in state.kalman_state:
        state.kalman_state[asset] = {
            "x":         [price, 0.0],
            "P":         [[1.0, 0.0], [0.0, 0.01]],
            "last_time": now,
        }
        return

    k  = state.kalman_state[asset]
    x0, x1 = k["x"]
    P  = k["P"]
    dt = min(now - k["last_time"], 60.0)   # cap to avoid runaway extrapolation
    k["last_time"] = now

    # Predict
    xp0 = x0 + x1 * dt
    xp1 = x1
    q0  = (price * 0.0002) ** 2            # process noise — price
    q1  = (price * 0.00001) ** 2           # process noise — velocity
    Pp  = [
        [P[0][0] + (P[1][0] + P[0][1]) * dt + P[1][1] * dt * dt + q0,
         P[0][1] + P[1][1] * dt],
        [P[1][0] + P[1][1] * dt,
         P[1][1] + q1],
    ]

    # Update (observe price only)
    R   = (price * 0.0005) ** 2            # observation noise ~0.05%
    inn = price - xp0
    S   = Pp[0][0] + R
    K0  = Pp[0][0] / S
    K1  = Pp[1][0] / S

    k["x"] = [xp0 + K0 * inn, xp1 + K1 * inn]
    k["P"] = [
        [(1 - K0) * Pp[0][0], (1 - K0) * Pp[0][1]],
        [Pp[1][0] - K1 * Pp[0][0], Pp[1][1] - K1 * Pp[0][1]],
    ]


# ── GARCH(1,1) variance estimator ────────────────────────────────────────────

def _garch_update(asset: str, price: float, state: MarketState):
    """
    GARCH(1,1): var_t = omega + alpha * ret^2 + beta * var_{t-1}
    Tracks realised per-second variance; strategies scale to hourly.
    """
    base_var = (config.ASSET_HOURLY_VOL.get(asset, 0.022) ** 2) / 720.0  # hourly → per-tick

    if asset not in state.garch_state:
        state.garch_state[asset] = {"var": base_var, "last_price": price}
        return

    g     = state.garch_state[asset]
    last  = g["last_price"]
    if last > 0:
        ret        = (price - last) / last
        omega      = base_var * 0.01          # long-run mean reversion anchor
        alpha      = 0.10                     # weight on squared return
        beta       = 0.88                     # persistence
        g["var"]   = omega + alpha * ret ** 2 + beta * g["var"]
        g["var"]   = max(g["var"], base_var * 0.4)   # floor at 40% of baseline
    g["last_price"] = price


# ── Systemic risk check ───────────────────────────────────────────────────────

def check_systemic_risk() -> str:
    if global_state.systemic_halt_until > time.time():
        mins_left = (global_state.systemic_halt_until - time.time()) / 60
        return f"Systemic halt active ({mins_left:.0f} min remaining)"
    return ""


def is_halted(asset: str) -> bool:
    return global_state.systemic_halt_until > time.time()


def _trigger_halt(asset: str, reason: str, duration_mins: int = None):
    if duration_mins is None:
        duration_mins = config.SYSTEMIC_RISK_HALT_MINS
    global_state.systemic_halt_until = time.time() + duration_mins * 60
    log.warning(f"SYSTEMIC HALT triggered ({reason}) for {duration_mins} min")


# ── Price history + quant state update ───────────────────────────────────────

def update_price_history(asset: str, price: float, state: MarketState = None):
    """
    Main entry point called on every price tick.
    Updates: price_history deque, Kalman state, GARCH state.
    All three are required for quant signals to work.
    """
    if state is None:
        state = global_state

    if asset not in state.price_history:
        state.price_history[asset] = deque(maxlen=2000)
    state.price_history[asset].append((time.time(), price))

    _kalman_update(asset, price, state)
    _garch_update(asset, price, state)


# ── BTC move recording (for CORRELATE) ───────────────────────────────────────

def record_btc_move(market: dict, yes_price_new: float, state: MarketState = None):
    if state is None:
        state = global_state
    if market.get("asset") != "BTC":
        return
    tf      = market["timeframe"]
    prev_p  = market.get("yes_price", 0.5)
    if prev_p <= 0:
        return
    move = (yes_price_new - prev_p) / prev_p
    if abs(move) >= 0.01:
        state.btc_signal_time[tf]      = time.time()
        state.btc_signal_direction[tf] = "UP" if move > 0 else "DOWN"
        state.btc_signal_move[tf]      = abs(move)


# ── Success/failure counters (for adaptive multipliers) ──────────────────────

_strategy_results: dict[str, list[int]] = {}   # strategy → [1,0,1,1,...]

def record_success(strategy: str, asset: str):
    key = f"{strategy}:{asset}"
    _strategy_results.setdefault(key, []).append(1)
    if len(_strategy_results[key]) > 50:
        _strategy_results[key].pop(0)

def record_failure(strategy: str, asset: str):
    key = f"{strategy}:{asset}"
    _strategy_results.setdefault(key, []).append(0)
    if len(_strategy_results[key]) > 50:
        _strategy_results[key].pop(0)


# ── Misc compat ───────────────────────────────────────────────────────────────

def set_user_context(chat_id: str):
    """No-op compat shim — was used for per-user log tagging."""
    pass

async def load_memory():
    """No-op — Kalman/GARCH state rebuilds from live ticks at runtime."""
    pass
