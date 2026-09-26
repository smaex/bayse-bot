"""The model's inputs must be measurements, and its output must be checkable.

Three first-principles defects, each pinned here:

1. **Volatility was a constant, not a measurement.** ``realized_vol_hourly``
   returned ``max(ASSET_HOURLY_VOL, sqrt(garch_var * 720))``. The 720 assumes
   one tick every five seconds; the oracle is a Binance ``bookTicker`` stream
   delivering many ticks a second, so that factor understated annualised vol by
   ``sqrt(ticks_per_hour/720)``, and because the result was compared with
   ``max()`` against the config baseline the hard-coded 1.8%/h BTC constant won
   in any calm tape. Every probability SNIPE produced was priced off a vol that
   had nothing to do with the market — and vol is the denominator of d2, so it
   set how far spot had to be from the strike before the model would claim an
   edge.

2. **The top of SNIPE's entry band was unreachable.** The EV ceiling was
   ``min(SNIPE_MAX_MARKET_PRICE, max_ev_price(...))`` and the gate is
   ``market_price >= ev_ceil``, so at exactly 0.65 the comparison held for
   *any* model probability. The band cap and the economics ceiling were
   conflated; they are now separate gates.

3. **Nothing could tell whether the model was right.** ``trades.certainty``
   stores the forecast and ``trades.won`` the outcome, but no code ever
   compared them. ``analysis.reliability_table`` does, which is what turns
   "should we lower SNIPE_MIN_CERTAINTY?" from an opinion into a measurement.

Also covered: Bayse pays **no fee to makers on CLOB** (takers only), and the
liquidity-reward endpoint MAKER's whole premise depends on was never read.
"""

from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

import pytest

