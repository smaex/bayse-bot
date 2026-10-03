import math
import os
import subprocess
import sys
import time

import config
import database
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


def test_documented_clob_quantity_is_preferred_as_received_shares():
    order = {
        "status": "partial_filled",
        "quantity": "1.5",
        "filledSize": "45",
    }
    assert BayseClient.parse_filled_shares(order) == 1.5


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


def test_fresh_install_is_dry_run_and_the_roster_is_just_two_strategies():
    """A fresh install must not trade, and must not secretly trade anything
    other than TAKER and MAKER.

    The quarantine list (ARB, PAIRED_SNIPER, MIDMARKET_MAKER) and the
    "experimental" flag that used to gate it are gone: those strategies were
    deleted rather than fenced off. What replaces the check is a harder
    assertion -- nothing outside the two-roster may appear in any of the
    scopes a new account can be given.
    """
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

    assert set(config.ACTIVE_STRATEGIES) == {"TAKER", "MAKER"}
    assert set(config.PERMITTED_STRATEGIES) == {"TAKER", "MAKER"}
    assert set(config.DEFAULT_STRATEGIES) == {"TAKER", "MAKER"}
    # A maker and a taker are charged differently, so both families are named.
    assert set(config.MAKER_STRATEGIES) == {"MAKER"}
    assert set(config.TAKER_STRATEGIES) == {"TAKER"}


def test_new_accounts_start_paused():
    """The single most expensive mistake available is a fresh install that
    starts trading before anyone has looked at it."""
    assert database.DEFAULT_SETTINGS["paused"] is True


def test_readiness_requires_declared_startup_and_recent_core_progress():
    health.set_ready(False)
    ready, reasons, _ = health.readiness()
    assert ready is False
    assert reasons

    health.touch("bot")
    health.touch("singleton_lock")
    # Readiness also requires a live scanner heartbeat: a process whose market
    # discovery loop has died is not "ready", however healthy the event loop is.
    health.touch("scanner")
    health.set_ready(True)
    ready, reasons, _ = health.readiness()
    assert ready is True
    assert reasons == []

    # A scanner that has stopped reporting makes the process un-ready.
    health.touch("scanner")
    snapshot = health.snapshot()
    with health._lock:
        health._components["scanner"]["last_ok"] = time.time() - 600
    ready, reasons, _ = health.readiness()
    assert ready is False
    assert any("scanner" in reason for reason in reasons)

    health.touch("scanner")
    health.set_ready(False)
