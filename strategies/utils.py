import math
import logging
import config
from datetime import datetime, timezone

log = logging.getLogger("strategies.utils")


def measured_vol_hourly(asset: str, state, window_sec: float = 300.0,
                        min_ticks: int = 20):
    """Hourly volatility measured from the tick history's own timestamps.

    Returns ``None`` when there is not enough history to measure.

    Why this exists: the GARCH branch of :func:`realized_vol_hourly` scales
    per-tick variance by a fixed 720, which is an assumption of one tick every
    five seconds. The oracle feed is a Binance ``bookTicker`` stream — many
    ticks a second, with evaluations debounced at 250ms — so that factor
    understates the annualised vol by ``sqrt(ticks_per_hour / 720)``, and
    because the GARCH value is then compared with ``max()`` against
    ``ASSET_HOURLY_VOL``, in a calm tape the hard-coded constant won outright.
    Every probability the model produced was therefore priced off a 1.8%/h BTC
    vol regardless of what the tape was actually doing.

    Dividing by the *measured* mean tick interval makes the result correct at
    whatever cadence the feed happens to run. Bid-ask bounce inflates a
    tick-level estimate, so this is conservative rather than optimistic.
    """
    hist = getattr(state, "price_history", None)
    series = hist.get(asset) if hist else None
    if not series or len(series) < min_ticks:
        return None
    newest_t = series[-1][0]
    pts = [(t, p) for t, p in series if newest_t - t <= window_sec and p > 0]
    if len(pts) < min_ticks:
        return None
    span = pts[-1][0] - pts[0][0]
    if span < 20.0:
        return None
    squares = []
    for (_, p0), (_, p1) in zip(pts, pts[1:]):
        if p0 > 0 and p1 > 0:
            r = math.log(p1 / p0)
            squares.append(r * r)
    if not squares:
        return None
    mean_dt = span / len(squares)
    if mean_dt <= 0:
        return None
    var_per_sec = (sum(squares) / len(squares)) / mean_dt
    if not math.isfinite(var_per_sec) or var_per_sec <= 0:
        return None
    vol = math.sqrt(var_per_sec * 3600.0)
    base = config.ASSET_HOURLY_VOL.get(asset, 0.022)
    # Guard rails for degenerate data only (a stalled or duplicated feed can
    # produce a near-zero or absurd reading). Not a tuning knob: the point of
    # the measurement is to move away from the constant.
    return min(max(vol, base * 0.10), base * 10.0)


def realized_vol_hourly(asset: str, state) -> float:
    """Hourly vol for the diffusion model.

    Prefers a measurement taken from the live tick history; falls back to the
    GARCH estimate and finally to the configured baseline. Set
    ``USE_MEASURED_VOL=false`` to restore the old constant/GARCH behaviour.
    """
    base = config.ASSET_HOURLY_VOL.get(asset, 0.022)
    if getattr(config, "USE_MEASURED_VOL", True):
        measured = measured_vol_hourly(asset, state)
        if measured is not None:
            return measured
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