import analysis
import config
import stall
from client import BayseClient
from strategies import snipe as snipe_module
from strategies.manager import max_ev_price
from strategies.snipe import SnipeStrategy
from strategies.utils import (
    gbm_win_probability,
    measured_vol_hourly,
    probability_to_certainty,
    realized_vol_hourly,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    stall.reset()
    monkeypatch.setattr(snipe_module.global_state, "price_history", {})
    monkeypatch.setattr(snipe_module.global_state, "market_flips", {})
    yield
    stall.reset()


def _ticks(per_tick_return: float, dt: float, n: int = 200, start: float = 100_000.0):
    """A deterministic series alternating +/- per_tick_return every dt seconds."""
    now = time.time()
    out = []
    price = start
    for i in range(n):
        out.append((now - (n - i) * dt, price))
        price = price * (1.0 + per_tick_return) if i % 2 == 0 else \
            price / (1.0 + per_tick_return)
    out.append((now, price))
    return out


def _state(asset="BTC", ticks=None):
    return SimpleNamespace(price_history={asset: ticks or []}, kalman_state={},
                           garch_state={})


# ── 1. Volatility: measure it ─────────────────────────────────────────────────

def test_measured_vol_recovers_a_known_volatility():
    """0.02% per tick at one tick a second is exactly 1.2% per hour."""
    vol = measured_vol_hourly("BTC", _state(ticks=_ticks(0.0002, 1.0)))
    assert vol == pytest.approx(0.0002 * math.sqrt(3600.0), rel=1e-3)
    assert vol == pytest.approx(0.012, rel=1e-3)


def test_the_hard_coded_720_factor_understates_vol_at_real_tick_cadence():
    """The GARCH branch assumes 5s ticks; at 1s it is sqrt(5) too low."""
    vol = measured_vol_hourly("BTC", _state(ticks=_ticks(0.0002, 1.0)))
    legacy = 0.0002 * math.sqrt(720.0)          # sqrt(var * 720), var = r^2
    assert vol / legacy == pytest.approx(math.sqrt(3600.0 / 720.0), rel=1e-3)
    assert vol > legacy * 2


def test_realized_vol_now_prefers_the_measurement():
    state = _state(ticks=_ticks(0.0002, 1.0))     # measures 1.2%/h
    assert realized_vol_hourly("BTC", state) == pytest.approx(0.012, rel=1e-3)
    assert realized_vol_hourly("BTC", state) != config.ASSET_HOURLY_VOL["BTC"]


def test_the_kill_switch_restores_the_old_constant(monkeypatch):
    state = _state(ticks=_ticks(0.0002, 1.0))
    monkeypatch.setattr(config, "USE_MEASURED_VOL", False)
    assert realized_vol_hourly("BTC", state) == config.ASSET_HOURLY_VOL["BTC"]


def test_without_enough_history_it_falls_back_to_the_baseline():
    assert measured_vol_hourly("BTC", _state(ticks=_ticks(0.0002, 1.0, n=5))) is None
    assert realized_vol_hourly("BTC", _state(ticks=_ticks(0.0002, 1.0, n=5))) \
        == config.ASSET_HOURLY_VOL["BTC"]


def test_a_flat_or_duplicated_feed_cannot_produce_a_vol():
    flat = [(time.time() - i, 100_000.0) for i in range(60, 0, -1)]
    assert measured_vol_hourly("BTC", _state(ticks=flat)) is None


def test_a_calm_measurement_asks_for_less_disagreement():
    """Same 0.15% distance and 10-minute horizon; only the vol differs."""
    measured_calm = gbm_win_probability(1.0015, 1.0, 600.0, 0.005)
    from_constant = gbm_win_probability(1.0015, 1.0, 600.0,
                                        config.ASSET_HOURLY_VOL["BTC"])
    assert measured_calm == pytest.approx(0.768, abs=0.002)
    assert from_constant == pytest.approx(0.579, abs=0.002)
    # To reach that same 0.768 at the hard-coded 1.8%/h, spot has to be 0.543%
    # above the strike instead of 0.15% — the constant demanded 3.6x the move.
    lo, hi = 1.0, 1.02
    for _ in range(60):
        mid = (lo + hi) / 2
        if gbm_win_probability(mid, 1.0, 600.0, 0.018) < measured_calm:
            lo = mid
        else:
            hi = mid
    assert hi == pytest.approx(1.00543, abs=0.00005)


# ── 2. The top of the entry band ──────────────────────────────────────────────

def _market(yes: float, market_id: str, secs: float = 400.0):
    return {
        "event_id": "evt-1", "market_id": market_id, "asset": "BTC",
        "timeframe": "15min", "secs_to_close": secs, "threshold": 100_000.0,
        "yes_price": yes, "no_price": round(1.0 - yes, 4), "yes_id": "yes",
        "no_id": "no", "fee_rate": 0.02, "title": "BTC above $100,000?",
        "engine": "CLOB", "status": "open",
    }


def _evaluate(monkeypatch, market, model_probability):
    # Both entry points: SNIPE prices the settlement TWAP by default and only
    # falls back to the close print when SETTLEMENT_TWAP_SEC = 0. These tests
    # are about the gates, so pin whichever one the pipeline calls.
    monkeypatch.setattr(snipe_module, "gbm_win_probability",
                        lambda **kwargs: model_probability)
    monkeypatch.setattr(snipe_module, "twap_win_probability",
                        lambda **kwargs: model_probability)
    learned = {"chat_id": "u-band", "mode": "balanced"}
    signal = asyncio.run(SnipeStrategy().evaluate(
        market, learned, _state(), spot_price=market["threshold"] * 1.002))
    rejects = set(stall._users["u-band"]["rejects"]) if "u-band" in stall._users else set()
    return signal, rejects


def test_the_ev_ceiling_no_longer_blocks_the_top_of_the_band(monkeypatch):
    """0.65 is inside the advertised band and a strong model can now enter it."""
    assert max_ev_price(0.80, 0.65, 0.02, min_margin=0.03) > 0.65
    signal, rejects = _evaluate(monkeypatch, _market(0.65, "mkt-065"), 0.95)
    assert "price_at_or_above_ev_ceiling" not in rejects
    assert signal is not None, rejects
    assert signal.market_price == pytest.approx(0.65)


def test_the_band_gate_still_caps_the_price(monkeypatch):
    signal, rejects = _evaluate(monkeypatch, _market(0.66, "mkt-066"), 0.95)
    assert signal is None
    assert "SNIPE:entry_price_out_of_band" in rejects


# ── 3. Is the model actually right? ───────────────────────────────────────────

def test_certainty_to_win_prob_inverts_probability_to_certainty():
    for w in (0.50, 0.6215, 0.70, 0.85, 0.95):
        assert analysis.certainty_to_win_prob(probability_to_certainty(w)) \
            == pytest.approx(w, abs=1e-9)


def test_reliability_table_flags_an_overconfident_model():
    rows = [{"certainty": 0.6, "won": 1 if i % 4 else 0, "entry_price": 0.55,
             "strategy": "SNIPE"} for i in range(40)]
    table = {row["bucket"]: row for row in analysis.reliability_table(rows, buckets=5)}
    overall = table["ALL"]
    assert overall["predicted"] == pytest.approx(0.77, abs=0.01)
    assert overall["realised"] == pytest.approx(0.75, abs=0.01)
    assert overall["gap"] < 0                      # claims more than it wins
    assert overall["n"] == 40
    assert 0.0 < overall["brier"] < 0.25


def test_reliability_table_confirms_a_calibrated_model():
    rows = ([{"certainty": 0.6, "won": 1, "entry_price": 0.55}] * 77
            + [{"certainty": 0.6, "won": 0, "entry_price": 0.55}] * 23)
    overall = analysis.reliability_table(rows, buckets=5)[-1]
    assert overall["gap"] == pytest.approx(0.0, abs=0.01)


def test_reliability_table_on_no_data_is_empty():
    assert analysis.reliability_table([]) == []
    assert analysis.reliability_table([{"certainty": None, "won": None}]) == []


# ── Bayse: makers pay no fee, and rewards were never read ─────────────────────

def test_liquidity_reward_summary_totals_the_epochs():
    payload = {"data": [
        {"payout": 8.25, "isPaid": True, "sampleCount": 12, "marketId": "m1",
         "status": "completed"},
        {"payout": 2.00, "isPaid": False, "sampleCount": 3, "marketId": "m2",
         "status": "active"},
        {"payout": 1.50, "isPaid": True, "sampleCount": 5, "marketId": "m1",
         "status": "completed"},
    ]}
    summary = BayseClient.summarize_liquidity_rewards(payload)
    assert summary == {"epochs": 3, "markets": 2, "payout": 11.75,
                       "paid": 9.75, "unpaid": 2.00, "samples": 20}


def test_liquidity_reward_summary_survives_a_bad_payload():
    assert BayseClient.summarize_liquidity_rewards({})["epochs"] == 0
    assert BayseClient.summarize_liquidity_rewards({"data": "nope"})["payout"] == 0.0


def test_a_maker_fill_pays_no_taker_fee():
    """Bayse charges takers only on CLOB; a resting maker fill is free."""
    from strategies.manager import _effective_fee, clob_buy_effective_price
    taker_cost = clob_buy_effective_price(0.58, 0.02)
    assert taker_cost == pytest.approx(0.58 / (1.0 - _effective_fee(0.02, 0.58)))
    assert taker_cost > 0.58
    # A maker's break-even at a 0.58 bid is the bid itself, not the fee-inflated
    # taker cost — a 58% win rate, not 58.6%.
    assert 0.58 < taker_cost < 0.587
