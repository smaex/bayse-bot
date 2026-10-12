import asyncio
import math
import time

import pytest

import executor
from client import BayseClient
from risk import RiskManager
from strategies.base import TradeSignal


class FakeClient:
    def __init__(self, quote):
        self.quote = quote
        self.quote_calls = 0
        self.place_calls = []

    async def get_quote(self, **_kwargs):
        self.quote_calls += 1
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
    monkeypatch.setattr(executor.database, "check_connection", lambda: True)


def test_ambiguous_single_order_write_pauses_and_persists_the_account(monkeypatch):
    _install(monkeypatch)
    quote = {
        "price": 0.60, "quantity": 2, "amount": 100,
        "currencyBaseMultiplier": 100, "completeFill": True,
    }

    class _DispatchTimeoutClient(FakeClient):
        async def place_order(self, **kwargs):
            self.place_calls.append(kwargs)
            raise TimeoutError("connection lost after order dispatch")

    client = _DispatchTimeoutClient(quote)
    risk = RiskManager()
    settings = {
        "mode": "balanced", "risk_pct": 2,
        "mintrade": 100, "maxtrade": 5_000,
    }
    asyncio.run(executor._execute_logic(
        "chat", _signal(), client, risk, settings, 20_000, 20_000,
    ))

    assert len(client.place_calls) == 1
    assert risk.paused is True
    assert settings["paused"] is True
    assert settings["paused_reason"] == "order_reconciliation"


