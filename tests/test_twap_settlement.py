"""Bayse now settles on a Chainlink 60-second TWAP, not the close print.

Operator notice, 2026-09-26: "From today, these markets will use the Chainlink
60-second TWAP instead of Binance spot prices." The public docs still say only
that an event "resolves based on the real-world result" — the settlement source
is not documented — so the notice is the only specification available and these
tests pin our reading of it to a simulation rather than to an assumption.

The settlement value is now the arithmetic average of the reference price over
the final 60 seconds. That is a different random variable from the terminal
spot, and the difference is largest exactly where this bot trades:

* the last minute of diffusion is averaged away, so the variance acts over
  ``secs - 2w/3`` instead of ``secs``;
* at 60 seconds to close — ``SNIPE_MIN_SECS_TO_CLOSE``, the latest SNIPE will
  ever enter — the terminal-spot model **understates** the win probability of
  an above-strike spot by ~2.3 points (verified against simulation below);
* inside the window the elapsed part of the average is already known, which a
  terminal-spot model cannot express at all.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from types import SimpleNamespace

import pytest

import config
import stall
from strategies import snipe as snipe_module
from strategies.snipe import SnipeStrategy
from strategies.utils import (
    gbm_win_probability,
    realized_twap_integral,
    twap_effective_horizons,
    twap_win_probability,
)


def _simulate_twap(spot: float, threshold: float, secs: float, hourly_vol: float,
                   window: float = 60.0, paths: int = 20_000, sub: int = 12,
                   seed: int = 7) -> float:
    """Monte Carlo P(average price over the final window >= threshold).

    Two exact stages: one log-normal jump to the start of the window, then the
    average of a discretised path across it (trapezoid weights at the ends).
    """
    rnd = random.Random(seed)
    sigma = hourly_vol
    a = max(secs - window, 0.0) / 3600.0
    dt = (window / 3600.0) / sub
    wins = 0
    for _ in range(paths):
        price = spot * math.exp(-0.5 * sigma * sigma * a
                                + sigma * math.sqrt(a) * rnd.gauss(0, 1))
        total, weight = price * 0.5, 0.5
        for i in range(1, sub + 1):
            price *= math.exp(-0.5 * sigma * sigma * dt
                              + sigma * math.sqrt(dt) * rnd.gauss(0, 1))
            w = 0.5 if i == sub else 1.0
            total += price * w
            weight += w
        if total / weight >= threshold:
            wins += 1
    return wins / paths


# ── The formula is checked against the thing it models ────────────────────────

@pytest.mark.parametrize("secs,threshold,vol", [
    (300.0, 1.000, 0.10),
    (300.0, 1.002, 0.10),
    (600.0, 1.002, 0.06),
    (120.0, 1.000, 0.10),
    (60.0, 0.999, 0.10),
])
def test_the_closed_form_matches_a_simulation_of_the_actual_twap(secs, threshold, vol):
    closed = twap_win_probability(1.0, threshold, secs, vol)
    simulated = _simulate_twap(1.0, threshold, secs, vol)
    assert abs(closed - simulated) < 0.015, (closed, simulated)


def test_the_terminal_spot_model_is_materially_wrong_at_the_entry_deadline():
    """At SNIPE_MIN_SECS_TO_CLOSE, the old model understates our side by ~2pts."""
    secs, threshold, vol = 60.0, 0.999, 0.10
    simulated = _simulate_twap(1.0, threshold, secs, vol)
    terminal = gbm_win_probability(1.0, threshold, secs, vol)
    twap = twap_win_probability(1.0, threshold, secs, vol)
    assert abs(twap - simulated) < abs(terminal - simulated)
    assert terminal < simulated - 0.015          # understates an above-strike spot
    assert twap > terminal + 0.015


def test_a_zero_window_restores_the_terminal_spot_model_exactly():
    for secs, threshold in ((300.0, 1.002), (60.0, 0.999), (900.0, 1.0)):
        assert twap_win_probability(1.0, threshold, secs, 0.10, window_sec=0.0) \
            == pytest.approx(gbm_win_probability(1.0, threshold, secs, 0.10), abs=1e-12)


def test_the_effective_horizons_are_the_averaged_ones():
    assert twap_effective_horizons(300.0, 60.0) == pytest.approx((270.0, 260.0))
    assert twap_effective_horizons(60.0, 60.0) == pytest.approx((30.0, 20.0))
    assert twap_effective_horizons(30.0, 60.0) == pytest.approx((15.0, 10.0))
    assert twap_effective_horizons(300.0, 0.0) == (300.0, 300.0)


# ── Inside the window, part of the average is already banked ──────────────────

def test_an_elapsed_average_above_the_strike_is_nearly_settled():
    """Half the window banked at an average of 1.0167 against a 1.0 strike.

    Not a hard 1.0: the remaining 30s still has to average above 0.9833, which
    is 1.7% below spot, so the model returns a probability rather than a
    verdict. That smoothness is the point of modelling the partial window.
    """
    prob = twap_win_probability(1.0, 1.0, 30.0, 0.10, window_sec=60.0,
                                realized_integral=30.5, realized_secs=30.0)
    assert prob == pytest.approx(0.9993, abs=0.0005)


def test_a_banked_average_can_make_the_outcome_arithmetic():
    """When the elapsed average alone covers the strike, it is already settled."""
    prob = twap_win_probability(1.0, 0.40, 30.0, 0.10, window_sec=60.0,
                                realized_integral=30.5, realized_secs=30.0)
    assert prob == 0.999


def test_an_elapsed_average_below_the_strike_raises_the_bar():
    """Banked 0.995 over half the window: the rest has to average above 1.005."""
    banked = twap_win_probability(1.0, 1.0, 30.0, 0.10, window_sec=60.0,
                                  realized_integral=29.85, realized_secs=30.0)
    unbanked = twap_win_probability(1.0, 1.0, 30.0, 0.10, window_sec=60.0)
    assert banked < unbanked


def test_the_integral_helper_integrates_the_tick_history():
    now = time.time()
    series = [(now - 30 + i, 100.0) for i in range(31)]      # flat at 100
    state = SimpleNamespace(price_history={"BTC": series}, kalman_state={},
                            garch_state={})
    integral, elapsed = realized_twap_integral("BTC", state, 30.0)
    assert elapsed == pytest.approx(30.0)
    assert integral == pytest.approx(3000.0, rel=1e-9)
    assert realized_twap_integral("ETH", state, 30.0) == (0.0, 0.0)


# ── SNIPE prices the settlement rule it is actually paid on ───────────────────

def _market(secs: float):
    return {
        "event_id": "evt-1", "market_id": "mkt-twap", "asset": "BTC",
        "timeframe": "15min", "secs_to_close": secs, "threshold": 100_000.0,
        "yes_price": 0.55, "no_price": 0.45, "yes_id": "yes", "no_id": "no",
        "fee_rate": 0.02, "title": "BTC above $100,000?", "engine": "CLOB",
        "status": "open",
    }


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    stall.reset()
    monkeypatch.setattr(snipe_module.global_state, "price_history", {})
    monkeypatch.setattr(snipe_module.global_state, "market_flips", {})
    yield
    stall.reset()


def _model_probability(monkeypatch, secs: float) -> float:
    """Capture the probability SNIPE's own pipeline computes, at 0.1% above."""
    seen = {}
    monkeypatch.setattr(snipe_module, "twap_win_probability",
                        lambda **kw: (seen.__setitem__("p", twap_win_probability(**kw)),
                                      seen["p"])[1])
    monkeypatch.setattr(snipe_module, "gbm_win_probability",
                        lambda **kw: (seen.__setitem__("p", gbm_win_probability(**kw)),
                                      seen["p"])[1])
    state = SimpleNamespace(price_history={}, kalman_state={}, garch_state={})
    asyncio.run(SnipeStrategy().evaluate(
        _market(secs), {"chat_id": "u-twap", "mode": "balanced"}, state,
        spot_price=100_000.0 * 1.001))
    return seen.get("p", 0.5)


