"""MAKER must quote against the live order book, not against a midpoint.

Production symptom (Telegram, 2026-09-25): after /resume, MAKER placed five
₦100 post-only bids — every one at exactly 0.580 — and none filled; the account
went ~19 hours without a confirmed fill. The strategy computes its bid from
Bayse's outcome *probability* price and clamps it to the 0.58 risk/reward
ceiling, so the bid is 0.58 whatever the book looks like. With the chosen side
trading around 0.65–0.75 that bid sits many ticks under the best bid (it can
only fill if every bid above it is exhausted first), and with the side trading
below 0.58 it crosses the ask (a post-only reject). Each dead quote was
cancelled after MAKER_ORDER_TIMEOUT and re-placed a minute later.

These tests pin the fix: price the post-only bid against the live book, never
above the strategy's ceiling, and skip — with a named reason — quotes that
cannot fill. The 0.58 ceiling itself is unchanged.
"""

from __future__ import annotations

import asyncio
import random
import time

import pytest

import config
import executor
import stall
from risk import RiskManager
from strategies.base import TradeSignal


def _book(bids=(), asks=(), **extra):
    book = {
        "marketId": "m",
        "outcomeId": "yes",
        "bids": [{"price": p, "quantity": 500, "total": p * 500} for p in bids],
        "asks": [{"price": p, "quantity": 500, "total": p * 500} for p in asks],
    }
    book.update(extra)
    return book


# ── pure pricing ─────────────────────────────────────────────────────────────

def test_quote_buried_under_the_book_is_skipped_not_rested():
    """The production case: 0.58 ceiling, market bidding 0.66."""
    price, code, detail = executor._maker_quote_against_book(
        _book(bids=[0.66, 0.65], asks=[0.69, 0.70]), 0.58
    )
    assert price is None
    assert code == "maker_quote_behind_book"
    assert "8 tick(s)" in detail and "0.660" in detail


def test_quote_that_would_cross_steps_inside_the_ask():
    """Post-only at/through the ask is rejected by Bayse; rest one tick inside it."""
    price, code, _ = executor._maker_quote_against_book(
        _book(bids=[0.52], asks=[0.56, 0.57]), 0.58
    )
    assert code == ""
    assert price == pytest.approx(0.55)          # lower than intended, top of book


def test_no_passive_price_inside_the_band_is_skipped():
    price, code, _ = executor._maker_quote_against_book(_book(asks=[0.50]), 0.58)
    assert price is None
    assert code == "maker_would_cross_book"


@pytest.mark.parametrize(
    "bids,asks,expected",
    [
        ([0.57], [0.60], 0.58),   # improves the best bid
        ([0.58], [0.60], 0.58),   # joins the best bid
        ([0.59], [0.61], 0.58),   # one tick behind: still allowed
        ([], [], 0.58),           # empty book: we are the market
        ([0.55], [], 0.58),       # no asks: nothing to cross
    ],
)
def test_competitive_quotes_keep_the_strategy_price(bids, asks, expected):
    price, code, _ = executor._maker_quote_against_book(_book(bids=bids, asks=asks), 0.58)
    assert code == ""
    assert price == pytest.approx(expected)


def test_two_ticks_behind_is_skipped():
    price, code, _ = executor._maker_quote_against_book(_book(bids=[0.60], asks=[0.62]), 0.58)
    assert price is None and code == "maker_quote_behind_book"


def test_malformed_levels_are_ignored():
    book = {"bids": [{"price": "x"}, {"nope": 1}, {"price": 0.57}], "asks": [{"price": None}]}
    price, code, _ = executor._maker_quote_against_book(book, 0.58)
    assert code == "" and price == pytest.approx(0.58)


def test_book_pricing_never_pays_more_than_the_strategy_ceiling():
    """Property: the risk/reward ceiling is not a liquidity setting."""
    rng = random.Random(7)
    for _ in range(2_000):
        max_price = round(rng.uniform(0.50, 0.58), 3)
        best_bid = round(rng.uniform(0.30, 0.90), 2)
        best_ask = round(min(0.99, best_bid + rng.choice([0.01, 0.02, 0.05, 0.10])), 2)
        price, code, _ = executor._maker_quote_against_book(
            _book(bids=[best_bid], asks=[best_ask]), max_price
        )
        if price is None:
            assert code in {"maker_quote_behind_book", "maker_would_cross_book"}
            continue
        assert config.MAKER_MIN_BID - 1e-9 <= price <= max_price + 1e-9
        assert price < best_ask                                   # always passive
        assert best_bid - price <= config.MAKER_TICK * config.MAKER_MAX_TICKS_BEHIND_BEST_BID + 1e-9


# ── executor integration ─────────────────────────────────────────────────────

