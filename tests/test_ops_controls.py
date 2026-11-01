"""Operational controls from PR #24 open items (2026-11-01).

* Position drift: tracked filled shares must match the exchange portfolio.
  A mismatch that persists across two checks pauses the account; a single
  mismatch (an in-flight fill or sell) does not.
* Startup stale-order sweep: open orders the bot does not track are cancelled
  before the account trades again. Failure keeps the account paused.
* Mark-to-market equity: filled positions are valued at the exit (bid) price
  from the exchange portfolio, not at cost. Unpriced positions fail closed.
* Daily-PnL read: a range predicate on ``event_at`` so the index is usable,
  plus a short cache invalidated by local ledger writes.
* Unresolved-trade poll: one query for all users per cycle, not one per user.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import database
import ops_controls as ops


# ── Position drift ───────────────────────────────────────────────────────────

def _pos(outcome_id, qty, market="m1", **extra):
    return {"market_id": market, "outcome_id": outcome_id,
            "filled_quantity": qty, "amount_ngn": 100.0, **extra}


def _row(outcome_id, balance, sell_price=0.4, currency="NGN"):
    return {"outcomeId": outcome_id, "balance": balance, "availableBalance": balance,
            "sellPrice": sell_price, "market": {"id": "m1"}, "currency": currency}


def test_drift_reports_mismatch_in_either_direction():
    positions = {"m1": _pos("yes-1", 10.0), "m2": _pos("no-2", 5.0, market="m2")}
    rows = [_row("yes-1", 7.0), _row("no-2", 5.0)]
    drift = ops.find_share_drift(positions, rows)
    assert [d["outcome_id"] for d in drift] == ["yes-1"]
    assert drift[0]["tracked"] == 10.0 and drift[0]["held"] == 7.0


def test_drift_treats_missing_portfolio_row_as_zero_held():
    drift = ops.find_share_drift({"m1": _pos("yes-1", 3.0)}, [])
    assert drift and drift[0]["held"] == 0.0


def test_drift_ignores_unfilled_positions():
    unfilled = {"m1": {"market_id": "m1", "outcome_id": "yes-1", "amount_ngn": 100.0}}
    assert ops.find_share_drift(unfilled, []) == []


def test_drift_reports_untracked_holdings_for_alert_only():
    positions = {"m1": _pos("yes-1", 4.0)}
    rows = [_row("yes-1", 4.0), _row("other-9", 2.5)]
    assert ops.untracked_holdings(positions, rows) == [{"outcome_id": "other-9", "held": 2.5}]


def test_drift_gate_needs_two_consecutive_mismatches():
    gate = ops.DriftGate()
    key = ("m1", "yes-1")
    assert gate.observe({key}) == set(), "first sighting must not pause (in-flight sells)"
    assert gate.observe({key}) == {key}, "persistent mismatch pauses"
    assert gate.observe(set()) == set(), "clearing resets the gate"
    assert gate.observe({key}) == set()


# ── Startup stale-order sweep ────────────────────────────────────────────────

def test_orders_to_cancel_keeps_tracked_and_cancels_orphans():
    open_orders = [{"id": "a"}, {"orderId": "b"}, {"id": "c"}, {"id": ""}]
    assert ops.orders_to_cancel(open_orders, known={"a"}) == ["b", "c"]


def test_known_order_ids_collects_position_and_trade_order_ids():
    positions = {"m1": {"order_id": "o1", "maker_order_id": "o2", "outcome_id": "x"},
                 "m2": {"order_id": None}}
    trades = [{"order_id": "o3"}, {"order_id": None}]
    assert ops.known_order_ids(positions.values(), trades) == {"o1", "o2", "o3"}


def test_cancel_batches_are_chunked_to_exchange_limit():
    ids = [f"o{i}" for i in range(250)]
    chunks = ops.chunk(ids, ops.CANCEL_BATCH_LIMIT)
    assert ops.CANCEL_BATCH_LIMIT == 100
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert sum(chunks, []) == ids


# ── Mark-to-market equity ────────────────────────────────────────────────────

def test_mark_equals_cost_basis_when_nothing_is_filled():
    positions = {"m1": {"market_id": "m1", "outcome_id": "yes-1", "amount_ngn": 300.0}}
    assert ops.mark_positions(positions, [], multiplier=100) == pytest.approx(300.0)


def test_mark_values_filled_positions_at_bid_not_cost():
    positions = {"m1": _pos("yes-1", 10.0)}  # cost ₦100
    rows = [_row("yes-1", 10.0, sell_price=0.7)]
    value = ops.mark_positions(positions, rows, multiplier=100)
    assert value == pytest.approx(10.0 * 0.7 * 100)


def test_mark_fails_closed_without_a_price():
    positions = {"m1": _pos("yes-1", 10.0)}
    with pytest.raises(ops.MarkUnavailable):
        ops.mark_positions(positions, [], multiplier=100)
    with pytest.raises(ops.MarkUnavailable):
        ops.mark_positions(positions, [_row("yes-1", 10.0, sell_price=None)], multiplier=100)
    with pytest.raises(ops.MarkUnavailable):
        ops.mark_positions(positions, [_row("yes-1", 10.0, sell_price=1.5)], multiplier=100)


def test_mark_rejects_non_finite_balance():
    positions = {"m1": _pos("yes-1", 10.0)}
    with pytest.raises(ops.MarkUnavailable):
        ops.mark_positions(positions, [_row("yes-1", float("nan"))], multiplier=100)


# ── Portfolio cache ──────────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def get_portfolio(self):
        self.calls += 1
        return {"outcomeBalances": self.rows}


def test_portfolio_cache_limits_reads_to_one_per_window():
    clock = {"now": 0.0}
    cache = ops.PortfolioCache(ttl_seconds=10.0, clock=lambda: clock["now"])
    client = _FakeClient([_row("yes-1", 1.0)])

    async def run():
        await cache.rows("chat", client)
        await cache.rows("chat", client)
        clock["now"] = 11.0
        await cache.rows("chat", client)

    asyncio.run(run())
    assert client.calls == 2


# ── Daily PnL: range predicate and cache ─────────────────────────────────────

class _FakeFetch:
    def __init__(self, value=5.0):
        self.calls = []
        self.value = value

    def __call__(self, query, params=()):
        self.calls.append((" ".join(query.split()), params))
        return {"pnl": self.value}


def test_daily_pnl_uses_range_on_event_at_not_a_cast(monkeypatch):
    fake = _FakeFetch()
    monkeypatch.setattr(database, "_fetch_one", fake)
    database._invalidate_daily_pnl("chat-range")
    database.get_daily_resolved_pnl("chat-range", "2026-11-01", "Africa/Lagos")

    sql, params = fake.calls[0]
    assert "::DATE" not in sql, "a cast on the column defeats the (chat_id, event_at) index"
    assert "event_at >=" in sql and "event_at <" in sql
    start, end = params[1], params[2]
    lagos = ZoneInfo("Africa/Lagos")
    assert start == datetime(2026, 11, 1, tzinfo=lagos)
    assert end == datetime(2026, 11, 2, tzinfo=lagos)


def test_daily_pnl_is_cached_until_local_ledger_write(monkeypatch):
    fake = _FakeFetch(value=-12.0)
    monkeypatch.setattr(database, "_fetch_one", fake)
    database._invalidate_daily_pnl("chat-cache")
    assert database.get_daily_resolved_pnl("chat-cache", "2026-11-01") == -12.0
    assert database.get_daily_resolved_pnl("chat-cache", "2026-11-01") == -12.0
    assert len(fake.calls) == 1, "second read inside the TTL must come from cache"

    database._invalidate_daily_pnl("chat-cache")
    fake.value = -20.0
    assert database.get_daily_resolved_pnl("chat-cache", "2026-11-01") == -20.0
    assert len(fake.calls) == 2


def test_daily_pnl_errors_are_not_cached(monkeypatch):
    calls = {"n": 0}

    def flaky(query, params=()):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db down")
        return {"pnl": 3.0}

    monkeypatch.setattr(database, "_fetch_one", flaky)
    database._invalidate_daily_pnl("chat-err")
    with pytest.raises(ConnectionError):
        database.get_daily_resolved_pnl("chat-err", "2026-11-01")
    assert database.get_daily_resolved_pnl("chat-err", "2026-11-01") == 3.0


def test_ledger_writes_invalidate_the_daily_cache(monkeypatch):
    fake = _FakeFetch(value=-1.0)
    monkeypatch.setattr(database, "_fetch_one", fake)
    monkeypatch.setattr(database, "_execute", lambda *a, **k: None)
    database._invalidate_daily_pnl()
    assert database.get_daily_resolved_pnl("chat-w", "2026-11-01") == -1.0
    database.resolve_trade("trade-1", True, 5.0)  # writes trade_pnl_events
    fake.value = 4.0
    assert database.get_daily_resolved_pnl("chat-w", "2026-11-01") == 4.0, (
        "a ledger write must not leave a stale daily figure in the cache"
    )


# ── Unresolved-trade poll: one query for all users ───────────────────────────

def test_unresolved_for_chats_is_one_query_grouped_by_chat(monkeypatch):
    calls = []

    def fake_all(query, params=()):
        calls.append((" ".join(query.split()), params))
        return [{"chat_id": "a", "trade_id": "t1"}, {"chat_id": "b", "trade_id": "t2"},
                {"chat_id": "a", "trade_id": "t3"}]

    monkeypatch.setattr(database, "_fetch_all", fake_all)
    grouped = database.get_unresolved_for_chats(["a", "b", "c"], older_than_minutes=1)
    assert len(calls) == 1
    assert "chat_id = ANY" in calls[0][0]
    assert [t["trade_id"] for t in grouped["a"]] == ["t1", "t3"]
    assert [t["trade_id"] for t in grouped["b"]] == ["t2"]
    assert "c" not in grouped or grouped["c"] == []


def test_unresolved_for_chats_skips_query_when_no_users(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not query with no users")
    monkeypatch.setattr(database, "_fetch_all", boom)
    assert database.get_unresolved_for_chats([]) == {}


def test_init_db_creates_partial_index_for_open_trades(monkeypatch):
    statements = []

    class _C:
        def cursor(self, *a, **k):
            outer = self

            class _Cur:
                def __enter__(s):
                    return s

                def __exit__(s, *e):
                    return False

                def execute(s, sql, params=None):
                    statements.append(" ".join(sql.split()))

                def fetchone(s):
                    return None

                def fetchall(s):
                    return []
            return _Cur()

        def commit(self):
            pass

    @contextmanager
    def fake_cx():
        yield _C()

    monkeypatch.setattr(database, "_init_pool", lambda: None)
    monkeypatch.setattr(database, "_cx", fake_cx)
    database.init_db()
    assert any("CREATE INDEX IF NOT EXISTS idx_trades_open" in s and "WHERE resolved_at IS NULL" in s
               for s in statements)


# ── Client: batch cancel ─────────────────────────────────────────────────────

def test_client_cancel_orders_batch_sends_chunks(monkeypatch):
    from client import BayseClient

    sent = []

    async def fake_delete(self, path, body=None):
        sent.append((path, body))
        return {"results": [{"orderId": oid, "success": True} for oid in body["orderIds"]]}

    monkeypatch.setattr(BayseClient, "_delete", fake_delete)
    client = BayseClient("pk", "sk")
    ids = [f"o{i}" for i in range(105)]
    result = asyncio.run(client.cancel_orders(ids))
    assert [len(b["orderIds"]) for _, b in sent] == [100, 5]
    assert all(p == "/v1/pm/orders/batch" for p, _ in sent)
    assert result["cancelled"] == 105 and result["failed"] == []


def test_client_cancel_orders_reports_per_item_failures(monkeypatch):
    from client import BayseClient

    async def fake_delete(self, path, body=None):
        return {"results": [{"orderId": "o1", "success": True},
                            {"orderId": "o2", "success": False, "error": {"code": "NOT_FOUND"}}]}

    monkeypatch.setattr(BayseClient, "_delete", fake_delete)
    result = asyncio.run(BayseClient("pk", "sk").cancel_orders(["o1", "o2"]))
    assert result["cancelled"] == 1
    assert result["failed"] == ["o2"]


# ── Bot wiring: mark, startup sweep, drift check ─────────────────────────────

class _Risk:
    def __init__(self, positions):
        self.open_positions = positions


class _ExchangeClient:
    def __init__(self, portfolio_rows=None, open_orders=None, cancel_failed=None):
        self.portfolio_rows = portfolio_rows
        self.open_orders = open_orders or []
        self.cancel_failed = cancel_failed or []
        self.portfolio_calls = 0
        self.cancelled = []

    async def get_portfolio(self):
        self.portfolio_calls += 1
        if self.portfolio_rows is None:
            raise AssertionError("portfolio must not be read")
        return {"outcomeBalances": self.portfolio_rows}

    async def list_open_orders(self):
        return self.open_orders

    async def cancel_orders(self, ids):
        self.cancelled.extend(ids)
        return {"cancelled": len(ids) - len(self.cancel_failed),
                "failed": list(self.cancel_failed)}


def test_bot_book_value_reads_portfolio_only_when_a_fill_exists():
    import bot
    bot._portfolio_cache = ops.PortfolioCache(ttl_seconds=10.0)
    unfilled = _Risk({"m1": {"market_id": "m1", "outcome_id": "y", "amount_ngn": 250.0}})
    client = _ExchangeClient(portfolio_rows=None)  # would raise if read
    assert asyncio.run(bot._book_value("c1", client, unfilled)) == pytest.approx(250.0)
    assert client.portfolio_calls == 0

    filled = _Risk({"m1": _pos("y", 10.0)})
    client = _ExchangeClient(portfolio_rows=[_row("y", 10.0, sell_price=0.6)])
    value = asyncio.run(bot._book_value("c2", client, filled))
    assert value == pytest.approx(10.0 * 0.6 * 100)


def test_bot_startup_sweep_cancels_only_untracked_orders():
    import bot
    open_orders = [{"id": "tracked-1"}, {"id": "orphan-1"}]
    risk = _Risk({"m1": {"order_id": "tracked-1", "outcome_id": "y"}})
    client = _ExchangeClient(open_orders=open_orders)
    original = bot.database.get_all_unresolved
    bot.database.get_all_unresolved = lambda chat_id: []
    try:
        cancelled = asyncio.run(bot._startup_order_sweep("c1", client, risk))
    finally:
        bot.database.get_all_unresolved = original
    assert client.cancelled == ["orphan-1"]
    assert cancelled == 1


def test_bot_startup_sweep_raises_when_any_cancel_fails():
    import bot
    client = _ExchangeClient(open_orders=[{"id": "o1"}, {"id": "o2"}], cancel_failed=["o2"])
    original = bot.database.get_all_unresolved
    bot.database.get_all_unresolved = lambda chat_id: []
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(bot._startup_order_sweep("c1", client, _Risk({})))
    finally:
        bot.database.get_all_unresolved = original


def test_bot_drift_check_pauses_only_on_second_consecutive_mismatch(monkeypatch):
    import bot
    bot._drift_gates.clear()
    bot._portfolio_cache = ops.PortfolioCache(ttl_seconds=10.0)
    paused = []

    async def fake_pause(chat_id, settings, risk, pos, order_id, reason, **kw):
        paused.append(reason)

    monkeypatch.setattr(bot, "_pause_position_reconciliation", fake_pause)
    risk = _Risk({"m1": _pos("y", 10.0, order_id="o1")})
    client = _ExchangeClient(portfolio_rows=[_row("y", 7.0)])

    asyncio.run(bot._check_position_drift("c-drift", client, {}, risk))
    assert paused == [], "first mismatch may be an in-flight sell; do not pause"
    asyncio.run(bot._check_position_drift("c-drift", client, {}, risk))
    assert len(paused) == 1 and "position drift on y" in paused[0]
