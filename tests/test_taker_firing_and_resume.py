"""Two failures that both looked like "the bot is doing nothing".

1. **The taker never placed an order.** Two independent gates did it, and
   neither was visible in the config that was supposed to decide entries:

   * `_resolve_collision` preferred ANY MAKER signal over ANY TAKER signal.
     `MAKER_ALLOW_SINGLE_LEG` is on by default, so the "MAKER" that won was
     usually a one-sided passive bid -- exactly as directional as the taker,
     just resting instead of crossing.
   * `TradeSignal.mode_floor` defaulted to 0.48 and was never set by any
     strategy. Executor `_execute_logic` refuses any signal whose `certainty`
     is below it -- and a TAKER's certainty is the model probability mapped
     through `(p - 0.5) / 0.45`, so the shared 0.48 silently meant "model
     probability >= 0.716" and killed the entry *after* the 6%-net-EV gate had
     already approved it.

2. **`/resume` did not resume.** The daily loss stop, the daily target and the
   drawdown stop are all recomputed from a baseline captured at the start of
   the trading day, so clearing the pause flag re-tripped the same stop on the
   next cycle. The stall report promised "/resume overrides it explicitly";
   the code discarded the override.

3. **The structural complete-set take could never fire.** Each leg of a set had
   to clear the *directional* conviction floor (``model_prob >= 0.55``), but the
   two legs' probabilities are complementary -- they sum to 1.00 -- so both
   above 0.55 is unsatisfiable. Every candidate was rejected as
   ``complete_set_leg_not_standalone`` no matter how wide the lock.

4. **A multi-leg set was placed with equal *naira* on unequal prices.** A set
   settles on ``min(shares_yes, shares_no)``, so paying the same amount for a
   0.46 leg and a 0.50 leg buys more shares of the cheaper side: the "locked"
   part covered only the smaller quantity and the remainder was unhedged
   directional risk that neither the sizing nor the pair accounting charged.
"""

import asyncio
from types import SimpleNamespace

import pytest

import config
import executor
from risk import RiskManager
from strategies import _is_locked_pair, _resolve_collision, evaluate_all
from strategies.base import QuoteLeg, TradeSignal
from strategies.taker import TakerStrategy
from strategies.utils import certainty_to_prob, probability_to_certainty


# ── 1. The collision rule ─────────────────────────────────────────────────────

def _signal(strategy, price, *, outcome="YES", win_prob=None, edge=None,
            size_pct=0.02, legs=None, asset="BTC"):
    return TradeSignal(
        strategy=strategy,
        event_id="e",
        market_id="m",
        asset=asset,
        timeframe="15min",
        outcome=outcome,
        outcome_id=outcome.lower(),
        certainty=0.6,
        win_prob=win_prob if win_prob is not None else price,
        market_price=price,
        size_pct=size_pct,
        reason=f"{strategy} test",
        edge_at_entry=edge if edge is not None else max(0.0, price - price + 0.05),
        legs=legs or [],
    )


def test_a_single_leg_maker_quote_no_longer_suppresses_a_better_taker():
    """The regression that kept the taker at zero orders.

    Both signals are directional. The taker's expected return per unit of
    capital is higher, so it must be the one that reaches the executor.
    """
    maker = _signal("MAKER", 0.54, edge=0.02, legs=[
        QuoteLeg("YES", "yes", 0.54, 0.02, 0.56),
    ])
    taker = _signal("TAKER", 0.55, win_prob=0.66, edge=0.10)

    winner = _resolve_collision([maker, taker], {})

    assert len(winner) == 1
    assert winner[0].strategy == "TAKER", (
        "a one-sided maker quote is a directional bid; it cannot outrank a "
        "taker by name alone"
    )


def test_a_locked_maker_pair_still_wins_on_the_same_market():
    """The rule that must survive the fix: direction-free beats a forecast."""
    pair = _signal("MAKER", 0.48, outcome="BOTH", edge=0.04, legs=[
        QuoteLeg("YES", "yes", 0.46, 0.02, 0.55),
        QuoteLeg("NO", "no", 0.50, 0.02, 0.45),
    ])
    taker = _signal("TAKER", 0.55, win_prob=0.66, edge=0.10)

    winner = _resolve_collision([pair, taker], {})

    assert [s.strategy for s in winner] == ["MAKER"]
    assert _is_locked_pair(pair)


