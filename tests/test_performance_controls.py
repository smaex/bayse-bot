from types import SimpleNamespace

import config
import database
from analysis import _break_even_rate
from executor import _performance_size_multiplier
from learner import (
    _settlement_pnl,
    adjusted_combo_size_multiplier,
    capital_weighted_break_even,
)
from strategies import (
    _STRUCTURAL_STRATEGIES,
    _performance_adjusted_probability,
    _route_strategy_names,
)


def test_break_even_rate_depends_on_paid_prices_not_fixed_strategy_target():
    rows = [
        {
            "total_deployed": 100,
            "potential_payout": 200,  # price 0.50
        },
        {
            "total_deployed": 100,
            "potential_payout": 100 / 0.75,
        },
    ]

    # q_BE = total stake / total possible payout = 200 / 333.33 = 60%.
    assert abs(capital_weighted_break_even(rows) - 0.60) < 1e-9
    assert abs(
        _break_even_rate({"deployed": 200, "potential_payout": 200 / 0.60})
        - 0.60
    ) < 1e-9


def test_combo_loss_control_multiplies_strategy_size_control():
    sig = SimpleNamespace(strategy="MAKER", asset="ETH", timeframe="15min")
    learned = {
        "size_multipliers": {
            "MAKER": 0.80,
            "MAKER:ETH:15min": 0.25,
        }
    }

    assert _performance_size_multiplier(learned, sig) == 0.20
    assert adjusted_combo_size_multiplier(
        1.0, total=34, roi=-0.29,
        win_rate=15 / 34, break_even_rate=0.59,
    ) == 0.75


def test_single_leg_maker_uses_directional_performance_learning():
    assert "MAKER" not in _STRUCTURAL_STRATEGIES


def test_settled_underperformance_reduces_directional_probability_and_edge():
    original = 0.70
    adjusted = _performance_adjusted_probability(original, 0.72)

    assert abs(adjusted - 0.644) < 1e-12
    assert adjusted - 0.62 < 0.03  # no longer clears SNIPE's edge gate


def test_historical_performance_never_inflates_fresh_model_probability():
    assert _performance_adjusted_probability(0.70, 1.20) == 0.70


def test_liquidity_router_never_auto_enables_quarantined_midmarket_maker():
    routed = _route_strategy_names({"SNIPE", "MAKER"}, "DISLOCATED_WIDE")

    assert routed == {"MAKER"}
    assert "MIDMARKET_MAKER" not in routed


def test_new_accounts_default_to_multi_strategy_scope_and_stay_paused():
    assert config.DEFAULT_STRATEGIES == [
        "SNIPE", "MAKER", "ORACLE_ARB", "FRONTRUN", "CORRELATE",
    ]
    assert config.DEFAULT_ASSETS == ["BTC", "ETH", "SOL"]
    assert config.DEFAULT_TIMEFRAMES == ["15min", "5min"]
    assert database.DEFAULT_SETTINGS["strategies"] == [
        "SNIPE", "MAKER", "ORACLE_ARB", "FRONTRUN", "CORRELATE",
    ]
    assert database.DEFAULT_SETTINGS["assets"] == ["BTC", "ETH", "SOL"]
    assert database.DEFAULT_SETTINGS["timeframes"] == ["15min", "5min"]
    assert database.DEFAULT_SETTINGS["mode"] == "balanced"
    assert database.DEFAULT_SETTINGS["maxexposure"] == 15.0
    assert database.DEFAULT_SETTINGS["daily_loss_limit_pct"] == 5.0
    assert database.DEFAULT_SETTINGS["paused"] is True


def test_maker_settlement_does_not_invent_a_five_percent_fee():
    # ₦120 at 0.60 buys two normalized shares and pays ₦200 on a win.
    # Bayse CLOB makers are fee-free, so PnL is ₦80, not ₦74.
    assert _settlement_pnl(
        won=True, amount_ngn=120, entry_price=0.60, filled_quantity=2
    ) == 80
