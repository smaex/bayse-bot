"""The stall report must say what is true, and Telegram must be able to render it.

Reproduces the Telegram output an operator received on 2026-09-25 after
/resume, where every line of the report was misleading in a different way:

* the header said "1124 min without an order" directly above "5 orders placed";
* "5 still resting unfilled" counted every quote ever placed — they had all
  expired;
* the verdict flipped to COOLDOWN_BLOCKED because one MAKER re-quote hit the 60s
  cooldown, hiding the real finding (zero fills);
* the top "gates" were configuration exclusions recorded on every pass
  (scope:blocked_by_policy, MAKER:engine_not_clob,
  SNIPE:asset_not_in_allowed_scope);
* underscores in gate codes were eaten by Telegram's Markdown parser
  ("blockedbypolicy", "MAKERORDERTIMEOUT"), and an odd count rejects the
  message outright;
* every MAKER notification degraded to a one-line fallback reading
  "Cert: 100%".
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bot
import config
import stall
import telegram_bot
from risk import RiskManager


@pytest.fixture(autouse=True)
def _clean():
    stall.reset()
    yield
    stall.reset()


def legacy_markdown_error(text: str) -> str | None:
    """Minimal checker for Telegram's legacy ``parse_mode="Markdown"``.

    Rules (Bot API, "Markdown style"): ``\\`` escapes ``_ * ` [`` only
    *outside* entities; escaping inside an entity is not allowed; every
    ``_``/``*``/`````/```` ``` ```` entity must be closed; ``[`` starts a link.
    Returns a description of the first error, or None when Telegram would parse it.
    """
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] in "_*`[":
            i += 2
            continue
        if text.startswith("```", i):
            j = text.find("```", i + 3)
            if j < 0:
                return f"unterminated pre block at offset {i}"
            i = j + 3
            continue
        if c in "_*`":
            j = text.find(c, i + 1)       # escapes are NOT honoured inside an entity
            if j < 0:
                return f"can't find end of {c!r} entity starting at offset {i}"
            i = j + 1
            continue
        if c == "[":
            close = text.find("]", i + 1)
            if close < 0 or not text.startswith("(", close + 1) or text.find(")", close) < 0:
                return f"unescaped '[' at offset {i}"
            i = text.find(")", close) + 1
            continue
        i += 1
    return None


def test_markdown_checker_catches_the_production_notification_bug():
    # The pre-fix notify_trade tail: an escaped underscore inside an italic entity.
    assert legacy_markdown_error("_MAKER YES fv=0.750 spread\\_capture bid=0.580_") is not None
    assert legacy_markdown_error("scope:blocked\\_by\\_policy ×3") is None
    assert legacy_markdown_error("Code: `NO_CONFIRMED_FILL`") is None
    assert legacy_markdown_error("MAKER:too_close_to_settle") is not None


def _replay_production_stall(chat: str) -> None:
    """Counters shaped like the report pasted from Telegram."""
    stall.note_state(chat, paused=False, dry_run=False)
    stall.seed_last_trade(chat, time.time() - 1124 * 60)
    stall.note_evaluation(chat, markets_total=10, in_scope=4, evaluated=1, signals=0,
                          skips={"timeframe": 20943, "trigger": 10605})
    for _ in range(469):
        stall.reject(chat, "scope", "blocked_by_policy", "ARB,MIDMARKET_MAKER,PAIRED_SNIPER")
    for _ in range(306):
        stall.reject(chat, "MAKER", "engine_not_clob", "AMM")
        stall.reject(chat, "SNIPE", "asset_not_in_allowed_scope", "XAUUSD")
    for _ in range(120):
        stall.reject(chat, "SNIPE", "outside_entry_window", "secs=860")
    for _ in range(90):
        stall.reject(chat, "MAKER", "no_trend_or_edge_alignment", "edge_yes=-0.030")
    for _ in range(10):
        stall.note_signal(chat, "MAKER", "SOL")
    for _ in range(5):
        stall.note_order(chat, "MAKER", placed=True, reason="clob_limit_resting")
    for _ in range(3):  # MAKER re-quote signals inside the 60s window
        stall.note_order(chat, "MAKER", placed=False, reason="market_cooldown")


def test_production_report_names_the_real_problem():
    chat = "u-paste"
    _replay_production_stall(chat)

    data = stall.report(chat, resting_now=0)
    verdict = data["verdict"]
    # A cooldown hit is the echo of a placement, not why nothing fills.
    assert verdict["code"] == "NO_CONFIRMED_FILL", verdict
    assert "5 order(s) placed" in verdict["detail"]
    assert "0 resting on the book now" in verdict["detail"]
    assert "still resting" not in verdict["detail"]
    assert verdict["trade_gap_min"] == pytest.approx(1124, abs=1)

    gate_codes = [row["code"] for row in data["top_rejects"]]
    assert "SNIPE:outside_entry_window" in gate_codes
    for structural in ("scope:blocked_by_policy", "MAKER:engine_not_clob",
                       "SNIPE:asset_not_in_allowed_scope"):
        assert structural not in gate_codes
    assert {row["code"] for row in data["structural_rejects"]} >= {
        "scope:blocked_by_policy", "MAKER:engine_not_clob", "SNIPE:asset_not_in_allowed_scope",
    }


def test_production_report_renders_in_telegram_markdown():
    chat = "u-paste-md"
    _replay_production_stall(chat)

    text = stall.format_report(chat, markdown=True, resting_now=0)
    assert legacy_markdown_error(text) is None, text
    # Codes survive rendering instead of turning into "blockedbypolicy".
    assert "scope:blocked\\_by\\_policy" in text
    assert "Code: `NO_CONFIRMED_FILL`" in text
    assert "0 resting now" in text
    assert "(5 as passive quotes)" in text
    assert "evaluated in the last pass" in text
    assert "Excluded by configuration" in text

    plain = stall.format_report(chat, resting_now=0)
    assert "scope:blocked_by_policy" in plain and "\\_" not in plain


@pytest.mark.parametrize("code", [
    "too_close_to_settle", "shrunk_edge_below_requirement", "market_prices_unusable",
    "price_at_or_above_ev_ceiling", "lead_lag_btc_opposing_pump",
])
def test_any_gate_code_renders(code):
    chat = f"u-md-{code}"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=4, in_scope=4, evaluated=4, signals=0)
    stall.reject(chat, "SNIPE", code, "detail_with_underscores *and* `ticks` [x]")
    text = stall.format_report(chat, markdown=True)
    assert legacy_markdown_error(text) is None, text


