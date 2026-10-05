"""The exit policy, tested as arithmetic rather than through a mock exchange.

This is the single most money-relevant decision the bot makes: hold a binary
to resolution and a share is worth its win probability; sell it now and it is
worth the bid less a taker fee. Every exit rule is a comparison of those two
numbers.

It used to be 200 lines inside a 628-line function that also fetched books,
talked to the exchange and wrote to the database, so the only way to test it
was to stand up a fake client. Now the decision is a pure function and can be
checked over a grid -- including the properties that matter and that no single
example could prove: never sell at a loss and call it profit, never stop out
on noise alone, never exit inside the final seconds.
"""

from __future__ import annotations

import pytest

import config
import bot
from strategies import book as booklib


FEE = 0.02


def _d(**over):
    """A default in-the-money-ish position, overridden per test."""
    base = dict(
        outcome="YES",
        w_est=0.60,        # model: worth 0.60/share held to resolution
        bid=0.60,          # book will pay 0.60
        peak_price=0.60,
        entry_price=0.55,  # taker paid 0.55, so ~0.5556 all-in after fee
        secs=600.0,
        fee_rate=FEE,
        is_maker_pos=False,
        confirmed_filled=True,
    )
    base.update(over)
    return bot._exit_decision(**base)


def _basis(entry, maker=False):
    return entry if maker else booklib.effective_buy_price(entry, FEE, is_maker=False)


# ── hold: nothing to do ──────────────────────────────────────────────────────

def test_a_position_the_market_prices_fairly_is_held():
    """Worth 0.60, bid 0.60: selling pays a fee to receive less than holding."""
    assert _d(w_est=0.60, bid=0.60) is None


def test_a_position_the_market_underpays_for_is_held():
    assert _d(w_est=0.80, bid=0.50) is None


# ── take profit: only when overpaid, and only into profit ────────────────────

def test_take_profit_fires_when_the_market_overpays_the_model():
    """Worth 0.55, bid 0.75: someone is paying far above our estimate."""
    d = _d(w_est=0.55, bid=0.75, entry_price=0.50)
    assert d is not None and d["reason"] == "TAKE_PROFIT"
    assert d["ev_hold"] > 0


def test_take_profit_never_sells_below_cost_whatever_the_model_says():
    """The bug this rule carries: exit value beat a *depressed* model estimate
    while being under water, so a loss was booked and logged as profit."""
    # Bid 0.50 against a cost basis of ~0.5556: under water by construction.
    d = _d(w_est=0.10, bid=0.50, entry_price=0.55)
    assert d is None or d["reason"] != "TAKE_PROFIT"


def test_take_profit_waits_for_the_minimum_profit_window():
    """Churning in and out in the last two minutes pays fees for nothing."""
    overpay = _d(w_est=0.55, bid=0.75, entry_price=0.50,
                 secs=config.EXIT_TAKE_PROFIT_MIN_SECS + 60)
    assert overpay["reason"] == "TAKE_PROFIT"
    too_late = _d(w_est=0.55, bid=0.75, entry_price=0.50,
                  secs=config.EXIT_TAKE_PROFIT_MIN_SECS - 1)
    assert too_late is None or too_late["reason"] != "TAKE_PROFIT"


# ── the stop: on the estimate, not on P&L ────────────────────────────────────

def test_the_stop_fires_when_the_estimate_collapses():
    """Worth 0.20 against a 0.5556 basis: we were wrong, so sell."""
    d = _d(w_est=0.20, bid=0.30)
    assert d is not None and d["reason"] == "STOP_LOSS"


def test_noise_does_not_trigger_the_stop():
    """Worth 0.50 against a 0.5556 basis with ten minutes left: down on the
    mark, unchanged in expectation. Selling here turns recoverable variance
    into a realised loss."""
    assert _d(w_est=0.50, bid=0.40, secs=600.0) is None


def test_the_stop_needs_a_real_move_not_a_rounding_error():
    basis = _basis(0.55)
    just_inside = basis * (1.0 - config.EXIT_STOP_DRAWDOWN) - 1e-4
    assert _d(w_est=just_inside, bid=0.50)["reason"] == "STOP_LOSS"
    just_outside = basis * (1.0 - config.EXIT_STOP_DRAWDOWN) + 1e-4
    assert _d(w_est=just_outside, bid=0.50) is None


def test_a_hard_price_backstop_exists_independently_of_the_model():
    """The model is not the only thing that can go wrong."""
    d = _d(w_est=0.90, bid=0.10)   # model still likes it; price has collapsed
    assert d is not None and d["reason"] == "STOP_LOSS"