class _MakerClient:
    def __init__(self, book=None, book_error: Exception | None = None):
        self.book = book
        self.book_error = book_error
        self.place_calls: list[dict] = []
        self.book_calls = 0

    async def get_orderbook(self, outcome_id, depth=5):
        self.book_calls += 1
        if self.book_error is not None:
            raise self.book_error
        return self.book

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {"order": {"id": f"maker-{len(self.place_calls)}", "status": "open"}}

    async def cancel_order(self, order_id):
        return {"status": "cancelled"}


def _maker_signal(market_price=0.58, outcome="YES", market_id="m"):
    return TradeSignal(
        strategy="MAKER", event_id="e", market_id=market_id, asset="SOL",
        timeframe="15min", outcome=outcome, outcome_id="yes" if outcome == "YES" else "no",
        certainty=0.95, win_prob=0.75, market_price=market_price, size_pct=0.02,
        reason=f"MAKER {outcome} fv=0.750 spread_capture bid={market_price:.3f}",
    )


@pytest.fixture
def maker_env(monkeypatch):
    chat = "chat-maker-book"
    stall.reset(chat)
    monkeypatch.setattr(executor, "active_markets", [{
        "market_id": "m", "event_id": "e", "engine": "CLOB", "minimum_order_amount": 100,
        "secs_to_close": 400, "threshold": 150.0, "fee_rate": 0.02, "closing_date": "",
    }])
    monkeypatch.setattr(executor, "_trade_cooldown", {})
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *_a: 1.0)
    recorded = []
    monkeypatch.setattr(
        executor.database, "record_trade",
        lambda **kw: recorded.append(kw) or f"trade-{len(recorded)}",
    )
    import feeds_direct   # executor imports it lazily inside the MAKER branch

    monkeypatch.setattr(feeds_direct, "get_direct_price", lambda _a: (150.0, time.time()))
    notified = []

    async def _notify(app, cid, sig, amount, engine="AMM"):
        notified.append((sig.market_price, engine))

    monkeypatch.setattr(executor.telegram_bot, "notify_trade", _notify)
    monkeypatch.setattr(executor, "_tg_app", object())
    return {"chat": chat, "recorded": recorded, "notified": notified}


def _run(env, sig, client, risk=None):
    risk = risk or RiskManager()
    asyncio.run(executor._execute_logic(
        env["chat"], sig, client, risk,
        {"mode": "balanced", "risk_pct": 2, "mintrade": 100, "maxtrade": 5000},
        20_000.0, 20_000.0,
    ))
    return risk


def test_executor_does_not_rest_a_buried_quote(maker_env):
    client = _MakerClient(_book(bids=[0.66], asks=[0.69]))
    risk = _run(maker_env, _maker_signal(), client)

    assert client.place_calls == [], "a 0.58 bid under a 0.66 best bid can never fill"
    assert risk.open_positions == {}
    assert maker_env["notified"] == [], "no '📊 MAKER … @ 0.580' message for a quote never sent"
    rejects = stall._users[maker_env["chat"]]["rejects"]
    assert "exec:maker_quote_behind_book" in rejects
    assert "0.660" in rejects["exec:maker_quote_behind_book"]["detail"]
    # One book check per market per cooldown window, not per 5-second signal.
    key = executor._cooldown_key(maker_env["chat"], "m", "MAKER")
    assert key in executor._trade_cooldown


def test_executor_steps_a_crossing_quote_inside_the_ask(maker_env):
    client = _MakerClient(_book(bids=[0.52], asks=[0.56]))
    sig = _maker_signal()
    risk = _run(maker_env, sig, client)

    assert len(client.place_calls) == 1
    call = client.place_calls[0]
    assert call["post_only"] is True and call["time_in_force"] == "GTC"
    assert call["price"] == pytest.approx(0.55)
    # Everything downstream shows the price actually sent.
    assert maker_env["notified"] == [(pytest.approx(0.55), "CLOB_LIMIT")]
    assert maker_env["recorded"][0]["entry_price"] == pytest.approx(0.55)
    (pos,) = risk.open_positions.values()
    assert pos["entry_price"] == pytest.approx(0.55) and pos["confirmed_filled"] is False


def test_executor_places_a_competitive_quote_unchanged(maker_env):
    client = _MakerClient(_book(bids=[0.57], asks=[0.61]))
    _run(maker_env, _maker_signal(), client)
    assert [c["price"] for c in client.place_calls] == [pytest.approx(0.58)]


@pytest.mark.parametrize(
    "client,code",
    [
        (_MakerClient({}), "maker_book_unavailable"),                         # client error → {}
        (_MakerClient(book_error=TimeoutError("slow")), "maker_book_unavailable"),
        (_MakerClient(_book(bids=[0.57], asks=[0.61], timestamp=time.time() - 120)),
         "maker_book_stale"),
    ],
)
def test_executor_fails_closed_without_a_fresh_book(maker_env, client, code):
    """README: a trade requires fresh data. A MAKER quote is no exception."""
    _run(maker_env, _maker_signal(), client)
    assert client.place_calls == []
    assert f"exec:{code}" in stall._users[maker_env["chat"]]["rejects"]


