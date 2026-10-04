"""Position sizing is the number that decides whether a mistake is survivable.

It used to be 130 lines buried inside an 840-line order-placement function,
which meant the only way to test "does the bot risk too much?" was to run the
whole placement path against a mock exchange. Now it stands alone.

Every rule below is a ceiling. The property that matters is the one at the
bottom: whatever the strategy asks for, the size that comes out is bounded by
the account's own risk setting, the mode ceiling and the cash actually there.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import config
import executor
from risk import RiskManager


def _sig(**over):
    base = dict(
        strategy="TAKER", event_id="e", market_id="m", asset="BTC",
        timeframe="15min", outcome="YES", outcome_id="yes",
        certainty=0.70, win_prob=0.70, market_price=0.50,
        size_pct=0.02, reason="t",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _settings(**over):
    base = {"mode": "balanced", "risk_pct": 2.0, "mintrade": 100.0,
            "maxtrade": 5_000.0, "maxexposure": 15.0, "learned": {}}
    base.update(over)
    return base


def _run(sig=None, settings=None, *, equity=100_000.0, free_cash=100_000.0,
         n_legs=1, risk=None, mode=None, mult=1.0, user_risk=None,
         min_t=None, max_t=None):
    settings = settings if settings is not None else _settings()
    sig = sig if sig is not None else _sig()
    return asyncio.run(executor._size_for_signal(
        sig, settings, risk or RiskManager(), chat_id="c1",
        equity=equity, free_cash=free_cash, n_legs=n_legs,
        mode=mode or settings.get("mode", "balanced"),
        mult=mult,
        user_risk=user_risk if user_risk is not None
        else min(settings.get("risk_pct", 2.0) / 100.0, config.MAX_TRADE_RISK),
        min_t=min_t if min_t is not None else settings.get("mintrade", 100.0),
        max_t=max_t if max_t is not None else settings.get("maxtrade", 5_000.0),
    ))


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Sizing reads the alpha trend; treat it as healthy unless a test says so."""
    monkeypatch.setattr(
        executor.database, "get_alpha_trend", lambda *a, **k: 1.0, raising=False
    )
    monkeypatch.setattr(executor, "active_markets", [])


# ── the strategy's own size ──────────────────────────────────────────────────

def test_a_strategy_that_names_its_size_gets_it():
    assert _run(_sig(size_pct=0.02)).final_pct == pytest.approx(0.02)


def test_settled_performance_scales_the_strategys_own_size():
    assert _run(_sig(size_pct=0.02), mult=0.5).final_pct == pytest.approx(0.01)


def test_low_conviction_is_sized_down_and_high_conviction_is_capped():
    """At the default risk setting every signal at or above 0.55 certainty
    sizes at exactly the account ceiling: conviction never buys more size, it
    only costs you some when the signal is weak.

    Deliberately so. ``risk_pct`` is the operator's stated ceiling and
    ``certainty`` is a heuristic score, not a calibrated probability -- the
    Telegram notification calls it one. Letting a heuristic raise the stake is
    how a bot sizes up into its own worst strategy.
    """
    weak   = _run(_sig(size_pct=0.0, certainty=0.40)).final_pct
    mid    = _run(_sig(size_pct=0.0, certainty=0.60)).final_pct
    strong = _run(_sig(size_pct=0.0, certainty=0.96)).final_pct
    assert weak < mid
    assert strong == pytest.approx(mid)
    assert mid == pytest.approx(min(2.0 / 100.0, config.MAX_TRADE_RISK))


def test_fx_assets_are_sized_below_crypto_at_the_same_conviction():
    """FX moves on scheduled data releases, not continuous diffusion, so the
    model's confidence in it is worth less.

    Measured in the region where the tier actually binds -- above it the
    account ceiling masks the difference.
    """
    crypto = _run(_sig(size_pct=0.0, certainty=0.40, asset="BTC")).final_pct
    fx = _run(_sig(size_pct=0.0, certainty=0.40, asset="EURUSD")).final_pct
    assert fx == pytest.approx(crypto * 0.5)


def test_the_account_ceiling_is_what_actually_sets_the_size():
    """Whatever the conviction, the number that comes out is the operator's
    risk setting -- not a multiple of it."""
    for risk_pct in (0.5, 1.0, 2.0):
        settings = _settings(risk_pct=risk_pct)
        expected = min(risk_pct / 100.0, config.MAX_TRADE_RISK)
        for certainty in (0.60, 0.75, 0.90, 0.96):
            assert _run(_sig(size_pct=0.0, certainty=certainty),
                        settings).final_pct == pytest.approx(expected)


# ── the ceilings that bind ───────────────────────────────────────────────────

def test_the_risk_setting_is_a_ceiling_not_a_suggestion():
    """A Kelly-sized signal used to bypass risk_pct entirely."""
    settings = _settings(risk_pct=1.0)
    assert _run(_sig(size_pct=0.50), settings).final_pct <= 0.01 + 1e-9


