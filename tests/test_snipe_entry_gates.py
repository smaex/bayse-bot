"""SNIPE's entry gates compose into a requirement the tape almost never meets.

Production evidence (Telegram, 2026-09-26, one process): 10,615 evaluations,
12 signals — every one MAKER. SNIPE's counters were
``no_raw_edge_or_trend_alignment`` ×9998, ``too_close_to_settle`` ×923,
``outside_entry_window`` ×856: it never reached a single downstream gate.

That looked like "quiet tape", and the first gate really is a legitimate edge
test. But the four gates *after* direction selection are not independent, and
read separately they understate what SNIPE requires:

1. ``SNIPE_MIN_RAW_MODEL_EDGE`` = 0.035 — "model must beat market by 3.5c";
2. ``blend_with_market`` at ``SNIPE_MODEL_WEIGHT`` = 0.35 gives the market 65%
   of the log-odds, spending most of that edge;
3. ``SNIPE_MIN_BLENDED_EDGE`` = 0.025 re-imposes a gap from the *same* market
   price, so the raw edge actually needed is ~7c (2× the advertised gate);
4. ``SNIPE_MIN_CERTAINTY`` = 0.27 is applied to the *blended* probability, i.e.
   to a number the market already dominates. It needs blended ≥ 0.6215, which
   at a 0.55 market means a raw model probability of ~0.74 — a 19-point
   disagreement — and at a 0.45 market, ~0.86.

So the model must disagree with Bayse by 7–58 probability points before SNIPE
risks anything. On a functioning market that does not happen; it happens when
Bayse's price is stale, which the data-quality and oracle-staleness guards
(correctly) reject.

Nothing here loosens a gate: these tests pin the arithmetic so the next config
change is a visible decision, and the counters now name the condition that
bound instead of one lumped code. See reports/snipe_no_entry_diagnosis.md.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import config
import feeds_direct
import stall
from strategies import snipe as snipe_module
from strategies.snipe import SnipeStrategy, blend_with_market


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    stall.reset()
    monkeypatch.setattr(feeds_direct, "_ewma_vol",
                        {"BTC": 0.025, "ETH": 0.035, "SOL": 0.045})
    yield
    stall.reset()


def _market(asset="BTC", tf="15min", secs=400.0, yes=0.55, threshold=100_000.0,
            market_id="mkt-snipe"):
    return {
        "event_id": "evt-1",
        "market_id": market_id,
        "asset": asset,
        "timeframe": tf,
        "secs_to_close": secs,
        "threshold": threshold,
        "yes_price": yes,
        "no_price": round(1.0 - yes, 4),
        "yes_id": "yes",
        "no_id": "no",
        "fee_rate": 0.02,
        "title": "BTC above $100,000?",
        "engine": "CLOB",
        "status": "open",
    }


def _state(history=None):
    return SimpleNamespace(price_history=history or {}, kalman_state={}, garch_state={})


def _evaluate(monkeypatch, market, model_probability, history=None):
    """Run the real strategy with the diffusion model pinned to a probability."""
    monkeypatch.setattr(snipe_module, "gbm_win_probability",
                        lambda **kwargs: model_probability)
    monkeypatch.setattr(snipe_module.global_state, "price_history", {})
    monkeypatch.setattr(snipe_module.global_state, "market_flips", {})
    learned = {"chat_id": "u-snipe", "mode": "balanced"}
    # 0.2% above the strike: clears every asset's distance calibration.
    spot = market["threshold"] * 1.002
    signal = asyncio.run(
        SnipeStrategy().evaluate(market, learned, _state(history), spot_price=spot)
    )
    rejects = dict(stall._users["u-snipe"]["rejects"]) if "u-snipe" in stall._users else {}
    return signal, rejects


def _codes(rejects):
    return {code for code in rejects}


# ── The arithmetic ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("market_price", [0.35, 0.45, 0.55, 0.60, 0.649])
def test_the_raw_edge_needed_after_shrinkage_is_about_twice_the_advertised_gate(market_price):
    from strategies.snipe import effective_raw_edge_floor

    floor = effective_raw_edge_floor(market_price)
    # ~2x the advertised 0.035 across the whole entry band (0.069 at 0.65,
    # 0.073 at 0.35).
    assert 0.068 <= floor <= 0.074, floor
    assert 1.9 * config.SNIPE_MIN_RAW_MODEL_EDGE <= floor <= 2.2 * config.SNIPE_MIN_RAW_MODEL_EDGE
    # Consistency with the real blend, not a re-implementation of it.
    raw = market_price + floor
    assert blend_with_market(raw, market_price) - market_price >= config.SNIPE_MIN_BLENDED_EDGE
    assert blend_with_market(raw - 0.001, market_price) - market_price < \
        config.SNIPE_MIN_BLENDED_EDGE


def test_a_five_cent_raw_edge_clears_the_raw_gate_and_dies_in_the_blend(monkeypatch):
    """0.60 model vs a 0.55 market: 5c of raw edge, comfortably over 0.035."""
    signal, rejects = _evaluate(monkeypatch, _market(yes=0.55), 0.60)
    assert signal is None
    assert "SNIPE:shrunk_edge_below_requirement" in _codes(rejects)

    # The advertised knob is inert while the blend binds: dropping it to 0.5c
    # changes nothing at all, which is the trap in tuning it.
    monkeypatch.setattr(config, "SNIPE_MIN_RAW_MODEL_EDGE", 0.005)
    signal, rejects = _evaluate(monkeypatch, _market(yes=0.55, market_id="mkt-2"), 0.60)
    assert signal is None
    assert "SNIPE:shrunk_edge_below_requirement" in _codes(rejects)


def test_the_certainty_floor_needs_a_nineteen_point_disagreement(monkeypatch):
    """At a 0.55 market the last gate is certainty, not edge."""
    # 0.73 model vs 0.55 market = 18 points of disagreement: still refused.
    signal, rejects = _evaluate(monkeypatch, _market(yes=0.55), 0.73)
    assert signal is None
    assert "SNIPE:certainty_below_floor" in _codes(rejects)

    # 0.74 clears it. The threshold is a ~19-point model-vs-market gap.
    signal, _ = _evaluate(monkeypatch, _market(yes=0.55, market_id="mkt-3"), 0.74)
    assert signal is not None, "the gate chain should still be satisfiable"
    assert signal.outcome == "YES"
    assert signal.win_prob == pytest.approx(blend_with_market(0.74, 0.55), abs=1e-9)


def test_nothing_can_enter_at_the_top_of_the_price_band(monkeypatch):
    """``ev_ceil`` is clamped to SNIPE_MAX_MARKET_PRICE and compared with ``>=``,
    so a market priced exactly at the band's upper edge is always refused."""
    ceiling = config.SNIPE_MAX_MARKET_PRICE
    signal, rejects = _evaluate(monkeypatch, _market(yes=ceiling), 0.99)
    assert signal is None
    assert "SNIPE:price_at_or_above_ev_ceiling" in _codes(rejects)