def test_a_locked_taker_set_beats_a_single_leg_maker_quote():
    maker = _signal("MAKER", 0.54, edge=0.06, legs=[
        QuoteLeg("YES", "yes", 0.54, 0.02, 0.56),
    ])
    locked_taker = _signal("TAKER", 0.48, outcome="BOTH", edge=0.03, legs=[
        QuoteLeg("YES", "yes", 0.46, 0.02, 0.55),
        QuoteLeg("NO", "no", 0.50, 0.02, 0.45),
    ])

    winner = _resolve_collision([maker, locked_taker], {})

    assert [s.strategy for s in winner] == ["TAKER"]


def test_a_locked_set_is_not_shrunk_even_after_a_losing_streak(monkeypatch):
    """A complete set's profit is `1 - cost`, which no forecast can change.

    Performance shrinkage is a directional verdict. Applying it to a locked
    set does not make the set smaller -- it re-checks a model-independent lock
    against `win_prob - effective >= 0`, so once the multiplier dropped far
    enough every complete-set take was silently skipped as if the model had
    lost its edge. The exemption is a property of the *set* (two complementary
    legs priced below 1.00), not of the strategy name.
    """
    locked_taker = _signal("TAKER", 0.48, outcome="BOTH", win_prob=0.60, legs=[
        QuoteLeg("YES", "yes", 0.46, 0.02, 0.55),
        QuoteLeg("NO", "no", 0.50, 0.02, 0.45),
    ])

    class FakeTaker:
        name = "TAKER"

        async def evaluate(self, market, learned, state, spot_price=None, books=None):
            return locked_taker

    monkeypatch.setattr("strategies._strategies", {"TAKER": FakeTaker()})
    learned = {
        "chat_id": "c",
        "strategies": ["TAKER"],
        # A badly losing record: the directional path would shrink 0.60 to
        # 0.5025, below the 0.5556 fee-inclusive price of the expensive leg.
        "certainty_multipliers": {"TAKER": 0.1},
    }

    signals = asyncio.run(evaluate_all({"fee_rate": 0.02}, learned, None))

    assert len(signals) == 1
    assert signals[0].win_prob == pytest.approx(0.60), (
        "a locked set must not be shrunk by a directional performance verdict"
    )


def test_a_single_leg_maker_signal_is_shrunk_by_performance_evidence(monkeypatch):
    """`_LOCKED_STRATEGIES` used to exempt the whole MAKER family.

    A one-sided quote is a prediction, so settled evidence against the
    combination has to shrink it -- otherwise the learner's only lever on the
    direction that actually loses money was the size multiplier.
    """
    single_leg = _signal("MAKER", 0.54, win_prob=0.60, legs=[
        QuoteLeg("YES", "yes", 0.54, 0.02, 0.60),
    ])

    class FakeMaker:
        name = "MAKER"

        async def evaluate(self, market, learned, state, spot_price=None, books=None):
            return single_leg

    class FakeTaker:
        name = "TAKER"

        async def evaluate(self, market, learned, state, spot_price=None, books=None):
            return None

    monkeypatch.setattr(
        "strategies._strategies", {"MAKER": FakeMaker(), "TAKER": FakeTaker()}
    )
    learned = {
        "chat_id": "c",
        "strategies": ["MAKER", "TAKER"],
        "certainty_multipliers": {"MAKER": 0.5},
    }
    market = {"fee_rate": 0.02}

    signals = asyncio.run(evaluate_all(market, learned, None))

    assert len(signals) == 1
    assert signals[0].win_prob == pytest.approx(0.55), \
        "0.60 shrunk halfway toward 0.50 by a 0.5 multiplier"
    assert "PERF_P" in signals[0].reason


