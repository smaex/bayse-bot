"""Merging signals: de-duplicate, then stop treating one bet as three.

With two strategies there is no "convergence boost" to compute — two methods
agreeing was only ever meaningful when there were five, and a boost applied to
a probability the model never claimed is how a confident number becomes an
overconfident one. What is left is the part that was always the real job:

* the same strategy firing twice on one market is one signal, not two;
* BTC, ETH and SOL moving together is one bet, not three;
* what survives is ranked by profit per unit of capital, which is the only
  ranking that means the same thing for a maker pair and a directional taker.
"""

from types import SimpleNamespace

import pytest

from strategies import _score, merge_signals
from strategies.base import QuoteLeg, TradeSignal


def _signal(strategy, outcome, certainty, win_prob, price,
            asset="BTC", market_id="m", edge=None, size_pct=0.02, legs=None):
    return TradeSignal(
        strategy=strategy,
        event_id="e",
        market_id=market_id,
        asset=asset,
        timeframe="5min",
        outcome=outcome,
        outcome_id=outcome.lower(),
        certainty=certainty,
        win_prob=win_prob,
        market_price=price,
        size_pct=size_pct,
        reason=strategy,
        edge_at_entry=edge if edge is not None else max(0.0, win_prob - price),
        legs=legs or [],
    )


def test_the_same_strategy_firing_twice_on_one_market_is_one_signal():
    weaker = _signal("TAKER", "YES", 0.70, 0.75, 0.55)
    stronger = _signal("TAKER", "YES", 0.80, 0.85, 0.55)

    merged = merge_signals([weaker, stronger])

    assert len(merged) == 1
    # The higher-scoring signal survives, not merely the last one seen.
    assert merged[0].certainty == pytest.approx(0.80)
    assert _score(merged[0]) == pytest.approx(_score(stronger))


def test_order_does_not_decide_which_signal_survives():
    weaker = _signal("TAKER", "YES", 0.70, 0.75, 0.55)
    stronger = _signal("TAKER", "YES", 0.80, 0.85, 0.55)

    assert merge_signals([stronger, weaker])[0].certainty == pytest.approx(0.80)
    assert merge_signals([weaker, stronger])[0].certainty == pytest.approx(0.80)


def test_different_strategies_on_one_market_are_not_collapsed():
    """The key is market *and* strategy. Collapsing on the market alone would
    let a resting maker quote silence a directional taker on the same
    market — the exact drought the cooldown key had to be fixed for."""
    taker = _signal("TAKER", "YES", 0.70, 0.75, 0.55)
    maker = _signal("MAKER", "YES", 0.80, 0.85, 0.55, legs=[
        QuoteLeg("YES", "yes", 0.45, 0.02, 0.55),
        QuoteLeg("NO", "no", 0.45, 0.02, 0.45),
    ])

    merged = merge_signals([taker, maker])

    assert len(merged) == 2
    assert {s.strategy for s in merged} == {"TAKER", "MAKER"}


def test_signals_are_ranked_by_profit_per_unit_of_capital():
    """A maker pair and a directional taker have to be comparable, or the risk
    budget funds whichever strategy uses the bigger numbers."""
    maker = _signal("MAKER", "YES", 0.80, 0.85, 0.55, market_id="m1", legs=[
        QuoteLeg("YES", "yes", 0.45, 0.02, 0.50),
        QuoteLeg("NO", "no", 0.45, 0.02, 0.50),
    ])
    # 1.00 - 0.90 on 0.90 of capital
    taker = _signal("TAKER", "YES", 0.80, 0.60, 0.50, market_id="m2", edge=0.055)

    merged = merge_signals([taker, maker])

    assert [s.market_id for s in merged] == ["m1", "m2"]
    assert _score(merged[0]) >= _score(merged[1])


def test_correlated_directional_bets_are_sized_down(monkeypatch):
    """BTC and SOL moving together is one bet. Funding both at full size is
    not diversification, it is a larger position with a better story."""
    import strategies
    from strategies import utils

    monkeypatch.setattr(utils, "realized_correlation", lambda a, b, state: 0.95)

    btc = _signal("TAKER", "YES", 0.70, 0.75, 0.55, asset="BTC", size_pct=0.02)
    sol = _signal("TAKER", "YES", 0.70, 0.75, 0.55, asset="SOL",
                  market_id="m2", size_pct=0.02)

    merged = merge_signals([btc, sol], state=SimpleNamespace(price_history={}))

    assert len(merged) == 2
    assert all("RISK_PARITY" in s.reason for s in merged)
    assert all(s.size_pct < 0.02 for s in merged)
    # Sized down, never to nothing.
    assert all(s.size_pct >= 0.01 for s in merged)


def test_uncorrelated_bets_keep_their_size(monkeypatch):
    from strategies import utils

    monkeypatch.setattr(utils, "realized_correlation", lambda a, b, state: 0.10)

    btc = _signal("TAKER", "YES", 0.70, 0.75, 0.55, asset="BTC", size_pct=0.02)
    sol = _signal("TAKER", "YES", 0.70, 0.75, 0.55, asset="SOL",
                  market_id="m2", size_pct=0.02)

    merged = merge_signals([btc, sol], state=SimpleNamespace(price_history={}))

    assert all(s.size_pct == 0.02 for s in merged)


def test_risk_parity_only_applies_to_directional_bets(monkeypatch):
    """A maker pair is already both sides of one market; halving it for
    "correlation" would punish the only position that has no forecast risk."""
    from strategies import utils

    monkeypatch.setattr(utils, "realized_correlation", lambda a, b, state: 0.95)

    legs = [QuoteLeg("YES", "yes", 0.45, 0.02, 0.50),
            QuoteLeg("NO", "no", 0.45, 0.02, 0.50)]
    btc = _signal("MAKER", "YES", 0.70, 0.75, 0.55, asset="BTC",
                  size_pct=0.02, legs=legs)
    sol = _signal("TAKER", "YES", 0.70, 0.75, 0.55, asset="SOL",
                  market_id="m2", size_pct=0.02)

    merged = merge_signals([btc, sol], state=SimpleNamespace(price_history={}))

    maker = next(s for s in merged if s.strategy == "MAKER")
    assert "RISK_PARITY" not in maker.reason
    assert maker.size_pct == 0.02
