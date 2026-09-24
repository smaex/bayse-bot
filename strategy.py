"""
Quant state: Kalman filter (price + velocity) and GARCH(1,1) variance.
Drives momentum_score, regime_score, velocity_score, realized_vol_hourly.
"""

import logging
import time
import math
from collections import deque

import config
from strategies.base import TradeSignal, MarketState, global_state

log = logging.getLogger("strategy")


def _kalman_update(asset: str, price: float, state: MarketState):
    now = time.time()
    if asset not in state.kalman_state:
        state.kalman_state[asset] = {
            "x": [price, 0.0], "P": [[1.0, 0.0], [0.0, 0.01]], "last_time": now,
        }
        return
    k = state.kalman_state[asset]
    x0, x1 = k["x"]
    P  = k["P"]
    dt = min(now - k["last_time"], 60.0)
    k["last_time"] = now
    xp0 = x0 + x1 * dt
    xp1 = x1
    q0  = (price * 0.0002) ** 2
    q1  = (price * 0.00001) ** 2
    Pp  = [
        [P[0][0] + (P[1][0] + P[0][1]) * dt + P[1][1] * dt * dt + q0, P[0][1] + P[1][1] * dt],
        [P[1][0] + P[1][1] * dt, P[1][1] + q1],
    ]
    R   = (price * 0.0005) ** 2
    inn = price - xp0
    S   = Pp[0][0] + R
    K0  = Pp[0][0] / S
    K1  = Pp[1][0] / S
    k["x"] = [xp0 + K0 * inn, xp1 + K1 * inn]
    k["P"] = [
        [(1 - K0) * Pp[0][0], (1 - K0) * Pp[0][1]],
        [Pp[1][0] - K1 * Pp[0][0], Pp[1][1] - K1 * Pp[0][1]],
    ]


def _garch_update(asset: str, price: float, state: MarketState):
    """
    GARCH(1,1). Absolute floor of 1e-10 prevents subnormal values
    that crash PostgreSQL REAL (min ~1.18e-38).
    """
    base_var = (config.ASSET_HOURLY_VOL.get(asset, 0.022) ** 2) / 720.0
    if asset not in state.garch_state:
        state.garch_state[asset] = {"var": base_var, "last_price": price}
        return
    g    = state.garch_state[asset]
    last = g["last_price"]
    if last > 0:
        ret      = (price - last) / last
        omega    = base_var * 0.01
        alpha    = 0.10
        beta     = 0.88
        g["var"] = omega + alpha * ret ** 2 + beta * g["var"]
        g["var"] = max(g["var"], base_var * 0.4)  # 40% of baseline floor
        g["var"] = max(g["var"], 1e-10)           # absolute floor — prevents PostgreSQL REAL underflow
    g["last_price"] = price


def check_systemic_risk() -> str:
    if global_state.systemic_halt_until > time.time():
        mins = (global_state.systemic_halt_until - time.time()) / 60
        return f"Systemic halt active ({mins:.0f} min remaining)"
    return ""


def is_halted(asset: str) -> bool:
    return global_state.systemic_halt_until > time.time()


def update_price_history(asset: str, price: float, state: MarketState = None):
    if state is None:
        state = global_state
    if asset not in state.price_history:
        state.price_history[asset] = deque(maxlen=2000)
    state.price_history[asset].append((time.time(), price))
    _kalman_update(asset, price, state)
    _garch_update(asset, price, state)


def record_btc_move(market: dict, yes_price_new: float, state: MarketState = None):
    if state is None:
        state = global_state
    if market.get("asset") != "BTC":
        return
    tf     = market["timeframe"]
    prev_p = market.get("yes_price", 0.5)
    if prev_p <= 0:
        return
    move = (yes_price_new - prev_p) / prev_p
    if abs(move) >= 0.01:
        state.btc_signal_time[tf]      = time.time()
        state.btc_signal_direction[tf] = "UP" if move > 0 else "DOWN"
        state.btc_signal_move[tf]      = abs(move)


_strategy_results: dict[str, list[int]] = {}


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


def set_user_context(chat_id: str):
    pass


async def load_memory():
    pass
