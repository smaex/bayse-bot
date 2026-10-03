"""MAKER only quotes where a quote can be both filled and worth filling.

Production evidence (Telegram, 2026-09-26): MAKER was the only strategy that
signalled — 12 signals, 2 resting quotes at 0.574 and 0.580, 0 fills. Two
causes, both structural rather than a matter of tuning:

* **It quoted against a ceiling, not against the book.** The bid came from a
  fixed 0.58 risk/reward number, so with the side trading at 0.65–0.75 the
  quote sat far under the best bid and could only fill if every bid above it
  were exhausted; with the side below 0.58 it crossed the ask and was rejected
  as post-only. The ceiling is now derived per leg from fair value less a
  required edge, and the book decides where inside it the order rests.
* **It re-read its own oracle.** ``evaluate`` accepted ``spot_price`` and never
  used it, falling back to the Bayse relay with a private staleness rule, so
  MAKER priced "fair value" from the same source as the market price it was
  comparing against. It now uses the price the evaluation loop resolved.

What the gates are now, in the order they fire: engine must be CLOB (passive
orders do not exist on an AMM); a quoting window bounded on both sides; both
books readable and not stale; a fair value; then, per leg, a price that is
passive, competitive, and worth the edge. Finally the pair must lock.

No risk parameter is relaxed by these tests — they exist to keep the gates
from quietly becoming decorative.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import config
import stall
import strategy as global_strategy
from strategies import model as model_module
from strategies.maker import MakerStrategy


SPOT = 100_200.0          # 0.2% above the strike
THRESHOLD = 100_000.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    stall.reset()
    monkeypatch.setattr(global_strategy.global_state, "price_history", {})
    monkeypatch.setattr(global_strategy.global_state, "market_flips", {})
    yield
    stall.reset()


def _market(mid, asset="BTC", secs=600.0, market_id="mkt-maker", **over):
    market = {
        "event_id": "evt-1",
        "market_id": market_id,
        "asset": asset,
        "timeframe": "15min",
        "secs_to_close": secs,
        "threshold": THRESHOLD,
        "yes_price": mid,
        "no_price": round(1.0 - mid, 3),
        "yes_id": "yes",
        "no_id": "no",
        "fee_rate": 0.02,
        "title": "BTC above $100,000?",
        "engine": "CLOB",
        "status": "open",
    }
    market.update(over)
    return market


def _books(mid, spread=0.02):
    """A two-sided book centred on ``mid`` for YES, and on ``1 - mid`` for NO.

    Consistent on both sides: a book that bids 0.45 for the NO of a market
    whose YES is worth 0.70 is not a market, and quoting against one proves
    nothing.
    """
    best_bid = round(mid - spread / 2, 3)
    best_ask = round(mid + spread / 2, 3)
    lvl = lambda p: [{"price": p, "quantity": 900.0}]
    return {
        "yes": {"timestamp": time.time(), "bids": lvl(best_bid), "asks": lvl(best_ask)},
        "no": {"timestamp": time.time(),
               "bids": lvl(round(1.0 - best_ask, 3)),
               "asks": lvl(round(1.0 - best_bid, 3))},
    }


def _books_leaving_edge(fair_value, edge=0.12, spread=0.02):
    """An arbitrage-free book sitting ``edge`` under our fair value on YES.

    Arbitrage-free means the NO side is priced at ``1 - mid``. That
    relationship is why a two-sided quote is only available when the model
    roughly agrees with the market: if we think YES is worth more than the
    book does, we necessarily think NO is worth less, and only one leg can
    clear its edge requirement.
    """
    return _books(max(0.06, min(0.94, fair_value - edge)), spread)


def _state():
    return SimpleNamespace(price_history={}, kalman_state={}, garch_state={})


def _evaluate(monkeypatch, market, fair_value, *, spot_price=SPOT, books=None,
              state=None, direct_price=None):
    """Run the real strategy with fair value pinned and the feeds controlled."""
    monkeypatch.setattr(model_module, "gbm_win_probability", lambda **kw: fair_value)
    monkeypatch.setattr(model_module, "twap_win_probability", lambda **kw: fair_value)
    signal = asyncio.run(MakerStrategy().evaluate(
        market, {"chat_id": "u-maker", "mode": "balanced"},
        state if state is not None else _state(), spot_price=spot_price,
        books=books if books is not None else _books_leaving_edge(fair_value),
    ))
    rejects = dict(stall._users["u-maker"]["rejects"]) if "u-maker" in stall._users else {}
    return signal, rejects


# ── the oracle the loop selected is the oracle MAKER uses ────────────────────

def test_maker_uses_the_oracle_price_the_loop_handed_it(monkeypatch):
    """The strategy prices off the spot it is handed, not off a feed it reads
    for itself.

    It used to accept ``spot_price`` and ignore it, falling back to the Bayse
    relay with a private staleness rule -- so for an oracle aged 10-30s MAKER
    priced "fair value" from the same source as the market price it compared
    against, while the evaluation loop used the independent oracle.
    """
    import feeds_direct

    seen = {}
    real = model_module.fair_value

    def _spy(asset, market, state, spot=None, **kw):
        seen["spot"] = spot
        return 0.75

    monkeypatch.setattr(model_module, "fair_value", _spy)
    monkeypatch.setattr(model_module, "fair_value_pair",
                        lambda asset, market, state, spot=None: (0.75, 0.25))
    # A feed saying the opposite side of the strike: if the strategy read it,
    # the fair value would not be the one the loop computed.
    monkeypatch.setattr(feeds_direct, "get_direct_price",
                        lambda _a: (99_900.0, time.time()))
    signal = asyncio.run(MakerStrategy().evaluate(
        _market(0.55), {"chat_id": "u-maker", "mode": "balanced"}, _state(),
        spot_price=SPOT, books=_books(0.63)))
    assert signal is not None
    assert seen.get("spot") == SPOT


def test_maker_refuses_to_price_without_a_spot(monkeypatch):
    """No spot means no fair value, and a quote without one is a guess."""
    signal, rejects = _evaluate(monkeypatch, _market(0.55), 0.75, spot_price=None)
    assert signal is None
    assert "MAKER:no_fair_value" in rejects


# ── the quoting window is bounded on both sides ──────────────────────────────

def test_maker_does_not_quote_into_settlement(monkeypatch):
    signal, rejects = _evaluate(monkeypatch, _market(0.55, secs=10.0), 0.75)
    assert signal is None
    assert "MAKER:too_close_to_settle" in rejects


def test_maker_does_not_quote_beyond_its_window(monkeypatch):
    signal, rejects = _evaluate(
        monkeypatch, _market(0.55, secs=config.MAKER_MAX_SECS_TO_CLOSE + 60), 0.75)
    assert signal is None
    assert "MAKER:too_far_from_settle" in rejects


def test_maker_never_quotes_an_amm(monkeypatch):
    """Passive orders do not exist on an AMM; sending them was the single
    largest source of zero execution."""
    signal, rejects = _evaluate(monkeypatch, _market(0.55, engine="AMM"), 0.75)
    assert signal is None
    assert "MAKER:engine_not_clob" in rejects


# ── no book, no quote ────────────────────────────────────────────────────────

def test_maker_fails_closed_without_a_readable_book(monkeypatch):
    signal, rejects = _evaluate(monkeypatch, _market(0.55), 0.75, books={})
    assert signal is None
    assert "MAKER:book_unavailable" in rejects


def test_a_stale_book_is_not_a_price(monkeypatch):
    old = time.time() - 300
    books = _books(0.55)
    for side in books.values():
        side["timestamp"] = old
    signal, rejects = _evaluate(monkeypatch, _market(0.55), 0.75, books=books)
    assert signal is None
    assert "MAKER:book_stale" in rejects


# ── the edge gate is what decides, not a fixed price ceiling ─────────────────

def test_a_market_that_agrees_with_us_and_shows_no_spread_offers_nothing(monkeypatch):
    """Our fair value is the market's price and the spread is a tick: there is
    no room to rest a bid that is both competitive and worth our edge."""
    signal, rejects = _evaluate(monkeypatch, _market(0.50, market_id="mkt-tight"),
                                0.50, books=_books(0.50, spread=0.01))
    assert signal is None
    assert any("behind_book" in k or "edge" in k for k in rejects), rejects


def test_the_same_fair_value_quotes_when_the_book_gives_it_room(monkeypatch):
    """fv 0.70 against a book at 0.58: the edge is there, so quote it.

    One leg, not two: we think YES is worth 0.70 and the book says 0.58, so
    we must think NO is worth 0.30 against a book saying 0.42. Only the side
    we think is cheap can clear its edge.
    """
    signal, _ = _evaluate(monkeypatch, _market(0.58), 0.70)
    assert signal is not None
    assert signal.outcome == "YES"
    assert len(signal.legs) == 1
    assert "single-leg" in signal.reason


def test_a_two_sided_quote_needs_the_model_to_agree_with_the_book(monkeypatch):
    """Where the model has no quarrel with the market on either side, both
    legs price and the pair locks. This is the quote that earns the full
    liquidity-reward score."""
    signal, _ = _evaluate(monkeypatch, _market(0.50), 0.50, books=_books(0.50, spread=0.06))
    assert signal is not None
    assert signal.outcome == "BOTH"
    assert len(signal.legs) == 2
    assert signal.edge_at_entry >= config.MAKER_PAIR_MIN_EDGE


def test_a_one_sided_fill_skews_the_next_quote_to_the_other_side():
    """Inventory self-corrects into a set instead of accumulating: after a YES
    fill, only NO may be quoted."""
    m = MakerStrategy()
    m.record_fill("mkt-maker", "YES", 10.0)
    assert m.inventory_skew("mkt-maker") > 0
    m.record_fill("mkt-maker", "NO", 10.0)
    assert m.inventory_skew("mkt-maker") == 0


def test_the_ceiling_moves_with_fair_value_not_with_a_constant(monkeypatch):
    """The 2026-09-26 bug: one fixed price, whatever the market was worth."""
    bids = {}
    for fv, mid in ((0.62, 0.50), (0.70, 0.58), (0.80, 0.66)):
        signal, _ = _evaluate(monkeypatch, _market(mid, market_id=f"m-{mid}"), fv)
        assert signal is not None, f"fv {fv} against a {mid} book should quote"
        bids[fv] = max(leg.price for leg in signal.legs)
    # More valuable side, higher bid — a constant could not do this.
    assert bids[0.62] < bids[0.70] < bids[0.80]


def test_no_leg_is_ever_quoted_above_what_it_is_worth(monkeypatch):
    m = MakerStrategy()
    for fv in (0.55, 0.62, 0.70, 0.80, 0.90):
        signal, _ = _evaluate(monkeypatch, _market(min(0.90, fv - 0.12),
                                                   market_id=f"m-{fv}"), fv)
        if signal is None:
            continue
        for leg, leg_fv in zip(signal.legs, (fv, 1.0 - fv)):
            assert leg.price <= leg_fv, (fv, leg)
            assert leg.price <= config.MAKER_MAX_LEG_BID


def test_a_pair_only_reaches_the_exchange_when_it_locks(monkeypatch):
    """Both legs together must cost less than the 1.00 they settle to."""
    signal, _ = _evaluate(monkeypatch, _market(0.50), 0.50, books=_books(0.50, spread=0.06))
    assert signal is not None
    total = sum(leg.price for leg in signal.legs)
    assert total <= 1.0 - config.MAKER_PAIR_MIN_EDGE + 1e-9
    assert signal.edge_at_entry == pytest.approx(1.0 - total, abs=1e-9)


def test_each_leg_clears_its_own_edge_requirement(monkeypatch):
    """The pair lock is not a licence for one leg to be a bad trade that the
    other leg subsidises. Both have to be worth owning on their own."""
    m = MakerStrategy()
    signal, _ = _evaluate(monkeypatch, _market(0.50), 0.50, books=_books(0.50, spread=0.06))
    assert signal is not None
    for leg in signal.legs:
        edge = m.leg_edge(leg.fair_value, leg.price)
        assert edge >= 0.5 * config.MAKER_MIN_LEG_EDGE - 1e-9


def test_a_single_leg_clears_the_same_edge_requirement_as_a_pair_leg():
    """The fallback is not a relaxed gate. Same band, same edge, same book
    checks -- it just carries directional risk the pair would not."""
    m = MakerStrategy()
    signal, _ = None, None
    import asyncio as _a
    from types import SimpleNamespace as _NS
    from strategies import model as _mm
    import stall as _stall

    _stall.reset()
    saved_g, saved_t = _mm.gbm_win_probability, _mm.twap_win_probability
    _mm.gbm_win_probability = lambda **kw: 0.70
    _mm.twap_win_probability = lambda **kw: 0.70
    try:
        signal = _a.run(MakerStrategy().evaluate(
            _market(0.58), {"chat_id": "u-single", "mode": "balanced"},
            _NS(price_history={}, kalman_state={}, garch_state={}),
            spot_price=SPOT, books=_books(0.58)))
    finally:
        _mm.gbm_win_probability, _mm.twap_win_probability = saved_g, saved_t

    assert signal is not None
    leg = signal.legs[0]
    assert m.leg_edge(leg.fair_value, leg.price) >= m.min_leg_edge(leg.price) - 1e-9


def test_the_single_leg_fallback_can_be_switched_off(monkeypatch):
    """An operator who wants pair-only quoting can have it."""
    monkeypatch.setattr(config, "MAKER_ALLOW_SINGLE_LEG", False)
    signal, _ = _evaluate(monkeypatch, _market(0.58), 0.70)
    assert signal is None