def projected_drift_pct(asset: str, secs: float, state, horizon_cap: float = None) -> float:
    """
    Kalman-filter projected price drift over the next `secs` seconds (or
    `horizon_cap` seconds if provided and shorter), expressed as a fraction
    of current price — i.e. the same units as distance_pct in
    win_probability().

    This is a real drift term for a GBM-with-drift probability estimate,
    NOT a normalised [-1,1] heuristic score (that's what momentum_score is
    for, used by CORRELATE). SNIPE uses this raw value to fold momentum
    directly into its diffusion model rather than bolting it on afterward
    as a separate additive bonus — the textbook-correct way to incorporate
    drift into a boundary-crossing probability estimate.

    horizon_cap limits how far the instantaneous velocity reading gets
    extrapolated. Verified directly against production data: across 6 real
    trades, drift was 5x to 379x larger than the actual raw price distance
    when extrapolated over the full 10-14 minutes remaining — an
    instantaneous Kalman velocity snapshot simply isn't reliable that far
    out. Without this cap, the model can manufacture high confidence almost
    entirely from momentum extrapolation with near-zero support from actual
    price position.

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
    horizon = min(secs, horizon_cap) if horizon_cap is not None else secs
    return (velocity / price) * horizon


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


def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def gbm_win_probability(
    spot: float,
    threshold: float,
    secs: float,
    hourly_vol: float,
    hourly_drift: float = 0.0,
    horizon_cap: float = 180.0,
) -> float:
    """
    Rigorous GBM boundary-crossing probability — the quant standard for binary
    prediction-market contracts on a price-vs-threshold outcome.

    Under Geometric Brownian Motion dS = μS dt + σS dW, the probability that
    the spot price S_T finishes above the threshold K at time T is:

        P(S_T > K) = Φ(d2)
        d2 = [ln(S/K) + (μ_eff − ½σ²)·T] / (σ·√T)

    where:
      - ln(S/K)   is the exact log-distance to threshold (vs the linear
                  approximation (S−K)/K used previously).
      - μ_eff     is the Kalman drift rate (per hour), dampened so it only
                  extrapolates over min(secs, horizon_cap) seconds instead of
                  the full remaining time — prevents a noisy instantaneous
                  velocity reading from dominating over long horizons.
      - −½σ²·T   is the Jensen / Itô correction term that GBM requires.
                  Under log-normal dynamics the *median* path drifts downward
                  by this amount relative to the mean; omitting it causes the
                  model to systematically overestimate win probability under
                  high volatility — which was the primary source of SNIPE losses.

    Returns P(S_T > K) ∈ (0, 1).
    """
    if spot <= 0 or threshold <= 0:
        return 0.5  # degenerate input — no edge claimed
    if secs <= 0:
        return 1.0 if spot > threshold else 0.0
    if hourly_vol <= 0:
        hourly_vol = config.ASSET_HOURLY_VOL.get("BTC", 0.018)

    t_hours = secs / 3600.0

    # Drift dampening: only extrapolate Kalman velocity over min(secs, cap).
    # At secs=900 (15 min) with cap=180s, f_drift=0.20 — momentum contributes
    # only 20% of what an un-capped extrapolation would give, preventing the
    # instantaneous snapshot from manufacturing false confidence.
    f_drift      = min(secs, horizon_cap) / secs if secs > 0 else 0.0
    effective_mu = hourly_drift * f_drift         # still in hourly units

    # GBM d2 numerator
    log_distance = math.log(spot / threshold)     # exact; ≈ (S−K)/K for small gaps
    jensen_corr  = -0.5 * (hourly_vol ** 2)       # Itô / Jensen correction
    drift_term   = (effective_mu + jensen_corr) * t_hours

    denominator  = hourly_vol * math.sqrt(t_hours)
    if denominator <= 0:
        return 1.0 if spot > threshold else 0.0

    d2 = (log_distance + drift_term) / denominator
    return _norm_cdf(d2)


def win_probability(dist_pct: float, secs: float, asset: str,
                    sigma_override: float = None) -> float:
    """
    Backward-compatible wrapper — used by frontrun.py and the exit evaluator
    in bot.py, which pass a pre-computed linear distance (S−K)/K and have
    no Kalman drift available.

    Routes through the GBM formula with zero drift (conservative: no
    momentum assumption when called without velocity data) so the Jensen
    correction is still applied, fixing the vol-overestimation bias.
    Reconstructs spot/threshold from dist_pct as spot = K*(1+dist_pct),
    so log(spot/threshold) = log(1+dist_pct) ≈ dist_pct for small values
    but is exact for larger moves.
    """
    if secs <= 0:
        return 1.0 if dist_pct > 0 else 0.0
    sigma = sigma_override if sigma_override is not None else config.ASSET_HOURLY_VOL.get(asset, 0.022)

    # Reconstruct exact log-distance from linear approximation.
    # For |dist_pct| < 5% the difference is negligible; for larger moves
    # (e.g. +10% away from threshold) the log is materially more accurate.
    spot      = 1.0 + dist_pct          # normalised: threshold = 1.0
    threshold = 1.0

    return gbm_win_probability(
        spot=spot,
        threshold=threshold,
        secs=secs,
        hourly_vol=sigma,
        hourly_drift=0.0,    # no Kalman data available here — zero-drift assumption
        horizon_cap=0.0,     # irrelevant when drift=0
    )


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


def note_reject(learned: dict | None, stage: str, code: str, detail: str = "") -> None:
    """Count a rejected candidate under the gate that rejected it.

    Telemetry for the trading-drought watchdog: a strategy declining to trade is
    usually correct behaviour, and the counter is what proves it. It is attributed
    to the user whose evaluation is running, and any failure here is swallowed so
    a counting bug can never become a trading bug.
    """
    try:
        import stall

        chat_id = (learned or {}).get("chat_id")
        stall.reject(chat_id, stage, code, detail)
    except Exception:
        pass
