import database


def test_resting_order_is_replaced_with_confirmed_fill(monkeypatch):
    calls = []
    monkeypatch.setattr(database, "_execute", lambda query, params=(): calls.append((query, params)))

    database.update_trade_fill("trade-1", 285.0, 5.0, 0.57)

    query, params = calls[0]
    assert "filled_quantity = %s" in query
    assert "entry_price = %s" in query
    assert params == (285.0, 5.0, 0.57, "trade-1")


def test_partial_exit_persists_remaining_cost_and_realized_pnl(monkeypatch):
    calls = []
    monkeypatch.setattr(database, "_execute", lambda query, params=(): calls.append((query, params)))

    database.update_trade_remaining("trade-1", 300.0, 5.0, -52.5)

    query, params = calls[0]
    assert "filled_quantity = %s" in query
    assert "COALESCE(pnl_ngn, 0) + %s" in query
    # The realized leg is also written to the PnL ledger, only if the update applied.
    assert "INSERT INTO trade_pnl_events" in query
    assert params == (300.0, 5.0, -52.5, "trade-1", -52.5)


def test_daily_pnl_uses_configured_trading_timezone(monkeypatch):
    calls = []
    monkeypatch.setattr(
        database,
        "_fetch_one",
        lambda query, params=(): calls.append((query, params)) or {"pnl": 125},
    )

    pnl = database.get_daily_resolved_pnl("chat", "2026-09-10", "Africa/Lagos")

    assert pnl == 125
    query, params = calls[0]
    # The day is the trading-timezone calendar day, expressed as a half-open
    # range on event_at so the (chat_id, event_at) index can serve it.
    assert "::DATE" not in query
    assert "event_at >= %s" in query and "event_at < %s" in query
    from datetime import datetime
    from zoneinfo import ZoneInfo
    lagos = ZoneInfo("Africa/Lagos")
    assert params == (
        "chat",
        datetime(2026, 9, 10, tzinfo=lagos),
        datetime(2026, 9, 11, tzinfo=lagos),
    )


def test_resolution_is_idempotent_and_adds_prior_partial_pnl(monkeypatch):
    calls = []
    monkeypatch.setattr(database, "_execute", lambda query, params=(): calls.append((query, params)))

    database.resolve_trade("trade-1", True, 200.0)

    query, params = calls[0]
    assert "COALESCE(pnl_ngn, 0) + %s" in query
    assert "resolved_at IS NULL" in query
    assert "INSERT INTO trade_pnl_events" in query
    assert params == (1, 200.0, "trade-1", 200.0)
