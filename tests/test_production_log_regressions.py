"""Regression coverage for the production log's stale-fill and resume symptoms."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import bot
import config
import database
import stall
import telegram_bot
from risk import RiskManager


def test_a_historic_fill_does_not_make_a_950_minute_drought_healthy(monkeypatch):
    chat = "u-fill-drought"
    stall.reset(chat)
    monkeypatch.setattr(config, "TRADE_STALL_ALERT_MIN", 120.0)

    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=10, in_scope=3, evaluated=1, signals=1)
    stall.note_signal(chat, "MAKER", "BTC")
    stall.note_order(chat, "MAKER", placed=True, reason="clob_limit_resting")
    stall.note_trade(chat, market_id="btc-15m")

    now = time.time()
    state = stall._users[chat]
    state["last_trade"] = now - 950 * 60
    state["last_order_placed"] = now

    verdict = stall.verdict(chat, now=now, eval_max_age_sec=10**9)
    assert verdict["code"] == "FILL_DROUGHT"
    assert "950 min ago" in verdict["headline"]
    assert "no later fill is confirmed" in verdict["detail"]
    assert "recording fills" not in verdict["headline"]


def test_resume_uses_fresh_equity_before_lifting_the_pause(monkeypatch):
    chat = "u-resume-balance-race"
    today = bot._session_date()
    saved: dict = {}

    class Client:
        async def get_balance_ngn(self):
            return 1_780.75

    risk = RiskManager()
    # Reproduce /resume arriving before the first user-loop balance refresh.
    risk.current_free_cash = 0.0
    risk.paused = True
    risk.peak_balance = 1_798.75
    monkeypatch.setitem(telegram_bot._user_clients, chat, Client())
    monkeypatch.setitem(telegram_bot._user_risks, chat, risk)
    monkeypatch.setitem(bot._user_risks, chat, risk)

    monkeypatch.setattr(
        database,
        "get_user",
        lambda cid, force_fresh=False: {
            "chat_id": cid,
            "settings": {
                "paused": True,
                "paused_reason": "daily_loss_limit",
                "daily_state": {"date": today, "start_balance": 0.0, "target_hit": True},
            },
        },
    )
    monkeypatch.setattr(database, "get_daily_resolved_pnl", lambda *args, **kwargs: -18.06)
    monkeypatch.setattr(database, "update_settings", lambda cid, settings: saved.update(settings))
    monkeypatch.setattr(database, "invalidate_user_cache", lambda cid=None: None)

    text = asyncio.run(telegram_bot._apply_resume(chat))

    day = saved["daily_state"]
    assert saved["paused"] is False
    assert day["start_balance"] == 1_780.75
    assert day["pnl_baseline"] == -18.06
    assert risk.current_free_cash == 1_780.75
    assert risk.peak_balance == 1_780.75
    assert "Trading resumed" in text
    assert "Equity baseline: ₦1,780.75" in text

    session_pnl = bot._session_pnl_for_day(-18.06, day)
    loss_limit = day["start_balance"] * config.DEFAULT_DAILY_LOSS_LIMIT_PCT / 100
    assert session_pnl == 0.0
    assert session_pnl > -loss_limit


def test_legacy_zero_daily_baseline_is_repaired_without_clearing_a_pause(monkeypatch):
    chat = "u-repair-old-baseline"
    today = "2026-10-06"
    settings = {
        "paused": True,
        "paused_reason": "daily_loss_limit",
        "daily_state": {
            "date": today,
            "start_balance": 0.0,
            "target_hit": True,
            "pnl_baseline": -18.06,
        },
    }
    saved: dict = {}
    monkeypatch.setattr(bot, "_session_date", lambda: today)
    monkeypatch.setattr(database, "update_settings", lambda cid, value: saved.update(value))
    monkeypatch.setitem(bot._user_daily, chat, dict(settings["daily_state"]))

    async def advance():
        day, resumed = bot._advance_trading_day(chat, 1_780.75, settings)
        await asyncio.sleep(0.01)  # allow the persisted settings task to finish
        return day, resumed

    day, resumed = asyncio.run(advance())

    assert day["start_balance"] == 1_780.75
    assert day["pnl_baseline"] == -18.06
    assert day["target_hit"] is False
    assert settings["paused"] is True
    assert settings["paused_reason"] == "daily_loss_limit"
    assert resumed == ""
    assert saved["daily_state"]["start_balance"] == 1_780.75


def test_resume_refuses_to_persist_a_zero_equity_baseline(monkeypatch):
    chat = "u-no-balance-resume"
    saved = []

    class Client:
        async def get_balance_ngn(self):
            raise RuntimeError("exchange unavailable")

    risk = RiskManager()
    risk.current_free_cash = 0.0
    risk.paused = True
    monkeypatch.setitem(telegram_bot._user_clients, chat, Client())
    monkeypatch.setitem(telegram_bot._user_risks, chat, risk)
    monkeypatch.setitem(bot._user_risks, chat, risk)
    monkeypatch.setattr(database, "update_settings", lambda *args: saved.append(args))

    text = asyncio.run(telegram_bot._apply_resume(chat))

    assert "was not resumed" in text
    assert risk.paused is True
    assert saved == []


def test_status_reports_realized_session_pnl_not_total_equity(monkeypatch):
    chat = "u-status-after-resume"
    today = datetime.now(ZoneInfo(config.TRADING_TIMEZONE)).date().isoformat()

    class Client:
        async def get_balance_ngn(self):
            return 1_780.75

    risk = RiskManager()
    risk.current_free_cash = 1_780.75
    risk.peak_balance = 1_798.75
    monkeypatch.setitem(telegram_bot._user_clients, chat, Client())
    monkeypatch.setitem(telegram_bot._user_risks, chat, risk)
    monkeypatch.setitem(
        telegram_bot._user_daily,
        chat,
        {"date": today, "start_balance": 1_780.75, "pnl_baseline": -18.06},
    )
    monkeypatch.setattr(
        telegram_bot,
        "_safe_get_user",
        lambda cid: {
            "settings": {
                "daily_multiplier": 3,
                "daily_state": {
                    "date": today,
                    "start_balance": 1_780.75,
                    "pnl_baseline": -18.06,
                },
                "paused": True,
                "mode": "custom",
            }
        },
    )
    monkeypatch.setattr(database, "get_daily_resolved_pnl", lambda *args, **kwargs: -18.06)
    monkeypatch.setattr(
        database,
        "all_time_stats",
        lambda cid: {"wins": 307, "total": 1700, "win_rate": 0.18, "total_pnl": -3925.0},
    )

    text = asyncio.run(telegram_bot._status_text(chat))
    assert "Today's realized PnL: ₦+0.00" in text
    assert "Today's realized PnL: ₦+1,780.75" not in text
    assert "Total equity: ₦1,780.75" in text


def test_closed_filled_position_stays_in_risk_equity_until_settled(monkeypatch):
    chat = "u-awaiting-settlement"
    risk = RiskManager()
    key = "btc-old:NO:order-1"
    risk.add_position(key, {
        "market_id": "btc-old",
        "order_id": "order-1",
        "outcome_id": "no-token",
        "outcome": "NO",
        "strategy": "MAKER",
        "asset": "BTC",
        "timeframe": "15min",
        "entry_price": 0.55,
        "amount_ngn": 100.0,
        "filled_quantity": 1.81,
        "confirmed_filled": True,
        "placed_at": time.time() - 1_000,
    })
    monkeypatch.setattr(bot, "active_markets", [])
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda asset: (None, 0.0))

    class Client:
        async def get_orderbooks(self, outcome_ids, depth=5):
            return {}

    asyncio.run(bot._evaluate_and_exit_positions(chat, Client(), risk, {}))

    assert key in risk.open_positions
    assert risk.open_positions[key]["awaiting_settlement"] is True
    assert risk.deployed() == 100.0
    assert risk.deployed_filled() == 100.0
