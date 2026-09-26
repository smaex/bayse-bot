"""A stall report must not contradict itself, and must not invent a fault.

Reproduces the Telegram output an operator received on 2026-09-26, ~26 h after
the last confirmed fill. Three defects, all in the reporting layer:

* two reports read ``HEALTHY`` — "The trading pipeline is evaluating and has
  placed orders" — one line above "0 signal(s), 0 order(s) placed in this
  process". ``HEALTHY`` was the fall-through of :func:`stall.verdict`, so it
  asserted a placement that never happened. Because ``NO_EDGE`` was keyed on
  the *last pass* having evaluated a market, a scanner-triggered pass (which
  covers no market) flipped the same account to ``HEALTHY`` and back every
  minute — and a changed verdict code bypasses the alert rate limit, so each
  flip sent an alert.
* every ~15 minutes the verdict flipped to ``SCOPE_EMPTY`` — "None of the 6
  open markets match this account's scope ... check /settings" — for the two
  minutes around a series boundary, when the 15-minute market has closed and
  the next has not opened. That is the exchange's calendar, not the account's
  configuration, and it displaced the verdict that was actually true
  (``NO_CONFIRMED_FILL``), producing an alert pair on every boundary.
* the header and the body of one alert disagreed about the drought clock
  ("stall — 1513 min" over "Last confirmed fill: 1514 min ago") because each
  read its own ``time.time()``.

No gate, ceiling or sizing value is touched here: every fix is a sentence the
report was getting wrong.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import bot
import config
import executor
import stall
import telegram_bot
from risk import RiskManager


@pytest.fixture(autouse=True)
def _clean():
    stall.reset()
    yield
    stall.reset()


def legacy_markdown_error(text: str) -> str | None:
    """Telegram legacy-Markdown validity; canonical copy in
    ``test_stall_report_accuracy.py`` (kept local so this file stands alone)."""
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
            j = text.find(c, i + 1)
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


# ── The paste, replayed ───────────────────────────────────────────────────────

def _replay_quiet_process(chat: str, *, minutes_dark: int = 1450,
                          evaluations: int = 1214) -> None:
    """The 1450-min report: 1214 evaluations, 0 signals, 0 orders, 0 fills."""
    stall.note_state(chat, paused=False, dry_run=False)
    stall.seed_last_trade(chat, time.time() - minutes_dark * 60)
    for _ in range(evaluations):
        stall.note_evaluation(chat, markets_total=8, in_scope=2, evaluated=0, signals=0,
                              skips={"timeframe": 6, "trigger": 2}, open_markets=8,
                              detail="strategies=['SNIPE','MAKER'] assets=['BTC'] tfs=['15min']")
    for _ in range(1195):
        stall.reject(chat, "SNIPE", "no_raw_edge_or_trend_alignment", "edge=0.004")
    for _ in range(922):
        stall.reject(chat, "MAKER", "distance_below_calibration", "fv=0.58")
    for _ in range(236):
        stall.reject(chat, "MAKER", "late_candle_window", "secs=40")


def test_a_fill_less_drought_with_no_orders_is_not_reported_healthy():
    chat = "u-quiet"
    _replay_quiet_process(chat)

    verdict = stall.verdict(chat, eval_max_age_sec=10 ** 9)
    assert verdict["code"] != "HEALTHY", verdict
    assert verdict["code"] == "NO_EDGE", verdict
    # The headline may not claim a placement that never happened.
    assert "placed order" not in verdict["headline"]
    assert "0 order(s) placed" not in verdict["detail"]
    assert "SNIPE:no_raw_edge_or_trend_alignment×1195" in verdict["detail"]
    assert "Lowering a gate" in verdict["action"]


def test_the_verdict_does_not_flap_with_the_last_pass_market_count():
    """NO_EDGE keyed on the last pass made one account healthy and edge-less
    on alternating minutes — and every flip bypassed the alert rate limit."""
    chat = "u-flap"
    _replay_quiet_process(chat, evaluations=4)

    codes = []
    for evaluated in (1, 0, 1, 0, 1, 0):          # feed pass, scanner pass, ...
        stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=evaluated,
                              signals=0, skips={"timeframe": 6}, open_markets=9)
        codes.append(stall.verdict(chat, eval_max_age_sec=10 ** 9)["code"])
    assert codes == ["NO_EDGE"] * 6, codes

    alerted = [code for code in codes if stall.note_alert(chat, code)]
    assert len(alerted) == 1, f"a stable condition alerted {len(alerted)} times"


def test_healthy_still_means_orders_were_placed_and_filled():
    chat = "u-working"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=3, signals=1,
                          open_markets=9)
    stall.note_signal(chat, "MAKER", "BTC")
    stall.note_order(chat, "MAKER", placed=True, reason="filled")
    stall.note_trade(chat, market_id="mkt-1")

    verdict = stall.verdict(chat, eval_max_age_sec=10 ** 9)
    assert verdict["code"] == "HEALTHY", verdict
    assert "recording fills" in verdict["headline"]
    assert "1 confirmed fill(s)" in verdict["detail"]


def test_signals_that_never_reached_the_executor_are_not_healthy():
    chat = "u-lost-signals"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=3, signals=2,
                          open_markets=9)
    stall.note_signal(chat, "SNIPE", "SOL")
    stall.note_signal(chat, "SNIPE", "SOL")

    verdict = stall.verdict(chat, eval_max_age_sec=10 ** 9)
    assert verdict["code"] == "SIGNALS_NOT_EXECUTED", verdict
    assert verdict["severity"] == "warn"
    assert "2 signal(s)" in verdict["headline"]


# ── The scope gap between two rounds of a series ──────────────────────────────

def _trading_with_resting_quote(chat: str) -> None:
    stall.note_state(chat, paused=False, dry_run=False)
    stall.seed_last_trade(chat, time.time() - 1467 * 60)
    stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=1, signals=0,
                          skips={"timeframe": 6}, open_markets=9,
                          detail="strategies=['SNIPE','MAKER'] tfs=['15min','5min']")
    stall.note_signal(chat, "MAKER", "BTC")
    stall.note_order(chat, "MAKER", placed=True, reason="clob_limit_resting")


def test_the_boundary_gap_is_not_a_settings_fault():
    """At 1467 min the operator was told to fix /settings; 60 s later the same
    account was back to 3 markets in scope."""
    chat = "u-boundary"
    _trading_with_resting_quote(chat)
    # The 15-minute round closed and the next has not opened: discovery holds
    # only the out-of-scope 1h markets for a couple of minutes.
    stall.note_evaluation(chat, markets_total=6, in_scope=0, evaluated=0, signals=0,
                          skips={"timeframe": 6}, open_markets=6,
                          detail="strategies=['SNIPE','MAKER'] tfs=['15min','5min']")

    verdict = stall.verdict(chat, eval_max_age_sec=10 ** 9, resting_now=0)
    assert verdict["code"] == "NO_CONFIRMED_FILL", verdict
    assert "check /settings" not in verdict["action"].lower()


def test_a_persistent_empty_scope_is_still_named_and_explained():
    chat = "u-misconfigured"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=6, in_scope=0, evaluated=0, signals=0,
                          skips={"timeframe": 6}, open_markets=6,
                          detail="strategies=['SNIPE','MAKER'] tfs=['15min','5min']")
    # Backdate the run of empty-scope passes past the confirmation window.
    stall._users[chat]["scope_empty_since"] = time.time() - stall.SCOPE_EMPTY_CONFIRM_SEC - 60

    verdict = stall.verdict(chat, eval_max_age_sec=10 ** 9)
    assert verdict["code"] == "SCOPE_EMPTY", verdict
    assert verdict["severity"] == "warn"
    # The reason comes from the pass that found nothing, not from a guess.
    assert "6 excluded by timeframe" in verdict["detail"]
    assert "tfs=['15min','5min']" in verdict["detail"]
    assert "ALLOW_EXPERIMENTAL_STRATEGIES" in verdict["action"]


def test_a_real_scope_fault_survives_a_recovered_pass():
    chat = "u-misconfigured-2"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=6, in_scope=0, evaluated=0, signals=0,
                          skips={"asset": 6}, open_markets=6, detail="assets=['XAUUSD']")
    stall._users[chat]["scope_empty_since"] = time.time() - stall.SCOPE_EMPTY_CONFIRM_SEC - 60
    assert stall.verdict(chat, eval_max_age_sec=10 ** 9)["code"] == "SCOPE_EMPTY"

    # One pass with something in scope ends the run.
    stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=3, signals=0,
                          skips={"timeframe": 6}, open_markets=9)
    assert stall.verdict(chat, eval_max_age_sec=10 ** 9)["code"] != "SCOPE_EMPTY"


def test_no_alert_pair_on_the_series_boundary(monkeypatch):
    """The pasted log alternated SCOPE_EMPTY and NO_CONFIRMED_FILL one minute
    apart, every 15 minutes, forever."""
    chat = "u-pair"
    _trading_with_resting_quote(chat)
    codes = []
    for in_scope, total in ((3, 9), (0, 6), (3, 9), (0, 6), (3, 9)):
        stall.note_evaluation(chat, markets_total=total, in_scope=in_scope,
                              evaluated=1 if in_scope else 0, signals=0,
                              skips={"timeframe": 6}, open_markets=total)
        codes.append(stall.verdict(chat, eval_max_age_sec=10 ** 9, resting_now=0)["code"])

    assert set(codes) == {"NO_CONFIRMED_FILL"}, codes
    alerts = [code for code in codes if stall.note_alert(chat, code)]
    assert len(alerts) == 1, f"{len(alerts)} alerts for one unchanged condition"


# ── One clock for the whole alert ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (1512.5001, "1513"),   # header said 1513, body said 1512
    (1513.4999, "1513"),   # header said 1513, body said 1514
    (1572.5001, "1573"),   # header said 1573, body said 1572
    (1512.5, "1513"),      # half-up, not Python's half-to-even "1512"
    (1513.5, "1514"),
    (0.4, "0"),
])
def test_the_drought_clock_rounds_half_up_and_only_once(raw, expected):
    assert stall.format_gap_minutes(raw) == expected
    # Pre-rounding to one decimal is what moved values onto an exact half and
    # made the body contradict the header. Rendering must not depend on it.
    assert stall.format_gap_minutes(round(raw, 1)) in (expected, str(int(raw) + 1))


def test_report_and_drought_clock_carry_the_same_unrounded_gap():
    chat = "u-clock"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=1, signals=0,
                          open_markets=9)
    # 1512.5 minutes: the value the pasted alert disagreed about.
    now = time.time()
    stall.seed_last_trade(chat, now - 1512.5 * 60)

    data = stall.report(chat, now=now, eval_max_age_sec=10 ** 9)
    gap = stall.trade_gap_minutes(chat, now=now)
    assert data["verdict"]["trade_gap_min"] == pytest.approx(1512.5, abs=0.01)
    assert data["verdict"]["trade_gap_min"] == gap
    assert stall.format_gap_minutes(data["verdict"]["trade_gap_min"]) == \
        stall.format_gap_minutes(gap) == "1513"


class _Sent:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    async def send(self, app, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


def _watchdog_now_values(monkeypatch, chat: str) -> dict:
    """Run the watchdog with spies on both clocks and return what each saw."""
    seen: dict[str, list] = {"report": [], "gap": []}
    real_report = stall.report
    real_gap = stall.trade_gap_minutes

    def spy_report(cid, **kwargs):
        seen["report"].append(kwargs.get("now"))
        return real_report(cid, **kwargs)

    def spy_gap(cid, now=None):
        seen["gap"].append(now)
        return real_gap(cid, now=now)

    sent = _Sent()
    monkeypatch.setattr(bot.stall, "report", spy_report)
    monkeypatch.setattr(bot.stall, "trade_gap_minutes", spy_gap)
    monkeypatch.setattr(bot.telegram_bot, "send_message", sent.send)
    monkeypatch.setattr(bot, "_tg_app", object())
    monkeypatch.setattr(bot, "_active_users_cache", [{"chat_id": chat, "settings": {}}])
    bot._user_risks.setdefault(chat, RiskManager())

    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=1, signals=0,
                          open_markets=9)
    stall.seed_last_trade(chat, time.time() - 1512.5 * 60)

    limit = config.TRADE_STALL_ALERT_MIN
    config.TRADE_STALL_ALERT_MIN = 60.0
    try:
        asyncio.run(bot._check_trading_stalls())
    finally:
        config.TRADE_STALL_ALERT_MIN = limit

    assert sent.messages, "the drought produced no alert"
    return {"seen": seen, "text": sent.messages[0][1]}


def test_watchdog_renders_header_and_body_from_one_instant(monkeypatch):
    chat = "u-watchdog-clock"
    result = _watchdog_now_values(monkeypatch, chat)

    # The header clock and the report clock are the same reading.
    assert result["seen"]["gap"] and result["seen"]["report"]
    assert set(result["seen"]["report"]) == set(result["seen"]["gap"]), result["seen"]
    assert None not in result["seen"]["report"], "a report read its own clock"

    text = result["text"]
    header = int(text.split("Trading stall — ")[1].split(" min")[0])
    body = int(text.split("Last confirmed fill: ")[1].split(" min ago")[0])
    assert header == body, f"header {header} vs body {body} in one message:\n{text}"


# ── What the report now says ──────────────────────────────────────────────────

def test_markets_line_separates_discovered_from_open():
    chat = "u-markets"
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=1, signals=0,
                          skips={"status": 3}, open_markets=6)
    text = stall.format_report(chat, eval_max_age_sec=10 ** 9)
    assert "Markets: 9 discovered (6 open), 3 in scope" in text

    # No pass has reported an open count yet: say only what is known.
    stall.reset(chat)
    stall.note_state(chat, paused=False, dry_run=False)
    stall.note_evaluation(chat, markets_total=9, in_scope=3, evaluated=1, signals=0)
    text = stall.format_report(chat, eval_max_age_sec=10 ** 9)
    assert "Markets: 9 discovered, 3 in scope" in text
    assert "(0 open)" not in text


def test_the_scope_gap_duration_is_shown_instead_of_a_verdict():
    chat = "u-gap-note"
    _trading_with_resting_quote(chat)
    stall.note_evaluation(chat, markets_total=6, in_scope=0, evaluated=0, signals=0,
                          skips={"timeframe": 6}, open_markets=6)
    text = stall.format_report(chat, eval_max_age_sec=10 ** 9, resting_now=0)
    assert "nothing in scope for" in text
    assert "Code: `NO_CONFIRMED_FILL`" in text


def test_executor_outcomes_are_listed_and_expire():
    chat = "u-exec"
    _replay_quiet_process(chat, evaluations=2)
    sig = SimpleNamespace(strategy="MAKER")
    stall.note_signal(chat, "MAKER", "BTC")
    executor._stall_skip(chat, sig, "maker_quote_behind_book",
                         "max bid 0.580 is 8 tick(s) under the best bid 0.660")
    for _ in range(9):
        executor._stall_skip(chat, sig, "market_cooldown", "52s left")

    data = stall.report(chat, eval_max_age_sec=10 ** 9)
    rows = {row["code"]: row for row in data["recent_exec"]}
    assert "exec:maker_quote_behind_book" in rows
    assert "exec:market_cooldown" in rows
    # One skip is one count: the executor used to record each of these twice.
    assert rows["exec:market_cooldown"]["count"] == 9
    assert rows["exec:maker_quote_behind_book"]["count"] == 1
    assert "0.660" in rows["exec:maker_quote_behind_book"]["detail"]

    text = stall.format_report(chat, eval_max_age_sec=10 ** 9)
    assert "Executor outcomes (last 15 min):" in text
    assert "exec:maker_quote_behind_book" in text
    assert "exec:market_cooldown: 9×" in text
    # A hit outside the recency window is history, not the current cause.
    stall._users[chat]["rejects"]["exec:market_cooldown"]["last"] = (
        time.time() - stall.RECENT_WINDOW_SEC - 5
    )
    stale = stall.report(chat, eval_max_age_sec=10 ** 9)
    assert "exec:market_cooldown" not in [row["code"] for row in stale["recent_exec"]]


def test_the_new_lines_still_render_in_telegram_markdown():
    chat = "u-md"
    _replay_quiet_process(chat, evaluations=2)
    stall.note_evaluation(chat, markets_total=6, in_scope=0, evaluated=0, signals=0,
                          skips={"timeframe": 6}, open_markets=6,
                          detail="strategies=['SNIPE','MAKER'] tfs=['15min','5min']")
    stall.note_order(chat, "MAKER", placed=False, reason="maker_quote_behind_book")
    stall.reject(chat, "exec", "maker_quote_behind_book", "8 tick(s) under the best bid")

    text = stall.format_report(chat, markdown=True, eval_max_age_sec=10 ** 9)
    assert legacy_markdown_error(text) is None, text
    assert "nothing in scope for" in text


def test_report_stays_json_serialisable_with_the_new_fields():
    chat = "u-json"
    _trading_with_resting_quote(chat)
    stall.note_evaluation(chat, markets_total=6, in_scope=0, evaluated=0, signals=0,
                          skips={"timeframe": 6}, open_markets=6)
    stall.note_order(chat, "MAKER", placed=False, reason="market_cooldown")
    payload = stall.report(chat, eval_max_age_sec=10 ** 9)
    json.dumps(payload)
    assert {"markets_open", "scope_empty_sec", "recent_exec"} <= set(payload)
    assert payload["markets_open"] == 6
    assert payload["scope_empty_sec"] >= 0
