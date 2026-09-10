import asyncio
import time

import pytest

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


def test_official_quote_amount_and_quantity_define_effective_buy_price():
    # Exact public documentation fixture (USD multiplier = 1).
    official_quote = {
        "price": 0.7235,
        "quantity": 138.21,
        "amount": 100,
        "costOfShares": 98.04,
        "fee": 1.96,
        "currencyBaseMultiplier": 1,
        "completeFill": True,
    }
    ngn_quote = {
        "price": 0.65,
        "quantity": 1.5,
        "costOfShares": 97.5,
        "fee": 2.5,
        "amount": 100,
        "currencyBaseMultiplier": 100,
        "completeFill": True,
    }

    assert executor._quote_effective_buy_price(
        official_quote
    ) == pytest.approx(100 / 138.21)
    assert executor._quote_effective_buy_price(ngn_quote) == pytest.approx(2 / 3)
    assert executor._clob_buy_effective_price(0.65, 0.05) == pytest.approx(
        2 / 3
    )


def test_book_timestamp_accepts_seconds_milliseconds_and_iso8601():
    now = time.time()

    assert not executor._book_is_stale(
        {"timestamp": now - 1}, max_age=5, now=now
    )
    assert not executor._book_is_stale(
        {"timestamp": (now - 1) * 1000}, max_age=5, now=now
    )
    assert executor._book_is_stale(
        {"updatedAt": "2000-01-01T00:00:00Z"}, max_age=5, now=now
    )
    assert executor._book_is_stale(
        {"timestamp": "not-a-date"}, max_age=5, now=now
    )
    # The official level schema does not guarantee a timestamp; a freshly
    # fetched timestamp-less response is therefore permitted.
    assert not executor._book_is_stale({}, max_age=5, now=now)


class FailingBookClient:
    def __init__(self):
        self.place_calls = []

    async def get_orderbook(self, *_args, **_kwargs):
        raise TimeoutError("book unavailable")

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        raise AssertionError("order placement must not be reached")


class BookClient(FailingBookClient):
    def __init__(self, asks, timestamp=None):
        super().__init__()
        self.asks = asks
        self.timestamp = timestamp

    async def get_orderbook(self, *_args, **_kwargs):
        book = {"asks": self.asks, "bids": []}
        if self.timestamp is not None:
            book["timestamp"] = self.timestamp
        return book


def _install_clob(monkeypatch):
    monkeypatch.setattr(
        executor,
        "active_markets",
        [{
            "market_id": "m",
            "engine": "CLOB",
            "minimum_order_amount": 100,
            "secs_to_close": 120,
            "threshold": 100,
            "fee_rate": 0.05,
        }],
    )
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *_args: 1.0)
    executor._trade_cooldown.clear()


def _clob_signal(win_prob=0.90, market_price=0.55):
    signal = _signal()
    signal.asset = "SOL"
    signal.timeframe = "15min"
    signal.win_prob = win_prob
    signal.market_price = market_price
    return signal


def _run_clob(signal, client):
    asyncio.run(executor._execute_logic(
        "chat", signal, client, RiskManager(),
        {"mode": "aggressive", "risk_pct": 1,
         "mintrade": 100, "maxtrade": 5000},
        20_000, 20_000,
    ))


def test_clob_book_outage_fails_closed_instead_of_using_midpoint(monkeypatch):
    _install_clob(monkeypatch)
    client = FailingBookClient()

    _run_clob(_clob_signal(), client)

    assert client.place_calls == []


def test_clob_exact_share_reducing_fee_can_reject_apparent_edge(monkeypatch):
    _install_clob(monkeypatch)
    client = BookClient([{"price": 0.60, "total": 500}])

    # 0.615 looks profitable against a 0.60 ask, but a 5% Bayse fee
    # reduces received shares: effective price = 0.60 / 0.975 = 0.61538.
    _run_clob(_clob_signal(win_prob=0.615), client)

    assert client.place_calls == []


def test_clob_insufficient_executable_depth_fails_closed(monkeypatch):
    _install_clob(monkeypatch)
    client = BookClient([{"price": 0.59, "total": 50}])

    _run_clob(_clob_signal(win_prob=0.75), client)

    assert client.place_calls == []


def test_clob_explicitly_stale_book_fails_closed(monkeypatch):
    _install_clob(monkeypatch)
    client = BookClient(
        [{"price": 0.59, "total": 500}],
        timestamp=time.time() - 60,
    )

    _run_clob(_clob_signal(win_prob=0.75), client)

    assert client.place_calls == []