def test_maker_book_skips_get_their_own_verdict():
    chat = "u-maker-book"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=4, in_scope=4, evaluated=4, signals=3)
    for _ in range(3):
        stall.note_signal(chat, "MAKER", "BTC")
    stall.note_order(chat, "MAKER", placed=False, reason="maker_quote_behind_book")
    stall.reject(chat, "exec", "maker_quote_behind_book",
                 "max bid 0.580 is 8 tick(s) under the best bid 0.660 / best ask 0.690")
    for _ in range(12):
        stall.note_order(chat, "MAKER", placed=False, reason="market_cooldown")

    verdict = stall.verdict(chat)
    assert verdict["code"] == "MAKER_QUOTE_UNCOMPETITIVE"
    assert verdict["severity"] == "warn"            # a policy boundary, not a fault
    assert "0.660" in verdict["detail"]
    assert "MAKER_MAX_BID" in verdict["action"]


def test_execution_blocked_detail_does_not_lead_with_the_cooldown():
    chat = "u-exec-cooldown"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=4, in_scope=4, evaluated=4, signals=2)
    stall.note_signal(chat, "SNIPE", "SOL")
    stall.note_order(chat, "SNIPE", placed=False, reason="clob_ask_above_cap")
    for _ in range(20):
        stall.note_order(chat, "SNIPE", placed=False, reason="market_cooldown")
    verdict = stall.verdict(chat)
    assert verdict["code"] == "EXECUTION_BLOCKED"
    assert verdict["detail"].startswith("Executor outcomes: exec:clob_ask_above_cap")


def test_a_stale_safety_valve_hit_does_not_name_todays_drought():
    chat = "u-stale-valve"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=4, in_scope=4, evaluated=4, signals=1)
    stall.note_signal(chat, "SNIPE", "SOL")
    stall.note_order(chat, "SNIPE", placed=True, reason="filled")
    stall.note_trade(chat, market_id="m-1")
    stall.note_order(chat, "SNIPE", placed=False, reason="exposure_cap")

    assert stall.verdict(chat)["code"] == "EXPOSURE_CAPPED"
    later = time.time() + stall.RECENT_WINDOW_SEC + 60
    assert stall.verdict(chat, now=later, eval_max_age_sec=10**9)["code"] == "HEALTHY"


def test_no_edge_falls_back_to_structural_codes_when_they_are_all_there_is():
    chat = "u-only-structural"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=4, in_scope=4, evaluated=4, signals=0)
    stall.reject(chat, "SNIPE", "asset_not_in_allowed_scope", "BTC")
    verdict = stall.verdict(chat)
    assert verdict["code"] == "NO_EDGE"
    assert "SNIPE:asset_not_in_allowed_scope" in verdict["detail"]


def test_resting_order_count_comes_from_the_risk_book():
    risk = RiskManager()
    risk.add_position("m1", {"order_id": "o1", "confirmed_filled": False, "filled_quantity": 0,
                             "strategy": "MAKER", "outcome": "YES", "entry_price": 0.58,
                             "amount_ngn": 100})
    risk.add_position("m2", {"order_id": "o2", "confirmed_filled": True, "filled_quantity": 2,
                             "strategy": "SNIPE", "outcome": "NO", "entry_price": 0.6,
                             "amount_ngn": 100})
    assert bot._resting_order_count(risk) == 1
    assert bot._resting_order_count(None) is None


# ── delivery ─────────────────────────────────────────────────────────────────