def test_the_shrinkage_check_uses_the_fee_inclusive_price(monkeypatch):
    """The guard divided by `(1 - 0.0)` -- i.e. by one.

    So a fee-bearing taker was re-checked against a raw price and the fee had
    no room in the check at all, while the comment claimed the opposite.
    """
    maker_leg = _signal("TAKER", 0.55, win_prob=0.60)

    class FakeTaker:
        name = "TAKER"

        async def evaluate(self, market, learned, state, spot_price=None, books=None):
            return maker_leg

    monkeypatch.setattr(
        "strategies._strategies", {"TAKER": FakeTaker()}
    )
    learned = {
        "chat_id": "c",
        "strategies": ["TAKER"],
        "certainty_multipliers": {"TAKER": 0.99},
    }
    # 0.60 shrunk by 0.99 -> 0.599. Raw price 0.55 leaves it alone; the
    # fee-inclusive 0.555 still leaves it alone, but a 1.2% fee does not.
    signals = asyncio.run(evaluate_all({"fee_rate": 0.02}, learned, None))
    assert len(signals) == 1
    assert signals[0].edge_at_entry == pytest.approx(
        signals[0].win_prob - 0.55 / (1.0 - 0.02 * 0.5), abs=1e-9
    )


# ── 2. The conviction floor must match the strategy's own gates ───────────────

def test_taker_mode_floor_is_its_own_admission_floor():
    taker = TakerStrategy()

    # The floor the executor compares against, and the probability it means.
    assert taker is not None
    floor = probability_to_certainty(config.TAKER_MIN_MODEL_PROB)
    assert certainty_to_prob(floor) == pytest.approx(config.TAKER_MIN_MODEL_PROB)


def test_a_taker_signal_that_clears_the_ev_gate_clears_the_floor_too():
    """End to end through the real strategy and the real executor.

    Model 0.662 against a 0.55 ask (fee-inclusive 0.556) is a +19.2% EV
    mispricing -- comfortably through the 6% gate, and previously refused as a
    "probe" because its certainty (0.36) was below the shared 0.48 default.
    """
    from collections import deque
    import time as _time

    from strategies.base import MarketState

    spot, threshold = 100_200.0, 100_000.0
    now = _time.time()
    state = MarketState()
    state.price_history["BTC"] = deque(
        [(now - (60 - i), spot) for i in range(60)], maxlen=2000
    )
    state.kalman_state["BTC"] = {
        "x": [spot, 0.0], "P": [[1.0, 0.0], [0.0, 0.01]], "last_time": now,
    }
    state.garch_state["BTC"] = {
        "var": (config.ASSET_HOURLY_VOL["BTC"] ** 2) / 720.0, "last_price": spot,
    }

    market = {
        "event_id": "e1", "market_id": "m1", "asset": "BTC", "timeframe": "15min",
        "title": "BTC above 100k", "threshold": threshold,
        "yes_id": "yes", "no_id": "no", "yes_price": 0.55, "no_price": 0.45,
        "fee_rate": 0.02, "secs_to_close": 600, "status": "open", "engine": "CLOB",
        "minimum_order_amount": 100.0, "closing_date": "2026-10-05T12:15:00Z",
    }
    books = {
        "yes": {"bids": [{"price": 0.54, "quantity": 800}],
                "asks": [{"price": 0.55, "quantity": 800}]},
        "no": {"bids": [{"price": 0.44, "quantity": 800}],
               "asks": [{"price": 0.45, "quantity": 800}]},
    }

    monkeypatch_measured_vol = config.USE_MEASURED_VOL
    config.USE_MEASURED_VOL = False
    try:
        signal = asyncio.run(TakerStrategy().evaluate(
            market, {"chat_id": "c", "mode": "balanced"}, state,
            spot_price=spot, books=books,
        ))
    finally:
        config.USE_MEASURED_VOL = monkeypatch_measured_vol

    assert signal is not None, "a 19% fee-inclusive mispricing must be a signal"
    assert signal.strategy == "TAKER"
    assert signal.certainty >= signal.mode_floor, (
        "the executor's below_mode_floor gate must not contradict the "
        "strategy's own EV gate"
    )
    assert signal.mode_floor == pytest.approx(
        probability_to_certainty(config.TAKER_MIN_MODEL_PROB)
    )


