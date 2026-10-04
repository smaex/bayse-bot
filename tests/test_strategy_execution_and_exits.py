import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import config
from client import BayseClient
from risk import RiskManager
from strategies.base import MarketState
from strategies.maker import MakerStrategy
from strategies.taker import TakerStrategy


class MockClient(BayseClient):
    def __init__(self, order_response=None, cancel_response=None):
        super().__init__("public", "secret")
        self.order_response = order_response or {"status": "open"}
        self.cancel_response = cancel_response or {"status": "cancelled"}
        self.cancelled_orders = []

    async def get_order(self, order_id):
        return self.order_response

    async def cancel_order(self, order_id):
        self.cancelled_orders.append(order_id)
        return self.cancel_response

    async def get_orderbooks(self, outcome_ids, depth=5):
        return {}

    async def get_position(self, outcome_id):
        return {
            "availableBalance": 10,
            "sellPrice": 0.85,
            "currentValue": 850,
        }

    async def get_quote(self, *args, **kwargs):
        return {"completeFill": True, "quantity": 10, "price": 0.85}

    async def place_order(self, **kwargs):
        return {
            "order": {
                "id": "exit-order-1",
                "status": "filled",
                "filledSize": 10,
                "avgFillPrice": 0.85,
                "amount": kwargs.get("amount", 800),
            }
        }


def test_early_candle_small_noise_does_not_trigger_panic_stop_loss(monkeypatch):
    """At 10 minutes remaining, a 0.01% dip below strike is noise, not a broken thesis.

    This is the whole reason the exit policy is priced rather than
    percentage based: spot is on the wrong side of the strike and the mark is
    below entry, yet the position's expected value is essentially unchanged.
    Selling here converts recoverable variance into a realised loss.
    """
    risk = RiskManager()
    risk.add_position(
        "market-1",
        {
            "market_id": "market-1",
            "event_id": "event-1",
            "outcome_id": "yes-1",
            "outcome": "YES",
            "entry_price": 0.55,
            "amount_ngn": 500,
            "filled_quantity": 9.09,
            "confirmed_filled": True,
            "strategy": "TAKER",
            "asset": "BTC",
            "timeframe": "15min",
        },
    )

    monkeypatch.setattr(
        bot,
        "active_markets",
        [{
            "market_id": "market-1",
            "threshold": 60_000.0,
            "secs_to_close": 600,  # 10 minutes left
            "yes_price": 0.54,
            "no_price": 0.46,
            "minimum_order_amount": 100,
            "fee_rate": 0.02,
        }],
    )
    # Spot is slightly below threshold ($59,994 -> -0.01% dip)
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _a: (59_994.0, time.time()))
    monkeypatch.setattr(bot, "_tg_app", None)

    client = MockClient()
    asyncio.run(bot._evaluate_and_exit_positions("chat-1", client, risk, {}))

    # Position must NOT be exited!
    assert "market-1" in risk.open_positions
    assert len(client.cancelled_orders) == 0


def test_late_candle_adverse_move_triggers_protective_stop_loss(monkeypatch):
    """At 2 minutes remaining (secs=120), if spot is decisively on the wrong side and win_prob collapsed, trigger stop loss."""
    risk = RiskManager()
    risk.add_position(
        "market-1",
        {
            "market_id": "market-1",
            "event_id": "event-1",
            "outcome_id": "yes-1",
            "outcome": "YES",
            "entry_price": 0.55,
            "amount_ngn": 500,
            "filled_quantity": 9.09,
            "confirmed_filled": True,
            "strategy": "TAKER",
            "asset": "BTC",
            "timeframe": "15min",
        },
    )

    monkeypatch.setattr(
        bot,
        "active_markets",
        [{
            "market_id": "market-1",
            "threshold": 60_000.0,
            "secs_to_close": 120,  # 2 minutes left
            "yes_price": 0.15,
            "no_price": 0.85,
            "minimum_order_amount": 100,
            "fee_rate": 0.02,
        }],
    )
    # Spot is decisively below threshold ($59,800 -> -0.33% dip)
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _a: (59_800.0, time.time()))
    monkeypatch.setattr(bot, "_tg_app", None)

    client = MockClient()
    asyncio.run(bot._evaluate_and_exit_positions("chat-1", client, risk, {}))

    # Position must be exited to salvage capital
    assert "market-1" not in risk.open_positions


