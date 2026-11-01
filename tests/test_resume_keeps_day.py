"""/resume only lifts the pause (2026-11-01 decision: option 3).

The day's realised total, its target progress, the loss stop and the gain-lock
peak all keep counting from the start of the trading day. A pause and resume
cycle must not change any of them. The loss stop re-checks on the next cycle,
so a day still past the limit is paused again straight away.
"""

import pytest

import bot
import database
from risk import RiskManager


def _install(monkeypatch, *, day_state, realised, saved):
    base = {"paused": True, "paused_reason": "daily_loss_limit",
            "daily_state": dict(day_state)}

    # Seed the store with the starting settings, then read back what the last
    # write stored, as the database would.
    saved.update(base)

    def get_user(chat_id, force_fresh=False):
        return {"chat_id": chat_id, "settings": dict(saved)}

    monkeypatch.setattr(database, "get_user", get_user)
    monkeypatch.setattr(database, "get_daily_resolved_pnl", lambda *a, **k: realised)
    monkeypatch.setattr(database, "update_settings", lambda cid, s: saved.update(s))
    monkeypatch.setattr(database, "invalidate_user_cache", lambda cid=None: None)
    risk = RiskManager()
    risk.current_free_cash = 2_698.0
    risk.paused = True
    risk.peak_balance = 2_698.0
    monkeypatch.setitem(bot._user_risks, "c", risk)
    return risk


def test_repeated_pause_and_resume_never_changes_the_day(monkeypatch):
    saved = {}
    today = bot._session_date()
    day = {"date": today, "start_balance": 2_698.43, "target_hit": False,
           "pnl_baseline": 0.0, "peak_profit": 121.73}
    risk = _install(monkeypatch, day_state=day, realised=229.62, saved=saved)

    for _ in range(3):
        # Pause again, then resume, as the operator did at 1:12, 1:50 and later.
        saved["paused"] = True
        bot.reset_session_restrictions("c", "manual_resume")
        state = saved["daily_state"]
        assert saved["paused"] is False
        assert state["start_balance"] == 2_698.43
        assert state["peak_profit"] == 121.73
        assert state["pnl_baseline"] == 0.0
        assert bot._session_pnl_for_day(229.62, state) == 229.62
        assert risk.daily_realized_pnl == 229.62


def test_resume_keeps_the_day_total_shown_in_status_inputs(monkeypatch):
    saved = {}
    day = {"date": bot._session_date(), "start_balance": 2_698.43, "target_hit": False,
           "pnl_baseline": 0.0}
    risk = _install(monkeypatch, day_state=day, realised=229.62, saved=saved)
    bot.reset_session_restrictions("c", "manual_resume")
    # The loop and /status both read the real total, not a session figure.
    assert risk.daily_realized_pnl == 229.62


def test_resume_on_a_new_trading_day_starts_a_fresh_day(monkeypatch):
    saved = {}
    yesterday = "2000-01-01"
    day = {"date": yesterday, "start_balance": 1_000.0, "target_hit": True,
           "pnl_baseline": 0.0, "peak_profit": 500.0}
    _install(monkeypatch, day_state=day, realised=0.0, saved=saved)
    summary = bot.reset_session_restrictions("c", "manual_resume", current_equity=2_000.0)
    fresh = saved["daily_state"]
    assert fresh["date"] == bot._session_date()
    assert fresh["start_balance"] == 2_000.0
    assert "peak_profit" not in fresh, "the gain-lock peak restarts with the day"
    assert fresh["target_hit"] is False
    assert summary["loss_limit_reached"] is False


def test_resume_reports_when_the_loss_limit_is_still_reached(monkeypatch):
    saved = {}
    day = {"date": bot._session_date(), "start_balance": 1_000.0, "target_hit": False,
           "pnl_baseline": 0.0}
    _install(monkeypatch, day_state=day, realised=-50.0, saved=saved)
    summary = bot.reset_session_restrictions("c", "manual_resume", current_equity=950.0)
    assert summary["loss_limit_reached"] is True  # 3% of 1,000 is 30


def test_resume_within_the_loss_limit_is_not_flagged(monkeypatch):
    saved = {}
    day = {"date": bot._session_date(), "start_balance": 1_000.0, "target_hit": False,
           "pnl_baseline": 0.0}
    _install(monkeypatch, day_state=day, realised=-10.0, saved=saved)
    summary = bot.reset_session_restrictions("c", "manual_resume", current_equity=990.0)
    assert summary["loss_limit_reached"] is False
