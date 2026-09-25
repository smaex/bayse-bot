"""Regressions for the "orders that never filled went silent" family.

Three production symptoms motivated these tests:

1. MAKER quotes rested, expired unfilled, and the user was never told.
2. A resting quote was recorded as a *trade*, so the trading-drought clock and
   ``/why``'s NO_CONFIRMED_FILL check could never see that nothing executed.
3. Unfilled resting quotes were charged against the portfolio exposure ceiling,
   so passive quoting on one market could refuse every other strategy — logged
   at INFO with no gate counter.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

import bot
import executor
import stall
from risk import RiskManager


class _FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


class _FakeApp:
    def __init__(self):
        self.bot = _FakeBot()


def _pos(**overrides):
    pos = {
        "market_id": "market-1",
        "event_id": "event-1",
        "outcome_id": "yes-1",
        "order_id": "maker-order-1",
        "outcome": "YES",
        "entry_price": 0.52,
        "amount_ngn": 100.0,
        "filled_quantity": 0.0,
        "confirmed_filled": False,
        "strategy": "MAKER",
        "asset": "SOL",
        "timeframe": "15min",
        "trade_id": "trade-1",
        "placed_at": time.time() - 200,
    }
    pos.update(overrides)
    return pos


# ── 1. A resting quote is not a trade ────────────────────────────────────────

def test_resting_maker_quote_is_not_counted_as_a_confirmed_trade():
    chat_id = "chat-resting-quote"
    stall.reset(chat_id)

    executor.stall.note_order(chat_id, "MAKER", placed=True, reason="clob_limit_resting")

    report = stall.report(chat_id)
    assert report["orders_placed"] == 1
    assert report["orders_resting"] == 1
    assert report["trades"] == 0  # nothing executed yet

    # Only an exchange-confirmed fill moves the drought clock.
    stall.note_trade(chat_id, market_id="market-1")
    report = stall.report(chat_id)
    assert report["trades"] == 1
    assert report["age_trade_sec"] is not None


def test_verdict_reports_no_confirmed_fill_while_quotes_rest():
    chat_id = "chat-no-fill"
    stall.reset(chat_id)
    stall.note_evaluation(
        chat_id, markets_total=3, in_scope=3, evaluated=3, signals=1,
        skips={}, detail="",
    )
    stall.note_signal(chat_id, "MAKER", "SOL")
    executor.stall.note_order(chat_id, "MAKER", placed=True, reason="clob_limit_resting")

    verdict = stall.verdict(chat_id)

    assert verdict["code"] == "NO_CONFIRMED_FILL"
    assert "resting" in verdict["detail"]


# ── 2. Exposure is created only by confirmed fills ───────────────────────────

def test_exposure_cap_counts_confirmed_fills_not_resting_quotes():
    risk = RiskManager()
    risk.add_position("market-resting", _pos())

    # ₦1,600 equity at 15% = ₦240 of budget. A resting (unfilled) quote must not
    # consume it: that was what let MAKER freeze every other strategy.
    assert risk.deployed() == pytest.approx(100.0)
    assert risk.deployed_filled() == pytest.approx(0.0)
    assert risk.deployed_resting() == pytest.approx(100.0)
    assert risk.can_trade(1600.0, 100.0, 0.15) is True

    # A CONFIRMED fill does consume it — directional risk is real.
    risk.add_position("market-filled", _pos(
        market_id="market-filled", order_id="snipe-order-1", strategy="SNIPE",
        confirmed_filled=True, filled_quantity=1.9,
    ))
    assert risk.deployed_filled() == pytest.approx(100.0)
    assert risk.can_trade(1600.0, 100.0, 0.15) is True   # ₦200 still within ₦240
    risk.add_position("market-filled-2", _pos(
        market_id="market-filled-2", order_id="snipe-order-2", strategy="SNIPE",
        confirmed_filled=True, filled_quantity=1.9,
    ))
    assert risk.can_trade(1600.0, 100.0, 0.15) is False  # ₦300 > ₦240


def test_partial_fill_counts_as_exposure():
    risk = RiskManager()
    risk.add_position("market-partial", _pos(confirmed_filled=False, filled_quantity=0.5))
    assert risk.deployed_filled() == pytest.approx(100.0)


# ── 3. MAKER re-quotes must not silence SNIPE ────────────────────────────────

def test_cooldown_key_is_per_strategy():
    assert executor._cooldown_key("c", "m", "MAKER") != executor._cooldown_key("c", "m", "SNIPE")


def test_maker_cooldown_does_not_block_snipe_on_the_same_market(monkeypatch):
    executed = []

    async def _fake_logic(chat_id, sig, client, risk, settings, equity, free_cash, **kwargs):
        executed.append(sig.strategy)

    monkeypatch.setattr(executor, "_execute_logic", _fake_logic)
    monkeypatch.setattr(executor, "active_markets", [])
    monkeypatch.setattr(executor, "_trade_cooldown", {})
    monkeypatch.setattr(executor.config, "LIVE_TRADING", True)
    executor._trade_cooldown[executor._cooldown_key("chat", "market-1", "MAKER")] = time.time()

    snipe = SimpleNamespace(
        strategy="SNIPE", market_id="market-1", asset="SOL", reason="test",
    )
    risk = RiskManager()
    asyncio.run(executor.execute_trade("chat", snipe, object(), risk, {}, 1600.0, 1600.0))

    assert executed == ["SNIPE"]


# ── 4. The unfilled path frees capital, settles the row, and tells the user ──

def test_resolve_unfilled_position_settles_and_notifies(monkeypatch):
    settled = []
    monkeypatch.setattr(
        bot.database, "resolve_trade",
        lambda trade_id, won, pnl: settled.append((trade_id, won, pnl)),
    )
    app = _FakeApp()
    monkeypatch.setattr(bot, "_tg_app", app)

    risk = RiskManager()
    pos = _pos()
    risk.add_position("market-1", dict(pos))

    asyncio.run(bot._resolve_unfilled_position(
        "chat-1", risk, pos, "market-1", "exchange status=cancelled"
    ))

    assert settled == [("trade-1", None, 0.0)]
    assert "market-1" not in risk.open_positions
    assert app.bot.messages, "an unfilled order must produce a user-visible message"
    assert "Unfilled" in app.bot.messages[0] or "UNFILLED" in app.bot.messages[0]


def test_unconfirmed_cancel_notifies_once_and_retains_the_position(monkeypatch):
    """Cancel unconfirmed → keep the order in the risk book, but say so."""

    class _Client:
        def __init__(self):
            self.cancels = []

        async def get_order(self, order_id):
            return {"status": "open", "filledSize": 0}

        async def cancel_order(self, order_id):
            self.cancels.append(order_id)
            return {"status": "open"}

        def parse_filled_shares(self, order):
            return 0.0

    app = _FakeApp()
    monkeypatch.setattr(bot, "_tg_app", app)
    monkeypatch.setattr(bot, "active_markets", [{
        "market_id": "market-1", "threshold": 100.0, "secs_to_close": 400,
        "yes_price": 0.52, "no_price": 0.48,
    }])

    risk = RiskManager()
    pos = _pos()
    risk.add_position("market-1", pos)
    client = _Client()

    asyncio.run(bot._manage_unfilled_maker_orders("chat-1", client, risk, {}))
    assert client.cancels == ["maker-order-1"]
    assert "market-1" in risk.open_positions          # still live on the exchange
    assert pos["unfilled_alerted"] is True
    assert len(app.bot.messages) == 1

    # A second cycle must not spam the user about the same order.
    asyncio.run(bot._manage_unfilled_maker_orders("chat-1", client, risk, {}))
    assert len(app.bot.messages) == 1


def test_placing_a_quote_does_not_reset_the_drought_clock():
    """The gap that drives the stall alert must measure confirmed fills only."""
    chat_id = "chat-drought"
    stall.reset(chat_id)
    # A live evaluation keeps the verdict from reporting NO_MARKETS first (the
    # scanner's process-wide state is shared with other tests).
    stall.note_evaluation(
        chat_id, markets_total=3, in_scope=3, evaluated=3, signals=1,
        skips={}, detail="",
    )

    stall.note_signal(chat_id, "MAKER", "SOL")
    executor.stall.note_order(chat_id, "MAKER", placed=True, reason="clob_limit_resting")

    assert stall.trade_gap_minutes(chat_id) > 0  # not zeroed by the quote
    verdict = stall.verdict(chat_id)
    assert verdict["trade_gap_min"] is None      # no fill has ever happened
    assert verdict["code"] == "NO_CONFIRMED_FILL"
