"""Orphaned complete-set leg: the exit policy sells it once the retry window closes."""

import bot


def test_orphan_exit_due_only_for_a_flagged_unpaired_position():
    assert bot._orphan_exit_due({"orphan_exit_due": True}, paired=None) is True
    assert bot._orphan_exit_due({"orphan_exit_due": True}, paired={"x": 1}) is False
    assert bot._orphan_exit_due({}, paired=None) is False
    assert bot._orphan_exit_due({"orphan_exit_due": False}, paired=None) is False


from test_exit_reconciliation import FakeExitClient, _install_market, _risk_with_position  # noqa: E402
import asyncio  # noqa: E402
import time  # noqa: E402

import database  # noqa: E402


def _orphan_risk():
    risk = _risk_with_position()
    risk.open_positions["market-1"]["orphan_exit_due"] = True
    return risk


def test_orphan_is_sold_even_when_fair_value_is_unavailable(monkeypatch):
    _install_market(monkeypatch)
    monkeypatch.setattr("strategies.model.fair_value", lambda *args, **kwargs: None)
    client = FakeExitClient()

    asyncio.run(bot._evaluate_and_exit_positions("chat", client, _orphan_risk(), {}))

    assert len(client.sell_calls) == 1, "a missing estimate must not block the orphan sale"


def test_orphan_is_sold_even_when_the_spot_feed_is_stale(monkeypatch):
    _install_market(monkeypatch)
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _asset: (None, 0.0))
    monkeypatch.setattr(bot.feeds, "spot", {})
    monkeypatch.setattr(bot.feeds, "spot_updated_at", {})
    client = FakeExitClient()

    asyncio.run(bot._evaluate_and_exit_positions("chat", client, _orphan_risk(), {}))

    assert len(client.sell_calls) == 1, "a stale spot feed must not block the orphan sale"


def test_orphan_flag_is_written_to_the_trade_row(monkeypatch):
    captured = []
    monkeypatch.setattr(database, "_execute", lambda sql, params: captured.append((sql, params)))

    database.set_trade_orphan("trade-1")

    assert len(captured) == 1
    sql, params = captured[0]
    assert "orphan_exit_due" in sql
    assert params == (True, "trade-1")


def test_orphan_flag_is_restored_from_the_trade_row():
    assert bot._restored_orphan_flag({"orphan_exit_due": True}) is True
    assert bot._restored_orphan_flag({"orphan_exit_due": False}) is False
    assert bot._restored_orphan_flag({}) is False
    assert bot._restored_orphan_flag({"orphan_exit_due": None}) is False