def test_taker_scope_gate_is_enforced(monkeypatch):
    """TAKER_ALLOWED_ASSETS/TIMEFRAMES were declared and never read."""
    taker = TakerStrategy()
    base = {
        "event_id": "e", "market_id": "m", "timeframe": "1h", "threshold": 1900.0,
        "yes_id": "y", "no_id": "n", "yes_price": 0.55, "no_price": 0.45,
        "fee_rate": 0.02, "secs_to_close": 600, "engine": "CLOB",
    }
    rejected = []

    class Learned(dict):
        pass

    learned = {"chat_id": "c"}

    import stall
    stall.reset("c")
    signal = asyncio.run(taker.evaluate(
        {**base, "asset": "XAUUSD"}, learned, None, spot_price=1900.0, books={}
    ))
    assert signal is None
    assert any(
        code.endswith("asset_not_in_allowed_scope")
        for code in (stall._users.get("c", {}).get("rejects") or {})
    ), "the scope gate must be visible in the drought report"


# ── 3. Unpaired maker exposure ────────────────────────────────────────────────

def test_unpaired_maker_notional_counts_only_one_sided_bets():
    risk = RiskManager()
    pair_yes = {"strategy": "MAKER", "market_id": "m1", "outcome": "YES",
                "amount_ngn": 100.0, "filled_quantity": 0.0}
    pair_no = {"strategy": "MAKER", "market_id": "m1", "outcome": "NO",
               "amount_ngn": 100.0, "filled_quantity": 0.0}
    risk.open_positions["m1:YES:o1"] = dict(pair_yes)
    risk.open_positions["m1:NO:o2"] = dict(pair_no)

    # A two-sided resting quote is not unpaired: neither leg is held yet.
    assert risk.maker_unpaired_notional() == 0.0

    # One leg fills: that leg is now a directional position until the other
    # completes it, and it is charged.
    risk.open_positions["m1:YES:o1"].update(
        {"filled_quantity": 50.0, "confirmed_filled": True}
    )
    assert risk.maker_unpaired_notional() == 100.0

    # Both legs filled: a complete set, nothing unpaired.
    risk.open_positions["m1:NO:o2"].update(
        {"filled_quantity": 50.0, "confirmed_filled": True}
    )
    assert risk.maker_unpaired_notional() == 0.0

    # A deliberately one-sided quote is unpaired by construction.
    risk.open_positions["m2:YES:o3"] = {
        "strategy": "MAKER", "market_id": "m2", "outcome": "YES",
        "amount_ngn": 250.0, "filled_quantity": 0.0,
    }
    assert risk.maker_unpaired_notional() == 250.0


def test_a_single_leg_maker_quote_is_capped_by_the_unpaired_budget(monkeypatch):
    """MAX_MAKER_UNPAIRED_PCT existed, was validated, and was read by nothing."""
    monkeypatch.setattr(
        executor, "active_markets",
        [{"market_id": "m9", "engine": "CLOB", "minimum_order_amount": 100,
          "secs_to_close": 300, "threshold": 100}],
    )
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *a: 1.0)

    class FakeClient:
        def __init__(self):
            self.place_calls = []

        async def place_order(self, **kw):
            self.place_calls.append(kw)
            raise AssertionError("the unpaired cap must fire before placement")

    risk = RiskManager()
    risk.current_free_cash = 5_000.0
    # ₦900 of a ₦10,000 account is already in a one-sided maker quote: below
    # the 20% total maker budget, above the 10% unpaired budget once the new
    # ₦200 one-sided quote is added.
    risk.open_positions["m1:YES:old"] = {
        "strategy": "MAKER", "market_id": "m1", "outcome": "YES",
        "amount_ngn": 900.0, "filled_quantity": 0.0,
    }
    signal = TradeSignal(
        strategy="MAKER", event_id="e", market_id="m9", asset="BTC",
        timeframe="15min", outcome="YES", outcome_id="yes",
        certainty=0.6, win_prob=0.6, market_price=0.50, size_pct=0.02,
        reason="MAKER single-leg test", mode_floor=config.MAKER_MIN_LEG_BID,
        legs=[QuoteLeg("YES", "yes", 0.50, 0.02, 0.60)],
    )
    client = FakeClient()

    asyncio.run(executor._execute_logic(
        "c", signal, client, risk,
        {"mode": "balanced", "risk_pct": 2.0, "mintrade": 100,
         "maxtrade": 5000, "maxexposure": 20.0},
        10_000.0, 5_000.0,
    ))

    assert client.place_calls == []
    import stall
    assert "exec:maker_unpaired_cap" in (stall._users.get("c", {}).get("rejects") or {}), \
        "the skip must be attributed to the unpaired budget, not silently dropped"