def test_take_profit_does_not_fire_just_because_the_price_rose(monkeypatch):
    """A mark of 0.85 against a near-certain model is not a reason to sell.

    The old rule sold at a fixed percentage gain. Under it, a position the
    model prices at ~1.00 with five minutes left was sold for 0.85 and a fee,
    handing away ~0.15 of expected value per share every time the market
    simply came round to our view. Exit value has to beat hold value, and
    here it does not.
    """
    risk = RiskManager()
    risk.add_position(
        "market-1",
        {
            "market_id": "market-1",
            "event_id": "event-1",
            "outcome_id": "yes-1",
            "outcome": "YES",
            "entry_price": 0.55,
            "amount_ngn": 500,
            "filled_quantity": 9.09,
            "confirmed_filled": True,
            "strategy": "TAKER",
            "asset": "SOL",
            "timeframe": "15min",
        },
    )

    monkeypatch.setattr(
        bot,
        "active_markets",
        [{
            "market_id": "market-1",
            "threshold": 100.0,
            "secs_to_close": 300,
            "yes_price": 0.80,  # the book is bidding far above our estimate
            "no_price": 0.20,
            "minimum_order_amount": 100,
            "fee_rate": 0.02,
        }],
    )
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _a: (103.0, time.time()))
    monkeypatch.setattr(bot, "_tg_app", None)

    client = MockClient()
    asyncio.run(bot._evaluate_and_exit_positions("chat-1", client, risk, {}))

    # The market agreeing with us is not an exit signal.
    assert "market-1" in risk.open_positions
    assert len(client.cancelled_orders) == 0


def test_take_profit_fires_when_the_market_overpays_the_model(monkeypatch):
    """Sell when the bid exceeds what the position is actually worth.

    The one time selling a winner is correct: someone is paying more than our
    own estimate of the outcome. That is edge, and it is the only form of
    profit-taking that survives the fee.
    """
    risk = RiskManager()
    risk.add_position(
        "market-1",
        {
            "market_id": "market-1",
            "event_id": "event-1",
            "outcome_id": "yes-1",
            "outcome": "YES",
            "entry_price": 0.55,
            "amount_ngn": 500,
            "filled_quantity": 9.09,
            "confirmed_filled": True,
            "strategy": "TAKER",
            "asset": "SOL",
            "timeframe": "15min",
        },
    )

    monkeypatch.setattr(
        bot,
        "active_markets",
        [{
            "market_id": "market-1",
            "threshold": 100.0,
            "secs_to_close": 300,
            "yes_price": 0.85,  # High price target reached!
            "no_price": 0.15,
            "minimum_order_amount": 100,
            "fee_rate": 0.02,
        }],
    )
    # Spot barely above the strike with five minutes left: the model prices
    # this around a coin flip, but the book is bidding 0.80 for it.
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _a: (100.02, time.time()))
    monkeypatch.setattr(bot, "_tg_app", None)

    client = MockClient(order_response={"status": "open", "filledSize": 10})
    asyncio.run(bot._evaluate_and_exit_positions("chat-1", client, risk, {}))

    # Position was sold into the overpay.
    assert "market-1" not in risk.open_positions
    assert risk.daily_realized_pnl > 0


def test_unfilled_maker_order_management(monkeypatch):
    """A maker quote past its timeout is withdrawn, both legs, and settled.

    Three things have to be true at once, and each was previously violated:

    * the order is actually cancelled on the exchange (it used to be dropped
      from the risk book while still live, so three unfilled MAKER entries
      resolved with no cancel and no message);
    * the position leaves the risk book only once the exchange confirms the
      cancel left nothing filled — a cancel and a fill can cross in flight,
      and deleting a filled leg would leave untracked shares;
    * the trade row is settled rather than left dangling.

    The sibling leg is withdrawn with it: cancelling one leg of a two-sided
    quote and leaving the other resting is how a market maker acquires a
    position it never meant to hold.
    """
    risk = RiskManager()
    risk.add_position(
        "market-resting:yes",
        {
            "market_id": "market-resting",
            "event_id": "event-resting",
            "outcome_id": "yes-1",
            "order_id": "maker-order-1",
            "outcome": "YES",
            "entry_price": 0.52,
            "amount_ngn": 500,
            "filled_quantity": 0.0,
            "confirmed_filled": False,
            "strategy": "MAKER",
            "asset": "SOL",
            "timeframe": "15min",
            "placed_at": time.time() - 60_000,  # far past the quote timeout
        },
    )
    risk.add_position(
        "market-resting:no",
        {
            "market_id": "market-resting",
            "event_id": "event-resting",
            "outcome_id": "no-1",
            "order_id": "maker-order-2",
            "outcome": "NO",
            "entry_price": 0.46,
            "amount_ngn": 500,
            "filled_quantity": 0.0,
            "confirmed_filled": False,
            "strategy": "MAKER",
            "asset": "SOL",
            "timeframe": "15min",
            "placed_at": time.time() - 60_000,
        },
    )
    monkeypatch.setattr(
        bot.strategies.maker.maker_strategy, "open_quotes",
        {"market-resting": {"spot": 100.0, "legs": [], "placed_at": time.time() - 60_000}},
    )

    monkeypatch.setattr(
        bot,
        "active_markets",
        [{
            "market_id": "market-resting",
            "threshold": 100.0,
            "secs_to_close": 400,
            "yes_price": 0.52,
            "no_price": 0.48,
            "engine": "CLOB",
            "fee_rate": 0.02,
        }],
    )
    monkeypatch.setattr(bot, "_tg_app", None)

    class _CancellingClient(MockClient):
        """Resting when polled, cancelled once we ask it to cancel."""

        async def get_order(self, order_id):
            if order_id in self.cancelled_orders:
                return {"status": "cancelled", "filledSize": 0}
            return {"status": "open", "filledSize": 0}

    client = _CancellingClient()
    asyncio.run(bot._manage_unfilled_maker_orders("chat-1", client, risk, {}))

    # Both legs went, not one.
    assert "maker-order-1" in client.cancelled_orders
    assert "maker-order-2" in client.cancelled_orders
    # The exchange confirmed nothing filled, so the whole quote is gone.
    assert risk.open_positions == {}
    assert "market-resting" not in bot.strategies.maker.maker_strategy.open_quotes


