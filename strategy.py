"""
Strategy engine — four independent signal generators.

Quantitative framework (5-model composite for SNIPE):

  1. Diffusion model (Brownian motion) — primary signal
  ──────────────────────────────────────────────────────
  P(win) = Φ( |d| / (σ_h × √T_h) )   ← same math as Black-Scholes d2
  σ_h is computed from LIVE realized volatility, not a fixed constant.

  2. Realized volatility (dynamic σ)
  ────────────────────────────────────
  Instead of a fixed hourly-vol config, the bot measures actual price
  movement from the last 10 minutes of ticks and blends that with the
  long-run config value. A calmer-than-usual market → lower σ → higher
  win probability for the same distance.

  3. Momentum confirmation
  ─────────────────────────
  Compares the early vs late thirds of the last 90-second price window.
  +1 = price strongly moving away from threshold (confirms our bet).
  −1 = price moving toward threshold (undermines our bet).
  Adds ±0.12 to composite certainty.

  4. Regime detection (efficiency ratio)
  ───────────────────────────────────────
  Net displacement ÷ total path length over the last 5 minutes.
  Clean trend → ratio near 1 → regime_factor up to 1.25× certainty.
  Random chop → ratio near 0 → regime_factor as low as 0.75×.

  5. Market mispricing / edge score
  ───────────────────────────────────
  Edge = our_model_win_prob − market_price.
  Positive edge (market underpricing our side) → up to +0.12 bonus.
  Negative edge (market already priced our move) → up to −0.08 penalty.

  Composite certainty
  ────────────────────
  composite = (base + mom_bonus + edge_bonus) × regime_factor
  Trade fires only when composite ≥ SNIPE_MIN_CERTAINTY (0.40).
  Hard veto: adverse momentum (< −0.7) on a weak base (< 0.55).

  Dynamic EV ceiling (Kelly-derived)
  ────────────────────────────────────
  Only enter when market_price < win_prob × (1 − fee).  Guarantees
  every accepted trade has positive expected value.

  Quarter-Kelly position sizing
  ──────────────────────────────
  f* = (W×b − (1−W)) / b   capped at 5% of bankroll.
  Position size is then scaled by composite certainty so high-conviction
  signals earn proportionally larger positions.
"""

import asyncio
import logging
import math
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Literal
import config
import feeds
import news as news_mod
import database

log = logging.getLogger(__name__)

# Per-user logging context — set once per asyncio task in _user_loop.
# Every signal log line is automatically prefixed with [chat_id].
_user_ctx: ContextVar[str] = ContextVar("user_ctx", default="")

def set_user_context(chat_id: str) -> None:
    _user_ctx.set(chat_id)

def _u() -> str:
    uid = _user_ctx.get()
    return f"[{uid}] " if uid else ""

StrategyType = Literal["SNIPE", "CORRELATE", "ARB", "NEWS"]


@dataclass
class TradeSignal:
    strategy: StrategyType
    event_id: str
    market_id: str
    asset: str
    timeframe: str
    outcome: str          # "YES" or "NO"
    outcome_id: str
    certainty: float      # composite 0–1
    market_price: float   # current AMM price of chosen outcome
    size_pct: float       # fraction of bankroll (quarter-Kelly)
    reason: str
    title: str = ""
    arb_quantity: float = 0.0
    converged_with: list = field(default_factory=list)
    # ML feature snapshot at entry time — stored in DB for future model training
    momentum_at_entry:     float = 0.0
    regime_at_entry:       float = 0.0
    edge_at_entry:         float = 0.0
    realized_vol_at_entry: float = 0.0


# ── BTC tracking for CORRELATE ────────────────────────────────────────────────
_btc_signal_time:      dict[str, float] = {}
_btc_signal_direction: dict[str, str]   = {}
_btc_signal_move:      dict[str, float] = {}


# ── GARCH and Kalman State ────────────────────────────────────────────────────
_price_history:        dict[str, deque] = {}
_HISTORY_MAXLEN:       int              = 180  # 15 minutes of 5s samples
_kalman_state:         dict[str, dict]  = {}
_garch_state:          dict[str, dict]  = {}
_last_history_update:  dict[str, float] = {}
_systemic_halt_until:  float            = 0.0
_circuit_breakers:     dict[str, dict]  = {} # {strategy_asset: {"fails": 0, "halt_until": 0}}

async def load_memory():
    """Load Kalman/GARCH states from DB to avoid 'blind' periods on restart."""
    try:
        states = await asyncio.to_thread(database.load_quant_states)
        for asset, data in states.items():
            _kalman_state[asset] = data.get("kalman")
            _garch_state[asset]  = data.get("garch")
            # Convert history list back to deque
            hist_list = data.get("history", [])
            _price_history[asset] = deque(hist_list, maxlen=_HISTORY_MAXLEN)
        log.info(f"Loaded memory for {len(states)} assets.")
    except Exception as e:
        log.warning(f"Could not load quant memory: {e}")

def _check_circuit_breaker(strategy: str, asset: str) -> bool:
    key = f"{strategy}_{asset}"
    breaker = _circuit_breakers.get(key)
    if not breaker: return True
    if time.time() < breaker.get("halt_until", 0):
        return False
    return True

def record_failure(strategy: str, asset: str):
    key = f"{strategy}_{asset}"
    breaker = _circuit_breakers.get(key, {"fails": 0, "halt_until": 0})
    breaker["fails"] += 1
    if breaker["fails"] >= 3:
        # Halt for 12 hours
        breaker["halt_until"] = time.time() + (12 * 3600)
        log.warning(f"CIRCUIT BREAKER TRIGGERED: {key} halted for 12h after 3 losses.")
    _circuit_breakers[key] = breaker

def record_success(strategy: str, asset: str):
    key = f"{strategy}_{asset}"
    if key in _circuit_breakers:
        _circuit_breakers[key]["fails"] = 0

def is_systemic_risk_active() -> bool:
    """Returns True if the bot is currently in a systemic risk cooldown."""
    return time.time() < _systemic_halt_until

