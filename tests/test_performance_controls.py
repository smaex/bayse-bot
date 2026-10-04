"""Performance feedback moves size and confidence, and only ever downward on
confidence.

Two rules run through this file:

* settled results may shrink a model probability, never inflate one — the
  multiplier is clamped at 1.0, so a good track record cannot make a fresh
  estimate more confident than the model claimed;
* break-even is derived from the prices actually paid, not from a fixed
  per-strategy target, because the price paid *is* the hit rate required.
"""

from types import SimpleNamespace

import config
import database
from analysis import _break_even_rate
from executor import _performance_size_multiplier
from learner import (
    _settlement_pnl,
    adjusted_combo_size_multiplier,
    capital_weighted_break_even,
)
from strategies import (
    _LOCKED_STRATEGIES,
    _performance_adjusted_probability,
    _score,
)
from strategies.base import QuoteLeg, TradeSignal


def test_break_even_rate_depends_on_paid_prices_not_fixed_strategy_target():
    rows = [
        {
            "total_deployed": 100,
            "potential_payout": 200,  # price 0.50
        },
        {
            "total_deployed": 100,
            "potential_payout": 100 / 0.75,
        },
    ]

    # q_BE = total stake / total possible payout = 200 / 333.33 = 60%.
    assert abs(capital_weighted_break_even(rows) - 0.60) < 1e-9
    assert abs(
        _break_even_rate({"deployed": 200, "potential_payout": 200 / 0.60})
        - 0.60
    ) < 1e-9


def test_combo_loss_control_multiplies_strategy_size_control():
    sig = SimpleNamespace(strategy="MAKER", asset="ETH", timeframe="15min")
    learned = {
        "size_multipliers": {
            "MAKER": 0.80,
            "MAKER:ETH:15min": 0.25,
        }
    }

    assert _performance_size_multiplier(learned, sig) == 0.20
    assert adjusted_combo_size_multiplier(
        1.0, total=34, roi=-0.29,
        win_rate=15 / 34, break_even_rate=0.59,
    ) == 0.75


def test_a_locked_pair_is_exempt_from_directional_shrinkage():
    """A completed pair has no forecast risk, so shrinking its 'probability'
    toward 50% would be shrinking a number that means nothing.

    Execution learning (fill rate, adverse selection) still applies to MAKER
    through the learner's size controls. What it is exempt from is the
    *directional* shrinkage, which assumes the number being shrunk is a
    prediction.
    """
    assert "MAKER" in _LOCKED_STRATEGIES
    assert "TAKER" not in _LOCKED_STRATEGIES, \
        "a directional taker is exactly what shrinkage is for"


def test_settled_underperformance_reduces_directional_probability_and_edge():
    original = 0.70
    adjusted = _performance_adjusted_probability(original, 0.72)

    assert abs(adjusted - 0.644) < 1e-12
    assert adjusted - 0.50 < config.TAKER_MIN_NET_EV_DEFAULT / 0.30, \
        "no longer clears the taker's net-EV margin"


def test_historical_performance_never_inflates_fresh_model_probability():
    assert _performance_adjusted_probability(0.70, 1.20) == 0.70


def test_new_accounts_default_to_both_strategies_and_stay_paused():
    assert config.DEFAULT_STRATEGIES == ["TAKER", "MAKER"]
    assert config.DEFAULT_ASSETS == ["BTC", "ETH", "SOL"]
    assert config.DEFAULT_TIMEFRAMES == ["15min", "5min"]
    assert database.DEFAULT_SETTINGS["strategies"] == ["TAKER", "MAKER"]
    assert database.DEFAULT_SETTINGS["assets"] == ["BTC", "ETH", "SOL"]
    assert database.DEFAULT_SETTINGS["timeframes"] == ["15min", "5min"]
    assert database.DEFAULT_SETTINGS["mode"] == "balanced"
    assert database.DEFAULT_SETTINGS["maxexposure"] == 15.0
    assert database.DEFAULT_SETTINGS["daily_loss_limit_pct"] == 5.0
    assert database.DEFAULT_SETTINGS["paused"] is True


def test_no_deleted_strategy_survives_in_the_default_scope():
    """The purge has to hold at the defaults, not just in the source tree."""
    for stale in ("SNIPE", "ORACLE_ARB", "FRONTRUN", "CORRELATE",
                  "MIDMARKET_MAKER", "PAIRED_SNIPER", "ARB"):
        assert stale not in config.DEFAULT_STRATEGIES
        assert stale not in database.DEFAULT_SETTINGS["strategies"]


def test_maker_settlement_does_not_invent_a_five_percent_fee():
    # ₦120 at 0.60 buys two normalized shares and pays ₦200 on a win.
    # Bayse CLOB makers are fee-free, so PnL is ₦80, not ₦74.
    assert _settlement_pnl(
        won=True, amount_ngn=120, entry_price=0.60, filled_quantity=2
    ) == 80


# ── ranking puts both strategies on the same footing ─────────────────────────

def _sig(strategy, price, **kw):
    base = dict(
        strategy=strategy, event_id="e", market_id="m", asset="SOL",
        timeframe="15min", outcome="YES", outcome_id="yes",
        certainty=0.8, win_prob=price, market_price=price,
        size_pct=0.02, reason="t",
    )
    base.update(kw)
    return TradeSignal(**base)


def test_ranking_is_profit_per_unit_of_capital_not_raw_edge():
    """A MAKER pair and a TAKER must be comparable, or the risk budget funds
    whichever strategy happens to use the bigger numbers."""
    maker = _sig("MAKER", 0.50, legs=[
        QuoteLeg("YES", "y", 0.45, 0.02, 0.50),
        QuoteLeg("NO", "n", 0.45, 0.02, 0.50),
    ])
    # 1.00 - 0.90 = 0.10 profit on 0.90 capital
    assert abs(_score(maker) - (0.10 / 0.90)) < 1e-9

    taker = _sig("TAKER", 0.50, edge_at_entry=0.05)
    assert abs(_score(taker) - 0.10) < 1e-9


def test_a_pair_that_locks_nothing_ranks_last():
    maker = _sig("MAKER", 0.50, legs=[
        QuoteLeg("YES", "y", 0.50, 0.02, 0.50),
        QuoteLeg("NO", "n", 0.50, 0.02, 0.50),
    ])
    assert _score(maker) == 0.0