def test_unavailable_trade_ledger_blocks_buy_before_exchange_write(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(executor.database, "check_connection", lambda: False)
    client = FakeClient({
        "price": 0.60, "quantity": 2, "amount": 100,
        "currencyBaseMultiplier": 100, "completeFill": True,
    })
    risk = RiskManager()
    settings = {
        "mode": "balanced", "risk_pct": 2,
        "mintrade": 100, "maxtrade": 5_000,
    }

    asyncio.run(executor._execute_logic(
        "chat", _signal(), client, risk, settings, 20_000, 20_000,
    ))

    assert client.place_calls == []
    assert risk.open_positions == {}
    assert "exec:database_unavailable" in (
        executor.stall._users.get("chat", {}).get("rejects") or {}
    )


def test_malformed_confirmed_share_quantity_is_not_booked_as_a_fill(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(executor, "_tg_app", None)
    monkeypatch.setattr(executor.telegram_bot, "_bot_app", None)
    response = {"order": {
        "id": "buy-malformed", "status": "filled",
        "filledSize": float("nan"), "avgFillPrice": 0.60,
    }}

    class _MalformedFillClient(FakeClient):
        parse_filled_shares = staticmethod(BayseClient.parse_filled_shares)

        async def place_order(self, **kwargs):
            self.place_calls.append(kwargs)
            return response

        async def get_order(self, _order_id):
            return response

    client = _MalformedFillClient({
        "price": 0.60, "quantity": 2, "amount": 100,
        "currencyBaseMultiplier": 100, "completeFill": True,
    })
    risk = RiskManager()
    settings = {
        "mode": "balanced", "risk_pct": 2,
        "mintrade": 100, "maxtrade": 5_000,
    }

    asyncio.run(executor._execute_logic(
        "chat", _signal(), client, risk, settings, 20_000, 20_000,
    ))

    assert risk.open_positions == {}
    assert risk.paused is True
    assert settings["paused"] is True


def test_confirmed_buy_without_ledger_row_pauses_with_position_tracked(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(executor.database, "record_trade", lambda **_kwargs: None)
    monkeypatch.setattr(executor, "_tg_app", None)
    monkeypatch.setattr(executor.telegram_bot, "_bot_app", None)

    class _FilledClient(FakeClient):
        parse_filled_shares = staticmethod(BayseClient.parse_filled_shares)

        async def place_order(self, **kwargs):
            self.place_calls.append(kwargs)
            return {"order": {
                "id": "buy-1", "status": "filled", "quantity": 2.0,
                "avgFillPrice": 0.60, "totalCost": kwargs["amount"],
            }}

    client = _FilledClient({
        "price": 0.60, "quantity": 2, "amount": 100,
        "currencyBaseMultiplier": 100, "completeFill": True,
    })
    risk = RiskManager()
    settings = {
        "mode": "balanced", "risk_pct": 2,
        "mintrade": 100, "maxtrade": 5_000,
    }

    asyncio.run(executor._execute_logic(
        "chat", _signal(), client, risk, settings, 20_000, 20_000,
    ))

    assert len(client.place_calls) == 1
    assert len(risk.open_positions) == 1
    position = next(iter(risk.open_positions.values()))
    assert position["ledger_reconciliation_pending"] is True
    assert position["filled_quantity"] == 2.0
    assert risk.paused is True
    assert settings["paused"] is True


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


def test_platform_minimum_never_overrides_the_per_trade_risk_cap(monkeypatch):
    _install(monkeypatch)
    client = FakeClient({
        "price": 0.60, "quantity": 2, "amount": 100,
        "currencyBaseMultiplier": 100, "completeFill": True,
    })

    # 1% of ₦1,000 is only ₦10. Even though the wallet and exchange minimum
    # can fund ₦100, the minimum must not enlarge the risk-approved order.
    asyncio.run(executor._execute_logic(
        "chat", _signal(), client, RiskManager(),
        {"mode": "safe", "risk_pct": 1, "mintrade": 100, "maxtrade": 5000},
        1_000, 1_000,
    ))

    assert client.quote_calls == 0
    assert client.place_calls == []


def test_non_finite_amm_quote_is_rejected_before_pricing_or_placement(monkeypatch):
    _install(monkeypatch)
    client = FakeClient({
        "price": 0.60, "quantity": float("inf"), "amount": 100,
        "completeFill": True,
    })

    asyncio.run(executor._execute_logic(
        "chat", _signal(), client, RiskManager(),
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


def test_quote_cost_helper_marks_non_finite_cost_fields_invalid():
    for quote in (
        {"quantity": math.inf, "amount": 100},
        {"quantity": 2, "amount": math.nan},
        {"quantity": 2, "amount": 100, "fee": math.inf},
    ):
        assert math.isnan(executor._quote_effective_buy_price(quote))


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
    monkeypatch.setattr(executor.database, "check_connection", lambda: True)
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


class RecordingBookClient(BookClient):
    """A tradable book, so a depth decision can be observed end to end."""

    def __init__(self, asks, timestamp=None):
        super().__init__(asks, timestamp)
        self.placed = []

    async def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"order": {
            "id": "o1", "status": "filled", "quantity": 100.0,
            "avgFillPrice": kwargs.get("price") or 0.5,
            "amount": kwargs.get("amount"),
        }}

    def parse_filled_shares(self, order):
        return float(order.get("quantity") or 0.0)


def test_clob_depth_is_measured_in_account_currency_not_raw_total(monkeypatch):
    """`quantity` is shares; `total` carries no unit in the payload.

    One share costs ``price * CURRENCY_BASE_MULTIPLIER`` (documented: ₦100 per
    share at 1.00), so 3 shares at 0.50 is ₦150 of depth -- not the level's
    unscaled ``total`` of 1.5. Reading that as naira turned a fundable market
    into a false "insufficient resting depth" skip (₦1.50 < ₦100 minimum), and
    on deeper books it clipped a sized order to a hundredth of the liquidity
    that was actually there. The share count is the unambiguous number.
    """
    _install_clob(monkeypatch)
    executor._market_min_cache.pop("m", None)
    monkeypatch.setattr(executor.database, "record_trade", lambda **_kwargs: 1)
    client = RecordingBookClient([
        {"price": 0.50, "quantity": 3.0, "total": 1.5},
    ])

    # equity 20,000 at 1% -> ₦200 desired; ₦150 available -> clip to 142.5.
    _run_clob(_clob_signal(win_prob=0.75, market_price=0.50), client)

    assert client.placed, "a ₦150 book must be tradable at a ₦100 minimum"
    assert client.placed[0]["amount"] == pytest.approx(142.5), (
        "the clip must be 95% of the real NGN depth, not of an unscaled total"
    )


def test_level_notional_prefers_shares_and_falls_back_to_total():
    from strategies.book import level_notional

    # Shares are unambiguous and priced with the account currency's multiplier.
    assert level_notional({"price": 0.50, "quantity": 3.0, "total": 1.5}) == 150.0
    # No share count: the exchange's own notional is the only number left.
    assert level_notional({"price": 0.50, "total": 150.0}) == 150.0
    # Tuple levels and junk are handled without raising.
    assert level_notional((0.50, 3.0)) == 150.0
    assert level_notional({"price": 0.50}) == 0.0


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
