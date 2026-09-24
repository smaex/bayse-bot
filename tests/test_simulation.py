import pytest

from tools.simulate_economics import Scenario, render, simulate


def test_implied_binary_price_reconstructs_observed_roi():
    scenario = Scenario("test", wins=60, trades=100, roi=0.20,
                        trades_per_day=2, note="")

    assert scenario.implied_effective_price == pytest.approx(0.50)
    reconstructed_roi = scenario.win_rate / scenario.implied_effective_price - 1
    assert reconstructed_roi == pytest.approx(0.20)


def test_positive_expectancy_simulation_still_contains_losing_months():
    scenario = Scenario("test", wins=60, trades=100, roi=0.10,
                        trades_per_day=2, note="")
    result = simulate(scenario, paths=1000, days=30, seed=7)

    assert 0 < result["month_loss"] < 1
    assert result["all_trading_days_positive"] < 0.01
    assert result["p05"] < result["median"] < result["p95"]


def test_committed_report_discloses_simulation_limitations():
    report = render(paths=50, days=5, stake=100)

    assert "not a claim" in report
    assert "does **not** mean daily profit" in report
    assert "SNIPE all assets (legacy policy)" in report
