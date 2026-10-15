"""Exercise Resume under the same module identity used by `python bot.py`."""
import asyncio
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import bot
import database
import executor
import telegram_bot
from risk import RiskManager


def test_script_entrypoint_resume_clears_the_live_risk_manager():
    # Isolate sys.modules: tests normally import bot, hiding this production bug.
    script = r'''
import asyncio, copy, runpy, sys
real_run = asyncio.run

def check(main_coro):
    runtime = main_coro.cr_frame.f_globals
    main_coro.close()  # Do not start network tasks or connect to production.
    import bot, database, telegram_bot
    from risk import RiskManager
    assert bot.__dict__ is runtime, "Resume imported a second bot module"
    cid = "runtime-resume"
    risk = RiskManager()
    risk.paused = True
    risk.pause_reason = "daily_loss_limit"
    risk.current_free_cash = 741.74
    runtime["_user_risks"][cid] = risk
    runtime["_user_daily"][cid] = {"date": bot._session_date(), "start_balance": 1000}
    telegram_bot.inject(runtime["_user_clients"], runtime["_user_risks"],
                        runtime["_user_daily"], [], None)
    class Client:
        async def get_balance_ngn(self): return 741.74
    runtime["_user_clients"][cid] = Client()
    saved = {"paused": True, "paused_reason": "daily_loss_limit", "risk_pct": 2}
    database.get_user = lambda *a, **k: {"settings": copy.deepcopy(saved)}
    database.update_settings = lambda cid, s: (saved.clear(), saved.update(copy.deepcopy(s)))
    database.invalidate_user_cache = lambda *a: None
    database.get_daily_resolved_pnl = lambda *a: -50.0
    text = real_run(telegram_bot._apply_resume(cid))
    assert "Trading resumed" in text
    assert "Entry orders are still blocked" in text
    assert not risk.paused and not risk.pause_reason
    assert not bot._risk_pause_active(risk)
    assert runtime["_user_daily"][cid]["pnl_baseline"] == -50.0
    assert not saved["paused"]
    assert runtime["_active_users_cache_time"] == 0.0
asyncio.run = check
runpy.run_path("bot.py", run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-c", script],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("equity,risk_pct,budget", [
    (741.74, 1, 7.4174), (741.74, 2, 14.8348), (10000, 1, 100),
])
def test_resume_reports_sizing_without_expanding_risk(monkeypatch, equity, risk_pct, budget):
    cid = "resume-budget"
    risk = RiskManager()
    risk.current_free_cash = equity
    monkeypatch.setitem(bot._user_risks, cid, risk)
    monkeypatch.setitem(telegram_bot._user_risks, cid, risk)
    monkeypatch.setattr(executor.config, "MAX_TRADE_RISK", 0.02)
    monkeypatch.setattr(database, "get_user", lambda *a, **k: {
        "settings": {"paused": True, "risk_pct": risk_pct}})
    monkeypatch.setattr(database, "update_settings", lambda *a: None)
    monkeypatch.setattr(database, "invalidate_user_cache", lambda *a: None)
    monkeypatch.setattr(database, "get_daily_resolved_pnl", lambda *a: 0)
    summary = bot.reset_session_restrictions(cid, current_equity=equity)
    assert summary["order_budget"] == pytest.approx(budget)
    assert summary["minimum_order"] == 100
    assert not risk.paused


def test_learner_patch_preserves_a_concurrent_resume(monkeypatch):
    stored = {"paused": False, "session_reset_at": "new-session",
              "daily_state": {"pnl_baseline": -50}, "risk_pct": 1}
    queries = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params):
            queries.append(sql)
            # Model the top-level merge performed by PostgreSQL.
            assert "::jsonb || %s::jsonb" in sql
            stored.update(json.loads(params[0]))
        def fetchone(self): return {"settings": json.dumps(stored)}

    class Connection:
        def cursor(self, **kwargs): return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(database, "_cx", connection)
    monkeypatch.setattr(database, "_USER_CACHE", {"c": (0, {})})
    monkeypatch.setattr(database, "_ACTIVE_USERS_CACHE", (0, []))
    result = database.merge_settings("c", {"learned": {"size_multipliers": {"TAKER": 0.5}}})
    assert result["paused"] is False
    assert result["session_reset_at"] == "new-session"
    assert result["daily_state"]["pnl_baseline"] == -50
    assert result["learned"]["size_multipliers"]["TAKER"] == 0.5
    assert "c" not in database._USER_CACHE
    assert database._ACTIVE_USERS_CACHE is None
    assert len(queries) == 1


def test_status_names_a_live_pause_even_before_it_is_persisted(monkeypatch):
    cid = "live-pause-status"
    risk = RiskManager()
    risk.paused = True
    risk.pause_reason = "order_reconciliation"

    class Client:
        async def get_balance_ngn(self): return 741.74

    monkeypatch.setitem(telegram_bot._user_clients, cid, Client())
    monkeypatch.setitem(telegram_bot._user_risks, cid, risk)
    monkeypatch.setattr(telegram_bot, "_safe_get_user", lambda cid: {
        "settings": {"paused": False}})
    monkeypatch.setattr(database, "get_daily_resolved_pnl", lambda *a: 0)
    monkeypatch.setattr(database, "all_time_stats", lambda *a: {
        "wins": 0, "total": 0, "win_rate": 0, "total_pnl": 0})
    text = asyncio.run(telegram_bot._status_text(cid))
    assert "Status: ⏸ Paused" in text
    assert "Pause reason: `order_reconciliation`" in text