def test_maker_quote_does_not_overwrite_a_same_market_snipe_position(maker_env):
    """risk.already_in allows MAKER+SNIPE on one market (same side); keying the
    MAKER entry by the bare market id silently replaced the filled SNIPE one."""
    risk = RiskManager()
    snipe = {
        "market_id": "m", "order_id": "snipe-1", "outcome": "YES", "outcome_id": "yes",
        "entry_price": 0.60, "amount_ngn": 200.0, "filled_quantity": 3.3,
        "confirmed_filled": True, "strategy": "SNIPE", "asset": "SOL", "timeframe": "15min",
    }
    risk.add_position("m", dict(snipe))
    client = _MakerClient(_book(bids=[0.57], asks=[0.61]))

    _run(maker_env, _maker_signal(), client, risk)

    assert len(client.place_calls) == 1
    assert risk.open_positions["m"]["strategy"] == "SNIPE", "filled SNIPE position was overwritten"
    maker_keys = [k for k, p in risk.open_positions.items() if p["strategy"] == "MAKER"]
    assert maker_keys == ["m:YES:maker-1"]


# ── same-market side conflicts ───────────────────────────────────────────────

def _open(risk, key, **pos):
    base = {"market_id": "m", "asset": "SOL", "timeframe": "15min", "entry_price": 0.6,
            "amount_ngn": 100.0, "order_id": key}
    base.update(pos)
    risk.add_position(key, base)


def test_maker_and_snipe_may_share_a_market_only_on_the_same_side():
    risk = RiskManager()
    _open(risk, "m", strategy="SNIPE", outcome="YES", confirmed_filled=True)

    assert risk.already_in("m", asset="SOL", strategy="MAKER", outcome="YES") is False
    # Opposite sides of one binary cost > 1.00 together: one leg always loses.
    assert risk.already_in("m", asset="SOL", strategy="MAKER", outcome="NO") is True
    # An unknown side cannot be proven safe.
    assert risk.already_in("m", asset="SOL", strategy="MAKER") is True


def test_already_in_sees_compound_keyed_positions():
    risk = RiskManager()
    _open(risk, "m:YES:maker-1", strategy="MAKER", outcome="YES", confirmed_filled=False)

    # Same strategy family on the same market: blocked even though the entry is
    # not stored under the bare market id.
    assert risk.already_in("m", strategy="MAKER", outcome="YES") is True
    # A directional taker on the opposite side of that resting quote: blocked.
    assert risk.already_in("m", strategy="SNIPE", outcome="NO") is True
    assert risk.already_in("m", strategy="SNIPE", outcome="YES") is False


def test_dual_leg_midmarket_keys_keep_their_existing_behaviour():
    """MIDMARKET_MAKER legs use '<market>_YES' keys and their own lock namespace."""
    risk = RiskManager()
    _open(risk, "m_YES", strategy="MIDMARKET_MAKER", outcome="YES")
    _open(risk, "m_NO", strategy="MIDMARKET_MAKER", outcome="NO")
    assert risk.already_in("m", strategy="SNIPE", outcome="YES") is False


# ── strategy ceiling is explicit and unchanged by default ────────────────────

def test_maker_ceiling_defaults_to_the_audited_value():
    assert config.MAKER_MAX_BID == pytest.approx(0.58)
    assert config.MAKER_MIN_BID == pytest.approx(0.50)


def test_maker_strategy_reads_its_ceiling_from_config(monkeypatch):
    from strategies.base import MarketState
    from strategies.maker import MakerStrategy
    import feeds
    import feeds_direct

    spot = 150.0
    monkeypatch.setattr(feeds_direct, "get_direct_price", lambda _a: (spot, time.time()))
    monkeypatch.setitem(feeds.spot, "SOL", spot)
    state = MarketState()
    now = time.time()
    state.price_history = {"SOL": [(now - 300, spot * 0.998)] * 6 + [(now - 1, spot)]}
    market = dict(
        asset="SOL", market_id="m", event_id="e", timeframe="15min", engine="CLOB",
        secs_to_close=420, threshold=spot / 1.008, yes_price=0.70, no_price=0.30,
        yes_id="y", no_id="n", title="t",
    )

    sig = asyncio.run(MakerStrategy().evaluate(market, {}, state))
    assert sig is not None and sig.market_price == pytest.approx(0.58)

    monkeypatch.setattr(config, "MAKER_MAX_BID", 0.66)
    sig = asyncio.run(MakerStrategy().evaluate(market, {}, state))
    assert sig is not None and 0.58 < sig.market_price <= 0.66