# ── 4. Resume clears the restriction it claims to override ────────────────────

def test_session_pnl_is_measured_from_the_resume_baseline():
    import bot

    day = {"date": "2026-10-05", "start_balance": 6_650.0,
           "target_hit": False, "pnl_baseline": -350.0}

    # ₦350 of losses booked before the operator resumed do not count again.
    assert bot._session_pnl_for_day(-350.0, day) == 0.0
    # New losses after the resume do.
    assert bot._session_pnl_for_day(-420.0, day) == -70.0
    # A normal day (no resume) is unchanged.
    assert bot._session_pnl_for_day(-120.0, {"date": "x", "start_balance": 1.0}) == -120.0


def test_resume_moves_the_baseline_and_persists_it(monkeypatch):
    import bot
    import database

    saved = {}

    monkeypatch.setattr(
        database, "get_user",
        lambda chat_id, force_fresh=False: {"chat_id": chat_id, "settings": {"paused": True,
                                                "paused_reason": "daily_loss_limit",
                                                "daily_state": {"date": bot._session_date(),
                                                                "start_balance": 7_000.0,
                                                                "target_hit": True}}},
    )
    monkeypatch.setattr(database, "get_daily_resolved_pnl", lambda *a, **k: -350.0)
    monkeypatch.setattr(database, "update_settings", lambda cid, s: saved.update(s))
    monkeypatch.setattr(database, "invalidate_user_cache", lambda cid=None: None)

    risk = RiskManager()
    risk.current_free_cash = 6_650.0
    risk.paused = True
    risk.daily_realized_pnl = -350.0
    risk.peak_balance = 7_000.0
    monkeypatch.setitem(bot._user_risks, "c", risk)

    executor._trade_cooldown[("c", "m1", "TAKER")] = 1e12
    executor._trade_cooldown[("other", "m1", "TAKER")] = 1e12

    summary = bot.reset_session_restrictions("c", "manual_resume")

    assert summary["booked_pnl"] == -350.0
    assert summary["equity"] == 6_650.0
    assert summary["cleared_cooldowns"] == 1

    assert saved["paused"] is False
    assert "paused_reason" not in saved
    day = saved["daily_state"]
    assert day["pnl_baseline"] == -350.0
    assert day["start_balance"] == 6_650.0
    assert day["target_hit"] is False

    assert risk.paused is False
    assert risk.peak_balance == 6_650.0
    assert risk.daily_realized_pnl == 0.0
    assert ("c", "m1", "TAKER") not in executor._trade_cooldown
    assert ("other", "m1", "TAKER") in executor._trade_cooldown, \
        "another account's cooldowns are not ours to clear"


def test_resume_lifts_a_persisted_learner_suspension(monkeypatch):
    """A stored suspension removes strategies from scope entirely.

    Nothing in the current codebase writes the key, but it is read from
    whatever an earlier release left in the user's settings -- and it makes
    `_evaluate_single_user` drop the strategy, so the account trades nothing
    and no command clears it. An explicit resume must.
    """
    import bot
    import database

    saved = {}

    monkeypatch.setattr(
        database, "get_user",
        lambda chat_id, force_fresh=False: {"chat_id": chat_id, "settings": {
            "paused": True,
            "learned": {"suspended_strategies": ["TAKER"], "size_multipliers": {"TAKER": 0.5}},
        }},
    )
    monkeypatch.setattr(database, "get_daily_resolved_pnl", lambda *a, **k: 0.0)
    monkeypatch.setattr(database, "update_settings", lambda cid, s: saved.update(s))
    monkeypatch.setattr(database, "invalidate_user_cache", lambda cid=None: None)

    summary = bot.reset_session_restrictions("c", "manual_resume")

    assert summary["cleared_suspensions"] == ["TAKER"]
    assert "suspended_strategies" not in saved["learned"]
    # Evidence is not a restriction: the size multiplier survives the resume.
    assert saved["learned"]["size_multipliers"] == {"TAKER": 0.5}


