"""Regression tests for Bayse API drift and daily PnL / balance accounting.

Covers:
* burn/mint ``quantity`` is a WALLET amount, not a share count (API docs).
* cancel_order surfaces per-ID rejections in the batch response.
* list_orders uses ``size`` and lowercase statuses; open-order listing paginates.
* panic button actually cancels open orders.
* get_pnl is requested in the bot's currency.
* daily PnL includes partial-exit realized PnL on the day it was booked and
  fails closed (raises) instead of returning 0.0 on DB error.
* day-scoped pause reset keeps the drawdown peak on equity, not free cash.
"""
import asyncio
import datetime
import os
import shutil
import tempfile
from zoneinfo import ZoneInfo

import pytest

import database
import panic_button
from client import BayseClient
from risk import RiskManager


# ── Bayse client boundary ─────────────────────────────────────────────────────

class _RecordingClient(BayseClient):
    def __init__(self):
        super().__init__("public", "secret")
        self.posts = []
        self.deletes = []
        self.gets = []

    async def _post(self, path, body, extra_headers=None):
        self.posts.append((path, body))
        return {"ok": True}

    async def _delete(self, path, body=None):
        self.deletes.append((path, body))
        return {"results": [{"orderId": "o1", "success": False,
                             "error": {"code": "ORDER_NOT_FOUND"}}]}

    async def _get(self, path, params=None, auth="read"):
        self.gets.append((path, dict(params or {})))
        return {}


def test_burn_quantity_is_sent_as_ngn_wallet_amount_not_share_count():
    client = _RecordingClient()
    asyncio.run(client.burn_shares("market", 1.2345, "NGN"))
    path, body = client.posts[0]
    assert path == "/v1/pm/markets/market/burn"
    # 1.2345 complete sets = ₦123.45 at ₦100 per set, rounded DOWN so we never
    # burn more shares than we hold.
    assert body == {"quantity": 123.45, "currency": "NGN"}


def test_burn_quantity_for_usd_is_one_unit_per_share():
    client = _RecordingClient()
    asyncio.run(client.burn_shares("market", 2.5, "USD"))
    assert client.posts[0][1] == {"quantity": 2.5, "currency": "USD"}


def test_cancel_order_raises_when_batch_reports_per_id_failure():
    client = _RecordingClient()
    with pytest.raises(Exception, match="rejected"):
        asyncio.run(client.cancel_order("o1"))


def test_list_orders_uses_documented_size_parameter():
    client = _RecordingClient()
    asyncio.run(client.list_orders(page=2, size=100))
    path, params = client.gets[0]
    assert path == "/v1/pm/orders"
    assert params == {"page": 2, "size": 100}


def test_get_pnl_is_requested_in_the_bot_currency():
    client = _RecordingClient()
    asyncio.run(client.get_pnl())
    path, params = client.gets[0]
    assert path == "/v1/pm/pnl"
    assert params.get("currency") == "NGN"


class _FakeOrdersApi(BayseClient):
    """Honours the documented list-orders params: page, size, status (lowercase)."""

    def __init__(self, orders):
        super().__init__("public", "secret")
        self._orders = orders
        self.cancelled = []

    async def _get(self, path, params=None, auth="read"):
        params = dict(params or {})
        rows = self._orders
        if "status" in params:
            rows = [o for o in rows if o["status"] == params["status"]]
        size = int(params.get("size", 20))
        page = int(params.get("page", 1))
        start = (page - 1) * size
        return {"orders": rows[start:start + size]}

    async def _delete(self, path, body=None):
        oid = (body or {}).get("orderIds", [None])[0]
        self.cancelled.append(oid)
        return {"results": [{"orderId": oid, "success": True}]}

    async def close(self):
        return None


def _orders(n_open, n_filled=0):
    rows = [{"id": f"open-{i}", "status": "open"} for i in range(n_open)]
    rows += [{"id": f"filled-{i}", "status": "filled"} for i in range(n_filled)]
    return rows


def test_list_open_orders_paginates_past_the_first_page():
    client = _FakeOrdersApi(_orders(n_open=45, n_filled=30))
    open_orders = asyncio.run(client.list_open_orders(page_size=20))
    ids = [o["id"] for o in open_orders]
    assert len(ids) == 45
    assert all(i.startswith("open-") for i in ids)


def test_panic_button_cancels_every_open_order(monkeypatch):
    fake = _FakeOrdersApi(_orders(n_open=45, n_filled=5))
    monkeypatch.setattr(panic_button.database, "init_db", lambda: None)
    monkeypatch.setattr(panic_button.database, "get_all_active", lambda: [
        {"chat_id": "chat", "public_key": "pub", "secret_key": "sec",
         "settings": {"paused": False}},
    ])
    monkeypatch.setattr(panic_button.database, "update_settings", lambda *a, **k: None)
    monkeypatch.setattr(panic_button, "BayseClient", lambda pub, sec: fake)

    asyncio.run(panic_button.emergency_shutdown())

    assert len(fake.cancelled) == 45
    assert all(oid.startswith("open-") for oid in fake.cancelled)


# ── Daily PnL and risk accounting ─────────────────────────────────────────────

