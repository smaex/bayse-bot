import pytest

from strategies import merge_signals
from strategies.base import TradeSignal


def _signal(strategy, outcome, certainty, win_prob, price):
    return TradeSignal(
        strategy=strategy,
        event_id="e",
        market_id="m",
        asset="BTC",
        timeframe="5min",
        outcome=outcome,
        outcome_id=outcome.lower(),
        certainty=certainty,
        win_prob=win_prob,
        market_price=price,
        size_pct=0.01,
        reason=strategy,
    )


def test_agreement_boosts_only_same_direction():
    first = _signal("SNIPE", "YES", 0.70, 0.75, 0.55)
    second = _signal("FRONTRUN", "YES", 0.65, 0.72, 0.55)

    merged = merge_signals([first, second])

    assert len(merged) == 1
    assert merged[0].outcome == "YES"
    assert merged[0].certainty == pytest.approx(0.80)
    assert "CONVERGENCE" in merged[0].reason


def test_opposite_signals_do_not_get_false_convergence_boost():
    yes = _signal("SNIPE", "YES", 0.70, 0.75, 0.55)  # edge .20
    no = _signal("FRONTRUN", "NO", 0.75, 0.65, 0.55)  # edge .10

    merged = merge_signals([yes, no])

    assert len(merged) == 1
    assert merged[0].outcome == "YES"
    assert merged[0].certainty == 0.70
    assert "CONVERGENCE" not in merged[0].reason
