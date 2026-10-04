"""
One fair-value model for the whole bot.

Every price the bot forms -- a TAKER entry, a MAKER quote, an exit decision --
is ``P(the settling quantity finishes above the threshold)``. When the entry
leg and the exit leg each carry their own copy of that calculation they drift,
and the drift shows up as a position that is entered on one number and
justified on another. There is exactly one implementation here.

Two things it gets right that a naive binary-option pricer does not:

* **Itô correction.** Under GBM the median path drifts down by ``-sigma^2/2``
  relative to the mean. Omitting it systematically overstates win probability
  at high vol, which is how a model becomes confidently wrong.
* **The settling quantity is a TWAP, not a print.** Bayse resolves its crypto
  series on a Chainlink 60-second time-weighted average price (operator
  notice 2026-09-26). The average is a different random variable from the
  terminal spot: the last minute of diffusion is partly averaged away.
  ``SETTLEMENT_TWAP_SEC = 0`` restores the close-print model exactly.

Drift is deliberately capped. A Kalman velocity reading is an instantaneous
estimate; extrapolating it across a whole 15-minute candle manufactures
confidence the tape never offered.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import config
from strategies.utils import (
    gbm_win_probability,
    realized_twap_integral,
    realized_vol_hourly,
    twap_win_probability,
)

log = logging.getLogger("strategies.model")

# Never claim more than this. A binary on a 15-minute crypto candle is not a
# certainty at any distance, and a 1.0 flowing into Kelly sizing is how a
# position becomes the whole account.
MAX_MODEL_PROB = 0.995
MIN_MODEL_PROB = 0.005

# Kalman velocity is extrapolated over at most this many seconds.
DRIFT_HORIZON_CAP_SEC = 180.0


def fair_value(
    asset: str,
    market: dict,
    state,
    spot: Optional[float] = None,
    *,
    hourly_vol: Optional[float] = None,
) -> Optional[float]:
    """``P(YES)`` for ``market``, or None when the inputs cannot support a number.

    ``spot`` is the price the caller already resolved for this evaluation pass.
    Passing it in is not an optimisation: reading the feed again here meant one
    decision could compare a distance computed from one price against a
    probability computed from another.
    """
    if spot is None or not math.isfinite(spot) or spot <= 0:
        return None
    threshold = market.get("threshold")
    secs_to_close = market.get("secs_to_close", 0)
    try:
        threshold = float(threshold)
        secs_to_close = float(secs_to_close)
    except (TypeError, ValueError):
        return None
    if threshold <= 0 or secs_to_close <= 0:
        return None

    vol = hourly_vol if hourly_vol is not None else realized_vol_hourly(asset, state)
    if not math.isfinite(vol) or vol <= 0:
        vol = config.ASSET_HOURLY_VOL.get(asset, 0.022)

    drift = hourly_drift(asset, state)

    twap_sec = float(getattr(config, "SETTLEMENT_TWAP_SEC", 0.0) or 0.0)
    if twap_sec > 0:
        integral, elapsed = (
            realized_twap_integral(asset, state, twap_sec - secs_to_close)
            if secs_to_close < twap_sec else (0.0, 0.0)
        )
        p = twap_win_probability(
            spot=spot,
            threshold=threshold,
            secs=secs_to_close,
            hourly_vol=vol,
            window_sec=twap_sec,
            realized_integral=integral,
            realized_secs=elapsed,
            hourly_drift=drift,
            horizon_cap=DRIFT_HORIZON_CAP_SEC,
        )
    else:
        p = gbm_win_probability(
            spot=spot,
            threshold=threshold,
            secs=secs_to_close,
            hourly_vol=vol,
            hourly_drift=drift,
            horizon_cap=DRIFT_HORIZON_CAP_SEC,
        )

    if not math.isfinite(p):
        return None
    return min(MAX_MODEL_PROB, max(MIN_MODEL_PROB, p))


def hourly_drift(asset: str, state) -> float:
    """Annualised-per-hour drift from the asset's Kalman velocity, capped.

    The cap is a *physically* motivated bound, not a comfort blanket: a
    trend that would move BTC more than ~5%/h would be an event, and trading
    size off an extrapolated velocity that large is chasing noise.
    """
    kalman = None
    try:
        kalman = (state.kalman_state or {}).get(asset)
    except AttributeError:
        kalman = None
    if not kalman:
        return 0.0
    try:
        k_price, k_velocity = kalman["x"]
        if not k_price or k_price <= 0:
            return 0.0
        drift = (float(k_velocity) / float(k_price)) * 3600.0
    except (TypeError, ValueError, KeyError, IndexError):
        return 0.0
    if not math.isfinite(drift):
        return 0.0
    return max(-0.05, min(0.05, drift))


def fair_value_pair(
    asset: str, market: dict, state, spot: Optional[float] = None
) -> Optional[tuple[float, float]]:
    """``(P(YES), P(NO))`` clamped so both sides stay quotable.

    The clamp is not a cosmetic guard: a probability of exactly 1.0 or 0.0
    makes every downstream edge, EV and Kelly number infinite or meaningless,
    and the clamped band is where a market can actually transact.
    """
    p_yes = fair_value(asset, market, state, spot)
    if p_yes is None:
        return None
    return p_yes, 1.0 - p_yes


def distance_pct(spot: float, threshold: Optional[float]) -> Optional[float]:
    """Signed fractional distance from spot to the strike."""
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return None
    if not threshold or threshold <= 0 or not spot or spot <= 0:
        return None
    return (spot - threshold) / threshold
