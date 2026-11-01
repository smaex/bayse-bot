"""Supabase exposure, DB round-trip, and log volume regressions (2026-11-01 audit).

Production evidence (bayse-bot project, read-only queries):

* RLS was OFF on every public table and the ``anon`` / ``authenticated`` roles
  held INSERT/UPDATE/DELETE/TRUNCATE on ``users`` (encrypted API keys). The
  bot connects as ``postgres`` (table owner), so enabling RLS and revoking the
  public roles does not affect it. ``init_db`` must do the same for any table it
  creates, because Supabase default privileges re-grant new tables to anon.
* ``_cx`` issued ``SELECT 1`` on every checkout, roughly 9.4M times since June.
  A ping is only needed when a pooled connection has been idle for a while.
* Per-tick SKIP / EV / DRY RUN / PAUSED lines at INFO repeat for every user on
  every price tick, and the daily-PnL error repeats every 5 s during an outage.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager

import pytest

import database


# ── 1. init_db locks down the tables it creates ───────────────────────────────

class _RecordingCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append(" ".join(sql.split()))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _RecordingConn:
    def __init__(self, log):
        self.log = log

    def cursor(self, *a, **kw):
        return _RecordingCursor(self.log)

    def commit(self):
        self.log.append("COMMIT")

    def rollback(self):
        self.log.append("ROLLBACK")


def test_init_db_enables_rls_and_revokes_public_roles_on_trade_pnl_events(monkeypatch):
    statements: list[str] = []
    conn = _RecordingConn(statements)

    @contextmanager
    def fake_cx():
        yield conn

    monkeypatch.setattr(database, "_init_pool", lambda: None)
    monkeypatch.setattr(database, "_cx", fake_cx)

    database.init_db()

    joined = "\n".join(statements)
    assert re.search(
        r"ALTER TABLE (IF EXISTS )?(public\.)?trade_pnl_events ENABLE ROW LEVEL SECURITY",
        joined,
    ), "init_db must enable RLS on trade_pnl_events"
    assert re.search(
        r"REVOKE ALL ON (public\.)?trade_pnl_events FROM anon, authenticated",
        joined,
    ), "init_db must revoke anon/authenticated access on trade_pnl_events"


def test_init_db_revoke_is_guarded_for_plain_postgres(monkeypatch):
    """Plain Postgres has no anon/authenticated roles; the REVOKE must not crash."""
    statements: list[str] = []
    conn = _RecordingConn(statements)

    @contextmanager
    def fake_cx():
        yield conn

    monkeypatch.setattr(database, "_init_pool", lambda: None)
    monkeypatch.setattr(database, "_cx", fake_cx)
    database.init_db()

    revoke = [s for s in statements if "REVOKE ALL ON" in s and "trade_pnl_events" in s]
    assert revoke, "expected a REVOKE for trade_pnl_events"
    assert all("pg_roles" in s or "DO $$" in s for s in revoke), (
        "REVOKE on anon/authenticated must be wrapped in a role-existence guard"
    )


# ── 2. _cx pings only after the connection has been idle ──────────────────────

class _PingCountingCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if " ".join(sql.split()) == "SELECT 1":
            self.conn.pings += 1


class _PingCountingConn:
    def __init__(self):
        self.pings = 0
        self.commits = 0

    def cursor(self, *a, **kw):
        return _PingCountingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def getconn(self):
        return self.conn

    def putconn(self, conn, close=False):
        pass


def test_cx_pings_only_when_connection_has_been_idle(monkeypatch):
    conn = _PingCountingConn()
    clock = {"now": 1000.0}
    monkeypatch.setattr(database, "_pool", _FakePool(conn))
    monkeypatch.setattr(database, "_CONN_LAST_USED", {}, raising=False)
    monkeypatch.setattr(database.time, "monotonic", lambda: clock["now"])

    with database._cx():
        pass  # first checkout of an unknown connection: validate it
    assert conn.pings == 1

    for _ in range(50):  # busy loop well inside the idle window
        clock["now"] += 0.1
        with database._cx():
            pass
    assert conn.pings == 1, "recently used connection must not be pinged per checkout"

    clock["now"] += database.PING_IDLE_SECONDS + 1
    with database._cx():
        pass
    assert conn.pings == 2, "connection idle past the threshold must be validated again"


# ── 3. Repeated per-tick INFO lines are collapsed inside a window ─────────────

from log_throttle import RepeatLogFilter, Throttle, attach  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _record(name, level, msg):
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def test_repeat_filter_collapses_repeated_skip_lines_and_reports_count():
    clock = _Clock()
    f = RepeatLogFilter(window_seconds=60, clock=clock)

    first = _record("executor", logging.INFO, "[111] SKIP MAKER BTC — passive orders require CLOB")
    assert f.filter(first) is True

    for n in range(20):
        rec = _record(
            "executor", logging.INFO,
            f"[{200 + n}] SKIP MAKER BTC — passive orders require CLOB",
        )
        assert f.filter(rec) is False, "same normalised line inside the window must be dropped"

    clock.now = 61.0
    again = _record("executor", logging.INFO, "[111] SKIP MAKER BTC — passive orders require CLOB")
    assert f.filter(again) is True
    assert "20 similar" in again.getMessage()


@pytest.mark.parametrize("msg", [
    "[1] EV +1.2% too low at size ₦500 (price=0.410). Scaling down...",
    "[1] DRY RUN taker BTC — LIVE_TRADING=false",
    "[1] Reconnected | mode=live | PAUSED — notified",
    "[1] SKIP taker BTC — systemic halt active",
])
def test_repeat_filter_covers_every_noisy_family(msg):
    clock = _Clock()
    f = RepeatLogFilter(window_seconds=60, clock=clock)
    assert f.filter(_record("strategies.taker", logging.INFO, msg)) is True
    assert f.filter(_record("strategies.taker", logging.INFO, msg)) is False


def test_repeat_filter_never_hides_warnings_errors_or_unrelated_info():
    clock = _Clock()
    f = RepeatLogFilter(window_seconds=60, clock=clock)
    err = "[1] SKIP trade — invalid numeric inputs: ['x']"
    for _ in range(3):
        assert f.filter(_record("executor", logging.ERROR, err)) is True
    for _ in range(3):
        assert f.filter(_record("bot", logging.INFO, "Supabase connection pool ready")) is True


def test_attach_installs_filter_on_handler():
    handler = logging.StreamHandler()
    attach(handler)
    assert any(isinstance(x, RepeatLogFilter) for x in handler.filters)


# ── 4. Error paths log once per interval per key ──────────────────────────────

def test_throttle_allows_once_per_interval_per_key():
    clock = _Clock()
    t = Throttle(interval_seconds=60, clock=clock)
    assert t.allow("chat-a") is True
    assert t.allow("chat-a") is False
    assert t.allow("chat-b") is True, "throttle is per key"
    clock.now = 59.9
    assert t.allow("chat-a") is False
    clock.now = 60.1
    assert t.allow("chat-a") is True
