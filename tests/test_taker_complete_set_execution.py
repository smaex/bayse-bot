"""Regression coverage for the CLOB complete-set TAKER execution path.

The strategy gates and pair sizing were already tested independently. These
checks exercise the live executor branch that sends the two FAK orders, books
confirmed partial fills, and reports them to the user.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import config
import executor
import stall
from risk import RiskManager
from strategies.base import QuoteLeg, TradeSignal
from strategies import book as booklib


CHAT_ID = "taker-complete-set-test"


def _book(ask: float, quantity: float = 10_000.0, *, timestamp=None):
    result = {
        "bids": [{"price": max(0.01, ask - 0.01), "quantity": quantity}],
        "asks": [{"price": ask, "quantity": quantity}],
    }
    if timestamp is not None:
        result["timestamp"] = timestamp
    return result


def _signal(*, yes_price=0.55, no_price=0.37, yes_fair=0.60, no_fair=0.40):
    return TradeSignal(
        strategy="TAKER", event_id="event-1", market_id="market-1", asset="BTC",
        timeframe="15min", outcome="BOTH", outcome_id="yes",
        certainty=max(yes_fair, no_fair), win_prob=max(yes_fair, no_fair),
        market_price=max(yes_price, no_price), size_pct=0.02,
        reason="COMPLETE_SET locked edge", mode_floor=0.0,
        min_net_ev=config.TAKER_MIN_NET_EV_DEFAULT,
        legs=[
            QuoteLeg("YES", "yes", yes_price, 0.02, yes_fair),
            QuoteLeg("NO", "no", no_price, 0.02, no_fair),
        ],
    )


class _FakeBot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


class _FakeClient:
    def __init__(self, books=None, *, fills=None, fail_on_call=None):
        self.books = books or {"yes": _book(0.55), "no": _book(0.37)}
        self.fills = fills
        self.fail_on_call = fail_on_call
        self.book_calls: list[list[str]] = []
        self.place_calls: list[dict] = []
        self.burn_calls: list[tuple[str, float, str]] = []

    async def get_orderbooks(self, outcome_ids, depth=5):
        self.book_calls.append(list(outcome_ids))
        return {outcome_id: self.books.get(outcome_id, {}) for outcome_id in outcome_ids}

    async def get_orderbook(self, outcome_id, depth=5):
        return self.books.get(outcome_id, {})

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        if len(self.place_calls) == self.fail_on_call:
            raise RuntimeError("simulated second-leg exchange failure")

        outcome_id = kwargs["outcome_id"]
        ask = booklib.best_ask(self.books[outcome_id])
        if self.fills is not None:
            shares = float(self.fills[len(self.place_calls) - 1])
        else:
            fee_fraction = booklib.taker_fee_fraction(ask, 0.02)
            shares = (
                float(kwargs["amount"])
                / (ask * config.CURRENCY_BASE_MULTIPLIER)
                * (1.0 - fee_fraction)
            )
        amount = float(kwargs["amount"])
        return {
            "order": {
                "id": f"order-{len(self.place_calls)}",
                "status": "filled",
                "quantity": shares,
                "avgFillPrice": ask,
                "totalCost": amount,
            }
        }

    async def burn_shares(self, market_id, quantity, currency):
        self.burn_calls.append((market_id, quantity, currency))
        return {"amount": quantity * config.CURRENCY_BASE_MULTIPLIER}

    @staticmethod
    def parse_filled_shares(order):
        try:
            return float(order.get("quantity") or order.get("filledSize") or 0.0)
        except (TypeError, ValueError):
            return 0.0


@pytest.fixture
def taker_env(monkeypatch):
    stall.reset(CHAT_ID)
    market = {
        "market_id": "market-1", "event_id": "event-1", "asset": "BTC",
        "timeframe": "15min", "engine": "CLOB", "fee_rate": 0.02,
        "minimum_order_amount": 100.0, "secs_to_close": 400.0,
        "threshold": 100_000.0, "closing_date": "2026-10-06T12:15:00Z",
        "yes_id": "yes", "no_id": "no",
    }
    monkeypatch.setattr(executor, "active_markets", [market])
    monkeypatch.setattr(executor, "_trade_cooldown", {})
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *_args: 1.0)
    monkeypatch.setattr(executor, "_tg_app", SimpleNamespace(bot=_FakeBot()))

    recorded = []
    resolved = []
    monkeypatch.setattr(
        executor.database, "record_trade",
        lambda **kwargs: recorded.append(kwargs) or f"trade-{len(recorded)}",
    )
    monkeypatch.setattr(
        executor.database, "resolve_trade",
        lambda *args: resolved.append(args),
    )
    risk = RiskManager()
    risk.current_free_cash = 20_000.0
    return {
        "app": executor._tg_app,
        "market": market,
        "recorded": recorded,
        "resolved": resolved,
        "risk": risk,
    }


def _execute(env, client, signal=None, *, equity=20_000.0, free_cash=20_000.0):
    asyncio.run(executor._execute_logic(
        CHAT_ID,
        signal or _signal(),
        client,
        env["risk"],
        {"mode": "balanced", "risk_pct": 2.0, "mintrade": 100.0,
         "maxtrade": 5_000.0, "maxexposure": 20.0},
        equity,
        free_cash,
    ))


def test_complete_set_sends_numeric_per_leg_stakes_and_notifies_the_entry(taker_env):
    client = _FakeClient()

    _execute(taker_env, client)

    assert client.book_calls == [["yes", "no"]], "both legs need one fresh shared snapshot"
    assert len(client.place_calls) == 2
    assert all(isinstance(call["amount"], (int, float)) for call in client.place_calls)
    assert all(call["time_in_force"] == "FAK" for call in client.place_calls)
    assert all(call["order_type"] == "LIMIT" for call in client.place_calls)
    # Each fill cap preserves the same fee-inclusive EV floor as the strategy,
    # and the worst allowed pair prices still preserve the structural lock.
    fair_values = {"yes": 0.60, "no": 0.40}
    effective_caps = []
    for call in client.place_calls:
        effective = booklib.effective_buy_price(call["price"], 0.02)
        effective_caps.append(effective)
        assert (
            fair_values[call["outcome_id"]] / effective - 1.0
            >= config.TAKER_MIN_NET_EV_DEFAULT - 1e-9
        )
    assert 1.0 - sum(effective_caps) >= config.COMPLETE_SET_TAKER_MIN_EDGE - 1e-9
    assert len(taker_env["recorded"]) == 2
    assert len(taker_env["risk"].open_positions) == 0, "a balanced pair is burned"
    assert len(client.burn_calls) == 1
    messages = taker_env["app"].bot.messages
    assert any("TAKER Fill Confirmed" in message for message in messages)
    assert any("Complete set burned" in message for message in messages)
    assert len(taker_env["resolved"]) == 2


def test_single_taker_entry_notification_uses_the_confirmed_fill_price(taker_env):
    signal = TradeSignal(
        strategy="TAKER", event_id="event-1", market_id="market-1", asset="BTC",
        timeframe="15min", outcome="YES", outcome_id="yes", certainty=0.80,
        win_prob=0.75, market_price=0.54, size_pct=0.02,
        reason="TAKER directional test", mode_floor=0.0,
        min_net_ev=config.TAKER_MIN_NET_EV_DEFAULT,
    )
    client = _FakeClient()

    _execute(taker_env, client, signal)

    message = taker_env["app"].bot.messages[0]
    assert "exchange-confirmed fill" in message
    assert "0.550" in message, "entry alert must use the confirmed fill, not the signal quote"
    assert len(taker_env["recorded"]) == 1
    assert len(taker_env["risk"].open_positions) == 1


def test_first_leg_is_persisted_tracked_and_notified_if_second_order_fails(taker_env):
    client = _FakeClient(fail_on_call=2)

    _execute(taker_env, client)

    assert len(client.place_calls) == 2
    assert len(taker_env["recorded"]) == 1
    assert len(taker_env["risk"].open_positions) == 1
    position = next(iter(taker_env["risk"].open_positions.values()))
    assert position["outcome"] == "YES"
    assert position["confirmed_filled"] is True
    assert position["filled_quantity"] > 0
    assert taker_env["risk"].current_free_cash < 20_000.0
    assert client.burn_calls == []
    messages = taker_env["app"].bot.messages
    assert any("TAKER Fill Confirmed" in message for message in messages)
    assert any("only one leg" in message.lower() for message in messages)
    assert stall._users[CHAT_ID]["trades"] == 1


def test_pair_execution_rechecks_both_fresh_prices_before_sending_either_leg(taker_env):
    # The NO ask has moved through its own fair value. A stale signal should not
    # authorize it just because the YES side is still good.
    client = _FakeClient(books={"yes": _book(0.55), "no": _book(0.43)})

    _execute(taker_env, client)

    assert client.place_calls == []
    assert taker_env["recorded"] == []
    assert taker_env["risk"].open_positions == {}


def test_complete_set_burn_does_not_mix_with_other_market_positions(taker_env):
    taker_env["risk"].add_position("market-1:YES:maker-1", {
        "market_id": "market-1", "strategy": "MAKER", "outcome": "YES",
        "outcome_id": "yes", "amount_ngn": 100.0,
        "filled_quantity": 2.0, "confirmed_filled": True,
        "entry_price": 0.50, "order_id": "maker-1",
    })
    client = _FakeClient()

    _execute(taker_env, client)

    assert client.book_calls == []
    assert client.place_calls == []
    assert len(taker_env["risk"].open_positions) == 1
    assert taker_env["risk"].open_positions["market-1:YES:maker-1"]["strategy"] == "MAKER"


def test_complete_set_refuses_a_stale_snapshot_for_either_leg(taker_env):
    old = time.time() - config.CLOB_MAX_BOOK_AGE_SECONDS - 1.0
    client = _FakeClient(books={"yes": _book(0.55), "no": _book(0.37, timestamp=old)})

    _execute(taker_env, client)

    assert client.place_calls == []
    assert taker_env["recorded"] == []
    assert taker_env["risk"].open_positions == {}


def test_single_taker_refuses_a_live_ask_that_breaks_its_ev_floor(taker_env):
    signal = TradeSignal(
        strategy="TAKER", event_id="event-1", market_id="market-1", asset="BTC",
        timeframe="15min", outcome="YES", outcome_id="yes", certainty=0.65,
        win_prob=0.60, market_price=0.54, size_pct=0.02,
        reason="TAKER directional test", mode_floor=0.0,
        min_net_ev=config.TAKER_MIN_NET_EV_DEFAULT,
    )
    # The signal price would have cleared the strategy's 6% floor, but the
    # current best ask at 0.57 no longer does after fees.
    client = _FakeClient(books={"yes": _book(0.57), "no": _book(0.37)})

    _execute(taker_env, client, signal)

    assert client.place_calls == []
    assert taker_env["recorded"] == []
    assert taker_env["risk"].open_positions == {}


def test_pair_fill_mismatch_is_not_burned_or_removed_from_risk(taker_env):
    client = _FakeClient(fills=[20.0, 19.0])

    _execute(taker_env, client)

    assert client.burn_calls == []
    assert len(taker_env["recorded"]) == 2
    assert len(taker_env["risk"].open_positions) == 2
    messages = taker_env["app"].bot.messages
    assert any("share counts differ" in message.lower() for message in messages)


def test_unfunded_two_leg_minimum_is_refused_before_any_order(taker_env):
    client = _FakeClient(books={"yes": _book(0.45), "no": _book(0.45)})
    signal = _signal(yes_price=0.45, no_price=0.45, yes_fair=0.56, no_fair=0.44)

    _execute(taker_env, client, signal, equity=2_000.0, free_cash=150.0)

    assert client.place_calls == []
    assert taker_env["risk"].open_positions == {}
