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


def test_snipe_emits_conservative_signal_for_btc_eth_sol():
    # SOL
    sig_sol = _evaluate(_market(asset="SOL", threshold=100.0), spot=101.5)
    assert sig_sol is not None
    assert sig_sol.strategy == "SNIPE"
    assert sig_sol.asset == "SOL"
    assert sig_sol.timeframe == "15min"
    assert sig_sol.outcome == "YES"
    assert sig_sol.market_price == 0.55
    assert 0.70 < sig_sol.win_prob < 0.90
    assert "blended=" in sig_sol.reason

    # BTC
    sig_btc = _evaluate(_market(asset="BTC", threshold=60_000.0), spot=60_400.0)
    assert sig_btc is not None
    assert sig_btc.asset == "BTC"
    assert sig_btc.outcome == "YES"

    # ETH
    sig_eth = _evaluate(_market(asset="ETH", threshold=2_500.0), spot=2_525.0)
    assert sig_eth is not None
    assert sig_eth.asset == "ETH"
    assert sig_eth.outcome == "YES"


def test_snipe_rejects_unsupported_assets_timeframes_and_sub_threshold_distances():
    # Unsupported asset
    assert _evaluate(_market(asset="EURUSD", threshold=1.08), spot=1.09) is None
    # Unsupported timeframe
    assert _evaluate(_market(timeframe="1d")) is None
    # Sub-threshold distance for ETH (0.05% < 0.18% required)
    assert _evaluate(_market(asset="ETH", threshold=2_500.0), spot=2_501.2) is None


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
    assert config.SNIPE_ALLOWED_ASSETS == {"BTC", "ETH", "SOL"}
    assert config.SNIPE_ALLOWED_TIMEFRAMES == {"15MIN", "5MIN"}
    assert config.SNIPE_MAX_MARKET_PRICE == 0.65
