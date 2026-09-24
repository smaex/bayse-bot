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

import strategies
from strategies.base import MarketState
from strategies.maker import MakerStrategy
import scanner


class _RecordingStrategy:
    """Minimal async strategy fake that records evaluate() calls."""

    def __init__(self, calls: list):
        self._calls = calls

    async def evaluate(self, market, learned, state, spot_price=None):
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


def test_confident_binary_market_does_not_suppress_takers(monkeypatch):
    """YES=0.82/NO=0.18 (sum=1.00) is valid — SNIPE must still be evaluated."""
    taker_calls: list = []
    maker_calls: list = []
    monkeypatch.setattr(
        strategies,
        "_strategies",
        {
            "SNIPE": _RecordingStrategy(taker_calls),
            "MAKER": _RecordingStrategy(maker_calls),
        },
    )
    learned = {"strategies": ["SNIPE", "MAKER"], "mode": "balanced"}

    asyncio.run(strategies.evaluate_all(
        _eval_market(yes_price=0.82, no_price=0.18),
        dict(learned), MarketState(), spot_price=60_500.0,
    ))
    assert taker_calls == [0.82]
    assert maker_calls == [0.82]

    # True sum dislocation (sum=1.30) still suppresses takers, not makers.
    taker_calls.clear()
    maker_calls.clear()
    asyncio.run(strategies.evaluate_all(
        _eval_market(yes_price=0.70, no_price=0.60),
        dict(learned), MarketState(), spot_price=60_500.0,
    ))
    assert taker_calls == []
    assert maker_calls == [0.70]


def test_scanner_infers_clob_engine_when_api_omits_it():
    """Missing engine field → CLOB for crypto short-term series, else AMM."""
    now = datetime.now(timezone.utc)
    lean = {
        "id": "event-1",
        "closingDate": (now + timedelta(minutes=10)).isoformat(),
        "openingDate": (now - timedelta(minutes=5)).isoformat(),
    }

    def _full(**market_overrides):
        market = {
            "id": "market-1",
            "outcome1Id": "yes-1",
            "outcome2Id": "no-1",
            "outcome1Price": 0.55,
            "outcome2Price": 0.45,
            "feePercentage": 2,
        }
        market.update(market_overrides)
        return {
            "title": "BTC test",
            "status": "open",
            "eventThreshold": 60_000.0,
            "markets": [market],
        }

    class _Client:
        def __init__(self, payload):
            self._payload = payload

        async def get_event(self, _event_id, currency=None):
            return self._payload

    # Engine omitted on crypto 15min → CLOB (MAKER can quote).
    enriched = asyncio.run(scanner._enrich(_Client(_full()), lean, "BTC", "15min"))
    assert enriched is not None
    assert enriched["engine"] == "CLOB"

    # Engine omitted on crypto 5min / 1h → CLOB as well.
    assert asyncio.run(
        scanner._enrich(_Client(_full()), lean, "ETH", "5min")
    )["engine"] == "CLOB"
    assert asyncio.run(
        scanner._enrich(_Client(_full()), lean, "SOL", "1h")
    )["engine"] == "CLOB"

    # Engine omitted on non-crypto / long series → AMM fallback preserved.
    assert asyncio.run(
        scanner._enrich(_Client(_full()), lean, "XAUUSD", "1h")
    )["engine"] == "AMM"

    # Explicitly declared engine is always respected.
    assert asyncio.run(
        scanner._enrich(_Client(_full(engine="amm")), lean, "BTC", "15min")
    )["engine"] == "AMM"


def test_maker_resting_order_staleness_by_age():
    """is_stale() is False for fresh quotes and True once past timeout."""
    strat = MakerStrategy()
    strat.track_order(
        market_id="market-1", order_id="order-1", placed_price=0.55,
        binance_price=60_000.0, amount=500.0, outcome_id="yes-1", asset="",
    )
    assert strat.is_stale("market-1", timeout_sec=120.0) is False

    # Unknown market → not stale (nothing to manage).
    assert strat.is_stale("market-unknown", timeout_sec=120.0) is False

    # Aged quote → stale.
    strat.open_orders["market-1"]["placed_at"] = time.time() - 200.0
    assert strat.is_stale("market-1", timeout_sec=120.0) is True