def test_a_resumed_account_cannot_be_re_paused_by_the_same_loss():
    import bot
    import config as cfg

    start = 6_650.0
    limit = start * cfg.DEFAULT_DAILY_LOSS_LIMIT_PCT / 100.0
    day = {"date": bot._session_date(), "start_balance": start,
           "target_hit": False, "pnl_baseline": -350.0}

    # The loss stop, the way `_user_loop` evaluates it.
    assert bot._session_pnl_for_day(-350.0, day) > -limit
    # And the daily target, which set `risk.target_hit` and blocked evaluation.
    assert bot._session_pnl_for_day(-350.0, day) < bot._daily_target(
        {"daily_multiplier": 3}, start
    )


# ── 5. The structural take is reachable, and the set is balanced ──────────────

def _locked_book_market(fee_rate=0.02):
    return {
        "event_id": "e1", "market_id": "m1", "asset": "BTC", "timeframe": "15min",
        "title": "BTC above 100k", "threshold": 100_000.0,
        "yes_id": "yes", "no_id": "no", "yes_price": 0.55, "no_price": 0.37,
        "fee_rate": fee_rate, "secs_to_close": 600, "status": "open",
        "engine": "CLOB", "minimum_order_amount": 100.0,
    }


def _locked_books():
    # ask_yes + ask_no = 0.92 before fees: an 8-cent lock, well past the 1.5%
    # floor, and each leg clears the 6% EV margin on its own -- which is the
    # test that matters for a partial fill (0.60 vs 0.556 = +8.0%, 0.40 vs
    # 0.3747 = +6.8%). Note the NO leg's model probability, 0.40, is below the
    # directional conviction floor of 0.55: that is exactly the point.
    return {
        "yes": {"bids": [{"price": 0.54, "quantity": 800}],
                "asks": [{"price": 0.55, "quantity": 800}]},
        "no": {"bids": [{"price": 0.36, "quantity": 800}],
               "asks": [{"price": 0.37, "quantity": 800}]},
    }


def test_a_complete_set_is_signalled_and_carries_no_directional_floor():
    """The structural take was mathematically unreachable before this.

    `_leg_ev` demanded `model_prob >= TAKER_MIN_MODEL_PROB` from *both* legs,
    and `p_yes + p_no = 1.00`, so no book could ever satisfy it.
    """
    signal = TakerStrategy()._evaluate_complete_set(
        _locked_book_market(), {"chat_id": "c", "mode": "balanced"},
        "BTC", 0.55, 0.37, 0.60, 0.40, 0.02, 0.06, _locked_books(),
    )

    assert signal is not None, (
        "a 7-cent lock with both legs +EV must be a signal; the directional "
        "conviction floor cannot be required of complementary legs"
    )
    assert signal.outcome == "BOTH"
    assert len(signal.legs) == 2
    assert signal.mode_floor == 0.0
    # 0.40 on the NO leg is below TAKER_MIN_MODEL_PROB -- and that is fine:
    # the leg is bought at 0.38 (fee-inclusive 0.384), not at even money.
    assert min(signal.legs[1].fair_value, signal.legs[1].fair_value) > 0


def test_a_pair_leg_still_has_to_be_worth_owning_alone():
    """The orphan-leg rule the docstring promises: EV, not conviction."""
    taker = TakerStrategy()

    # The NO leg is priced through its own fair value, so a partial fill would
    # leave a bad position. The pair is still locked, and is still refused.
    signal = taker._evaluate_complete_set(
        _locked_book_market(), {"chat_id": "c", "mode": "balanced"},
        "BTC", 0.55, 0.45, 0.60, 0.40, 0.02, 0.06, {
            "yes": {"bids": [{"price": 0.54, "quantity": 800}],
                    "asks": [{"price": 0.55, "quantity": 800}]},
            "no": {"bids": [{"price": 0.44, "quantity": 800}],
                   "asks": [{"price": 0.45, "quantity": 800}]},
        },
    )
    assert signal is None, "a leg that loses money on its own is not a pair leg"