def test_daily_pnl_read_fails_closed_instead_of_returning_zero(monkeypatch):
    def boom(query, params=()):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "_fetch_one", boom)
    with pytest.raises(RuntimeError):
        database.get_daily_resolved_pnl("chat", "2026-11-01", "Africa/Lagos")


def test_day_rollover_clears_pause_and_keeps_peak_on_equity_not_cash():
    risk = RiskManager()
    risk.last_reset_date = "2000-01-01"
    risk.paused = True
    risk.pause_reason = "drawdown"
    risk.peak_balance = 10_000.0
    risk.current_free_cash = 5_000.0
    # Equity = free cash + capital deployed in open positions.
    risk.deployed = lambda: 3_000.0

    risk.reset_daily_if_needed()

    assert risk.paused is False
    assert risk.peak_balance == pytest.approx(8_000.0)


# ── Real PostgreSQL: partial exits must hit today's daily PnL ────────────────

@pytest.fixture
def pg(monkeypatch):
    try:
        import pgserver
    except ImportError:
        pytest.skip("pgserver not installed")
    data_dir = tempfile.mkdtemp(prefix="bayse_pg_")
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    uri = server.get_uri()
    monkeypatch.setattr(database, "DATABASE_URL", uri)
    monkeypatch.setattr(database, "_pool", None)
    try:
        database.init_db()
        yield uri
    finally:
        if database._pool is not None:
            database._pool.closeall()
            database._pool = None
        try:
            server.cleanup()
        except Exception:
            pass
        shutil.rmtree(data_dir, ignore_errors=True)


def _today():
    return datetime.datetime.now(ZoneInfo("Africa/Lagos")).date().isoformat()


def _new_trade(chat="chat-1"):
    return database.record_trade(
        chat, strategy="taker", asset="BTC", timeframe="15m", outcome="YES",
        outcome_id="out", market_id="mkt", event_id="evt", order_id="ord",
        entry_price=0.5, amount_ngn=1000.0, filled_quantity=20.0,
    )


def test_partial_exit_loss_counts_today_before_final_resolution(pg):
    chat = "chat-1"
    trade = _new_trade(chat)

    database.update_trade_remaining(trade, 500.0, 10.0, -150.0)

    assert database.get_daily_resolved_pnl(chat, _today(), "Africa/Lagos") == pytest.approx(-150.0)


def test_final_resolution_adds_only_the_remaining_leg(pg):
    chat = "chat-1"
    trade = _new_trade(chat)
    database.update_trade_remaining(trade, 500.0, 10.0, -150.0)

    database.resolve_trade(trade, False, -200.0)

    # Total realized loss is -350; the daily sum must not double count the -150.
    assert database.get_daily_resolved_pnl(chat, _today(), "Africa/Lagos") == pytest.approx(-350.0)
    row = database._fetch_one("SELECT pnl_ngn FROM trades WHERE trade_id = %s", (trade,))
    assert row["pnl_ngn"] == pytest.approx(-350.0)


def test_resolution_is_idempotent_for_the_daily_ledger(pg):
    chat = "chat-1"
    trade = _new_trade(chat)
    database.resolve_trade(trade, True, 80.0)
    database.resolve_trade(trade, True, 80.0)  # replayed monitor pass
    assert database.get_daily_resolved_pnl(chat, _today(), "Africa/Lagos") == pytest.approx(80.0)


def test_legacy_pnl_is_backfilled_into_the_ledger_exactly_once(pg):
    chat = "chat-legacy"
    trade = _new_trade(chat)
    # Simulate a database written before the ledger existed.
    database._execute("DELETE FROM config WHERE key = 'trade_pnl_events_v1'")
    database._execute("DELETE FROM trade_pnl_events")
    database._execute(
        "UPDATE trades SET won = 0, pnl_ngn = -60.0, resolved_at = NOW() WHERE trade_id = %s",
        (trade,),
    )

    database.init_db()
    database.init_db()

    assert database.get_daily_resolved_pnl(chat, _today(), "Africa/Lagos") == pytest.approx(-60.0)


# ── Complete-set payout: never book cash the wallet may not hold ──────────────

def test_confirmed_burn_with_matching_payout_books_reported_proceeds():
    import executor
    proceeds, review, _ = executor._complete_set_payout(
        {"status": "success", "quantity": 1.2345, "proceeds": 123.45}, 1.2345
    )
    assert proceeds == pytest.approx(123.45)
    assert review is False


def test_confirmed_burn_with_inconsistent_payout_is_not_booked_at_guaranteed_amount():
    import executor
    # The pre-fix failure mode: 1.2345 sets burned against a ₦1.23 wallet credit.
    proceeds, review, reason = executor._complete_set_payout(
        {"status": "success", "quantity": 0.0123, "proceeds": 1.23}, 1.2345
    )
    assert proceeds is None
    assert review is True
    assert "unverified" in reason


def test_confirmed_burn_without_payout_is_not_booked_at_guaranteed_amount():
    import executor
    proceeds, review, _ = executor._complete_set_payout({"status": "success"}, 2.0)
    assert proceeds is None
    assert review is True
