"""MAKER must quote against the live order book, and never pay more than the
side is worth.

Production symptom (Telegram, 2026-09-25): after /resume, MAKER placed five
₦100 post-only bids — every one at exactly 0.580 — and none filled; the account
went ~19 hours without a confirmed fill. The strategy took its bid from a fixed
risk/reward ceiling (0.58) instead of from the book, so with the chosen side
trading at 0.65–0.75 the quote sat many ticks under the best bid and could only
fill if every bid above it were exhausted first; with the side trading below
0.58 it crossed the ask and was rejected as post-only.

The fix is not "clamp to 0.58 but check the book". It is that the ceiling is
now *derived*, per leg, from what the side is worth:

    ceiling = fair value − required edge − inventory skew

where required edge grows with the price we are paying. The book then decides
where, inside that ceiling, an order can actually rest. There is no longer a
fixed number for a quote to hide behind, and there is no longer a price at
which we would post a bid that cannot fill or cannot be worth filling.

These tests pin both halves: the value judgement (the ceiling) and the book
mechanics (where the order rests and when we decline to rest one at all).
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
from strategies.book import passive_bid_price
from strategies.maker import MakerStrategy


def _book(bids=(), asks=(), **extra):
    book = {
        "marketId": "m",
        "outcomeId": "yes",
        "timestamp": time.time(),
        "bids": [{"price": p, "quantity": 500, "total": p * 500} for p in bids],
        "asks": [{"price": p, "quantity": 500, "total": p * 500} for p in asks],
    }
    book.update(extra)
    return book


# ── the value half: a bid is never worth more than the side ──────────────────

def test_the_ceiling_is_the_fair_value_less_the_required_edge():
    m = MakerStrategy()
    # fv 0.80, no book constraints: the bid lands at fv − required edge.
    price, code, detail = m._price_leg(fair_value=0.80, book=_book(bids=[0.70], asks=[0.74]))
    assert code == ""
    required = m.min_leg_edge(price)
    assert price == pytest.approx(passive_bid_price(
        _book(bids=[0.70], asks=[0.74]), 0.80 - required)[0])
    assert m.leg_edge(0.80, price) >= required - 1e-9
    assert "edge=" in detail


def test_a_leg_never_bids_more_than_it_is_worth_less_its_edge():
    """The production bug in one line: bidding 0.58 for a side worth 0.55.

    Whatever the book says, the bid is capped by the value judgement. If no
    passive price satisfies it, we decline rather than pay up.
    """
    m = MakerStrategy()
    price, code, _ = m._price_leg(fair_value=0.55, book=_book(bids=[0.54], asks=[0.58]))
    if price is not None:
        assert price <= 0.55 - m.min_leg_edge(price) + 1e-9
        assert m.leg_edge(0.55, price) >= m.min_leg_edge(price) - 1e-9
    else:
        assert code in {"leg_edge_below_floor", "leg_behind_book",
                        "leg_would_cross_book", "leg_no_passive_price"}


def test_far_out_of_the_money_sides_are_not_quotable():
    m = MakerStrategy()
    for fv in (0.001, 0.999):
        price, code, _ = m._price_leg(fair_value=fv, book=_book(bids=[0.50], asks=[0.52]))
        assert price is None
        assert code == "leg_fv_not_quotable"


def test_required_edge_grows_with_the_price_paid():
    """Capital at risk scales with price, and the tails are least calibrated."""
    m = MakerStrategy()
    cheap = m.min_leg_edge(0.20)
    dear = m.min_leg_edge(0.80)
    assert dear > cheap
    assert cheap == pytest.approx(config.MAKER_MIN_LEG_EDGE)


def test_inventory_skew_reduces_the_bid_on_the_side_we_are_long():
    """Long YES → we pay less for YES. Filling NO completes a set, so the side
    we already hold is worth less to us than a fresh one."""
    m = MakerStrategy()
    book = _book(bids=[0.48], asks=[0.54])
    flat, code0, _ = m._price_leg(fair_value=0.50, book=book)
    assert code0 == ""
    long_yes, cy, _ = m._price_leg(fair_value=0.50, book=book, skew=0.01)
    assert cy == ""
    assert long_yes == pytest.approx(flat - 0.01)


def test_skew_never_buys_us_into_a_sub_edge_trade():
    """The upward side of a skew is capped by passivity *and* by the edge floor.

    "We would like to complete the set" is not a licence to pay more than the
    leg is worth: the book already limits how much of that willingness we can
    express, and the edge check is the last word.
    """
    m = MakerStrategy()
    book = _book(bids=[0.48], asks=[0.54])
    price, code, _ = m._price_leg(fair_value=0.50, book=book, skew=-0.01)
    # The leg we want may be taken on a thinner edge than a fresh one, but
    # never on less than half the base requirement.
    assert code == ""
    assert m.leg_edge(0.50, price) >= 0.5 * config.MAKER_MIN_LEG_EDGE - 1e-9


def test_price_pair_skews_the_two_legs_in_opposite_directions():
    """At the pair level the skew is what it is for: one leg down, other up.

    Given room on both sides, being long YES must make the YES bid cheaper
    and the NO bid dearer, because the NO fill is the one that locks a profit.
    """
    m = MakerStrategy()
    book_y = _book(bids=[0.48], asks=[0.54])
    book_n = _book(bids=[0.48], asks=[0.54])
    flat, _ = m.price_pair(fv_yes=0.50, fv_no=0.50,
                           book_yes=book_y, book_no=book_n)
    skewed, _ = m.price_pair(fv_yes=0.50, fv_no=0.50,
                             book_yes=book_y, book_no=book_n,
                             inventory_skew=1.0)
    assert "yes_bid" in flat and "yes_bid" in skewed
    assert skewed["yes_bid"] < flat["yes_bid"]
    assert skewed["no_bid"] > flat["no_bid"]


def test_the_production_case_now_quotes_competitively():
    """2026-09-25: five bids at a fixed 0.580 under a book bidding 0.65–0.75.

    The ceiling is derived from value now, so the same situation produces a
    bid that can actually fill. This is the bug, inverted.
    """
    m = MakerStrategy()
    price, code, _ = m._price_leg(
        fair_value=0.789, book=_book(bids=[0.66, 0.65], asks=[0.69, 0.70]))
    assert code == ""
    assert price == pytest.approx(0.67)
    assert m.leg_edge(0.789, price) >= m.min_leg_edge(price)


def test_a_bid_that_cannot_fill_before_timeout_is_still_declined():
    """Being competitive is not the same as paying anything: if our own value
    judgement puts the ceiling far under the book, the quote would rest until
    timeout and teach us nothing."""
    m = MakerStrategy()
    price, code, detail = m._price_leg(
        fair_value=0.62, book=_book(bids=[0.75], asks=[0.78]))
    assert price is None
    assert code == "leg_behind_book"
    assert "0.750" in detail
    assert "fv=0.620" in detail


def test_quote_that_would_cross_steps_inside_the_ask():
    """Post-only at/through the ask is rejected by Bayse; rest inside it.

    Note it rests a tick over the best bid, not a tick under the ask: there is
    no reason to pay 0.55 for something the book will sell at 0.56 while
    0.53 already leads the queue.
    """
    m = MakerStrategy()
    price, code, _ = m._price_leg(
        fair_value=0.80, book=_book(bids=[0.52], asks=[0.56, 0.57]))
    assert code == ""
    assert price == pytest.approx(0.53)
    assert 0.52 <= price < 0.56


def test_a_dead_book_with_no_passive_price_above_the_floor_is_skipped():
    """One bid at 0.01 and nothing on the ask: no price we can rest at."""
    m = MakerStrategy()
    price, code, detail = m._price_leg(fair_value=0.80, book=_book(bids=[0.01]))
    assert price is None
    assert code == "leg_no_passive_price"
    assert str(config.MAKER_MIN_LEG_BID) in detail


def test_malformed_levels_are_ignored():
    m = MakerStrategy()
    book = {"timestamp": time.time(),
            "bids": [{"price": "x"}, {"nope": 1}, {"price": 0.57, "quantity": 500}],
            "asks": [{"price": None}]}
    price, code, _ = m._price_leg(fair_value=0.80, book=book)
    assert code == "" and price is not None


# ── the pair lock ───────────────────────────────────────────────────────────

def test_a_pair_is_only_quoted_when_it_locks_a_profit():
    """fv_yes + fv_no = 1, so the two bids must sum below 1 by our edge."""
    m = MakerStrategy()
    res, detail = m.price_pair(
        fv_yes=0.60, fv_no=0.40,
        book_yes=_book(bids=[0.50], asks=[0.54]),
        book_no=_book(bids=[0.30], asks=[0.34]),
    )
    assert "yes_bid" in res and "no_bid" in res
    assert res["yes_bid"] + res["no_bid"] <= 1.0 - config.MAKER_PAIR_MIN_EDGE + 1e-9


def test_a_pair_that_cannot_lock_is_skipped_named():
    m = MakerStrategy()
    res, detail = m.price_pair(
        fv_yes=0.55, fv_no=0.45,
        # Books force us to pay nearly the whole unit for the pair.
        book_yes=_book(bids=[0.52], asks=[0.53]),
        book_no=_book(bids=[0.45], asks=[0.46]),
    )
    assert "skip" in res
    assert res["skip"] in {"pair_cannot_lock", "leg_would_cross_book",
                           "leg_quote_behind_book", "leg_edge_below_floor",
                           "pair_leg_unpriceable_YES", "pair_leg_unpriceable_NO"}


def test_pair_edge_holds_under_random_books():
    """Property: whatever the book, a quoted pair never costs more than 1."""
    rng = random.Random(7)
    m = MakerStrategy()
    for _ in range(2_000):
        fv_yes = round(rng.uniform(0.20, 0.80), 3)
        fv_no = 1.0 - fv_yes
        best_bid_y = round(rng.uniform(0.05, 0.90), 2)
        best_ask_y = round(min(0.99, best_bid_y + rng.choice([0.01, 0.02, 0.05])), 2)
        best_bid_n = round(rng.uniform(0.05, 0.90), 2)
        best_ask_n = round(min(0.99, best_bid_n + rng.choice([0.01, 0.02, 0.05])), 2)
        res, _ = m.price_pair(
            fv_yes=fv_yes, fv_no=fv_no,
            book_yes=_book(bids=[best_bid_y], asks=[best_ask_y]),
            book_no=_book(bids=[best_bid_n], asks=[best_ask_n]),
        )
        if "yes_bid" not in res:
            continue
        by, bn = res["yes_bid"], res["no_bid"]
        assert config.MAKER_MIN_LEG_BID - 1e-9 <= by <= config.MAKER_MAX_LEG_BID + 1e-9
        assert config.MAKER_MIN_LEG_BID - 1e-9 <= bn <= config.MAKER_MAX_LEG_BID + 1e-9
        assert by + bn <= 1.0 - config.MAKER_PAIR_MIN_EDGE + 1e-9
        # Both bids are passive on their own book.
        assert by < best_ask_y and bn < best_ask_n
        # And neither pays more than the side is worth, net of required edge.
        assert m.leg_edge(fv_yes, by) >= m.min_leg_edge(by) - 1e-9
        assert m.leg_edge(fv_no, bn) >= m.min_leg_edge(bn) - 1e-9


# ── executor integration ─────────────────────────────────────────────────────

class _MakerClient:
    def __init__(self, books=None, book_error: Exception | None = None):
        self.books = books or {}
        self.book_error = book_error
        self.place_calls: list[dict] = []
        self.book_calls = 0

    async def get_orderbooks(self, outcome_ids, depth=5):
        self.book_calls += 1
        if self.book_error is not None:
            raise self.book_error
        return {oid: self.books.get(oid, {}) for oid in outcome_ids}

    async def get_orderbook(self, outcome_id, depth=5):
        self.book_calls += 1
        if self.book_error is not None:
            raise self.book_error
        return self.books.get(outcome_id, {})

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {"order": {"id": f"maker-{len(self.place_calls)}", "status": "open"}}

    async def cancel_order(self, order_id):
        return {"status": "cancelled"}

    @staticmethod
    def parse_filled_shares(order: dict) -> float:
        return 0.0


def _maker_signal(market_price=0.55, outcome="YES", market_id="m", **over):
    from strategies.base import QuoteLeg
    base = dict(
        strategy="MAKER", event_id="e", market_id=market_id, asset="SOL",
        timeframe="15min", outcome=outcome,
        outcome_id="yes" if outcome == "YES" else "no",
        certainty=0.95, win_prob=0.75, market_price=market_price, size_pct=0.02,
        reason=f"MAKER {outcome} fv=0.750 spread_capture bid={market_price:.3f}",
    )
    base.update(over)
    return TradeSignal(**base)


@pytest.fixture
def maker_env(monkeypatch):
    chat = "chat-maker-book"
    stall.reset(chat)
    monkeypatch.setattr(executor, "active_markets", [{
        "market_id": "m", "event_id": "e", "engine": "CLOB", "minimum_order_amount": 100,
        "secs_to_close": 400, "threshold": 150.0, "fee_rate": 0.02, "closing_date": "",
        "yes_id": "yes", "no_id": "no",
    }])
    monkeypatch.setattr(executor, "_trade_cooldown", {})
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *_a: 1.0)
    recorded = []
    monkeypatch.setattr(
        executor.database, "record_trade",
        lambda **kw: recorded.append(kw) or f"trade-{len(recorded)}",
    )
    import feeds_direct

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
    """A bid many ticks under the best bid cannot fill; do not post it."""
    client = _MakerClient({"yes": _book(bids=[0.66], asks=[0.69])})
    risk = _run(maker_env, _maker_signal(), client)

    assert client.place_calls == []
    assert risk.open_positions == {}
    assert maker_env["notified"] == []
    rejects = stall._users[maker_env["chat"]]["rejects"]
    assert any("behind_book" in c for c in rejects), list(rejects)


def test_executor_fails_closed_without_a_fresh_book(maker_env):
    """README: a trade requires fresh data. A MAKER quote is no exception."""
    client = _MakerClient(book_error=TimeoutError("slow"))
    _run(maker_env, _maker_signal(), client)
    assert client.place_calls == []


def test_a_stale_book_is_not_a_price(maker_env):
    old = time.time() - 120
    client = _MakerClient({"yes": _book(bids=[0.50], asks=[0.54], timestamp=old)})
    _run(maker_env, _maker_signal(), client)
    assert client.place_calls == []


def test_maker_quote_does_not_overwrite_a_same_market_taker_position(maker_env):
    """Keying a MAKER entry by the bare market id silently replaced the filled
    TAKER one. The risk book is keyed per leg, so both can coexist."""
    risk = RiskManager()
    risk.add_position("m:YES:taker-1", {
        "market_id": "m", "order_id": "taker-1", "outcome": "YES", "outcome_id": "yes",
        "entry_price": 0.60, "amount_ngn": 200.0, "filled_quantity": 3.3,
        "confirmed_filled": True, "strategy": "TAKER", "asset": "SOL",
        "timeframe": "15min",
    })
    client = _MakerClient({"yes": _book(bids=[0.50], asks=[0.54])})

    _run(maker_env, _maker_signal(), client, risk)

    assert risk.open_positions["m:YES:taker-1"]["strategy"] == "TAKER", \
        "filled TAKER position was overwritten"


# ── same-market side conflicts ───────────────────────────────────────────────

def _open(risk, key, **pos):
    base = {"market_id": "m", "asset": "SOL", "timeframe": "15min", "entry_price": 0.6,
            "amount_ngn": 100.0, "order_id": key}
    base.update(pos)
    risk.add_position(key, base)


def test_maker_and_taker_may_share_a_market_only_on_the_same_side():
    risk = RiskManager()
    _open(risk, "m:YES:taker-1", strategy="TAKER", outcome="YES", confirmed_filled=True)

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
    # A *resting* quote is an order, not a position: it cannot be on the other
    # side of anything yet, so it must not suppress the taker. (While it did,
    # a two-sided MAKER quote -- one leg always on the "wrong" side -- blocked
    # the taker on the market for as long as it rested.)
    assert risk.already_in("m", strategy="TAKER", outcome="NO") is False
    assert risk.already_in("m", strategy="TAKER", outcome="YES") is False

    # Once that same leg FILLS it is a position, and the opposite-side rule is
    # real again: the two sides of one binary cannot both be bets for us.
    risk.open_positions["m:YES:maker-1"]["confirmed_filled"] = True
    risk.open_positions["m:YES:maker-1"]["filled_quantity"] = 10.0
    assert risk.already_in("m", strategy="TAKER", outcome="NO") is True
    assert risk.already_in("m", strategy="TAKER", outcome="YES") is False


def test_a_complete_set_lock_is_always_allowed():
    """A pair is not a directional bet; it is already both sides."""
    risk = RiskManager()
    _open(risk, "m:YES:maker-1", strategy="MAKER", outcome="YES", confirmed_filled=False)
    assert risk.already_in("m", strategy="TAKER", outcome="BOTH") is False


def test_a_two_leg_quote_is_placed_with_equal_share_counts(maker_env):
    """A set settles on min(shares_yes, shares_no).

    Placing the same *naira* amount on both legs bought more shares of the
    cheaper side, so the "locked" part covered only the smaller quantity and
    the remainder was unhedged directional risk that the strategy's sizing and
    its exemption from directional shrinkage never charged for. Each leg now
    gets the stake that buys the same number of shares, on the prices actually
    sent (the executor re-prices both legs off one fresh book first).
    """
    from strategies.base import QuoteLeg

    client = _MakerClient({
        "yes": _book(bids=[0.46], asks=[0.52]),
        "no": _book(bids=[0.50], asks=[0.56]),
    })
    client.books["no"] = {"marketId": "m", "outcomeId": "no", "timestamp": time.time(),
                          "bids": [{"price": 0.50, "quantity": 500, "total": 250.0}],
                          "asks": [{"price": 0.56, "quantity": 500, "total": 280.0}]}
    signal = _maker_signal(
        market_price=0.46, outcome="BOTH", certainty=0.60, win_prob=0.55,
        legs=[QuoteLeg("YES", "yes", 0.46, 0.02, 0.55),
              QuoteLeg("NO", "no", 0.50, 0.02, 0.45)],
    )
    risk = _run(maker_env, signal, client)

    assert len(client.place_calls) == 2, client.place_calls
    stakes = [call["amount"] for call in client.place_calls]
    prices = [call["price"] for call in client.place_calls]
    shares = [
        stake / (price * config.CURRENCY_BASE_MULTIPLIER)
        for stake, price in zip(stakes, prices)
    ]
    assert shares[0] == pytest.approx(shares[1], rel=1e-6), (
        f"legs bought {shares[0]:.4f} vs {shares[1]:.4f} shares: the pair is "
        "not locked on the full quantity"
    )
    # Both legs are resting orders in the risk book, keyed per leg.
    assert len(risk.open_positions) == 2
    assert all(p["amount_ngn"] == pytest.approx(stake)
               for p, stake in zip(risk.open_positions.values(), stakes))