def test_the_directional_leg_still_requires_conviction():
    """The floor still binds where it is meaningful."""
    taker = TakerStrategy()

    # 0.52 model against a 0.40 ask is a +30% EV trade, and still not a trade:
    # the directional leg must not buy a side the model calls a coin flip.
    net_ev, _, fail = taker._leg_ev(0.52, 0.40, 0.02, 0.06)
    assert fail == "model_prob_below_floor"
    # The same numbers are a legitimate pair leg.
    _, _, fail_pair = taker._leg_ev(0.52, 0.40, 0.02, 0.06, require_conviction=False)
    assert fail_pair is None


def test_balanced_pair_amounts_buy_the_same_number_of_shares():
    from strategies.base import QuoteLeg

    legs = [QuoteLeg("YES", "yes", 0.46, 0.02, 0.55),
            QuoteLeg("NO", "no", 0.50, 0.02, 0.45)]
    amounts = executor._balanced_pair_amounts(legs, 200.0, min_order=100.0)

    assert sum(amounts) == pytest.approx(200.0 + 200.0 * 0.46 / 0.50, abs=0.01)
    # Same share count on both legs: the pair lock covers the whole position.
    shares_yes = amounts[0] / (0.46 * config.CURRENCY_BASE_MULTIPLIER)
    shares_no = amounts[1] / (0.50 * config.CURRENCY_BASE_MULTIPLIER)
    assert shares_yes == pytest.approx(shares_no, rel=1e-6)


def test_balanced_pair_amounts_account_for_the_taker_fee():
    from strategies.base import QuoteLeg

    # A taker's fee is taken out of the shares received, so equal *naira*
    # stakes buy fewer net shares on the more expensive leg. Apportioning by
    # the fee-inclusive price is what makes the net quantities equal.
    legs = [QuoteLeg("YES", "yes", 0.40, 0.02, 0.52),
            QuoteLeg("NO", "no", 0.55, 0.02, 0.48)]
    amounts = executor._balanced_pair_amounts(
        legs, 150.0, min_order=100.0, fee_rate=0.10,
    )
    from strategies.book import effective_buy_price

    shares = [
        amount / (effective_buy_price(leg.price, 0.10) * config.CURRENCY_BASE_MULTIPLIER)
        for leg, amount in zip(legs, amounts)
    ]
    assert shares[0] == pytest.approx(shares[1], rel=1e-9)


def test_balanced_pair_amounts_fail_closed_when_it_cannot_clear_minimums():
    from strategies.base import QuoteLeg

    # ₦60 per leg cannot buy both sides above a ₦100 order minimum: the pair
    # is not sent at all rather than sent with one leg under the exchange's
    # minimum (which the exchange would reject after the other leg filled).
    legs = [QuoteLeg("YES", "yes", 0.46, 0.02, 0.55),
            QuoteLeg("NO", "no", 0.50, 0.02, 0.45)]
    assert executor._balanced_pair_amounts(legs, 60.0, min_order=100.0) is None
    # An exotic book where the cheap leg cannot reach the minimum inside the
    # pair's two-leg budget is refused too.
    wide = [QuoteLeg("YES", "yes", 0.90, 0.02, 0.95),
            QuoteLeg("NO", "no", 0.05, 0.02, 0.05)]
    assert executor._balanced_pair_amounts(wide, 100.0, min_order=100.0) is None


def test_a_buy_fill_costs_its_fee_not_just_its_shares():
    """`quantity` is net of the fee, so `shares * price` is not the debit."""
    # Documented CLOB BUY fixture scaled to NGN: 150 net shares at 0.65 on a
    # 10% fee. The wallet pays 10,000: 9,750 of shares plus the 250 fee.
    order = {"status": "filled", "quantity": 150.0, "avgFillPrice": 0.65, "fee": 250.0}
    assert executor._order_actual_cost(order, 150.0, 0.65) == pytest.approx(10_000.0)
    # The exchange's own total wins when it is present.
    assert executor._order_actual_cost(
        {**order, "totalCost": 10_050.0}, 150.0, 0.65
    ) == pytest.approx(10_050.0)
    # And a fee-free maker fill is just the shares.
    assert executor._order_actual_cost(
        {"status": "filled", "quantity": 200.0, "avgFillPrice": 0.46, "fee": 0.0},
        200.0, 0.46,
    ) == pytest.approx(9_200.0)