def test_a_leg_that_fills_in_the_cancel_race_is_kept(monkeypatch):
    """The exchange reporting a fill after our cancel must not erase the leg."""
    risk = RiskManager()
    risk.add_position(
        "market-resting:yes",
        {
            "market_id": "market-resting",
            "event_id": "event-resting",
            "outcome_id": "yes-1",
            "order_id": "maker-order-1",
            "outcome": "YES",
            "entry_price": 0.52,
            "amount_ngn": 500,
            "filled_quantity": 0.0,
            "confirmed_filled": False,
            "strategy": "MAKER",
            "asset": "SOL",
            "timeframe": "15min",
            "placed_at": time.time() - 60_000,
        },
    )
    monkeypatch.setattr(
        bot.strategies.maker.maker_strategy, "open_quotes",
        {"market-resting": {"spot": 100.0, "legs": [], "placed_at": time.time() - 60_000}},
    )
    monkeypatch.setattr(
        bot,
        "active_markets",
        [{
            "market_id": "market-resting",
            "threshold": 100.0,
            "secs_to_close": 400,
            "yes_price": 0.52,
            "no_price": 0.48,
            "engine": "CLOB",
            "fee_rate": 0.02,
        }],
    )
    monkeypatch.setattr(bot, "_tg_app", None)

    class RaceClient(MockClient):
        """Open and empty when polled, filled once we try to cancel it."""

        async def get_order(self, order_id):
            if order_id in self.cancelled_orders:
                return {"status": "filled", "filledSize": 9.6, "avgFillPrice": 0.52}
            return {"status": "open", "filledSize": 0}

    client = RaceClient()
    asyncio.run(bot._manage_unfilled_maker_orders("chat-1", client, risk, {}))

    # We still hold it: untracked shares are a worse outcome than a stale row.
    assert "market-resting:yes" in risk.open_positions
    assert risk.open_positions["market-resting:yes"]["confirmed_filled"] is True


def test_taker_fires_on_a_near_certain_setup_near_close():
    """TAKER fires in the final minute on a large distance, at a capped price.

    The price ceiling is what keeps this honest: a near-certain outcome still
    has to be priced inside the band where the fee-adjusted EV can clear the
    margin, so the strategy refuses to pay 0.95 for a 0.97 shot.
    """
    strat = TakerStrategy()
    market = {
        "event_id": "e",
        "market_id": "m",
        "asset": "BTC",
        "timeframe": "15min",
        "secs_to_close": 60,
        "threshold": 60_000.0,
        "yes_price": 0.60,
        "no_price": 0.40,
        "engine": "CLOB",
        "fee_rate": 0.02,
        "status": "open",
        "yes_id": "y",
        "no_id": "n",
        "title": "BTC > 60k",
    }
    # Spot is $60,350 (+0.58% distance) with a minute left: near-certain.
    books = {
        "y": {"bids": [{"price": 0.58, "quantity": 900.0}],
              "asks": [{"price": 0.60, "quantity": 900.0}]},
        "n": {"bids": [{"price": 0.38, "quantity": 900.0}],
              "asks": [{"price": 0.40, "quantity": 900.0}]},
    }
    sig = asyncio.run(strat.evaluate(
        market, {"chat_id": "u-near-close", "mode": "balanced"},
        MarketState(), spot_price=60_350.0, books=books,
    ))
    assert sig is not None
    assert sig.strategy == "TAKER"
    assert sig.outcome == "YES"
    assert sig.certainty >= 0.90
    assert sig.market_price == 0.60
    assert 0.0 < sig.size_pct <= config.TAKER_MAX_SIZE_PCT
