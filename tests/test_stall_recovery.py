"""A quiet bot must be *explainable*, and a safety stop must expire when it is
supposed to. These tests cover both halves of that promise:

* the trading-day rollover lifts day-scoped safety pauses even while the account
  is paused (the bug that could keep an account dark indefinitely), but never a
  manual pause;
* the stall telemetry classifies the reason instead of guessing;
* the drought watchdog reports the reason and touches nothing else;
* the live gate stack is still satisfiable, so "no trades" can never quietly
  become "no trades are possible".
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path

import pytest

import bot
import config
import stall
from risk import RiskManager
from strategies.snipe import SnipeStrategy
from strategies.base import global_state

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_stall_state():
    stall.reset()
    yield
    stall.reset()


def _favorable_market(**overrides) -> dict:
    """A SOL 15-minute market where the model and the tape genuinely agree there is edge."""
    market = {
        "market_id": "mkt-favorable-1",
        "event_id": "evt-1",
        "asset": "SOL",
        "timeframe": "15min",
        "title": "SOL above $200 at close?",
        "threshold": 200.0,
        "yes_price": 0.60,
        "no_price": 0.40,
        "yes_id": "yes-1",
        "no_id": "no-1",
        "fee_rate": 0.02,
        "status": "open",
        "engine": "CLOB",
        "secs_to_close": 300,
        "minimum_order_amount": 100.0,
        "closing_date": "2026-01-01T00:00:00Z",
    }
    market.update(overrides)
    return market


# ── the stuck-pause regression ───────────────────────────────────────────────

def _roll(chat_id: str, equity: float, settings: dict, risks: dict) -> tuple[dict, str]:
    async def run():
        result = bot._advance_trading_day(chat_id, equity, settings)
        await asyncio.sleep(0)   # let the persistence task be created
        await asyncio.sleep(0)
        return result

    previous_risks = bot._user_risks
    bot._user_risks = risks
    try:
        return asyncio.run(run())
    finally:
        bot._user_risks = previous_risks


def test_new_trading_day_releases_a_daily_loss_pause(monkeypatch):
    """One losing day must not be able to stop a live account forever."""
    saved: dict[str, dict] = {}
    monkeypatch.setattr(bot.database, "update_settings",
                        lambda cid, s: saved.setdefault(cid, dict(s)))

    chat = "u-dayloss"
    bot._user_daily[chat] = {"date": "2000-01-01", "start_balance": 1_000.0, "target_hit": True}
    risk = RiskManager()
    risk.paused = True
    risk.peak_balance = 1_200.0
    settings = {
        "paused": True,
        "paused_reason": "daily_loss_limit",
        "daily_state": {"date": "2000-01-01", "start_balance": 900.0, "target_hit": True},
    }

    day, resumed = _roll(chat, 1_000.0, settings, {chat: risk})

    assert resumed == "daily_loss_limit"
    assert settings["paused"] is False
    assert "paused_reason" not in settings
    assert risk.paused is False, "in-memory drawdown/pause flag survived the rollover"
    assert day["date"] == bot._session_date()
    assert saved[chat]["paused"] is False, "the resume was never persisted"


def test_new_trading_day_releases_a_drawdown_pause(monkeypatch):
    monkeypatch.setattr(bot.database, "update_settings", lambda cid, s: None)
    chat = "u-drawdown"
    bot._user_daily[chat] = {"date": "2000-01-01"}
    risk = RiskManager()
    risk.paused = True
    settings = {"paused": True, "paused_reason": "drawdown",
                "daily_state": {"date": "2000-01-01", "target_hit": False}}

    _, resumed = _roll(chat, 800.0, settings, {chat: risk})
    assert resumed == "drawdown"
    assert settings.get("paused") is False


def test_manual_pause_is_never_auto_resumed(monkeypatch):
    monkeypatch.setattr(bot.database, "update_settings", lambda cid, s: None)
    chat = "u-manual"
    bot._user_daily[chat] = {"date": "2000-01-01"}
    settings = {"paused": True, "paused_reason": "manual",
                "daily_state": {"date": "2000-01-01", "target_hit": False}}

    _, resumed = _roll(chat, 1_000.0, settings, {chat: RiskManager()})
    assert resumed == ""
    assert settings["paused"] is True, "an operator's /pause must survive a day boundary"


def test_unattributed_pause_is_left_alone(monkeypatch):
    """A pause whose reason we cannot prove is treated as manual, not expired."""
    monkeypatch.setattr(bot.database, "update_settings", lambda cid, s: None)
    chat = "u-unknown"
    bot._user_daily[chat] = {"date": "2000-01-01"}
    settings = {"paused": True, "daily_state": {"date": "2000-01-01"}}

    _, resumed = _roll(chat, 1_000.0, settings, {chat: RiskManager()})
    assert resumed == ""
    assert settings["paused"] is True


def test_rollover_expires_only_the_in_memory_drawdown_flag(monkeypatch):
    """`risk.paused` without a persisted pause used to block every evaluation
    with nothing to point at. It now expires with the trading day."""
    monkeypatch.setattr(bot.database, "update_settings", lambda cid, s: None)
    chat = "u-inmem"
    bot._user_daily[chat] = {"date": "2000-01-01"}
    risk = RiskManager()
    risk.paused = True
    settings = {"paused": False, "daily_state": {"date": "2000-01-01"}}

    _roll(chat, 1_000.0, settings, {chat: risk})
    assert risk.paused is False
    assert settings["paused"] is False


def test_same_day_rollover_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(bot.database, "update_settings", lambda cid, s: calls.append(cid))
    chat = "u-same-day"
    today = bot._session_date()
    bot._user_daily[chat] = {"date": today, "start_balance": 1_000.0, "target_hit": False}
    settings = {"daily_state": {"date": today}}

    _roll(chat, 1_000.0, settings, {})
    _roll(chat, 1_000.0, settings, {})
    assert calls == [], "an intra-day cycle must not rewrite settings twice"


def test_user_loop_rolls_the_day_before_the_paused_gate():
    """Structural guard: the release code has to run before the gate that skips it.

    The bug was not a wrong condition — it was a right condition in the wrong
    place, so a behavioural test alone would let it move back.
    """
    source = inspect.getsource(bot._user_loop)
    roll_at = source.index("_roll_trading_day(")
    gate_at = source.index('if settings.get("paused"):')
    assert roll_at < gate_at


# ── stall classification ─────────────────────────────────────────────────────

def test_verdict_names_dry_run_as_the_reason():
    chat = "u-dry"
    stall.note_state(chat, paused=False, dry_run=True)
    stall.note_evaluation(chat, markets_total=6, in_scope=6, evaluated=6, signals=3)
    data = stall.verdict(chat)
    assert data["code"] == "DRY_RUN"
    assert "LIVE_TRADING" in data["headline"]


def test_verdict_distinguishes_session_and_manual_pauses():
    stall.note_state("u-p1", paused=True, paused_reason="daily_target")
    assert stall.verdict("u-p1")["code"] == "PAUSED_SESSION"
    stall.note_state("u-p2", paused=True, paused_reason="manual")
    assert stall.verdict("u-p2")["code"] == "PAUSED_MANUAL"


def test_verdict_reports_the_gates_when_a_candidate_never_qualifies():
    chat = "u-edge"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=8, in_scope=8, evaluated=8, signals=0)
    for _ in range(5):
        stall.reject(chat, "SNIPE", "shrunk_edge_below_requirement", "2.1% < 3%")
    data = stall.verdict(chat)
    assert data["code"] == "NO_EDGE"
    assert "shrunk_edge_below_requirement" in data["detail"]
    # Explicitly refuses the tempting "fix".
    assert "not a fix" in data["action"]


def test_verdict_flags_signals_that_never_become_orders():
    chat = "u-exec"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=4, in_scope=4, evaluated=4, signals=2)
    stall.note_signal(chat, "SNIPE", "SOL")
    stall.note_order(chat, "SNIPE", placed=False, reason="risk_budget_below_platform_minimum")
    data = stall.verdict(chat)
    assert data["code"] == "EXECUTION_BLOCKED"
    assert "risk_budget_below_platform_minimum" in data["detail"]


def test_verdict_flags_missing_markets_and_stale_feeds():
    chat = "u-nomarkets"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=0, in_scope=0, evaluated=0, signals=0)
    stall.note_scan(0)
    assert stall.verdict(chat)["code"] == "NO_MARKETS"

    chat2 = "u-stale"
    stall.note_state(chat2, paused=False, dry_run=False)
    stall.note_evaluation(chat2, markets_total=6, in_scope=6, evaluated=0, signals=0,
                          skips={"stale_feed": 6})
    assert stall.verdict(chat2, feed_age_sec=900.0)["code"] == "FEEDS_STALE"


def test_verdict_flags_low_balance(monkeypatch):
    chat = "u-poor"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=6, in_scope=6, evaluated=6, signals=1)
    data = stall.verdict(chat, equity=250.0, min_viable=500.0)
    assert data["code"] == "LOW_BALANCE"
    assert "500" in data["headline"]


def test_reject_counters_are_bounded():
    chat = "u-bounded"
    for i in range(200):
        stall.reject(chat, "SNIPE", f"code_{i}", "detail")
    report = stall.report(chat)
    assert len(report["top_rejects"]) <= 6
    # The bucket itself stays bounded so a long-running process cannot grow
    # memory through gate codes alone.
    state = stall._users[chat]
    assert len(state["rejects"]) <= stall._MAX_CODES


def test_alerts_are_rate_limited_per_verdict_code():
    chat = "u-alert"
    assert stall.note_alert(chat, "NO_EDGE", now=1_000.0) is True
    assert stall.note_alert(chat, "NO_EDGE", now=1_060.0) is False
    # A *different* explanation is always worth sending: the previous one was wrong.
    assert stall.note_alert(chat, "FEEDS_STALE", now=1_061.0) is True


def test_trade_clock_survives_a_restart_from_the_ledger():
    chat = "u-seed"
    stall.seed_last_trade(chat, time.time() - 6 * 3600)
    assert stall.trade_gap_minutes(chat) == pytest.approx(360.0, abs=1.0)
    # A later real order wins and cannot be overwritten by seeding.
    stall.note_trade(chat, market_id="mkt-1")
    assert stall.trade_gap_minutes(chat) < 1.0


# ── the watchdog ─────────────────────────────────────────────────────────────

class _Sent:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    async def send(self, app, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


def test_watchdog_alerts_with_the_reason_and_changes_nothing(monkeypatch):
    chat = "u-dark"
    sent = _Sent()
    monkeypatch.setattr(bot.telegram_bot, "send_message", sent.send)
    monkeypatch.setattr(bot, "_tg_app", object())
    monkeypatch.setattr(bot, "_active_users_cache", [
        {"chat_id": chat, "settings": {"paused": False, "assets": ["SOL"], "timeframes": ["15min"]}},
    ])
    bot._user_risks.setdefault(chat, RiskManager())
    # A three-hour-old last order with no evaluation since startup = stalled hard.
    stall.seed_last_trade(chat, time.time() - 3 * 3600)

    before = config.TRADE_STALL_ALERT_MIN
    config.TRADE_STALL_ALERT_MIN = 60.0
    try:
        asyncio.run(bot._check_trading_stalls())
    finally:
        config.TRADE_STALL_ALERT_MIN = before

    assert sent.messages, "the drought produced no alert"
    text = sent.messages[0][1]
    assert "Trading stall" in text
    assert "Code:" in text
    # And it must have been observation only.
    account = bot._active_users_cache[0]
    assert account["settings"]["paused"] is False
    assert not account["settings"].get("paused_reason")


def test_watchdog_stays_quiet_while_the_pipeline_is_working(monkeypatch):
    chat = "u-busy"
    sent = _Sent()
    monkeypatch.setattr(bot.telegram_bot, "send_message", sent.send)
    monkeypatch.setattr(bot, "_tg_app", object())
    monkeypatch.setattr(bot, "_active_users_cache", [{"chat_id": chat, "settings": {}}])
    bot._user_risks.setdefault(chat, RiskManager())

    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=6, in_scope=6, evaluated=6, signals=1)
    stall.note_trade(chat, market_id="mkt-now")

    asyncio.run(bot._check_trading_stalls())
    assert sent.messages == []


def test_watchdog_never_raises_when_a_feed_lookup_breaks(monkeypatch):
    monkeypatch.setattr(bot, "_active_users_cache", [{"chat_id": "u-x", "settings": {}}])
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price",
                        lambda asset: (_ for _ in ()).throw(RuntimeError("feed down")))
    asyncio.run(bot._check_trading_stalls())   # must not propagate


# ── the gate stack must stay satisfiable ─────────────────────────────────────

@pytest.mark.parametrize(
    "spot,expected_signal",
    [
        # Real edge the current model is allowed to see.
        (201.5, True),
        # Inside the calibration buffer: the rejection is correct, and it must be
        # counted rather than silently swallowed.
        (200.05, False),
    ],
)
def test_snipe_gates_are_satisfiable_and_counted(spot, expected_signal):
    import strategies

    chat = "u-live"
    learned = {
        "mode": "balanced",
        "strategies": ["SNIPE"],
        "certainty_multipliers": {},
        "size_multipliers": {},
        "snipe_min_certainty": config.SNIPE_MIN_CERTAINTY,
        "chat_id": chat,
    }
    signals = asyncio.run(
        strategies.evaluate_all(
            _favorable_market(), learned, global_state, spot_price=spot
        )
    )
    if expected_signal:
        assert signals, "no market can pass the live SNIPE gates — the strategy is dead, not cautious"
        sig = signals[0]
        assert sig.strategy == "SNIPE"
        assert sig.asset == "SOL" and sig.timeframe == "15min"
        assert 0.40 <= sig.market_price <= 0.65
        assert sig.win_prob > sig.market_price
        assert sig.size_pct > 0.0
    else:
        assert signals == []
        # The rejection must be attributed to a specific gate (any of the ones
        # that legitimately can reject a barely-off-threshold market), never
        # dropped on the floor.
        counters = stall._users[chat]["rejects"]
        assert counters, "a rejected candidate produced no gate counter"
        assert any(
            marker in code
            for code in counters
            for marker in ("distance", "no_raw_edge", "market_prices_unusable")
        ), f"unhelpful gate attribution: {list(counters)}"


def test_evaluate_markets_records_the_pass_for_the_right_reason(monkeypatch):
    """End-to-end wiring: evaluation → signals → executor, all attributed."""
    chat = "u-e2e"
    calls: list[str] = []

    class _Executor:
        async def execute_trade(self, cid, sig, client, risk, settings, equity, free_cash):
            calls.append(f"execute:{sig.strategy}")
            stall.note_order(cid, sig.strategy, placed=True, reason="test")
            stall.note_trade(cid, market_id=sig.market_id)

        async def execute_arb(self, *a, **k):
            calls.append("arb")

        async def execute_midmarket_maker(self, *a, **k):
            calls.append("midmarket")

    async def fake_evaluate_all(market, learned, state, spot_price=None):
        from strategies.base import TradeSignal

        return [TradeSignal(
            strategy="SNIPE", event_id=market["event_id"], market_id=market["market_id"],
            asset=market["asset"], timeframe=market["timeframe"], outcome="YES",
            outcome_id=market["yes_id"], certainty=0.35, win_prob=0.66,
            market_price=0.60, size_pct=0.02, reason="test signal",
        )]

    markets = [_favorable_market()]
    monkeypatch.setattr(bot, "active_markets", markets)
    monkeypatch.setattr(bot, "executor", _Executor())
    monkeypatch.setattr(bot.strategies, "evaluate_all", fake_evaluate_all)
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda asset: (201.5, time.time()))
    monkeypatch.setattr(bot.feeds, "spot", {"SOL": 201.5}, raising=False)
    monkeypatch.setattr(bot.feeds, "spot_updated_at", {"SOL": time.time()}, raising=False)

    ok = asyncio.run(bot._evaluate_markets(
        chat, {}, client=None, risk=RiskManager(), equity=5_000.0, free_cash=5_000.0,
        learned={"strategies": ["SNIPE"], "chat_id": chat}, max_exp=0.15,
        user_assets=["SOL"], user_tfs=["15min"],
    ))

    assert ok is True
    assert calls == ["execute:SNIPE"]
    report = stall.report(chat)
    assert report["markets_evaluated"] == 1
    assert report["signals"] == 1
    assert report["trades"] == 1
    assert report["verdict"]["code"] == "HEALTHY"


def test_why_command_is_reachable():
    """A diagnostic nobody can ask for is a log file, not an interface."""
    import telegram_bot

    # build_app validates the token format; no network is involved.
    telegram_bot.TELEGRAM_TOKEN = "123456:unit-test-token"
    app = telegram_bot.build_app()
    commands = {c for h in app.handlers[0] for c in getattr(h, "commands", ())}
    assert {"why", "whytrading"} <= commands, sorted(commands)
    assert hasattr(telegram_bot, "cmd_why")


def test_report_is_safe_to_serialise():
    """The dashboard endpoint exposes this structure; it must be JSON-clean."""
    import json

    stall.note_state("u-json", paused=False, dry_run=True)
    stall.reject("u-json", "MAKER", "engine_not_clob", "engine=AMM")
    payload = stall.report("u-json")
    json.dumps(payload)
    # Which of these it is depends on how much has been observed; what must hold
    # is that the answer is a named, serialisable state and not an exception.
    assert payload["verdict"]["code"] in {
        "DRY_RUN", "NO_EDGE", "HEALTHY", "NO_MARKETS", "NO_EVALUATION",
    }
    assert set(payload["verdict"]) >= {"code", "headline", "detail", "action", "severity"}
