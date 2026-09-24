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
from strategies.oracle_arb import OracleArbStrategy
from strategies.snipe import SnipeStrategy


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
    """At 10 minutes remaining (secs=600), a 0.01% dip below strike is normal noise and must NOT trigger stop loss."""
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
            "strategy": "SNIPE",
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
    monkeypatch.setattr(bot, "win_probability", lambda *_a, **_kw: 0.48)  # Model is 48% (normal noise)
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
            "strategy": "SNIPE",
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
    monkeypatch.setattr(bot, "win_probability", lambda *_a, **_kw: 0.15)  # Thesis broken
    monkeypatch.setattr(bot, "_tg_app", None)

    client = MockClient()
    asyncio.run(bot._evaluate_and_exit_positions("chat-1", client, risk, {}))

    # Position must be exited to salvage capital
    assert "market-1" not in risk.open_positions


def test_take_profit_triggers_on_price_target(monkeypatch):
    """If market price reaches 0.85 (>= 0.82 target), take-profit locks in the gain."""
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
            "strategy": "SNIPE",
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
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _a: (103.0, time.time()))
    monkeypatch.setattr(bot, "win_probability", lambda *_a, **_kw: 0.92)
    monkeypatch.setattr(bot, "_tg_app", None)

    client = MockClient()
    asyncio.run(bot._evaluate_and_exit_positions("chat-1", client, risk, {}))

    # Position was exited for profit
    assert "market-1" not in risk.open_positions
    assert risk.daily_realized_pnl > 0


def test_unfilled_maker_order_management(monkeypatch):
    """Unfilled maker orders that get filled on exchange are marked filled; stale orders are cancelled."""
    risk = RiskManager()
    risk.add_position(
        "market-resting",
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
            "placed_at": time.time() - 200,  # 200s old (stale)
        },
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
        }],
    )
    monkeypatch.setattr(bot, "_tg_app", None)

    client = MockClient(order_response={"status": "open", "filledSize": 0})
    asyncio.run(bot._manage_unfilled_maker_orders("chat-1", client, risk, {}))

    # Stale order was cancelled and removed from risk
    assert "maker-order-1" in client.cancelled_orders
    assert "market-resting" not in risk.open_positions


def test_oracle_arb_evaluates_near_close():
    """Oracle Arb fires in final 120s with high distance and price capped at 0.75."""
    strat = OracleArbStrategy()
    market = {
        "event_id": "e",
        "market_id": "m",
        "asset": "BTC",
        "timeframe": "15min",
        "secs_to_close": 60,
        "threshold": 60_000.0,
        "yes_price": 0.65,
        "no_price": 0.35,
        "yes_id": "y",
        "no_id": "n",
        "title": "BTC > 60k",
    }
    # Direct price is $60,350 (+0.58% distance) and 1s old
    strat._get_oracle_price = lambda _a: (60_350.0, 1.0)

    sig = asyncio.run(strat.evaluate(market, {}, MarketState()))
    assert sig is not None
    assert sig.strategy == "ORACLE_ARB"
    assert sig.outcome == "YES"
    assert sig.certainty >= 0.90
    assert sig.market_price == 0.65
