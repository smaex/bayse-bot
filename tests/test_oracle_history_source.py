"""The price history behind the model must be the independent oracle.

``strategy.update_price_history`` feeds four estimators: the measured hourly
volatility, the 5-minute momentum, the Kalman velocity drift and the GARCH
variance. ``bot._on_spot_price`` is the *relay* callback, and it used to record
``check_lag(...)["price"]`` — which is the Bayse relay whenever the oracle
sample is more than 2 seconds old, because ``check_lag`` is answering "which
price is fresher right now".

While the relay was Binance-derived that substitution was invisible. Since
2026-09-26 the relay is a Chainlink 60-second TWAP, so the same line injects a
smoothed series into every volatility and drift estimate — each of which is
then biased low, which makes the model overconfident rather than cautious.

A 5-second-old Binance print is still a Binance print. The history now uses the
oracle until it is stale by ``FEED_STALE_SEC`` (30s), the standard the rest of
the bot already applies, and falls back to the relay only past that. The
no-oracle branch in ``_on_spot_price`` is unchanged: it has always recorded the
relay, deliberately, because blocking evaluations once caused 4-hour blackouts.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import bot
import config
import feeds_direct
import strategy


@pytest.fixture
def recorded(monkeypatch):
    seen = []
    monkeypatch.setattr(strategy, "update_price_history",
                        lambda asset, price, state=None: seen.append((asset, price)))

    async def _noop(asset, penalty=0.0):
        return None

    monkeypatch.setattr(bot, "_evaluate_all_users_for_asset", _noop)
    return seen


def _feed(monkeypatch, *, oracle_price: float, oracle_age: float,
          relay_price: float, status: str = "ok"):
    """One relay tick arrives; the oracle sample is ``oracle_age`` seconds old."""
    monkeypatch.setattr(feeds_direct, "get_direct_price",
                        lambda asset: (oracle_price, time.time() - oracle_age))
    monkeypatch.setattr(feeds_direct, "check_lag",
                        lambda asset, price: {"status": status, "price": relay_price,
                                              "diff_pct": 0.0, "lag_sec": oracle_age})

    async def _run():
        bot._on_spot_price("BTC", relay_price)
        await asyncio.sleep(0)          # let the evaluation task be created

    asyncio.run(_run())


def test_a_few_seconds_old_oracle_still_feeds_the_history(monkeypatch, recorded):
    """5s old: past check_lag's 2s preference, but nowhere near stale."""
    _feed(monkeypatch, oracle_price=100_000.0, oracle_age=5.0, relay_price=99_950.0)
    assert recorded == [("BTC", 100_000.0)]


def test_a_smoothed_relay_never_replaces_a_usable_oracle(monkeypatch, recorded):
    """The gap between oracle and relay is exactly what must not be averaged in."""
    _feed(monkeypatch, oracle_price=100_000.0, oracle_age=29.0, relay_price=99_700.0)
    assert recorded == [("BTC", 100_000.0)]


def test_a_stale_oracle_still_falls_back_to_the_relay(monkeypatch, recorded):
    """Past FEED_STALE_SEC the relay beats no data at all — history must not stop."""
    stale = config.FEED_STALE_SEC + 5.0
    _feed(monkeypatch, oracle_price=100_000.0, oracle_age=stale, relay_price=99_950.0)
    assert recorded == [("BTC", 99_950.0)]


def test_a_missing_oracle_falls_back_to_the_relay(monkeypatch, recorded):
    _feed(monkeypatch, oracle_price=0.0, oracle_age=0.0, relay_price=99_950.0)
    assert recorded == [("BTC", 99_950.0)]


def test_the_no_oracle_branch_is_unchanged(monkeypatch, recorded):
    """check_lag says stale: record the relay and evaluate, as before."""
    _feed(monkeypatch, oracle_price=100_000.0, oracle_age=0.0,
          relay_price=99_950.0, status="stale")
    assert recorded == [("BTC", 99_950.0)]