def test_the_mode_ceiling_binds_when_risk_setting_is_above_it():
    settings = _settings(risk_pct=50.0)   # absurd on purpose
    assert _run(_sig(size_pct=0.50), settings).final_pct == pytest.approx(0.05)


def test_a_decaying_edge_halves_the_size(monkeypatch):
    healthy = _run(_sig(size_pct=0.02)).final_pct
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *a, **k: 0.5)
    decaying = _run(_sig(size_pct=0.02)).final_pct
    assert decaying == pytest.approx(healthy * 0.5)


def test_confidence_never_cancels_the_learners_performance_decay():
    """The regression this extraction was for.

    Conviction used to scale size *up* (2.0x above 0.90 certainty, plus
    another 1.5x above 0.95). With the learner saying "this strategy is
    underperforming, halve it" (mult 0.5), those multipliers multiplied back
    out: 2.0 x 0.5 = 1.0, so a halved strategy still bet the full risk_pct
    whenever a signal looked confident. The performance control was being
    overridden precisely where overconfidence is most likely.
    """
    decaying = _run(_sig(size_pct=0.0, certainty=0.96), mult=0.5).final_pct
    healthy  = _run(_sig(size_pct=0.0, certainty=0.96), mult=1.0).final_pct
    assert decaying == pytest.approx(healthy * 0.5)

    for certainty in (0.60, 0.75, 0.90, 0.96):
        assert _run(_sig(size_pct=0.0, certainty=certainty), mult=0.5).final_pct \
            == pytest.approx(
                _run(_sig(size_pct=0.0, certainty=certainty), mult=1.0).final_pct * 0.5
            )


def test_a_halved_strategy_never_bets_more_than_a_healthy_one():
    """The property version: decay must bind at every conviction level."""
    for certainty in (0.40, 0.60, 0.75, 0.90, 0.96):
        for mult in (0.25, 0.5, 0.75, 1.0):
            base = _run(_sig(size_pct=0.0, certainty=certainty), mult=1.0).final_pct
            got = _run(_sig(size_pct=0.0, certainty=certainty), mult=mult).final_pct
            assert got <= base * mult + 1e-9, (certainty, mult, got, base)


def test_probation_halves_the_size():
    risk = RiskManager()
    risk.probation_trades_left = 3
    assert risk.is_on_probation()
    assert _run(_sig(size_pct=0.02), risk=risk).final_pct == pytest.approx(0.01)


def test_the_hard_cap_never_exceeds_free_cash_across_the_legs():
    """A two-sided quote commits its size twice. Each leg only gets its share,
    or the second placement fails after the first has already filled."""
    one = _run(_sig(), equity=100_000.0, free_cash=1_000.0, n_legs=1)
    two = _run(_sig(), equity=100_000.0, free_cash=1_000.0, n_legs=2)
    assert two.hard_cap == pytest.approx(one.hard_cap / 2)


def test_the_hard_cap_respects_the_users_max_trade():
    assert _run(_sig(size_pct=0.9), equity=1_000_000.0,
                max_t=2_000.0).hard_cap <= 2_000.0 + 1e-9


def test_the_market_minimum_is_the_floor_when_the_exchange_says_so(monkeypatch):
    monkeypatch.setattr(
        executor, "active_markets",
        [{"market_id": "m", "minimum_order_amount": 250.0}],
    )
    assert _run(_sig(), min_t=100.0).effective_min == pytest.approx(250.0)


# ── the property that matters ────────────────────────────────────────────────

def test_no_signal_can_commit_more_than_the_accounts_own_risk_setting():
    """The whole point of the extraction: a property, checked over a grid,
    that no single example could have proved."""
    for mode in ("safe", "balanced", "aggressive", "full_send"):
        for risk_pct in (0.5, 1.0, 2.0, 5.0):
            for size_pct in (0.0, 0.01, 0.05, 0.5):
                for certainty in (0.40, 0.60, 0.75, 0.96):
                    settings = _settings(mode=mode, risk_pct=risk_pct)
                    out = _run(_sig(size_pct=size_pct, certainty=certainty), settings)
                    ceiling = min(risk_pct / 100.0, config.MAX_TRADE_RISK)
                    assert out.final_pct <= ceiling + 1e-9, (
                        mode, risk_pct, size_pct, certainty, out
                    )
                    assert out.hard_cap >= 0.0


def test_a_bigger_account_does_not_mean_a_bigger_fraction():
    """Risk is a fraction of equity, so the fraction must not grow with it."""
    small = _run(_sig(size_pct=0.02), equity=10_000.0, free_cash=10_000.0)
    large = _run(_sig(size_pct=0.02), equity=10_000_000.0, free_cash=10_000_000.0)
    assert small.final_pct == pytest.approx(large.final_pct)
