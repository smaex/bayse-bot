import asyncio

import pytest

import config
from strategies.base import MarketState
from strategies.snipe import SnipeStrategy, blend_with_market


def _market(**overrides):
    market = {
        "event_id": "event",
        "market_id": "market",
        "asset": "SOL",
        "timeframe": "15min",
        "secs_to_close": 120,
        "threshold": 100.0,
        "yes_price": 0.55,
        "no_price": 0.45,
        "yes_id": "yes",
        "no_id": "no",
        "fee_rate": 0.05,
        "title": "SOL above 100",
    }
    market.update(overrides)
    return market


def _evaluate(market=None, *, spot=101.5, learned=None, state=None):
    return asyncio.run(SnipeStrategy().evaluate(
        market or _market(), learned or {"mode": "safe"},
        state or MarketState(), spot_price=spot,
    ))


def test_log_odds_blend_shrinks_model_toward_market_consensus():
    blended = blend_with_market(0.95, 0.55, model_weight=0.35)

    assert 0.55 < blended < 0.95
    assert blended == pytest.approx(0.761512, abs=1e-6)


def test_snipe_emits_conservative_sol_15m_signal_only_for_large_edge():
    signal = _evaluate()

    assert signal is not None
    assert signal.strategy == "SNIPE"
    assert signal.asset == "SOL"
    assert signal.timeframe == "15min"
    assert signal.outcome == "YES"
    assert signal.market_price == 0.55
    assert 0.70 < signal.win_prob < 0.90
    assert "blended=" in signal.reason


def test_snipe_rejects_assets_and_timeframes_without_supporting_evidence():
    assert _evaluate(_market(asset="BTC")) is None
    assert _evaluate(_market(asset="ETH")) is None
    assert _evaluate(_market(timeframe="5min")) is None


def test_snipe_rejects_final_minute_and_expensive_payoff_traps():
    assert _evaluate(_market(secs_to_close=59)) is None
    assert _evaluate(_market(yes_price=0.70, no_price=0.30)) is None


def test_snipe_does_not_extrapolate_unstable_kalman_velocity():
    quiet = _evaluate(state=MarketState())
    noisy_state = MarketState(kalman_state={
        "SOL": {"x": [101.5, 10_000.0], "P": [[1, 0], [0, 1]]}
    })
    noisy = _evaluate(state=noisy_state)

    assert quiet is not None and noisy is not None
    assert noisy.win_prob == pytest.approx(quiet.win_prob)


def test_snipe_scope_defaults_match_production_evidence():
    assert config.SNIPE_ALLOWED_ASSETS == {"SOL"}
    assert config.SNIPE_ALLOWED_TIMEFRAMES == {"15MIN"}
    assert config.SNIPE_MAX_MARKET_PRICE == 0.65
