"""Offline tests for tools/reconcile_burn_payouts.py (parsing and holdings only)."""
from tools import reconcile_burn_payouts as rb

BOOKED = (
    "2026-10-20 10:00:00 CRITICAL [5551] 🚨 Fill reconciliation required for order "
    "burn:mkt-A (BTC maker). The fill remains tracked conservatively; verify shares "
    "and cash debit in Bayse before restarting. Reason: reported burn payout was "
    "malformed or inconsistent; used guaranteed set payout"
)
BOOKED_OMITTED = (
    "CRITICAL [5551] 🚨 Fill reconciliation required for order burn:mkt-B (ETH taker). "
    "Reason: burn response omitted payout; used guaranteed set payout"
)
POST_FIX_PAUSE = (
    "CRITICAL [5552] 🚨 Fill reconciliation required for order burn:mkt-C (SOL taker). "
    "Reason: reported burn payout 1.23 disagrees with 123.45 for 1.2345 sets; "
    "cash received is unverified"
)
OTHER = "CRITICAL [5551] 🚨 Fill reconciliation required for order ord-9 (BTC taker). Reason: stale order"


def test_parses_pre_fix_phantom_bookings_as_booked():
    alerts = list(rb.parse_alerts([BOOKED, BOOKED_OMITTED]))
    assert [(c, m, cat) for c, m, _o, cat, _r in alerts] == [
        ("5551", "mkt-A", "booked"),
        ("5551", "mkt-B", "booked"),
    ]


def test_post_fix_pauses_are_not_counted_as_phantom():
    alerts = list(rb.parse_alerts([POST_FIX_PAUSE]))
    assert alerts[0][3] == "unverified"


def test_unrelated_reconciliation_alerts_are_ignored():
    assert list(rb.parse_alerts([OTHER])) == []


def test_summarize_groups_per_account():
    report = rb.summarize(rb.parse_alerts([BOOKED, BOOKED_OMITTED, POST_FIX_PAUSE]))
    assert report["5551"]["booked_events"] == 2
    assert report["5551"]["markets"] == ["mkt-A", "mkt-B"]
    assert report["5552"]["booked_events"] == 0
    assert report["5552"]["unverified_events"] == 1


def test_holdings_count_complete_sets_as_min_of_yes_and_no():
    rows = [
        {"outcome": "YES", "balance": 10.0, "market": {"id": "mkt-A"}},
        {"outcome": "NO", "balance": 7.5, "market": {"id": "mkt-A"}},
        {"outcome": "YES", "balance": 3.0, "market": {"id": "other"}},
    ]
    held = rb.market_holdings(["mkt-A", "mkt-B"], rows)
    assert held["mkt-A"]["complete_sets_held"] == 7.5
    assert held["mkt-A"]["value_ngn_if_burned"] == 750.0
    assert held["mkt-B"]["complete_sets_held"] == 0.0


def test_holdings_are_per_account_inputs_not_shared_state():
    # Two accounts must not see each other's portfolio.
    a = rb.market_holdings(["mkt-A"], [{"outcome": "YES", "balance": 5, "market": {"id": "mkt-A"}},
                                       {"outcome": "NO", "balance": 5, "market": {"id": "mkt-A"}}])
    b = rb.market_holdings(["mkt-A"], [])
    assert a["mkt-A"]["complete_sets_held"] == 5.0
    assert b["mkt-A"]["complete_sets_held"] == 0.0
