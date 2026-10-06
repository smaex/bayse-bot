"""The Telegram surface must match what the bot actually does.

A command that names a deleted strategy is worse than no command: it tells
the operator something is enabled when nothing is running. A notification
that never fires is a log file, not an interface.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import config
import telegram_bot as tgb
from strategies.base import QuoteLeg
from strategies.maker import maker_strategy


class _FakeBot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


class _FakeMessage:
    """Commands reply on the message object, not on the bot."""

    def __init__(self, bot):
        self.bot = bot
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, bot):
        self.message = _FakeMessage(bot)
        self.effective_chat = SimpleNamespace(id="c1")


def _app():
    bot = _FakeBot()
    return bot, SimpleNamespace(bot=bot)


@pytest.fixture
def _connected(monkeypatch):
    """Satisfy @_guard without a database."""
    monkeypatch.setattr(
        tgb, "_safe_get_user",
        lambda cid: {"chat_id": cid, "is_active": True,
                     "settings": dict(tgb._VALID_STRATEGIES and {})},
    )


def _guarded_run(update):
    return update.message.replies[0] if update.message.replies else ""


# ── the command surface matches the two-strategy roster ──────────────────────

def test_no_command_references_a_deleted_strategy():
    import inspect

    for name, fn in vars(tgb).items():
        if not (name.startswith("cmd_") or name.startswith("notify_")):
            continue
        try:
            src = inspect.getsource(fn)
        except (TypeError, OSError):
            continue
        for stale in ("SNIPE", "ORACLE_ARB", "FRONTRUN", "CORRELATE",
                      "MIDMARKET_MAKER", "PAIRED_SNIPER"):
            # Prose explaining the purge is fine; a live code path is not.
            assert f'"{stale}"' not in src, f"{name} still references {stale}"


def test_strategy_validation_rejects_deleted_names():
    assert tgb._VALID_STRATEGIES == {"TAKER", "MAKER"}
    assert tgb._STRATEGY_ALIASES == {}, "an alias makes a dead strategy look enabled"
    assert tgb._normalize_strat("snipe") == "SNIPE"
    assert "SNIPE" not in tgb._VALID_STRATEGIES


def test_every_registered_command_has_a_handler():
    """A command in /help that has no handler is a dead end for the operator."""
    import re

    help_src = tgb.cmd_help.__doc__ or ""
    declared = set(re.findall(r"/([a-z]+) —", tgb.cmd_help.__doc__ or ""))
    # Read the registry out of build_app's source rather than importing PTB.
    import inspect

    build_src = inspect.getsource(tgb.build_app)
    registered = set(re.findall(r'\("([a-z]+)",\s+cmd_', build_src))
    assert declared <= registered, f"documented but not registered: {declared - registered}"


# ── notifications that must exist and must fire ──────────────────────────────

def test_a_burned_set_is_reported_as_direction_independent():
    """The only trade whose result is known at entry deserves saying so."""
    bot, app = _app()
    asyncio.run(tgb.notify_set_burned(
        app, "c1", "BTC", "15min", 10.0, 950.0, 1000.0, 50.0))
    assert len(bot.messages) == 1
    msg = bot.messages[0]
    assert "Complete set burned" in msg
    assert "₦+50" in msg
    assert "Direction-independent" in msg


def test_a_burn_report_names_the_edge_not_just_the_pnl():
    """+5.3% on locked capital and +₦50 are different facts; show both."""
    bot, app = _app()
    asyncio.run(tgb.notify_set_burned(
        app, "c1", "BTC", "15min", 10.0, 950.0, 1000.0, 50.0))
    assert "+5.3%" in bot.messages[0]


def test_a_structural_take_is_labelled_differently_from_a_maker_pair():
    """Same mechanism, different provenance: the operator is diagnosing, not
    admiring, and needs to know which path produced the fill."""
    bot, app = _app()
    asyncio.run(tgb.notify_set_burned(
        app, "c1", "BTC", "15min", 5.0, 480.0, 500.0, 20.0, structural=True))
    assert "Structural take" in bot.messages[0]

    bot2, app2 = _app()
    asyncio.run(tgb.notify_set_burned(
        app2, "c1", "BTC", "15min", 5.0, 480.0, 500.0, 20.0))
    assert "Maker pair completed" in bot2.messages[0]


def test_a_maker_pair_and_a_single_leg_are_not_described_the_same_way():
    """Calling a one-sided quote a two-sided one is how an operator comes to
    believe the book is hedged when it is not."""
    def _sig(legs):
        return SimpleNamespace(
            strategy="MAKER", asset="SOL", timeframe="15min", outcome="YES",
            certainty=0.8, win_prob=0.6, market_price=0.5, reason="",
            is_multi_leg=lambda: len(legs) > 1,
        )

    bot_pair, app_pair = _app()
    asyncio.run(tgb.notify_trade(
        app_pair, "c1",
        _sig([QuoteLeg("YES", "y", 0.45, 0.02, 0.5),
              QuoteLeg("NO", "n", 0.45, 0.02, 0.5)]),
        500.0, engine="CLOB_LIMIT"))
    assert "Two-sided quote" in bot_pair.messages[0]

    bot_one, app_one = _app()
    asyncio.run(tgb.notify_trade(
        app_one, "c1", _sig([QuoteLeg("YES", "y", 0.45, 0.02, 0.5)]),
        500.0, engine="CLOB_LIMIT"))
    assert "Single leg" in bot_one.messages[0]


def test_execution_is_announced_before_the_fill_not_only_after():
    """The operator asked to see trades go out, not only the ones that fill.

    A fill notice cannot describe the moment an order is sent: it is silent
    for every order that rests, partially fills or dies. This message is sent
    first, so every execution has a beginning the operator can see.
    """
    bot, app = _app()
    asyncio.run(tgb.notify_executing(
        app, "c1", "TAKER", "BTC", "15min", "YES", 400.0,
        price=0.553, engine="CLOB",
    ))
    message = bot.messages[0]
    assert "Order being executed" in message
    assert "BTC 15min" in message
    assert "0.553" in message
    assert "TAKER" in message


def test_a_complete_set_execution_lists_both_legs():
    """Two orders are being sent; the notice must not name only one side."""
    bot, app = _app()
    asyncio.run(tgb.notify_executing(
        app, "c1", "TAKER", "BTC", "15min", "BOTH", 800.0,
        legs=[QuoteLeg("YES", "y", 0.42, 0.02, 0.60),
              QuoteLeg("NO", "n", 0.37, 0.02, 0.40)],
        engine="CLOB",
    ))
    message = bot.messages[0]
    assert "YES @ 0.420" in message and "NO @ 0.370" in message


def test_an_unconfirmed_cancel_does_not_claim_the_money_came_back():
    """notify_unfilled says 'returned, no loss'. On an unconfirmed cancel the
    order can still fill, so that message would be a false assurance."""
    import inspect

    src = inspect.getsource(tgb.notify_unfilled)
    assert "returned" in src
    assert "resting" in inspect.getsource(tgb.notify_order_resting).lower()


# ── /quotes shows the maker book the operator cannot otherwise see ───────────

@pytest.fixture
def _clean_quotes():
    saved = dict(maker_strategy.open_quotes)
    maker_strategy.open_quotes.clear()
    yield
    maker_strategy.open_quotes.clear()
    maker_strategy.open_quotes.update(saved)


def test_quotes_reports_an_empty_book_honestly(_clean_quotes, _connected):
    update = _FakeUpdate(_FakeBot())
    asyncio.run(tgb.cmd_quotes(update, None))
    assert "No resting maker quotes" in _guarded_run(update)


def test_quotes_shows_price_fair_value_and_time_to_withdrawal(_clean_quotes, _connected):
    maker_strategy.open_quotes["mkt-1"] = {
        "legs": {
            "YES": QuoteLeg("YES", "y", 0.45, 0.02, 0.50),
            "NO": QuoteLeg("NO", "n", 0.45, 0.02, 0.50),
        },
        "placed_at": time.time() - 10,
        "spot": 100_000.0,
        "fv_yes": 0.50,
        "inventory": 0.0,
    }
    update = _FakeUpdate(_FakeBot())
    asyncio.run(tgb.cmd_quotes(update, None))
    text = _guarded_run(update)
    assert "mkt-1" in text
    assert "0.450" in text and "0.500" in text   # bid and fair value
    assert "withdraws in" in text


def test_quotes_surfaces_inventory_that_needs_completing(_clean_quotes, _connected):
    """A one-sided fill is only half a trade; the operator should see that the
    next quote is being skewed to finish it."""
    maker_strategy.open_quotes["mkt-2"] = {
        "legs": {"YES": QuoteLeg("YES", "y", 0.45, 0.02, 0.50)},
        "placed_at": time.time() - 5,
        "spot": 100_000.0,
        "fv_yes": 0.50,
        "inventory": 10.0,
    }
    update = _FakeUpdate(_FakeBot())
    asyncio.run(tgb.cmd_quotes(update, None))
    text = _guarded_run(update)
    assert "Long 10.00 YES" in text
    assert "complete the set" in text


def test_quotes_flags_a_quote_past_its_timeout(_clean_quotes, _connected):
    maker_strategy.open_quotes["mkt-3"] = {
        "legs": {"YES": QuoteLeg("YES", "y", 0.45, 0.02, 0.50)},
        "placed_at": time.time() - (config.MAKER_ORDER_TIMEOUT + 30),
        "spot": 100_000.0,
        "fv_yes": 0.50,
        "inventory": 0.0,
    }
    update = _FakeUpdate(_FakeBot())
    asyncio.run(tgb.cmd_quotes(update, None))
    text = _guarded_run(update)
    assert text.startswith("📊")
    assert "🔴" in text
