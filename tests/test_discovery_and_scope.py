"""Discovery, liquidity-routing, and resting-order lifecycle regressions.

1. A confident-but-valid binary market (YES=0.82, NO=0.18, sum=1.00) must NOT
   be classified DISLOCATED_WIDE — takers stay enabled. Only true sum
   dislocation (sum > 1.15 or sum < 0.85) suppresses takers.
2. When the Bayse API omits the engine field, the scanner infers CLOB for
   crypto 5min/15min/1h series so MAKER can evaluate and quote.
3. MakerStrategy.is_stale() flags resting quotes by age (or oracle drift).
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import config
import strategies
from strategies.base import MarketState
from strategies.maker import MakerStrategy
import scanner


class _RecordingStrategy:
    """Minimal async strategy fake that records evaluate() calls."""

    def __init__(self, calls: list):
        self._calls = calls

    async def evaluate(self, market, learned, state, spot_price=None, books=None):
        self._calls.append(market.get("yes_price"))
        return None


def _eval_market(**overrides):
    market = {
        "event_id": "event",
        "market_id": "market",
        "asset": "BTC",
        "timeframe": "15min",
        "secs_to_close": 600,
        "threshold": 60_000.0,
        "yes_id": "yes",
        "no_id": "no",
        "fee_rate": 0.02,
        "title": "BTC test",
    }
    market.update(overrides)
    return market


def test_a_confident_binary_market_is_not_treated_as_dislocated(monkeypatch):
    """YES=0.82/NO=0.18 (sum=1.00) is a valid, confident market.

    Both strategies must still be evaluated on it. Suppressing evaluation
    because one side looks "too sure" would cut the bot out of exactly the
    markets where the book and the model disagree most.
    """
    taker_calls: list = []
    maker_calls: list = []
    monkeypatch.setattr(
        strategies,
        "_strategies",
        {
            "TAKER": _RecordingStrategy(taker_calls),
            "MAKER": _RecordingStrategy(maker_calls),
        },
    )
    learned = {"strategies": ["TAKER", "MAKER"], "mode": "balanced"}

    asyncio.run(strategies.evaluate_all(
        _eval_market(yes_price=0.82, no_price=0.18),
        dict(learned), MarketState(), spot_price=60_500.0, books={},
    ))
    assert taker_calls == [0.82]
    assert maker_calls == [0.82]


def test_the_pair_sum_sanity_gate_rejects_a_dislocated_book(monkeypatch):
    """A book whose two sides cannot both be right (sum far from 1.00) is
    either a broken payload or an arbitrage, and quoting into it is not
    market making."""
    from strategies.book import pair_sum_sane

    assert pair_sum_sane(0.82, 0.18) is True    # sums to 1.00
    assert pair_sum_sane(0.70, 0.60) is False   # sums to 1.30
    assert pair_sum_sane(0.30, 0.20) is False   # sums to 0.50


def test_an_unknown_strategy_in_scope_is_reported_not_silently_ignored(monkeypatch):
    """A saved preference naming a deleted strategy must not look like "no
    signals" forever. It is named in the reject counters so /why can show it.
    """
    import stall

    chat = "u-scope"
    stall.reset(chat)
    taker_calls: list = []
    monkeypatch.setattr(
        strategies, "_strategies", {"TAKER": _RecordingStrategy(taker_calls)}
    )
    asyncio.run(strategies.evaluate_all(
        _eval_market(),
        {"chat_id": chat, "strategies": ["TAKER", "SNIPE"], "mode": "balanced"},
        MarketState(), spot_price=60_500.0, books={},
    ))
    assert len(taker_calls) == 1, "the known strategy must still be evaluated"
    rejects = stall._users[chat]["rejects"]
    assert any("unknown_strategy" in c for c in rejects), list(rejects)


def test_maker_resting_order_expiry_by_age():
    """is_expired() is False for fresh quotes and True once past timeout."""
    strat = MakerStrategy()
    strat.track_quote(
        market_id="market-1",
        legs=[{"outcome": "YES", "outcome_id": "yes-1", "order_id": "order-1",
               "price": 0.55, "amount": 500.0}],
        spot=60_000.0,
    )
    assert strat.is_expired("market-1") is False

    # Unknown market → not expired (nothing to manage).
    assert strat.is_expired("market-unknown") is False

    # Aged quote → expired.
    strat.open_quotes["market-1"]["placed_at"] = time.time() - (
        config.MAKER_ORDER_TIMEOUT + 60
    )
    assert strat.is_expired("market-1") is True


def test_maker_wants_a_requote_when_the_oracle_moves_through_its_quote():
    """A quote the oracle has walked away from is stale however young it is:
    it is now an offer to trade at a price we would not repeat."""
    import config as _config

    strat = MakerStrategy()
    spot = 60_000.0
    strat.track_quote(
        market_id="market-1",
        legs=[{"outcome": "YES", "outcome_id": "yes-1", "order_id": "order-1",
               "price": 0.55, "amount": 500.0}],
        spot=spot,
    )
    assert strat.should_requote("market-1", spot * 1.0001) is False
    moved = spot * (1.0 + _config.MAKER_REQUOTE_THRESHOLD * 2)
    assert strat.should_requote("market-1", moved) is True
