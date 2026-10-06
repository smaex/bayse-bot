"""
TAKER — cross the spread when the executable price is wrong.

A taker pays the fee and takes liquidity. That is only correct when the
mispricing is larger than the fee plus a margin for model error, so the whole
strategy is one question asked twice: *what does this cost me per net share,
and is the model's number enough higher to justify paying it?*

Two independent ways to be right

1. **Directional.** The model's fair value exceeds the fee-inclusive cost of
   the ask. We are paid for being right about the settling quantity.

2. **Structural (complete set).** YES ask + NO ask cost less than 1.00 after
   both taker fees. One of the two outcomes must resolve true, so a set always
   settles to exactly one unit of currency and the profit is locked the moment
   both legs fill. No forecast required.

The rule that makes the structural leg safe
-------------------------------------------
Every individual leg must clear the directional EV gate **on its own**.

This is the whole answer to the orphan-leg problem that got multi-leg
strategies quarantined before. Bayse batch placement is best effort, not
atomic: leg one can fill while leg two does not. If leg two were what made
leg one profitable, a partial fill is an unhedged loss. Requiring each leg to
stand alone means a partial fill leaves us holding a trade we were happy to
own anyway, and the completed pair is upside we did not need in order to be
right.

It also means a structural signal is never *worse* than a directional one: it
is a directional signal with a free option attached.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import config
from strategies.base import QuoteLeg, TradeSignal, BaseStrategy
from strategies import book as booklib
from strategies.model import distance_pct, fair_value
from strategies.utils import note_reject, probability_to_certainty

log = logging.getLogger("strat.taker")

# Minimum notional we will bother crossing for, as a fraction of the depth we
# can see. Below this the "opportunity" is one or two shares of dust and the
# round-trip cost of discovering that exceeds the edge.
MIN_VISIBLE_DEPTH_SHARES = 5.0


class TakerStrategy(BaseStrategy):
    """Fee-aware directional and complete-set liquidity taking."""

    def __init__(self):
        super().__init__("TAKER")

    # ── Gate helpers (pure, so they can be tested without a market) ──────────

    def _min_net_ev(self, mode: str, learned: dict | None = None) -> float:
        """Required return on capital per trade, by risk mode.

        Six percent in balanced mode is not arbitrary. A 15-minute crypto
        binary offers roughly one round turn per candle; at ~96 candles a day
        the difference between a 4% and a 6% required edge is the difference
        between a strategy that survives its own variance and one that does
        not, once the model's calibration error is charged against it.
        """
        base = float(learned.get("taker_min_net_ev", config.TAKER_MIN_NET_EV_DEFAULT)) \
            if isinstance(learned, dict) else config.TAKER_MIN_NET_EV_DEFAULT
        return {
            "safe": base * 1.6,
            "balanced": base,
            "aggressive": base * 0.7,
            "full_send": base * 0.5,
            "custom": base,
        }.get(str(mode or "balanced").lower(), base)

    def _min_distance(self, asset: str) -> float:
        """Minimum spot-to-strike separation before we trust the read.

        Inside this band the outcome is a coin flip and the model's number is
        mostly a restatement of noise. The per-asset calibration reflects
        baseline variance: BTC needs less separation than SOL to mean the
        same thing.
        """
        return {
            "BTC": 0.0008,
            "ETH": 0.0020,
            "SOL": 0.0025,
        }.get(str(asset).upper(), config.TAKER_MIN_DISTANCE_PCT)

    # ── The single-leg EV test ───────────────────────────────────────────────

    def _leg_ev(
        self,
        model_prob: float,
        price: float,
        fee_rate: float,
        min_net_ev: float,
        *,
        require_conviction: bool = True,
    ) -> tuple[float, float, Optional[str]]:
        """``(net_ev, effective_price, reject_code)`` for buying one side.

        ``net_ev = model_prob / effective_price - 1``: we pay ``effective_price``
        per net share and the share is worth ``model_prob`` in expectation,
        settling to 1.00 or nothing. This is the exact EV, not an
        approximation -- the fee enters through ``effective_price`` because
        Bayse takes it out of the shares we receive, not out of the cash we
        send.

        ``require_conviction`` is the directional filter: never take a side the
        model calls a coin flip (``TAKER_MIN_MODEL_PROB``). A *complete-set*
        leg must not be asked for it. The two legs' probabilities are
        complementary -- they sum to exactly 1.00 -- so requiring both to clear
        0.55 is unsatisfiable, and the structural take could therefore never
        fire at all. What a pair leg needs instead is the EV margin, which is
        what makes an orphanned leg (a partial batch fill) a trade worth owning
        on its own, plus the effective-price band, which already keeps both
        legs out of the long-shot tail.
        """
        if price is None or not math.isfinite(price) or price <= 0:
            return 0.0, 0.0, "no_executable_price"
        effective = booklib.effective_buy_price(price, fee_rate, is_maker=False)
        if not math.isfinite(effective) or effective <= 0:
            return 0.0, effective, "fee_makes_price_ineffective"
        if effective > config.TAKER_MAX_EFFECTIVE_PRICE:
            # Audit-backed ceiling. At 0.85 a win pays +17.6% while a loss
            # costs 100%, so a 93% win rate is needed to break even. Above
            # this price the fee structure, not the forecast, decides.
            return 0.0, effective, "price_above_ev_ceiling"
        if effective < config.TAKER_MIN_EFFECTIVE_PRICE:
            return 0.0, effective, "price_below_band"
        if require_conviction and model_prob < config.TAKER_MIN_MODEL_PROB:
            return 0.0, effective, "model_prob_below_floor"
        net_ev = model_prob / effective - 1.0
        if net_ev < min_net_ev:
            return net_ev, effective, "net_ev_below_margin"
        return net_ev, effective, None

    # ── Main entry point ─────────────────────────────────────────────────────

    async def evaluate(
        self,
        market: dict,
        learned: dict,
        state,
        spot_price: float = None,
        books: dict | None = None,
    ) -> Optional[TradeSignal]:
        asset = market.get("asset", "?")
        learned = learned or {}
        mode = learned.get("mode", "balanced")
        min_net_ev = self._min_net_ev(mode, learned)
        fee_rate = float(market.get("fee_rate") or 0.0)
        secs_to_close = float(market.get("secs_to_close") or 0.0)
        engine = str(market.get("engine") or "AMM").upper()

        # ── Allowed scope ───────────────────────────────────────────────────
        # TAKER_ALLOWED_ASSETS / TAKER_ALLOWED_TIMEFRAMES exist so the operator
        # can widen or narrow what the crossing leg is allowed to touch. They
        # were never read anywhere, so a scope of "BTC, ETH, SOL on 5/15-minute
        # CLOBs" was documentation rather than a rule, and the taker was free
        # to cross an AMM FX print. Enforced here, before any pricing work, and
        # reported with the same `..._not_in_allowed_scope` code the drought
        # report already treats as a configuration exclusion rather than a gate.
        if str(asset).upper() not in config.TAKER_ALLOWED_ASSETS:
            note_reject(learned, "TAKER", "asset_not_in_allowed_scope", str(asset))
            return None
        timeframe = str(market.get("timeframe") or "").upper()
        if timeframe and timeframe not in config.TAKER_ALLOWED_TIMEFRAMES:
            note_reject(learned, "TAKER", "timeframe_not_in_allowed_scope", timeframe)
            return None

        # ── Time window ──────────────────────────────────────────────────────
        # Too early in the candle the model has almost no information and the
        # book is thin; too late and settlement risk dominates any edge.
        window = config.TAKER_ENTRY_WINDOWS.get(market.get("timeframe", ""), 810)
        if secs_to_close <= 0:
            note_reject(learned, "TAKER", "market_closed", f"secs={secs_to_close:.0f}")
            return None
        if secs_to_close > window:
            note_reject(learned, "TAKER", "outside_entry_window",
                        f"secs={secs_to_close:.0f} > {window:.0f}")
            return None
        if secs_to_close < config.TAKER_MIN_SECS_TO_CLOSE:
            note_reject(learned, "TAKER", "too_close_to_settle",
                        f"secs={secs_to_close:.0f}")
            return None

        # ── Model ────────────────────────────────────────────────────────────
        p_yes = fair_value(asset, market, state, spot_price)
        if p_yes is None:
            note_reject(learned, "TAKER", "no_fair_value",
                        f"spot={spot_price} threshold={market.get('threshold')}")
            return None
        p_no = 1.0 - p_yes

        # ── Executable prices ────────────────────────────────────────────────
        # A book gives us a real ask. Without one (AMM) the displayed outcome
        # price is executable, which the executor still re-verifies by quote.
        books = books or {}
        book_yes = books.get(market.get("yes_id") or "")
        book_no = books.get(market.get("no_id") or "")

        if engine == "CLOB":
            if not booklib.is_usable(book_yes) and not booklib.is_usable(book_no):
                note_reject(learned, "TAKER", "book_unavailable",
                            "neither outcome book is readable")
                return None
            ask_yes = booklib.best_ask(book_yes)
            ask_no = booklib.best_ask(book_no)
        else:
            ask_yes = market.get("yes_price")
            ask_no = market.get("no_price")

        # A book we can read but cannot date is still a book; one whose
        # timestamp proves it is old is not a price at all.
        for label, ob in (("YES", book_yes), ("NO", book_no)):
            if ob and booklib.book_is_stale(ob):
                note_reject(learned, "TAKER", "book_stale", f"{label} side")
                return None

        # ── Structural complete-set take (tried first: it needs no forecast) ──
        if engine == "CLOB" and ask_yes is not None and ask_no is not None:
            pair = self._evaluate_complete_set(
                market, learned, asset, ask_yes, ask_no, p_yes, p_no,
                fee_rate, min_net_ev, books,
            )
            if pair is not None:
                return pair

        # ── Directional take ─────────────────────────────────────────────────
        return self._evaluate_directional(
            market, learned, asset, ask_yes, ask_no, p_yes, p_no,
            fee_rate, min_net_ev, spot_price, books, state,
        )

    # ── Structural leg ───────────────────────────────────────────────────────

    def _evaluate_complete_set(
        self, market, learned, asset, ask_yes, ask_no, p_yes, p_no,
        fee_rate, min_net_ev, books,
    ) -> Optional[TradeSignal]:
        if not booklib.pair_sum_sane(ask_yes, ask_no):
            note_reject(learned, "TAKER", "pair_sum_insane",
                        f"ask_yes={ask_yes} ask_no={ask_no}")
            return None

        eff_yes = booklib.effective_buy_price(ask_yes, fee_rate, is_maker=False)
        eff_no = booklib.effective_buy_price(ask_no, fee_rate, is_maker=False)
        lock_cost = eff_yes + eff_no
        lock_edge = 1.0 - lock_cost

        if lock_edge < config.COMPLETE_SET_TAKER_MIN_EDGE:
            note_reject(
                learned, "TAKER", "complete_set_edge_below_floor",
                f"edge={lock_edge:+.4f} (needs >={config.COMPLETE_SET_TAKER_MIN_EDGE:+.4f}) "
                f"cost={lock_cost:.4f} ask_yes={ask_yes:.3f} ask_no={ask_no:.3f}",
            )
            return None

        # Both legs must independently be worth owning. See the module
        # docstring: a partial fill must leave us with a good trade, not a
        # hope. This is the gate that makes a best-effort batch safe.
        #
        # `require_conviction=False` is load-bearing, not a relaxation: the
        # directional floor demands `model_prob >= TAKER_MIN_MODEL_PROB` on
        # each leg, and these legs are complementary (`p_yes + p_no = 1.00`),
        # so asking both for 0.55 is unsatisfiable and the structural take
        # could never fire. The standalone test that matters is the EV margin
        # below, and the effective-price band inside `_leg_ev` keeps both legs
        # out of the long-shot tail.
        ev_yes, _, fail_yes = self._leg_ev(
            p_yes, ask_yes, fee_rate, min_net_ev, require_conviction=False
        )
        ev_no, _, fail_no = self._leg_ev(
            p_no, ask_no, fee_rate, min_net_ev, require_conviction=False
        )
        if fail_yes or fail_no:
            note_reject(
                learned, "TAKER", "complete_set_leg_not_standalone",
                f"YES:{fail_yes or 'ok'} (p={p_yes:.3f} @{ask_yes:.3f}) "
                f"NO:{fail_no or 'ok'} (p={p_no:.3f} @{ask_no:.3f})",
            )
            return None

        # Depth: we need both sides to actually be there.
        for label, ob in (("YES", books.get(market.get("yes_id") or "")),
                          ("NO", books.get(market.get("no_id") or ""))):
            if booklib.depth(ob, "asks") < MIN_VISIBLE_DEPTH_SHARES:
                note_reject(learned, "TAKER", "complete_set_thin_book", f"{label} asks")
                return None

        log.info(
            f"TAKER COMPLETE-SET {asset} | edge={lock_edge:+.4f} "
            f"ask_yes={ask_yes:.3f} ask_no={ask_no:.3f} cost={lock_cost:.4f} "
            f"p_yes={p_yes:.3f} p_no={p_no:.3f} secs={market.get('secs_to_close', 0):.0f}"
        )
        return TradeSignal(
            strategy="TAKER",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=market.get("timeframe", ""),
            outcome="BOTH",
            outcome_id=market.get("yes_id", ""),
            certainty=min(0.99, max(p_yes, p_no)),
            # No conviction floor of its own: a complete set is not a
            # directional bet, and the gates that admitted it -- the lock edge,
            # the per-leg EV margin and the effective-price band -- already
            # decide. Declaring the directional floor here would re-impose, in
            # the executor, the exactly-unsatisfiable "both complementary legs
            # above 0.55" requirement that `_leg_ev` was just freed from.
            mode_floor=0.0,
            min_net_ev=min_net_ev,
            win_prob=max(p_yes, p_no),
            market_price=max(ask_yes, ask_no),
            size_pct=config.TAKER_COMPLETE_SET_SIZE_PCT,
            reason=(
                f"COMPLETE_SET lock edge={lock_edge:+.4f} "
                f"(yes@{ask_yes:.3f} + no@{ask_no:.3f} = {lock_cost:.4f} < 1.00)"
            ),
            title=market.get("title", ""),
            edge_at_entry=lock_edge,
            legs=[
                QuoteLeg("YES", market.get("yes_id", ""), ask_yes,
                         config.TAKER_COMPLETE_SET_SIZE_PCT, p_yes),
                QuoteLeg("NO", market.get("no_id", ""), ask_no,
                         config.TAKER_COMPLETE_SET_SIZE_PCT, p_no),
            ],
        )

    # ── Directional leg ──────────────────────────────────────────────────────

    def _evaluate_directional(
        self, market, learned, asset, ask_yes, ask_no, p_yes, p_no,
        fee_rate, min_net_ev, spot_price, books, state=None,
    ) -> Optional[TradeSignal]:
        candidates = []
        for outcome, model_prob, ask, outcome_id in (
            ("YES", p_yes, ask_yes, market.get("yes_id", "")),
            ("NO", p_no, ask_no, market.get("no_id", "")),
        ):
            if ask is None:
                continue
            ob = books.get(outcome_id) if books else None
            if ob is not None:
                if not booklib.is_usable(ob):
                    note_reject(learned, "TAKER", "side_book_unusable", outcome)
                    continue
                if booklib.depth(ob, "asks") < MIN_VISIBLE_DEPTH_SHARES:
                    note_reject(learned, "TAKER", "side_book_too_thin", outcome)
                    continue
            net_ev, effective, fail = self._leg_ev(model_prob, ask, fee_rate, min_net_ev)
            if fail:
                note_reject(
                    learned, "TAKER", fail,
                    f"{outcome} p={model_prob:.3f} ask={ask:.3f} "
                    f"eff={effective:.3f} ev={net_ev:+.1%} (needs >={min_net_ev:.0%})",
                )
                continue
            candidates.append((net_ev, outcome, model_prob, ask, effective, outcome_id))

        if not candidates:
            return None

        # Best EV wins. Ties break toward the side with the larger model edge
        # over the *raw* price, which is the more robust of the two numbers.
        net_ev, outcome, model_prob, ask, effective, outcome_id = max(
            candidates, key=lambda c: (c[0], c[2] - c[3])
        )

        # Momentum veto. Not a requirement that momentum agree -- that blocked
        # almost every evaluation while adding nothing -- but a refusal to buy
        # into a tape moving hard the other way, which is adverse selection.
        from strategies.utils import momentum_score
        # "YES" is the up-momentum direction: the helper negates for anything
        # else, so passing "UP" here would silently invert the veto.
        mom = momentum_score(asset, "YES", state) if state is not None else 0.0
        want_up = (outcome == "YES")
        if want_up and mom < -config.TAKER_MOMENTUM_VETO:
            note_reject(learned, "TAKER", "momentum_veto",
                        f"{outcome} with mom={mom:+.4f} < -{config.TAKER_MOMENTUM_VETO}")
            return None
        if (not want_up) and mom > config.TAKER_MOMENTUM_VETO:
            note_reject(learned, "TAKER", "momentum_veto",
                        f"{outcome} with mom={mom:+.4f} > +{config.TAKER_MOMENTUM_VETO}")
            return None

        # Separation from the strike: outside the noise band.
        dist = distance_pct(spot_price, market.get("threshold"))
        if dist is not None and abs(dist) < self._min_distance(asset):
            note_reject(learned, "TAKER", "distance_below_calibration",
                        f"{abs(dist):.4%} < {self._min_distance(asset):.4%}")
            return None

        # Kelly-ish sizing from the exact EV, floored and capped in config.
        size_pct = self._size(net_ev, effective, learned)

        log.info(
            f"TAKER SIGNAL {asset} | {outcome} p={model_prob:.3f} ask={ask:.3f} "
            f"eff={effective:.3f} ev={net_ev:+.1%} dist={dist if dist is None else f'{dist:+.3%}'} "
            f"mom={mom:+.4f} size={size_pct:.1%} secs={market.get('secs_to_close', 0):.0f}"
        )
        return TradeSignal(
            strategy="TAKER",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=market.get("timeframe", ""),
            outcome=outcome,
            outcome_id=outcome_id,
            # Certainty on the system-wide scale (w = 0.50 + 0.45c), so the
            # stored number, the calibration curve and the learner all read
            # the same probability the model actually claimed. This used to
            # divide by 0.5, i.e. claim a different certainty than every other
            # strategy for the same probability.
            certainty=probability_to_certainty(model_prob),
            # The real floor for a directional take is TAKER_MIN_MODEL_PROB,
            # already enforced in `_leg_ev` on the raw probability. Expressed
            # here so the executor's floor check cannot silently become a much
            # stricter, unrelated rule: with the old shared default of 0.48 on
            # this scale, "certainty >= 0.48" meant "model probability >= 0.74"
            # and a 0.65 model against a 0.55 ask was refused as a probe,
            # after the EV gate had already approved it.
            mode_floor=probability_to_certainty(config.TAKER_MIN_MODEL_PROB),
            min_net_ev=min_net_ev,
            win_prob=model_prob,
            market_price=ask,
            size_pct=size_pct,
            reason=(
                f"TAKER {outcome} p={model_prob:.3f} vs eff={effective:.3f} "
                f"(ask {ask:.3f} + fee) ev={net_ev:+.1%}"
            ),
            title=market.get("title", ""),
            momentum_at_entry=mom,
            edge_at_entry=model_prob - effective,
        )

    def _size(self, net_ev: float, effective_price: float, learned: dict) -> float:
        """Fraction of bankroll, from EV with a hard ceiling.

        Quarter-Kelly on a binary paying ``(1 - p) / p`` with an EV we have
        already charged the fee against, then floored so a real edge is never
        sized into irrelevance and capped so a single trade can never be the
        account. The cap is the real guardrail; Kelly is the shape.
        """
        try:
            raw = net_ev / max(effective_price, 1e-6)
        except (TypeError, ValueError, ZeroDivisionError):
            raw = 0.0
        size = 0.25 * max(0.0, raw)
        return float(min(config.TAKER_MAX_SIZE_PCT, max(config.TAKER_MIN_SIZE_PCT, size)))


def market_state_for(state):
    """Tolerates being handed either a MarketState or the global module."""
    return state


# Singleton used by the orchestrator.
taker_strategy = TakerStrategy()