def test_the_hard_backstop_will_not_sell_into_nothing():
    """Below the salvage floor a sale pays a fee to receive approximately
    zero, so holding to resolution is the better of two bad options."""
    d = _d(w_est=0.50, bid=config.EXIT_MIN_SALVAGE_PRICE / 2)
    assert d is None or d["reason"] != "STOP_LOSS"


# ── trailing protection ──────────────────────────────────────────────────────

def test_a_vanishing_gain_is_locked_in():
    d = _d(w_est=0.60, bid=0.62, peak_price=0.75, entry_price=0.50)
    assert d is not None and d["reason"] == "REVERSAL_EXIT"


def test_trailing_protection_never_sells_into_a_loss():
    """A reversal below entry is the stop's job; this rule only ever banks a
    gain that is still a gain."""
    d = _d(w_est=0.30, bid=0.42, peak_price=0.60, entry_price=0.55)
    assert d is None or d["reason"] != "REVERSAL_EXIT"


def test_a_small_pullback_from_the_peak_does_not_exit():
    d = _d(w_est=0.60, bid=0.73, peak_price=0.75, entry_price=0.50)
    assert d is None or d["reason"] != "REVERSAL_EXIT"


# ── maker and complete-set special cases ─────────────────────────────────────

def test_a_complete_set_is_burned_not_sold():
    """A set pays 1.00 whichever outcome wins, so there is no thesis to
    invalidate and nothing for a stop to protect."""
    d = _d(outcome="BOTH", w_est=1.0, bid=0.99)
    assert d["reason"] == "BURN_COMPLETE_SET"
    assert d["current_price"] == 1.0

    # A set held as two ordinary YES/NO positions is the same thing, and the
    # flag is how the exit loop says so. Selling either leg pays the bid and a
    # taker fee to give up a unit the burn pays in full.
    d = _d(outcome="YES", complete_set=True, w_est=0.62, bid=0.71)
    assert d["reason"] == "BURN_COMPLETE_SET"


def _position(risk, key, outcome, qty, *, strategy="TAKER", market_id="m", **over):
    risk.add_position(key, {
        "market_id": market_id, "outcome": outcome, "outcome_id": outcome.lower(),
        "entry_price": 0.50, "amount_ngn": 100.0, "strategy": strategy,
        "filled_quantity": qty, "confirmed_filled": True, **over,
    })


def test_a_taker_complete_set_is_recognised_across_the_risk_book():
    """`_paired_leg` used to require a MAKER sibling.

    A complete-set TAKER is two immediate FAK legs, so it never passes through
    the maker fill path that burns a resting pair; without this the lock would
    be managed -- and sold leg by leg -- as two directional bets.
    """
    from risk import RiskManager

    risk = RiskManager()
    _position(risk, "m:YES:o1", "YES", 50.0)
    _position(risk, "m:NO:o2", "NO", 50.0)

    paired = bot._paired_leg(risk, "m:YES:o1", "m")
    assert paired is not None and paired[0] == "m:NO:o2"


def test_an_unbalanced_pair_is_never_burned():
    """Only the overlap is a set, and the burn drops both entries.

    Burning 20 of 50 held shares would leave 30 shares untracked on the
    exchange, so an unbalanced pair is left alone: its overlap still settles
    to 1.00 and the larger leg is managed as the directional position it is.
    """
    from risk import RiskManager

    risk = RiskManager()
    _position(risk, "m:YES:o1", "YES", 50.0)
    _position(risk, "m:NO:o2", "NO", 20.0)
    assert bot._paired_leg(risk, "m:YES:o1", "m") is None

    # Same side is not a set.
    risk2 = RiskManager()
    _position(risk2, "m:YES:o1", "YES", 50.0)
    _position(risk2, "m:YES:o2", "YES", 50.0)
    assert bot._paired_leg(risk2, "m:YES:o1", "m") is None

    # An unfilled sibling is not a set.
    risk3 = RiskManager()
    _position(risk3, "m:YES:o1", "YES", 50.0)
    _position(risk3, "m:NO:o2", "NO", 50.0, confirmed_filled=False,
              filled_quantity=0.0)
    assert bot._paired_leg(risk3, "m:YES:o1", "m") is None

    # Opposite-side positions from different strategy families are opposite
    # bets (`already_in` blocks building them); they are not a set.
    risk4 = RiskManager()
    _position(risk4, "m:YES:o1", "YES", 50.0, strategy="TAKER")
    _position(risk4, "m:NO:o2", "NO", 50.0, strategy="MAKER")
    assert bot._paired_leg(risk4, "m:YES:o1", "m") is None


