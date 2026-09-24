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
    assert params == (300.0, 5.0, -52.5, "trade-1")


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
    assert "AT TIME ZONE %s" in query
    assert params == ("chat", "Africa/Lagos", "2026-09-10")


def test_resolution_is_idempotent_and_adds_prior_partial_pnl(monkeypatch):
    calls = []
    monkeypatch.setattr(database, "_execute", lambda query, params=(): calls.append((query, params)))

    database.resolve_trade("trade-1", True, 200.0)

    query, params = calls[0]
    assert "COALESCE(pnl_ngn, 0) + %s" in query
    assert "resolved_at IS NULL" in query
    assert params == (1, 200.0, "trade-1")
