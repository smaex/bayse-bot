import asyncio
import time

import bot
from client import BayseClient
from risk import RiskManager


class FakeExitClient:
    parse_filled_shares = staticmethod(BayseClient.parse_filled_shares)

    def __init__(self, *, order_state=None, sold_shares=10,
                 current_value=500, sell_price=0.5):
        self.order_state = order_state or {"status": "open", "quantity": 10}
        self.sold_shares = sold_shares
        self.current_value = current_value
        self.sell_price = sell_price
        self.cancelled = []
        self.sell_calls = []

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"status": "cancelled"}

    async def get_order(self, _order_id):
        return self.order_state

    async def get_position(self, _outcome_id):
        return {
            "availableBalance": 10,
            "sellPrice": self.sell_price,
            "currentValue": self.current_value,
        }

    async def get_quote(self, *_args):
        return {"completeFill": True, "quantity": 10, "price": 0.5}

    async def place_order(self, **kwargs):
        self.sell_calls.append(kwargs)
        return {
            "order": {
                "id": "sell-1",
                "status": "filled",
                "filledSize": self.sold_shares,
                "avgFillPrice": 0.5,
                "amount": kwargs["amount"],
            }
        }


def _risk_with_position(*, maker=False, confirmed=True):
    risk = RiskManager()
    risk.add_position(
        "market-1",
        {
            "market_id": "market-1",
            "event_id": "event-1",
            "outcome_id": "yes-1",
            "order_id": "buy-1" if maker else None,
            "outcome": "YES",
            "entry_price": 0.6,
            "amount_ngn": 600,
            "filled_quantity": 10 if confirmed else 0,
            "confirmed_filled": confirmed,
            "strategy": "MAKER" if maker else "SNIPE",
            "asset": "BTC",
            "timeframe": "5min",
        },
    )
    return risk


def _install_market(monkeypatch):
    monkeypatch.setattr(
        bot,
        "active_markets",
        [{
            "market_id": "market-1",
            "threshold": 100,
            "secs_to_close": 120,
            "yes_price": 0.5,
            "no_price": 0.5,
            "minimum_order_amount": 100,
            "fee_rate": 0.02,
        }],
    )
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _asset: (99, time.time()))
    monkeypatch.setattr(bot, "win_probability", lambda *_a, **_kw: 0.2)
    monkeypatch.setattr(bot, "realized_vol_hourly", lambda *_a, **_kw: 0.1)
    monkeypatch.setattr(bot, "_tg_app", None)


def test_unfilled_maker_is_cancelled_without_sending_a_sell(monkeypatch):
    _install_market(monkeypatch)
    risk = _risk_with_position(maker=True, confirmed=False)
    client = FakeExitClient(order_state={"status": "cancelled", "quantity": 10})

    asyncio.run(bot._evaluate_and_exit_positions("chat", client, risk, {}))

    assert client.cancelled == ["buy-1"]
    assert client.sell_calls == []
    assert risk.open_positions == {}


def test_exit_uses_desired_ngn_proceeds_and_confirmed_fill(monkeypatch):
    _install_market(monkeypatch)
    risk = _risk_with_position()
    client = FakeExitClient()

    asyncio.run(bot._evaluate_and_exit_positions("chat", client, risk, {}))

    assert len(client.sell_calls) == 1
    sell = client.sell_calls[0]
    assert sell["side"] == "SELL"
    assert sell["amount"] == 497.5  # 99.5% of exchange currentValue, not 10 shares
    assert sell["max_slippage"] == 0.20
    assert risk.open_positions == {}
    assert risk.daily_realized_pnl == -105.0


def test_partial_exit_keeps_unsold_shares_under_risk(monkeypatch):
    _install_market(monkeypatch)
    risk = _risk_with_position()
    client = FakeExitClient(sold_shares=5)

    asyncio.run(bot._evaluate_and_exit_positions("chat", client, risk, {}))

    remaining = risk.open_positions["market-1"]
    assert remaining["filled_quantity"] == 5
    assert remaining["amount_ngn"] == 300
    assert risk.current_free_cash == 247.5
    assert risk.daily_realized_pnl == -52.5


def test_model_probability_cannot_invent_an_executable_take_profit(monkeypatch):
    _install_market(monkeypatch)
    bot.active_markets[0]["yes_price"] = 0.59
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _asset: (101, time.time()))
    monkeypatch.setattr(bot, "win_probability", lambda *_a, **_kw: 0.99)
    risk = _risk_with_position()
    client = FakeExitClient(current_value=700, sell_price=0.70)

    asyncio.run(bot._evaluate_and_exit_positions("chat", client, risk, {}))

    assert client.sell_calls == []
    assert "market-1" in risk.open_positions


def test_take_profit_requires_at_least_five_percent_net_quote(monkeypatch):
    _install_market(monkeypatch)
    bot.active_markets[0]["yes_price"] = 0.70
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _asset: (101, time.time()))
    monkeypatch.setattr(bot, "win_probability", lambda *_a, **_kw: 0.90)
    risk = _risk_with_position()
    client = FakeExitClient(current_value=620, sell_price=0.62)

    asyncio.run(bot._evaluate_and_exit_positions("chat", client, risk, {}))

    assert client.sell_calls == []
    assert "market-1" in risk.open_positions