# ── The lumped counter is now specific ────────────────────────────────────────

def test_model_probability_below_the_floor_is_named(monkeypatch):
    # 0.53 model vs a 0.40 market is 13c of "edge", but the model is under 0.55.
    signal, rejects = _evaluate(monkeypatch, _market(yes=0.40), 0.53)
    assert signal is None
    assert "SNIPE:model_prob_below_floor" in _codes(rejects)
    assert "SNIPE:no_raw_edge_or_trend_alignment" not in _codes(rejects)
    detail = rejects["SNIPE:model_prob_below_floor"]["detail"]
    assert "needs >=0.035 raw" in detail
    assert "after 0.35-weight shrinkage" in detail


def test_a_thin_raw_edge_is_named(monkeypatch):
    signal, rejects = _evaluate(monkeypatch, _market(yes=0.55), 0.57)
    assert signal is None
    assert "SNIPE:raw_edge_below_floor" in _codes(rejects)


def test_opposing_momentum_is_named(monkeypatch):
    """Same numbers as a passing candidate, but spot is falling."""
    import time as _time

    now = _time.time()
    spot = 100_000.0 * 1.002
    high = spot / 0.998                      # 0.2% higher five minutes ago
    history = {"BTC": [(now - 350, high), (now - 330, high), (now - 310, high),
                       (now - 290, high), (now - 270, high), (now - 5, spot)]}
    signal, rejects = _evaluate(monkeypatch, _market(yes=0.55), 0.74, history=history)
    assert signal is None
    assert "SNIPE:momentum_opposing" in _codes(rejects)


def test_the_new_codes_reach_the_stall_report_as_gates():
    stall.reject("u-report", "SNIPE", "raw_edge_below_floor",
                 "YES p=57.0% vs mkt=0.550 edge=+0.020")
    data = stall.report("u-report")
    codes = [row["code"] for row in data["top_rejects"]]
    assert "SNIPE:raw_edge_below_floor" in codes
    assert data["structural_rejects"] == []