def test_a_maker_pair_still_pairs():
    from risk import RiskManager

    risk = RiskManager()
    _position(risk, "m:YES:o1", "YES", 40.0, strategy="MAKER")
    _position(risk, "m:NO:o2", "NO", 40.0, strategy="MAKER")
    assert bot._paired_leg(risk, "m:YES:o1", "m") is not None


def test_a_resting_quote_near_settlement_is_withdrawn():
    d = _d(is_maker_pos=True, confirmed_filled=False,
           secs=config.MAKER_LATE_CANCEL_SECS - 10, w_est=0.60, bid=0.60)
    assert d["reason"] == "CANCEL_RESTING"


def test_a_filled_maker_position_near_settlement_is_not_withdrawn():
    """It is a position now, not an order; the ordinary rules apply."""
    d = _d(is_maker_pos=True, confirmed_filled=True,
           secs=config.MAKER_LATE_CANCEL_SECS - 10, w_est=0.60, bid=0.60)
    assert d is None or d["reason"] != "CANCEL_RESTING"


def test_a_maker_pays_no_fee_so_its_basis_is_the_entry_price():
    """The maker's cost is what it bid; charging it the taker fee moved its
    stop closer and understated every maker profit.

    The two bases straddle the stop threshold here: 0.55 x 0.85 = 0.4675 for
    the maker, 0.5556 x 0.85 = 0.4722 for the taker. An estimate of 0.470 is
    inside the taker's stop and outside the maker's.
    """
    maker_basis = _basis(0.55, maker=True)
    taker_basis = _basis(0.55, maker=False)
    between = (maker_basis * (1.0 - config.EXIT_STOP_DRAWDOWN)
               + taker_basis * (1.0 - config.EXIT_STOP_DRAWDOWN)) / 2.0

    taker = _d(is_maker_pos=False, entry_price=0.55, w_est=between, bid=0.45)
    maker = _d(is_maker_pos=True, entry_price=0.55, w_est=between, bid=0.45)
    assert taker["reason"] == "STOP_LOSS"
    assert maker is None, "a fee-free fill must have more room, not less"


# ── properties over a grid ───────────────────────────────────────────────────

def test_no_exit_ever_realises_a_loss_under_a_profit_label():
    """The property version of the take-profit bug: over a grid of bids and
    estimates, TAKE_PROFIT and REVERSAL_EXIT may never fire below cost."""
    for entry in (0.30, 0.45, 0.55, 0.65):
        for maker in (False, True):
            basis = _basis(entry, maker)
            for bid in (0.02, 0.10, 0.25, 0.40, 0.55, 0.70, 0.85, 0.97):
                for w in (0.05, 0.25, 0.50, 0.75, 0.95):
                    d = bot._exit_decision(
                        outcome="YES", w_est=w, bid=bid, peak_price=max(bid, entry),
                        entry_price=entry, secs=600.0, fee_rate=FEE,
                        is_maker_pos=maker, confirmed_filled=True,
                    )
                    if d is None or d["reason"] not in ("TAKE_PROFIT", "REVERSAL_EXIT"):
                        continue
                    proceeds = booklib.effective_sell_proceeds(
                        bid, 1.0, FEE, is_maker=False)
                    assert proceeds > basis, (
                        f"{d['reason']} below cost: bid={bid} basis={basis:.4f} "
                        f"entry={entry} maker={maker}"
                    )


def test_the_decision_is_deterministic_and_total():
    """Over the whole input grid it returns a known reason or None -- never
    raises, never invents one."""
    allowed = {"BURN_COMPLETE_SET", "CANCEL_RESTING", "TAKE_PROFIT",
               "REVERSAL_EXIT", "STOP_LOSS"}
    seen = set()
    for outcome in ("YES", "NO", "BOTH"):
        for w in (0.0, 0.5, 1.0):
            for bid in (0.0, 0.01, 0.5, 0.99):
                for secs in (0.0, 60.0, 600.0):
                    for maker in (False, True):
                        for filled in (False, True):
                            d = bot._exit_decision(
                                outcome=outcome, w_est=w, bid=bid,
                                peak_price=bid, entry_price=0.55, secs=secs,
                                fee_rate=FEE, is_maker_pos=maker,
                                confirmed_filled=filled,
                            )
                            if d is None:
                                continue
                            assert set(d) == {"reason", "current_price", "ev_hold"}
                            assert d["reason"] in allowed
                            seen.add(d["reason"])
    assert seen, "the grid should exercise more than one outcome"