def check_systemic_risk() -> Optional[str]:
    """
    Scans all assets for volatility shocks. 
    If enough assets spike simultaneously, triggers a global halt.
    """
    global _systemic_halt_until
    if is_systemic_risk_active():
        return f"Systemic halt active for {int(_systemic_halt_until - time.time())}s"

    spike_assets = []
    for asset, garch in _garch_state.items():
        config_vol = config.ASSET_HOURLY_VOL.get(asset, 0.022)
        current_vol = math.sqrt(garch["var"] * 720.0)
        
        if current_vol > config_vol * config.SYSTEMIC_RISK_VOL_MULT:
            spike_assets.append(asset)
            
    if len(spike_assets) >= config.SYSTEMIC_RISK_COUNT_THRESHOLD:
        _systemic_halt_until = time.time() + (config.SYSTEMIC_RISK_HALT_MINS * 60)
        return f"GLOBAL VOLATILITY SHOCK: Spikes in {', '.join(spike_assets)}"
        
    return None

def _init_kalman(price: float) -> dict:
    # State: [price, velocity]
    return {
        "x": [price, 0.0],
        # Covariance matrix P
        "P": [[1.0, 0.0], [0.0, 1.0]]
    }

def _update_kalman(state: dict, z: float, dt: float) -> dict:
    """1D Kalman filter for price and velocity estimation."""
    x, P = state["x"], state["P"]
    
    # Process noise covariance Q (tuneable: assumes velocity changes are small but non-zero)
    q = 1e-5
    Q = [[q * (dt**3)/3, q * (dt**2)/2],
         [q * (dt**2)/2, q * dt]]
    # Measurement noise R (variance of price ticks)
    R = 1e-4

    # 1. Predict
    # x_pred = F * x
    x_pred = [x[0] + x[1] * dt, x[1]]
    # P_pred = F * P * F^T + Q
    P_pred = [
        [P[0][0] + dt*(P[1][0] + P[0][1]) + P[1][1]*(dt**2) + Q[0][0], P[0][1] + P[1][1]*dt + Q[0][1]],
        [P[1][0] + P[1][1]*dt + Q[1][0], P[1][1] + Q[1][1]]
    ]

    # 2. Update
    # y = z - H * x_pred (H = [1, 0])
    y = z - x_pred[0]
    # S = H * P_pred * H^T + R
    S = P_pred[0][0] + R
    # K = P_pred * H^T / S
    K = [P_pred[0][0] / S, P_pred[1][0] / S]

    # x_new = x_pred + K * y
    x_new = [x_pred[0] + K[0] * y, x_pred[1] + K[1] * y]
    # P_new = (I - K * H) * P_pred
    P_new = [
        [(1 - K[0]) * P_pred[0][0], (1 - K[0]) * P_pred[0][1]],
        [-K[1] * P_pred[0][0] + P_pred[1][0], -K[1] * P_pred[0][1] + P_pred[1][1]]
    ]

    return {"x": x_new, "P": P_new}

def _update_garch(asset: str, price: float) -> None:
    """Recursive pseudo-GARCH(1,1) update."""
    omega_weight = 0.05
    alpha = 0.15  # shock sensitivity
    beta = 0.80   # persistence
    
    state = _garch_state.get(asset)
    config_vol = config.ASSET_HOURLY_VOL.get(asset, 0.022)
    # Convert hourly config vol to a rough 5s variance target
    # hourly_vol = sqrt(var_5s * 720) -> var_5s = (hourly_vol^2) / 720
    target_var = (config_vol ** 2) / 720.0
    omega = target_var * omega_weight

    if not state:
        _garch_state[asset] = {"var": target_var, "last_price": price}
        return
        
    last_price = state["last_price"]
    if last_price <= 0:
        _garch_state[asset]["last_price"] = price
        return
        
    log_return = math.log(price / last_price)
    shock_sq = log_return ** 2
    
    # GARCH update: var_t = omega + alpha * shock^2 + beta * var_{t-1}
    new_var = omega + alpha * shock_sq + beta * state["var"]
    
    # ── Guard: Volatility Spike Kill-Switch ───────────────────────────────────
    # If variance accelerates too fast (>VOL_SPIKE_THRESHOLD), trigger halt.
    old_var = state.get("var", new_var)
    if old_var > 0:
        acceleration = new_var / old_var
        if acceleration > config.VOL_SPIKE_THRESHOLD:
            global _systemic_halt_until
            _systemic_halt_until = time.time() + (config.SYSTEMIC_RISK_HALT_MINS * 60)
            log.critical(
                f"VOLATILITY SPIKE DETECTED on {asset} | "
                f"accel={acceleration:.2f}x | HALTING ALL TRADING for {SYSTEMIC_RISK_HALT_MINS}m"
            )

    _garch_state[asset] = {"var": new_var, "last_price": price}

def update_price_history(asset: str, price: float) -> None:
    """Record spot tick for quant signals (throttled to 1 sample per 5 s)."""
    now = time.time()
    last_t = _last_history_update.get(asset, 0)
    dt = now - last_t
    if dt < 5:
        return
        
    _last_history_update[asset] = now
    if asset not in _price_history:
        _price_history[asset] = deque(maxlen=_HISTORY_MAXLEN)
        _kalman_state[asset] = _init_kalman(price)
    
    _price_history[asset].append((now, price))
    
    if dt < 60: # only update filters if tick is continuous (avoid huge jumps)
        _kalman_state[asset] = _update_kalman(_kalman_state[asset], price, dt)
        _update_garch(asset, price)
        
        # Periodically persist to DB (every 5 mins)
        if now - _last_history_update.get(f"{asset}_save", 0) > 300:
            _last_history_update[f"{asset}_save"] = now
            state = {
                "kalman": _kalman_state[asset],
                "garch": _garch_state[asset],
                "history": list(_price_history[asset])
            }
            asyncio.create_task(asyncio.to_thread(database.save_quant_state, asset, state))

# ── Quantitative helpers ──────────────────────────────────────────────────────

