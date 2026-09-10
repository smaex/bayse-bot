import math
import os
import subprocess
import sys

import config
import health
from client import BayseClient
from executor import _safe_float, _sell_proceeds_for_shares
from learner import _settlement_pnl
from risk import RiskManager


def _position(order_id: str, outcome_id: str) -> dict:
    return {
        "strategy": "SNIPE",
        "outcome": "YES",
        "entry_price": 0.6,
        "amount_ngn": 600.0,
        "order_id": order_id,
        "outcome_id": outcome_id,
        "market_id": "market-1",
    }


def test_open_clob_order_is_not_counted_as_a_fill():
    order = {"status": "open", "size": "10", "amount": "600", "quantity": "10"}
    assert BayseClient.parse_filled_shares(order) == 0.0


def test_explicit_partial_fill_is_counted_even_while_order_is_open():
    order = {"status": "open", "size": "10", "filledSize": "2.5"}
    assert BayseClient.parse_filled_shares(order) == 2.5


def test_rejected_order_quantity_is_not_counted_as_fill():
    assert BayseClient.parse_filled_shares(
        {"status": "rejected", "quantity": 12}
    ) == 0.0


def test_ngn_settlement_uses_one_hundred_naira_per_share():
    # Ten shares bought at 0.60 cost ₦600 and settle for ₦1,000.
    assert _settlement_pnl(
        won=True, amount_ngn=600, entry_price=0.6, filled_quantity=10
    ) == 400
    assert _settlement_pnl(
        won=False, amount_ngn=600, entry_price=0.6, filled_quantity=10
    ) == -600


def test_ngn_settlement_can_reconstruct_shares_from_cost():
    assert _settlement_pnl(
        won=True, amount_ngn=600, entry_price=0.6
    ) == 400


def test_sell_amount_is_currency_proceeds_not_share_count():
    proceeds = _sell_proceeds_for_shares(shares=10, price=0.6, fee_rate=0.02)
    assert 570 < proceeds < 600
    assert proceeds != 10


def test_position_removal_targets_only_matching_hedge_leg():
    risk = RiskManager()
    risk.add_position("market-1", _position("order-a", "yes"))
    risk.add_position("market-1:NO:order-b", _position("order-b", "no"))

    risk.remove_position("market-1", order_id="order-b")

    assert "market-1" in risk.open_positions
    assert "market-1:NO:order-b" not in risk.open_positions


def test_global_risk_policy_caps_user_exposure():
    risk = RiskManager()
    assert risk.can_trade(10_000, 10_000, max_exposure=1.0) is False
    assert config.MAX_PORTFOLIO_EXPOSURE <= 0.50
    assert config.MAX_TRADE_RISK <= 0.10


def test_unsafe_float_values_are_not_written_to_real_columns():
    assert _safe_float(9.4e-64) == 0.0
    assert _safe_float(math.inf, 7.0) == 7.0
    assert _safe_float(4e38, 7.0) == 7.0


def test_fresh_install_is_dry_run_and_experimental_strategies_are_blocked():
    env = os.environ.copy()
    env.pop("LIVE_TRADING", None)
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.LIVE_TRADING)"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == "False"
    if not config.ALLOW_EXPERIMENTAL_STRATEGIES:
        assert not (set(config.PERMITTED_STRATEGIES) & config.EXPERIMENTAL_STRATEGIES)


def test_readiness_requires_declared_startup_and_recent_core_progress():
    health.set_ready(False)
    ready, reasons, _ = health.readiness()
    assert ready is False
    assert reasons

    health.touch("bot")
    health.touch("singleton_lock")
    health.set_ready(True)
    ready, reasons, _ = health.readiness()
    assert ready is True
    assert reasons == []
    health.set_ready(False)
