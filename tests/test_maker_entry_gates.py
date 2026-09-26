"""MAKER's entries are decided by gates the code does not advertise.

Production evidence (Telegram, 2026-09-26): MAKER was the only strategy that
signalled — 12 signals, 2 resting quotes at 0.574 and 0.580, 0 fills — behind
`distance_below_calibration` ×7083, `late_candle_window` ×1875 and
`candle_warmup_window` ×1634.

Three findings, all verified against the real strategy and the real executor:

* ``MakerStrategy.evaluate`` accepted ``spot_price`` and never read it. It
  re-read the feeds with a private 10s staleness rule and fell back to the
  Bayse relay, so for an oracle aged 10-30s (``FEED_STALE_SEC``) MAKER priced
  "fair value" from the same source as the market price it compares against
  while the evaluation loop and SNIPE used the independent oracle. Fixed here.
* The direction gate's ``fv >= 0.62`` is inert. The certainty floor after it
  needs ``max(fv, 0.50 + 3.5*edge) >= 0.65``, i.e. **fv >= 0.65 or a 4.29c
  edge**. An fv of 0.64 with a 4c edge is refused; 0.64 with a 9c edge trades.
* With ``MAKER_MAX_BID`` = 0.58 and the executor's live-book check, orders only
  reach the exchange while the market mid is at or below ~0.60: above that the
  capped bid is more than a tick under the best bid and is skipped. The 0.58
  ceiling turns MAKER into a buyer of near-coin-flip markets.

Also pinned: fair value is mostly extrapolated Kalman drift, not price
position, at the distances MAKER actually requires — the failure mode
``projected_drift_pct``'s own docstring warns about.

No risk parameter is changed: 0.62, 0.65, 0.020, the 0.58 ceiling, the
quoting window and every distance calibration are exactly as they were.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import config
import executor
import feeds_direct
import stall
import strategy as global_strategy
from strategies import maker as maker_module
from strategies.maker import MakerStrategy
from strategies.utils import gbm_win_probability


SPOT = 100_200.0          # 0.2% above the strike: clears BTC's 0.08% calibration
THRESHOLD = 100_000.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    stall.reset()
    monkeypatch.setattr(global_strategy.global_state, "price_history", {})
    monkeypatch.setattr(global_strategy.global_state, "market_flips", {})
    yield
    stall.reset()


def _market(mid, asset="BTC", secs=600.0, market_id="mkt-maker"):
    return {
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


def _state(momentum=0.0004):
    """Price history whose 5-minute momentum is ``momentum`` (BTC needs +0.0002)."""
    now = time.time()
    old = SPOT / (1.0 + momentum)
    return SimpleNamespace(
        price_history={"BTC": [(now - 350, old), (now - 330, old), (now - 310, old),
                               (now - 290, old), (now - 270, old), (now - 5, SPOT)]},
        kalman_state={},
        garch_state={},
    )


def _evaluate(monkeypatch, market, fair_value, *, spot_price=SPOT, state=None,
              direct_price=None):
    """Run the real strategy with fair value pinned and the feeds under control."""
    # Both entry points: MAKER prices the settlement TWAP by default and only
    # falls back to the close print when SETTLEMENT_TWAP_SEC = 0. These tests
    # are about the gates, so pin whichever one _fair_value calls.
    monkeypatch.setattr(maker_module, "gbm_win_probability", lambda **kwargs: fair_value)
    monkeypatch.setattr(maker_module, "twap_win_probability", lambda **kwargs: fair_value)
    if direct_price is not None:
        monkeypatch.setattr(maker_module.feeds_direct, "get_direct_price",
                            lambda asset: direct_price)
    signal = asyncio.run(MakerStrategy().evaluate(
        market, {"chat_id": "u-maker", "mode": "balanced"},
        state if state is not None else _state(), spot_price=spot_price,
    ))
    rejects = dict(stall._users["u-maker"]["rejects"]) if "u-maker" in stall._users else {}
    return signal, rejects


def _book(mid, spread=0.02):
    best_bid = round(mid - spread / 2, 3)
    best_ask = round(mid + spread / 2, 3)
    return {"bids": [{"price": best_bid, "quantity": 500}],
            "asks": [{"price": best_ask, "quantity": 500}]}


# ── The oracle the loop selected must be the oracle MAKER uses ────────────────

def test_maker_uses_the_oracle_price_the_loop_handed_it(monkeypatch):
    """Loop picked 100,200 (above the strike); the raw feed says 99,900."""
    signal, _ = _evaluate(monkeypatch, _market(0.55), 0.75,
                          spot_price=SPOT, direct_price=(99_900.0, time.time()))
    assert signal is not None
    # Side selection is driven by the sign of (spot - threshold), so this is a
    # direct read of which price the strategy used.
    assert signal.outcome == "YES"


def test_maker_still_falls_back_to_the_feeds_without_a_passed_price(monkeypatch):
    signal, _ = _evaluate(monkeypatch, _market(0.55), 0.75,
                          spot_price=None, direct_price=(SPOT, time.time()))
    assert signal is not None
    assert signal.outcome == "YES"


# ── The certainty floor is the gate that decides ──────────────────────────────

def test_the_direction_gate_threshold_is_not_the_operative_one(monkeypatch):
    """fv 0.64 clears the advertised 0.62 and is still refused on a thin edge."""
    signal, rejects = _evaluate(monkeypatch, _market(0.60), 0.64)
    assert signal is None
    assert "MAKER:certainty_below_floor" in rejects
    detail = rejects["MAKER:certainty_below_floor"]["detail"]
    assert "fv>=0.650" in detail and "edge>=+0.043" in detail


def test_the_same_fair_value_trades_on_a_wider_edge(monkeypatch):
    """Same fv 0.64, 9c of edge: cert = 0.50 + 3.5*0.09 = 0.815, so it enters."""
    signal, _ = _evaluate(monkeypatch, _market(0.55), 0.64)
    assert signal is not None
    assert signal.certainty == pytest.approx(0.815, abs=1e-9)


def test_a_fair_value_above_the_floor_enters_on_a_thin_edge(monkeypatch):
    signal, _ = _evaluate(monkeypatch, _market(0.62, market_id="mkt-2"), 0.65)
    assert signal is not None


# ── Where an order can actually reach the exchange ────────────────────────────

@pytest.mark.parametrize("mid,expected", [
    (0.52, "quoted"),       # steps inside the ask
    (0.58, "quoted"),       # at the ceiling, level with the best bid
    (0.60, "quoted"),       # last mid where a 0.58 bid is within a tick
    (0.64, "maker_quote_behind_book"),
])
def test_the_058_ceiling_limits_quoting_to_near_coin_flip_markets(monkeypatch, mid, expected):
    signal, _ = _evaluate(monkeypatch, _market(mid, market_id=f"mkt-{mid:.2f}"), 0.70)
    assert signal is not None, f"fv 0.70 against a {mid} mid should signal"
    assert signal.market_price == config.MAKER_MAX_BID
    price, code, _ = executor._maker_quote_against_book(
        _book(mid), signal.market_price, fair_value=signal.win_prob
    )
    if expected == "quoted":
        assert price is not None, code
        assert price <= config.MAKER_MAX_BID + 1e-9
    else:
        assert price is None and code == expected


def test_above_the_quoting_region_the_edge_gate_refuses_before_the_book_is_read(monkeypatch):
    """fv 0.70 against a 0.70 mid: no 2c of edge, so there is nothing to quote."""
    signal, rejects = _evaluate(monkeypatch, _market(0.70, market_id="mkt-0.70"), 0.70)
    assert signal is None
    assert "MAKER:edge_below_floor" in rejects


def test_a_flat_tape_cannot_quote_and_says_so(monkeypatch):
    """mom_5m = 0 fails BTC's +0.0002 requirement — named, not lumped."""
    signal, rejects = _evaluate(monkeypatch, _market(0.55), 0.75,
                                state=_state(momentum=0.0))
    assert signal is None
    assert "MAKER:momentum_not_supporting" in rejects
    assert "needs >=+0.0002" in rejects["MAKER:momentum_not_supporting"]["detail"]


# ── Fair value is mostly extrapolated drift at the distances MAKER allows ─────

@pytest.mark.parametrize("secs,expected_with_drift", [
    (700, 0.612),   # 0.539 from position alone
    (400, 0.648),   # 0.552 from position alone
    (200, 0.705),   # 0.574 from position alone
])
def test_drift_contributes_more_than_price_position_at_the_minimum_distance(
        secs, expected_with_drift):
    """0.08% is BTC's distance calibration; 3%/h is an ordinary 0.05%/min move."""
    distance_only = gbm_win_probability(1.0008, 1.0, secs, 0.018)
    with_drift = gbm_win_probability(1.0008, 1.0, secs, 0.018,
                                     hourly_drift=0.03, horizon_cap=180.0)
    from_position = distance_only - 0.5
    from_drift = with_drift - distance_only
    assert from_drift > from_position, (from_position, from_drift)
    assert with_drift == pytest.approx(expected_with_drift, abs=0.002)
    if secs <= 400:
        # Inside the usual quoting window the extrapolation alone clears the
        # direction gate on a distance that by itself is worth ~5 points.
        assert with_drift >= 0.62