def _norm_cdf(z: float) -> float:
    """Standard normal CDF via math.erf (stdlib — no scipy needed)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def win_probability(distance_pct: float, secs_remaining: float, asset: str,
                    sigma_override: float = None) -> float:
    """
    P(our direction wins) via diffusion model.

    P = Φ( |d| / (σ_h × √T_h) )

    sigma_override lets callers pass realized vol instead of config vol.
    """
    sigma_h = sigma_override if sigma_override is not None else ASSET_HOURLY_VOL.get(asset, 0.022)
    t_hours = max(secs_remaining / 3600.0, 1.0 / 3600.0)
    z = abs(distance_pct) / (sigma_h * math.sqrt(t_hours))
    return _norm_cdf(z)


def max_ev_price(win_prob: float, fee_rate: float = 0.04, min_margin: float = 0.10) -> float:
    """
    Max entry price guaranteeing EV > 0 with a profit cushion.
    
    Formula v2: 
    1. Primary: win_prob * (1.0 - fee_rate) - min_margin
    2. Floor: Must allow at least MIN_PAYOUT_RATIO net profit.
       payout = (1.0 - fee_rate) / price
       (1.0 - fee_rate) / price >= 1.0 + MIN_PAYOUT_RATIO
       price <= (1.0 - fee_rate) / (1.0 + MIN_PAYOUT_RATIO)
    """
    ev_limit = win_prob * (1.0 - fee_rate) - min_margin
    payout_limit = (1.0 - fee_rate) / (1.0 + MIN_PAYOUT_RATIO)
    return min(ev_limit, payout_limit)


def kelly_size(win_prob: float, market_price: float, fee_rate: float = 0.04,
               fraction: float = 0.25, cap: float = 0.05, asset: str = None) -> float:
    """
    Quarter-Kelly position size, capped at `cap`.
    v2: Dynamic scaling based on GARCH volatility.
    """
    b = (1.0 - fee_rate) / market_price - 1.0
    if b <= 0:
        return 0.0
        
    # ── Dynamic Kelly Scaling ──────────────────────────────────────────────────
    # Compare current hourly GARCH vol to baseline config vol.
    # High vol relative to baseline = reduce bet size (risk of trend break).
    # Low vol relative to baseline = increase bet size (stable environment).
    if asset and asset in _garch_state:
        garch_var = _garch_state[asset]["var"]
        current_vol = math.sqrt(garch_var * 720.0) # approx hourly
        base_vol = config.ASSET_HOURLY_VOL.get(asset, 0.022)
        
        # Vol ratio: 1.0 = normal, >1.0 = high vol, <1.0 = low vol
        vol_ratio = current_vol / base_vol
        # Invert ratio for scaling: higher vol -> lower multiplier
        # Scale fraction between DYNAMIC_KELLY_MIN and DYNAMIC_KELLY_MAX
        dynamic_fraction = min(max(fraction / vol_ratio, config.DYNAMIC_KELLY_MIN), config.DYNAMIC_KELLY_MAX)
        fraction = dynamic_fraction

    raw_kelly = (win_prob * b - (1.0 - win_prob)) / b
    return min(max(raw_kelly * fraction, 0.0), cap)


def _certainty_from_prob(win_prob: float) -> float:
    """Map win_prob [0.50–0.999] → certainty [0–1]."""
    return max(0.0, min((win_prob - 0.50) / 0.45, 0.99))


def certainty_to_prob(certainty: float) -> float:
    """Legacy helper for CORRELATE/NEWS: certainty [0–1] → prob [0.50–0.95]."""
    return 0.50 + 0.45 * min(certainty, 1.0)


# ── Quant signal 2: realized volatility ──────────────────────────────────────

def realized_vol_hourly(asset: str) -> float:
    """
    Actual hourly vol computed from recursive GARCH(1,1) estimates.
    Blends with config value if insufficient data.
    """
    config_vol = config.ASSET_HOURLY_VOL.get(asset, 0.022)
    
    garch = _garch_state.get(asset)
    if not garch:
        return config_vol
        
    # GARCH variance is per 5s tick. Convert to annualized hourly std dev
    # std_dev = sqrt(variance * 720)
    hourly_garch_vol = math.sqrt(garch["var"] * 720.0)
    
    # Volatility Floor (The "Turkey Problem" Fix): 
    # If the market is dead quiet for 15 minutes, blended vol approaches zero, causing the bot 
    # to become wildly overconfident (e.g. 99% win prob) right before a breakout.
    # We must NEVER use a volatility lower than the asset's baseline historical average.
    return max(config_vol, hourly_garch_vol)


# ── Quant signal 3: momentum ──────────────────────────────────────────────────

def _momentum_score(asset: str, direction: str) -> float:
    """
    +1 = price moving strongly in our favour (away from threshold).
    −1 = price moving strongly against us (toward threshold).
    direction: 'YES' (we want higher price) or 'NO' (we want lower price).
    Uses Kalman Filter velocity to estimate 90s smoothed trajectory.
    """
    kalman = _kalman_state.get(asset)
    if not kalman:
        return 0.0
        
    price, velocity = kalman["x"]
    if price <= 0: return 0.0
    
    # Project price change over a 90s window
    projected_change = velocity * 90.0
    fractional_change = projected_change / price
    
    signed = fractional_change if direction == "YES" else -fractional_change
    # ±0.1% over the window maps to ±1.0
    return min(max(signed / 0.001, -1.0), 1.0)


def _velocity_score(asset: str, threshold: float, direction: str) -> float:
    """
    Measures the 'crash velocity' toward the threshold using the Kalman filter.
    Returns the fraction of the safety gap projected to be closed in the next SNIPE_VELOCITY_WINDOW.
    
    Positive = price moving AWAY from threshold (safe).
    Negative = price moving TOWARD threshold (dangerous).
    -1.0 = price projected to close the entire gap.
    """
    kalman = _kalman_state.get(asset)
    if not kalman:
        return 0.0
        
    price, velocity = kalman["x"]
    if price <= 0: return 0.0
    
    now_gap = abs(price - threshold)
    
    # If we are on the wrong side already, it's irrelevant
    if (direction == "YES" and price < threshold) or (direction == "NO" and price > threshold):
        return -1.0
        
    projected_move = velocity * SNIPE_VELOCITY_WINDOW
    
    if direction == "YES":
        # Price > threshold. If move is negative, gap is shrinking.
        gap_change = projected_move
    else:
        # Price < threshold. If move is positive, gap is shrinking.
        gap_change = -projected_move
        
    # Normalize by the current gap to see how much of our remaining safety is projected to be lost
    return gap_change / max(now_gap, 1e-9)


# ── Quant signal 4: regime (efficiency ratio) ─────────────────────────────────

def _regime_score(asset: str) -> float:
    """
    0 = pure choppy noise, 1 = clean directional trend.
    Uses efficiency ratio: net displacement / total path length over 5 min.
    Efficiency ≥ 0.50 scores 1.0 (already a decent trend).
    """
    hist = list(_price_history.get(asset, []))
    n = min(len(hist), 60)   # 60 × 5s = 5-minute window
    if n < 10:
        return 0.5            # neutral when insufficient data
    prices = [p for _, p in hist[-n:]]
    net  = abs(prices[-1] - prices[0])
    path = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
    if path < 1e-10:
        return 0.5
    return min(net / path / 0.5, 1.0)


# ── Quant signal 5b: FX distance trend ───────────────────────────────────────

def _fx_distance_trend(asset: str, threshold: float, direction: str) -> float:
    """
    How the price-to-threshold distance has changed over the last 10 minutes.
      Positive = distance growing in our direction (move has conviction).
      Negative = distance shrinking (price converging back — reversal risk).
    Returns 0.0 when insufficient history (treated as neutral).
    """
    hist = list(_price_history.get(asset, []))
    if len(hist) < 12:       # need at least ~60 s of ticks
        return 0.0
    now_price  = hist[-1][1]
    past_price = hist[max(0, len(hist) - _FX_TREND_LOOKBACK)][1]   # ~10 min ago
    if direction == "YES":
        return (now_price - past_price) / threshold  # +ve = moved further above threshold
    else:
        return (past_price - now_price) / threshold  # +ve = moved further below threshold


# ── Strategy 1: SNIPE — 5-model composite ────────────────────────────────────

def snipe_signal(market: dict, learned: dict = None, spot_price: float = None) -> Optional[TradeSignal]:
    """
    Enter when the diffusion model AND supporting signals agree.

    Five-model composite certainty:
      base     = _certainty_from_prob(win_prob using realized vol)
      mom      = ±0.12 from 90-second momentum
      edge     = ±0.08–0.12 from model vs market-implied price
      regime   = ×0.75 (choppy) to ×1.25 (trending)
      composite = (base + mom_bonus + edge_bonus) × regime_factor

    Hard veto: composite below threshold, or strong adverse momentum
    on a weak base signal.  EV ceiling still applied after all filters.
    """
    tf    = market["timeframe"]
    secs  = market["secs_to_close"]
    asset = market["asset"]

    entry_window = SNIPE_ENTRY_WINDOWS.get(tf)
    # Gate 1 (FX): tighter entry window — 20 min instead of 30 min for crypto.
    # More time elapsed = move is more confirmed before we commit.
    if asset in FX_SESSION_UTC and tf == "1h":
        entry_window = FX_ENTRY_WINDOW_1H
    if entry_window is None or secs > entry_window or secs < 0:
        return None

    # Time Decay Guard (Gamma Guard): Reject if too close to expiration
    if secs < 90:
        log.debug(f"{_u()}SNIPE [{asset} {tf}] REJECTED — {secs:.0f}s left (Gamma Guard)")
        return None

    threshold = market.get("threshold")
    live_spot  = spot_price if spot_price is not None else feeds.spot.get(asset)
    if not threshold or not live_spot:
        if not live_spot:
            log.warning(f"{_u()}SNIPE [{asset} {tf}] no spot price in feeds.spot")
        return None

    distance_pct = (live_spot - threshold) / threshold   # +ve → YES wins

    # Crypto Minimum Distance Guard (Pin Risk)
    if asset not in FX_SESSION_UTC:
        min_dist = CRYPTO_MIN_DISTANCE.get(asset, 0.0010)
        if abs(distance_pct) < min_dist:
            log.debug(
                f"{_u()}SNIPE [{asset} {tf}] REJECTED — distance {distance_pct:+.4%} "
                f"< min {min_dist:.4%} (Pin Risk Guard)"
            )
            return None

    # ── FX gate cascade (gates 1–3) ───────────────────────────────────────────
    if asset in FX_SESSION_UTC:
        # Gate 1: active session only
        hour_utc = datetime.now(timezone.utc).hour
        session_start, session_end = FX_SESSION_UTC[asset]
        if not (session_start <= hour_utc < session_end):
            log.debug(f"{_u()}SNIPE [{asset} {tf}] outside active session (UTC {hour_utc:02d}h)")
            return None

        # Gate 2: minimum distance — need a genuine move, not noise
        min_dist = FX_MIN_DISTANCE[asset]
        if abs(distance_pct) < min_dist:
            log.info(
                f"{_u()}SNIPE [{asset} {tf}] FX G2 DIST — {distance_pct:+.4%} < "
                f"min {min_dist:.4%} (too close to threshold)"
            )
            return None

        # Gate 3: distance trend — move must be holding or growing, not reversing
        direction_early = "YES" if distance_pct > 0 else "NO"
        trend = _fx_distance_trend(asset, threshold, direction_early)
        veto_level = -min_dist * FX_TREND_VETO_MULT
        if trend < veto_level:
            log.info(
                f"{_u()}SNIPE [{asset} {tf}] FX G3 TREND — converging {trend:+.4%}/10min "
                f"(veto < {veto_level:.4%})"
            )
            return None
        log.debug(
            f"{_u()}SNIPE [{asset} {tf}] FX gates 1-3 passed | "
            f"dist={distance_pct:+.4%} trend={trend:+.4%}/10min session=UTC{hour_utc:02d}h"
        )

    # ── Signal 1: diffusion win probability (realized vol) ────────────────────
    rv    = realized_vol_hourly(asset)

    # Fat-tail penalty: inflate realized volatility artificially in the final minutes
    # to force the model to demand a larger price gap for high certainty.
    if secs < 300:
        penalty_multiplier = 1.0 + 0.5 * ((300 - secs) / 210.0)  # scales from 1.0 to 1.5
        rv = rv * penalty_multiplier

    w_est = win_probability(distance_pct, secs, asset, sigma_override=rv)
    base  = _certainty_from_prob(w_est)

    # ── Direction + market price ───────────────────────────────────────────────
    direction = "YES" if distance_pct > 0 else "NO"
    if direction == "YES":
        outcome, outcome_id, market_price = "YES", market["yes_id"], market["yes_price"]
    else:
        outcome, outcome_id, market_price = "NO",  market["no_id"],  market["no_price"]

    # ── Signal 3: momentum ────────────────────────────────────────────────────
    mom       = _momentum_score(asset, direction)
    mom_bonus = 0.12 * mom                     # ±0.12 range

    # ── Signal 4: regime ──────────────────────────────────────────────────────
    regime        = _regime_score(asset)
    regime_factor = 0.75 + 0.50 * regime       # 0.75 (choppy) → 1.25 (trending)

    # ── Signal 5: Velocity (Falling Knife Guard) ──────────────────────────────
    velocity = _velocity_score(asset, threshold, direction)
    if velocity < -SNIPE_VELOCITY_VETO:
        log.info(
            f"{_u()}SNIPE [{asset} {tf}] VETOED — Velocity crash detected "
            f"({velocity:+.1%} gap closed in {SNIPE_VELOCITY_WINDOW}s)"
        )
        return None

    # Gate 4 (FX): regime veto — choppy FX markets mean-revert and kill the edge
    if asset in FX_SESSION_UTC and regime < FX_MIN_REGIME:
        log.info(
            f"{_u()}SNIPE [{asset} {tf}] FX G4 REGIME — regime={regime:.2f} < {FX_MIN_REGIME} "
            f"(choppy, likely to revert)"
        )
        return None

    # ── Signal 5: edge vs market-implied probability ───────────────────────────
    raw_edge   = w_est - market_price           # +ve = market underpricing our side
    edge_bonus = min(max(raw_edge * 0.40, -0.08), 0.12)

    # ── Composite certainty ───────────────────────────────────────────────────
    composite = min((base + mom_bonus + edge_bonus) * regime_factor, 0.99)

    log.info(
        f"{_u()}SNIPE [{asset} {tf}] {secs:.0f}s | "
        f"spot={live_spot:,.2f} threshold={threshold:,.2f} ({distance_pct:+.3%}) | "
        f"w={w_est:.1%} rv={rv:.3f} base={base:.2f} mom={mom:+.2f} "
        f"regime={regime:.2f} edge={raw_edge:+.3f} ➜ composite={composite:.2f} "
        f"{'✓' if composite >= min_certainty else '✗ LOW'}"
    )

    # Hard veto: price racing toward threshold on an already-weak signal
    if mom < -0.7 and base < 0.55:
        log.info(
            f"{_u()}SNIPE [{asset} {tf}] VETOED — adverse momentum ({mom:+.2f}) "
            f"with weak base ({base:.2f})"
        )
        return None

    if composite < min_certainty:
        return None

    # ── Dynamic EV gate ───────────────────────────────────────────────────────
    fee_rate  = market.get("fee_rate", 0.04)
    # Require at least a 10% margin above fees
    ev_ceiling = max_ev_price(w_est, fee_rate, min_margin=0.10)

    if market_price >= ev_ceiling:
        log.info(
            f"{_u()}SNIPE [{asset} {tf}] REJECTED — market {market_price:.3f} >= "
            f"EV ceiling {ev_ceiling:.3f} (w_est={w_est:.1%}, margin buffer=10%)"
        )
        return None

    # ── Hard Price Ceiling ──────────────────────────────────────────────────
    if market_price > SNIPE_MAX_MARKET_PRICE:
        log.info(
            f"{_u()}SNIPE [{asset} {tf}] REJECTED — market {market_price:.3f} > "
            f"SNIPE_MAX_MARKET_PRICE {SNIPE_MAX_MARKET_PRICE:.3f}"
        )
        return None

    # ── Quarter-Kelly sizing ──────────────────────────────────────────────────
    size = kelly_size(w_est, market_price, fee_rate, asset=asset)

    log.info(
        f"{_u()}SNIPE [{asset} {tf}] ✅ SIGNAL | "
        f"market={market_price:.3f} < ceiling={ev_ceiling:.3f} | "
        f"composite={composite:.2f} size={size:.2%}"
    )

    return TradeSignal(
        strategy="SNIPE",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=asset,
        timeframe=tf,
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=composite,
        market_price=market_price,
        size_pct=size,
        reason=(
            f"Spot {asset}={live_spot:,.2f} threshold={threshold:,.2f} "
            f"({distance_pct:+.3%}), {secs:.0f}s | "
            f"w={w_est:.1%} rv={rv:.3f} mom={mom:+.2f} "
            f"regime={regime:.2f} edge={raw_edge:+.3f} composite={composite:.2f} [{tf}]"
        ),
        title=market["title"],
        momentum_at_entry=round(mom, 4),
        regime_at_entry=round(regime, 4),
        edge_at_entry=round(raw_edge, 4),
        realized_vol_at_entry=round(rv, 6),
    )


# ── BTC spot move detector (feeds CORRELATE) ─────────────────────────────────

def _btc_spot_move_pct(window_sec: float = CORRELATION_WINDOW_SEC) -> tuple[float, str]:
    """
    Returns (move_pct, direction) of BTC spot price over the last window_sec.
    Uses price history — no market scan needed, fires as soon as spot moves.
    """
    hist = list(_price_history.get("BTC", []))
    if len(hist) < 6:
        return 0.0, ""
    now     = time.time()
    cutoff  = now - window_sec
    past    = next(((t, p) for t, p in hist if t >= cutoff), None)
    if past is None:
        return 0.0, ""
    move = (hist[-1][1] - past[1]) / past[1]
    return abs(move), ("UP" if move > 0 else "DOWN")


# ── Strategy 2: CORRELATE — BTC → ETH/SOL lead-lag ───────────────────────────

def record_btc_move(market: dict, yes_price_new: float):
    """Record any BTC market move ≥1%. Per-user threshold applied later."""
    if market["asset"] != "BTC":
        return
    tf  = market["timeframe"]
    old = feeds.prev_yes.get(market["market_id"], yes_price_new)
    move = yes_price_new - old
    if abs(move) >= 0.01:
        _btc_signal_time[tf]      = time.time()
        _btc_signal_direction[tf] = "UP" if move > 0 else "DOWN"
        _btc_signal_move[tf]      = abs(move)
        log.info(f"{_u()}BTC {tf} market moved {move:+.3f} — stored for CORRELATE")


_CRYPTO_ASSETS = {"BTC", "ETH", "SOL"}

def correlate_signal(market: dict, threshold: float = CORRELATION_THRESHOLD, learned: dict = None, spot_price: float = None) -> Optional[TradeSignal]:
    """
    BTC spot price moves ≥threshold % → trade same direction on ETH/SOL.

    Guards (v2 — fixed loss-making issues):
      1. Target asset already-moved check — if ETH/SOL already followed BTC, edge is gone
      2. Target distance from threshold — target spot must be on the correct side
      3. Regime filter — choppy target assets mean-revert, killing the correlation edge
      4. Market repricing gate — if market price > 0.65, the move is already priced in
      5. Lower base certainty (0.40 vs old 0.60) — more realistic win probability
    """
    if market["asset"] not in _CRYPTO_ASSETS or market["asset"] == "BTC":
        return None

    tf    = market["timeframe"]
    asset = market["asset"]

    # Primary: BTC spot move (fast, fires before markets reprice)
    spot_move, spot_dir = _btc_spot_move_pct(CORRELATION_WINDOW_SEC)
    if spot_move >= threshold:
        direction = spot_dir
        freshness = 1.0
        log.info(
            f"{_u()}CORRELATE [{asset} {tf}] BTC spot {spot_dir} "
            f"{spot_move:.2%} ≥ {threshold:.2%} — checking {asset}"
        )
    else:
        # Fallback: BTC market-price signal from record_btc_move
        signal_time = _btc_signal_time.get(tf)
        if not signal_time:
            return None
        age = time.time() - signal_time
        if age > CORRELATION_WINDOW_SEC or _btc_signal_move.get(tf, 0.0) < threshold:
            return None
        direction = _btc_signal_direction.get(tf)
        freshness = 1.0 - (age / CORRELATION_WINDOW_SEC)

    # ── Guard 0: Time to play out ──────────────────────────────────────────────
    secs = market.get("secs_to_close", 0)
    if secs < 300:
        log.info(
            f"{_u()}CORRELATE [{asset} {tf}] REJECTED — only {secs:.0f}s left "
            f"(need ≥300s for correlation to fully play out)"
        )
        return None

    # ── Guard 1: has the target asset already moved in the same direction? ─────
    target_move, target_dir = _btc_spot_move_pct(CORRELATION_WINDOW_SEC)  # reuse helper
    # Actually measure target asset's own move
    target_hist = list(_price_history.get(asset, []))
    if len(target_hist) >= 6:
        cutoff_time = time.time() - CORRELATION_WINDOW_SEC
        past_entry = next(((t, p) for t, p in target_hist if t >= cutoff_time), None)
        if past_entry:
            target_asset_move = abs(target_hist[-1][1] - past_entry[1]) / past_entry[1]
            if target_asset_move > spot_move * CORRELATE_ALREADY_MOVED:
                log.info(
                    f"{_u()}CORRELATE [{asset} {tf}] REJECTED — {asset} already moved "
                    f"{target_asset_move:.2%} (>{CORRELATE_ALREADY_MOVED:.0%} of BTC's {spot_move:.2%})"
                )
                return None

    # ── Guard 2: target spot must be on the correct side of threshold ──────────
    target_threshold = market.get("threshold")
    target_spot = spot_price if spot_price is not None else feeds.spot.get(asset)
    if target_threshold and target_spot:
        if direction == "UP" and target_spot < target_threshold:
            log.info(
                f"{_u()}CORRELATE [{asset} {tf}] REJECTED — BTC says UP but "
                f"{asset} spot {target_spot:,.2f} < threshold {target_threshold:,.2f}"
            )
            return None
        if direction == "DOWN" and target_spot > target_threshold:
            log.info(
                f"{_u()}CORRELATE [{asset} {tf}] REJECTED — BTC says DOWN but "
                f"{asset} spot {target_spot:,.2f} > threshold {target_threshold:,.2f}"
            )
            return None

    if direction == "UP":
        outcome, outcome_id, market_price = "YES", market["yes_id"], market["yes_price"]
    else:
        outcome, outcome_id, market_price = "NO",  market["no_id"],  market["no_price"]

    # ── Guard 3: market already repriced ───────────────────────────────────────
    if market_price > CORRELATE_MAX_MARKET_PRICE:
        log.info(
            f"{_u()}CORRELATE [{asset} {tf}] REJECTED — market already at "
            f"{market_price:.3f} > {CORRELATE_MAX_MARKET_PRICE} (move priced in)"
        )
        return None

    # ── Guard 4: regime filter on target asset ─────────────────────────────────
    regime = _regime_score(asset)
    if regime < CORRELATE_MIN_REGIME:
        log.info(
            f"{_u()}CORRELATE [{asset} {tf}] REJECTED — target regime "
            f"{regime:.2f} < {CORRELATE_MIN_REGIME} (choppy, will revert)"
        )
        return None

    mom_dir    = "YES" if direction == "UP" else "NO"
    target_mom = _momentum_score(asset, mom_dir)
    if target_mom < -0.4:
        log.info(
            f"{_u()}CORRELATE [{asset} {tf}] REJECTED — target convergence "
            f"detected ({target_mom:+.2f} momentum against BTC move)"
        )
        return None
    mom       = _momentum_score(asset, mom_dir)
    certainty = min(CORRELATE_BASE_CERTAINTY * freshness * (1.0 + 0.20 * mom), 0.99)

    w_est      = certainty_to_prob(certainty)
    fee_rate   = market.get("fee_rate", 0.04)
    ev_ceiling = max_ev_price(w_est, fee_rate)

    if market_price >= ev_ceiling:
        log.info(
            f"{_u()}CORRELATE [{asset} {tf}] REJECTED — market {market_price:.3f} "
            f">= EV ceiling {ev_ceiling:.3f}"
        )
        return None

    size = kelly_size(w_est, market_price, fee_rate, asset=asset)

    log.info(
        f"{_u()}CORRELATE [{asset} {tf}] ✅ SIGNAL | "
        f"BTC {direction} {spot_move:.2%} | mom={mom:+.2f} regime={regime:.2f} "
        f"certainty={certainty:.2f} market={market_price:.3f}"
    )

    return TradeSignal(
        strategy="CORRELATE",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=asset,
        timeframe=tf,
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=certainty,
        market_price=market_price,
        size_pct=size,
        reason=(
            f"BTC spot {direction} {spot_move:.2%} → {asset} | "
            f"freshness={freshness:.2f} mom={mom:+.2f} regime={regime:.2f} "
            f"market={market_price:.3f} ceiling={ev_ceiling:.3f}"
        ),
        title=market["title"],
        momentum_at_entry=round(mom, 4),
        regime_at_entry=round(regime, 4),
        edge_at_entry=round(w_est - market_price, 4),
        realized_vol_at_entry=round(realized_vol_hourly(asset), 6),
    )


# ── Strategy 3: ARB — Mint/Burn arbitrage (risk-free) ────────────────────────

def arb_signal(market: dict) -> Optional[TradeSignal]:
    """
    If YES + NO < 1.00, buy both sides then burn for 1.00 — risk-free profit.
    Net profit must exceed fee drag on both legs.
    """
    secs = market["secs_to_close"]
    if secs < 30:
        return None

    yes_p    = market["yes_price"]
    no_p     = market["no_price"]
    combined = yes_p + no_p

    if combined > ARB_TRIGGER:
        return None

    fee_rate   = market.get("fee_rate", 0.04)
    net_profit = (1.0 - combined) - (
        fee_rate * yes_p * max(1 - yes_p, 0.5) +
        fee_rate * no_p  * max(1 - no_p,  0.5)
    )

    if net_profit < 0.005:
        return None

    return TradeSignal(
        strategy="ARB",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=market["asset"],
        timeframe=market["timeframe"],
        outcome="YES",
        outcome_id=market["yes_id"],
        certainty=1.0,
        market_price=combined,
        size_pct=0.05,
        reason=(
            f"ARB: YES({yes_p:.3f})+NO({no_p:.3f})={combined:.3f} < 1.00 | "
            f"net_profit={net_profit:.4f}/unit"
        ),
        title=market["title"],
        arb_quantity=0,
    )


# ── Strategy 4: NEWS — sentiment-driven directional trade ────────────────────

def news_signal(market: dict, sentiment_threshold: float = 0.35, spot_price: float = None) -> Optional[TradeSignal]:
    """
    Trade in the direction of a live high-confidence news signal.

    v2 fixes (was losing money):
      1. Dampened certainty — VADER score × 0.55, not raw score as certainty
      2. Market repricing gate — reject if market already moved past 0.62
      3. Regime filter — only trade news in trending markets (regime ≥ 0.25)
      4. Adverse momentum veto — reject if price moving opposite to news
      5. Minimum time remaining — need ≥ 2 min for news to play out
      6. Conservative Kelly (12% vs old 20%)
    """
    asset = market["asset"]
    secs  = market.get("secs_to_close", 0)
    sig   = news_mod.best_signal_for(asset)
    if not sig:
        return None

    strength = sig.strength()
    if strength < sentiment_threshold:
        return None

    # ── Guard 1: minimum time remaining ────────────────────────────────────────
    if secs < NEWS_MIN_SECS_LEFT:
        log.info(
            f"{_u()}NEWS [{asset}] REJECTED — only {secs:.0f}s left "
            f"(need ≥{NEWS_MIN_SECS_LEFT}s for news to play out)"
        )
        return None

    # ── Guard 1.5: timeframe restriction ───────────────────────────────────────
    tf = market.get("timeframe", "")
    if tf == "5min":
        log.info(
            f"{_u()}NEWS [{asset}] REJECTED — timeframe is 5min "
            f"(too fast for news to reliably establish trend)"
        )
        return None

    yes_p = market["yes_price"]
    no_p  = market["no_price"]

    if sig.direction == "BULLISH":
        outcome, outcome_id, market_price = "YES", market["yes_id"], yes_p
        direction = "YES"
    else:
        outcome, outcome_id, market_price = "NO",  market["no_id"],  no_p
        direction = "NO"

    # ── Guard 2: market already repriced ───────────────────────────────────────
    if market_price > NEWS_MAX_MARKET_PRICE:
        log.info(
            f"{_u()}NEWS [{asset}] REJECTED — market at {market_price:.3f} "
            f"> {NEWS_MAX_MARKET_PRICE} (move already priced in)"
        )
        return None

    # ── Guard 3: regime filter ─────────────────────────────────────────────────
    regime = _regime_score(asset)
    if regime < NEWS_MIN_REGIME:
        log.info(
            f"{_u()}NEWS [{asset}] REJECTED — regime {regime:.2f} "
            f"< {NEWS_MIN_REGIME} (choppy market absorbs news shocks)"
        )
        return None

    # Momentum: confirming price move boosts confidence
    mom = _momentum_score(asset, direction)

    # ── Guard 4: adverse momentum veto ─────────────────────────────────────────
    if mom < -0.5:
        log.info(
            f"{_u()}NEWS [{asset}] REJECTED — adverse momentum {mom:+.2f} "
            f"(price moving opposite to {sig.direction} news)"
        )
        return None

    # ── Dampened certainty (v2) ────────────────────────────────────────────────
    # VADER compound 0.80 × dampen 0.55 = effective 0.44 → win_prob ≈ 70%
    # Old: 0.80 raw → certainty 0.80 → win_prob 86% (wildly overconfident)
    dampened     = strength * NEWS_CERTAINTY_DAMPEN
    strength_adj = min(dampened * (1.0 + 0.15 * mom), 0.99)

    w_est      = certainty_to_prob(strength_adj)
    fee_rate   = market.get("fee_rate", 0.04)
    ev_ceiling = max_ev_price(w_est, fee_rate)

    if market_price >= ev_ceiling:
        log.info(
            f"{_u()}NEWS [{asset}] REJECTED — market {market_price:.3f} "
            f">= EV ceiling {ev_ceiling:.3f} (strength_adj={strength_adj:.2f})"
        )
        return None

    # ── Guard 5: slippage buffer — news causes volatility, need extra cushion ──
    # If the market price is within 2% of the ceiling, skip it to allow for slippage.
    if market_price > ev_ceiling * 0.98:
        log.info(
            f"{_u()}NEWS [{asset}] REJECTED — too close to EV ceiling ({market_price:.3f} vs {ev_ceiling:.3f})"
        )
        return None

    size = kelly_size(w_est, market_price, fee_rate, fraction=NEWS_KELLY_FRACTION, asset=asset)

    log.info(
        f"{_u()}NEWS [{asset}] ✅ SIGNAL | {sig.direction} "
        f"raw={strength:.2f} dampened={dampened:.2f} adj={strength_adj:.2f} "
        f"mom={mom:+.2f} regime={regime:.2f} market={market_price:.3f}"
    )

    return TradeSignal(
        strategy="NEWS",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=asset,
        timeframe=market["timeframe"],
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=strength_adj,
        market_price=market_price,
        size_pct=size,
        reason=(
            f"News [{sig.direction}] src={sig.source} raw={strength:.2f} "
            f"dampened={dampened:.2f} mom={mom:+.2f} regime={regime:.2f} "
            f"market={market_price:.3f} ceiling={ev_ceiling:.3f}: {sig.headline[:60]}"
        ),
        title=market["title"],
        momentum_at_entry=round(mom, 4),
        regime_at_entry=round(regime, 4),
        edge_at_entry=round(w_est - market_price, 4),
        realized_vol_at_entry=round(realized_vol_hourly(asset), 6),
    )


# ── Signal convergence boost ──────────────────────────────────────────────────

def _apply_convergence(signals: list[TradeSignal]) -> list[TradeSignal]:
    """
    Boost the best signal when multiple independent strategies agree on direction.
    ARB excluded (risk-free, different category).
    2+ agreeing → certainty +7%, size +25% per confirming signal.
    Conflicting signals → no boost, strongest wins.
    """
    arb        = [s for s in signals if s.strategy == "ARB"]
    directional = [s for s in signals if s.strategy != "ARB"]

    if len(directional) < 2:
        return signals

    yes_sigs = [s for s in directional if s.outcome == "YES"]
    no_sigs  = [s for s in directional if s.outcome == "NO"]

    if yes_sigs and no_sigs:
        best = max(directional, key=lambda s: s.certainty)
        conflict = " vs ".join(
            f"{s.strategy}({'YES' if s.outcome=='YES' else 'NO'})" for s in directional
        )
        best.reason = f"[⚡ CONFLICT: {conflict}] " + best.reason
        return arb + sorted(directional, key=lambda s: s.certainty, reverse=True)

    dominant = yes_sigs or no_sigs
    top      = max(dominant, key=lambda s: s.certainty)
    n_extra  = len(dominant) - 1

    top.certainty      = min(0.99, top.certainty + 0.07 * n_extra)
    top.size_pct       = min(top.size_pct * (1.0 + 0.25 * n_extra), 0.05)
    top.converged_with = [s.strategy for s in dominant if s is not top]
    top.reason         = f"[🎯 CONVERGED: {'+'.join(s.strategy for s in dominant)}] " + top.reason

    log.info(
        f"{_u()}Convergence: {top.converged_with} → {top.outcome} "
        f"certainty={top.certainty:.2%} size={top.size_pct:.2%}"
    )

    return arb + [top]


# ── Main evaluate entrypoint ──────────────────────────────────────────────────

def evaluate(market: dict, strategies: list[str], learned: dict = None, spot_price: float = None) -> list[TradeSignal]:
    """
    Main evaluation entry point.
    spot_price: if provided, overrides the global feeds.spot[asset] (used for backtesting).
    """
    if strategies is None:
        strategies = ["SNIPE", "CORRELATE", "ARB", "NEWS"]
    learned = learned or {}

    raw_cert    = learned.get("snipe_min_certainty", SNIPE_MIN_CERTAINTY)
    min_cert    = max(SNIPE_MIN_CERTAINTY, min(raw_cert, 0.75))
    raw_corr    = learned.get("correlation_threshold", CORRELATION_THRESHOLD)
    corr_thresh = max(CORRELATION_THRESHOLD, min(raw_corr, 0.20))
    news_thresh = learned.get("news_sentiment_threshold", 0.35)

    signals = []
    try:
        if "SNIPE" in strategies:
            if _check_circuit_breaker("SNIPE", market["asset"]):
                sig = snipe_signal(market, learned, spot_price=spot_price)
                if sig: signals.append(sig)
            else:
                log.debug(f"SNIPE {market['asset']} skipped (Circuit Breaker active)")

        if "CORRELATE" in strategies:
            if _check_circuit_breaker("CORRELATE", market["asset"]):
                sig = correlate_signal(market, threshold=corr_thresh, spot_price=spot_price)
                if sig: signals.append(sig)

        if "ARB" in strategies:
            sig = arb_signal(market)
            if sig: signals.append(sig)

        if "NEWS" in strategies:
            sig = news_signal(market, sentiment_threshold=news_thresh, spot_price=spot_price)
            if sig: signals.append(sig)
    except Exception as e:
        log.error(f"Strategy error on {market.get('market_id')}: {e}", exc_info=True)

    if not signals:
        return []

    signals = _apply_convergence(signals)
    return sorted(signals, key=lambda s: s.certainty, reverse=True)
