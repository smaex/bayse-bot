import asyncio

import executor
from risk import RiskManager
from strategies.base import TradeSignal


class FakeClient:
    def __init__(self, quote):
        self.quote = quote
        self.place_calls = []

    async def get_quote(self, **_kwargs):
        if isinstance(self.quote, Exception):
            raise self.quote
        return self.quote

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        raise AssertionError("order placement must not be reached")


def _signal(certainty=0.8):
    return TradeSignal(
        strategy="SNIPE",
        event_id="e",
        market_id="m",
        asset="BTC",
        timeframe="5min",
        outcome="YES",
        outcome_id="yes",
        certainty=certainty,
        win_prob=0.8,
        market_price=0.6,
        size_pct=0.01,
        reason="test",
        mode_floor=0.6,
    )


def _install(monkeypatch):
    monkeypatch.setattr(
        executor,
        "active_markets",
        [{
            "market_id": "m",
            "engine": "AMM",
            "minimum_order_amount": 100,
            "secs_to_close": 120,
            "threshold": 100,
        }],
    )
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *_args: 1.0)


def test_amm_quote_outage_does_not_fall_back_to_a_live_order(monkeypatch):
    _install(monkeypatch)
    client = FakeClient(TimeoutError("quote unavailable"))

    asyncio.run(executor._execute_logic(
        "chat", _signal(), client, RiskManager(),
        {"mode": "safe", "risk_pct": 1, "mintrade": 100, "maxtrade": 5000},
        20_000, 20_000,
    ))

    assert client.place_calls == []


def test_incomplete_amm_quote_does_not_place_an_order(monkeypatch):
    _install(monkeypatch)
    client = FakeClient({"price": 0.6, "quantity": 1, "completeFill": False})

    asyncio.run(executor._execute_logic(
        "chat", _signal(), client, RiskManager(),
        {"mode": "safe", "risk_pct": 1, "mintrade": 100, "maxtrade": 5000},
        20_000, 20_000,
    ))

    assert client.place_calls == []


def test_low_certainty_probe_does_not_use_real_money(monkeypatch):
    _install(monkeypatch)
    client = FakeClient({"price": 0.6, "quantity": 1, "completeFill": True})

    asyncio.run(executor._execute_logic(
        "chat", _signal(certainty=0.4), client, RiskManager(),
        {"mode": "safe", "risk_pct": 1, "mintrade": 100, "maxtrade": 5000},
        20_000, 20_000,
    ))

    assert client.place_calls == []