class _Sent:
    def __init__(self):
        self.messages: list[tuple[str, dict]] = []

    async def send(self, app, chat_id, text, **kwargs):
        self.messages.append((text, kwargs))


def test_watchdog_header_measures_fills_and_the_alert_parses(monkeypatch):
    chat = "u-watchdog-md"
    sent = _Sent()
    monkeypatch.setattr(bot.telegram_bot, "send_message", sent.send)
    monkeypatch.setattr(bot, "_tg_app", object())
    monkeypatch.setattr(bot, "_active_users_cache", [{"chat_id": chat, "settings": {}}])
    monkeypatch.setattr(config, "TRADE_STALL_ALERT_MIN", 60.0)
    monkeypatch.setitem(bot._user_risks, chat, RiskManager())
    _replay_production_stall(chat)

    asyncio.run(bot._check_trading_stalls())

    assert sent.messages, "the drought produced no alert"
    text, kwargs = sent.messages[0]
    assert "min without a confirmed fill" in text
    assert "without an order" not in text
    assert kwargs.get("parse_mode") == "Markdown"
    assert legacy_markdown_error(text) is None, text
    assert "0 resting now" in text


class _FakeBot:
    def __init__(self, reject_markdown: bool = False):
        self.reject_markdown = reject_markdown
        self.calls: list[tuple[str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append((text, kwargs))
        if kwargs.get("parse_mode") and (self.reject_markdown or legacy_markdown_error(text)):
            raise RuntimeError(
                "Bad Request: can't parse entities: Can't find end of the entity "
                "starting at byte offset 42"
            )


def test_send_message_retries_as_plain_text_instead_of_dropping_the_alert():
    app = SimpleNamespace(bot=_FakeBot(reject_markdown=True))
    asyncio.run(telegram_bot.send_message(
        app, "c", "🩺 *Trading stall*\n\nscope:blocked\\_by\\_policy", parse_mode="Markdown",
    ))
    assert len(app.bot.calls) == 2
    plain, kwargs = app.bot.calls[1]
    assert "parse_mode" not in kwargs
    assert plain == "🩺 Trading stall\n\nscope:blocked_by_policy"


def test_send_message_does_not_retry_unrelated_failures():
    class _Down(_FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            self.calls.append((text, kwargs))
            raise RuntimeError("Timed out")

    app = SimpleNamespace(bot=_Down())
    asyncio.run(telegram_bot.send_message(app, "c", "*x*", parse_mode="Markdown"))
    assert len(app.bot.calls) == 1


def _maker_sig(**overrides):
    from strategies.base import TradeSignal

    sig = TradeSignal(
        strategy="MAKER", event_id="e", market_id="m", asset="BTC", timeframe="15min",
        outcome="YES", outcome_id="y", certainty=0.95, win_prob=0.74, market_price=0.58,
        size_pct=0.02, reason="MAKER YES fv=0.740 spread_capture bid=0.580 | MULT(x1.20)",
    )
    for key, value in overrides.items():
        setattr(sig, key, value)
    return sig


def test_maker_notification_is_valid_markdown_and_says_it_is_not_a_fill():
    app = SimpleNamespace(bot=_FakeBot())
    asyncio.run(telegram_bot.notify_trade(app, "c", _maker_sig(), 100.0, engine="CLOB_LIMIT"))

    assert len(app.bot.calls) == 1, "Markdown was rejected and the fallback was used"
    text, kwargs = app.bot.calls[0]
    assert kwargs.get("parse_mode") == "Markdown"
    assert legacy_markdown_error(text) is None, text
    assert "`MAKER YES fv=0.740 spread_capture bid=0.580 | MULT(x1.20)`" in text
    assert "not filled yet" in text


def test_exchange_rejection_notice_is_valid_markdown():
    app = SimpleNamespace(bot=_FakeBot())
    asyncio.run(telegram_bot.notify_order_rejected(
        app, "c", "MAKER", "SOL", "15min", "NO", 100, reason="POST_ONLY_WOULD_CROSS",
    ))
    assert len(app.bot.calls) == 1
    assert legacy_markdown_error(app.bot.calls[0][0]) is None


def test_boosted_certainty_is_never_reported_as_100_percent(monkeypatch):
    import strategies
    from strategies.base import MarketState

    class _Maker:
        async def evaluate(self, market, learned, state, spot_price=None):
            return _maker_sig()

    monkeypatch.setattr(strategies, "_strategies", {"MAKER": _Maker()})
    monkeypatch.setattr(strategies.regime_controller, "get_multipliers",
                        lambda asset, state: {"SNIPE": 1.2, "TREND": 1.2})
    market = {"asset": "BTC", "market_id": "m", "timeframe": "15min",
              "yes_price": 0.66, "no_price": 0.34}
    signals = asyncio.run(strategies.evaluate_all(
        market, {"strategies": ["MAKER"], "mode": "balanced"}, MarketState(),
    ))
    assert signals, "the boosted MAKER signal should still pass"
    assert signals[0].certainty == pytest.approx(strategies.MAX_REPORTED_CERTAINTY)
    assert signals[0].certainty < 1.0