def test_snipe_prices_the_twap_and_not_the_close_print(monkeypatch):
    at_deadline_twap = _model_probability(monkeypatch, 60.0)
    monkeypatch.setattr(config, "SETTLEMENT_TWAP_SEC", 0.0)
    at_deadline_spot = _model_probability(monkeypatch, 60.0)
    assert at_deadline_twap > at_deadline_spot + 0.01


def test_the_settlement_window_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_TWAP_SEC", 0.0)
    assert config.SETTLEMENT_TWAP_SEC == 0.0


# ── MAKER is paid on the same settled quantity ────────────────────────────────

def test_the_drift_cap_survives_a_zero_window():
    """MAKER caps its Kalman extrapolation at 180s; window 0 must match gbm."""
    for secs in (700.0, 400.0, 200.0):
        assert twap_win_probability(1.0, 0.998, secs, 0.018, window_sec=0.0,
                                    hourly_drift=0.03, horizon_cap=180.0) \
            == pytest.approx(gbm_win_probability(1.0, 0.998, secs, 0.018,
                                                 hourly_drift=0.03,
                                                 horizon_cap=180.0), abs=1e-12)


def _maker_fv(monkeypatch, secs: float, drift: bool = False) -> float:
    from strategies.maker import MakerStrategy
    kalman = {"BTC": {"x": [100_000.0, 100_000.0 * 0.03 / 3600.0]}} if drift else {}
    state = SimpleNamespace(price_history={}, kalman_state=kalman, garch_state={})
    market = {"asset": "BTC", "threshold": 100_000.0, "secs_to_close": secs}
    return MakerStrategy()._fair_value("BTC", market, state=state,
                                       spot=100_000.0 * 1.002)


def test_maker_prices_the_settlement_twap_too(monkeypatch):
    monkeypatch.setattr(config, "SETTLEMENT_TWAP_SEC", 0.0)
    at_close_print = _maker_fv(monkeypatch, 200.0)
    monkeypatch.setattr(config, "SETTLEMENT_TWAP_SEC", 60.0)
    at_twap = _maker_fv(monkeypatch, 200.0)
    assert at_twap > at_close_print + 0.01


def test_the_correction_grows_as_the_window_approaches(monkeypatch):
    """The averaged minute is a bigger share of what is left, so it matters more."""
    monkeypatch.setattr(config, "SETTLEMENT_TWAP_SEC", 60.0)
    gaps = []
    for secs in (700.0, 200.0):
        twap = _maker_fv(monkeypatch, secs)
        monkeypatch.setattr(config, "SETTLEMENT_TWAP_SEC", 0.0)
        spot = _maker_fv(monkeypatch, secs)
        monkeypatch.setattr(config, "SETTLEMENT_TWAP_SEC", 60.0)
        gaps.append(twap - spot)
    assert gaps[1] > gaps[0] > 0
