"""
MAKER — get paid for being the liquidity, twice.

Every number below comes from docs.bayse.markets and is load-bearing:

* **Makers pay no fee on CLOB.** Takers pay ``feeRate * max(1-P, 0.5)`` of
  notional; a resting order that is filled pays nothing at all. A maker fill
  at price ``b`` against a fair value ``fv`` is therefore worth ``fv - b``
  with no fee drag whatsoever.
* **Liquidity rewards are sampled every minute** and score resting orders by
  tightness to the midpoint. Single-sided quoting is divided by a penalty
  factor (~3x) and earns *zero* above 0.90. Two-sided quoting earns the full
  score.
* **Maker rebates** pay a share of the taker fees to whoever was resting.
* **A complete set (one YES + one NO) always settles to exactly one unit of
  currency**, because exactly one outcome resolves true. Combined with
  ``/burn``, which converts a set back to cash at 1.00, a set bought for less
  than 1.00 is a realised profit needing no forecast at all.

The quote
---------
Rest a bid on YES *and* a bid on NO, each independently priced so that being
filled there is good on its own terms::

    bid_yes <= fv_yes - edge
    bid_no  <= fv_no  - edge

Because a coherent model has ``fv_yes + fv_no == 1``, those two independent
conditions imply::

    bid_yes + bid_no <= 1 - 2*edge

...which is exactly the condition for a risk-free complete set. **The pair
lock is not a separate bet layered on top; it is a consequence of each leg
being priced honestly.** That is the whole design.

Which means the two ways a quote can resolve are both good:

* **Both legs fill** -> we hold a complete set bought below 1.00. Burn it.
  Profit is locked, direction-independent, fee-free.
* **One leg fills** -> we own a position at a price below its fair value.
  Positive expected value, and the other leg's resting bid is a standing offer
  to convert it into the risk-free case.

The second property is what makes this strictly better than the old
single-leg directional maker, and it is why a best-effort (non-atomic) batch
placement is safe here: a partial fill cannot leave us holding something we
only wanted as part of a pair.

Inventory skew
--------------
If one leg has already filled we are long that side. The correct skew is to
*raise* the opposite bid and *lower* the same-side bid: raising the opposite
bid is an offer to complete the set and convert an open directional position
into a locked profit. Quoting more of what we are already long is how makers
die.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Optional

import config
from strategies.base import QuoteLeg, TradeSignal, BaseStrategy
from strategies import book as booklib
from strategies.model import fair_value_pair
from strategies.utils import note_reject

log = logging.getLogger("strat.maker")

# Model prices are clamped to [0.005, 0.995]; a leg priced there is a
# statement we cannot back with capital, so we simply do not quote it.
MIN_QUOTABLE_FV = 0.02
MAX_QUOTABLE_FV = 0.98


class MakerStrategy(BaseStrategy):
    """Two-sided passive quoting, with a single-leg fallback."""

    def __init__(self):
        super().__init__("MAKER")
        # market_id -> {"legs": {outcome: {...}}, "placed_at", "spot", "fv"}
        self.open_quotes: dict[str, dict] = {}

    # ── Pure pricing core ────────────────────────────────────────────────────

    @staticmethod
    def leg_edge(fair_value: float, bid: float) -> float:
        """Expected profit per share on a maker fill. Makers pay no fee."""
        return fair_value - bid

    @staticmethod
    def min_leg_edge(price: float) -> float:
        """Required edge, growing with the price we are paying.

        Two effects, both real. First, a fill at ``b`` that resolves against
        loses ``b``, so the capital at risk scales with price. Second, the
        model's calibration is least trustworthy in the tails, and the tail
        is where a high price lives. Charging a linearly increasing edge is
        the cheap, honest way to price both without pretending to know the
        calibration curve.
        """
        base = config.MAKER_MIN_LEG_EDGE
        excess = max(0.0, float(price) - 0.50)
        return base + config.MAKER_EDGE_PRICE_COEF * excess

    def _price_leg(
        self,
        *,
        fair_value: float,
        book: dict | None,
        skew: float = 0.0,
    ) -> tuple[Optional[float], str, str]:
        """Price one post-only bid. ``(price, code, detail)``.

        The value judgement and the book mechanics are kept apart on purpose.
        This method decides *what the side is worth to us* and turns that into
        a hard ceiling; :func:`strategies.book.passive_bid_price` then decides
        where an order can actually rest. The executor re-runs that same
        function against a fresher book, so the price that gets sent is the
        price both halves of the system independently agree on.

        The ceiling has three parts: the model's value less the required edge,
        less any inventory skew, and never above the hard price ceiling (which
        exists so a broken book cannot make us post an absurd number).
        """
        if fair_value < MIN_QUOTABLE_FV or fair_value > MAX_QUOTABLE_FV:
            return None, "leg_fv_not_quotable", f"fv={fair_value:.3f} outside quotable band"

        required = self.min_leg_edge(max(fair_value - config.MAKER_MIN_LEG_EDGE, 0.0))
        ceiling = min(fair_value - required - skew, float(config.MAKER_MAX_LEG_BID))

        price, code, detail = booklib.passive_bid_price(
            book or {},
            ceiling,
            floor=float(config.MAKER_MIN_LEG_BID),
        )
        if price is None:
            return None, f"leg_{code}", f"{detail} (fv={fair_value:.3f} ceiling={ceiling:.3f})"

        # The check must use the same requirement the ceiling used, skew
        # included. It did not, which made half the skew inert: a leg we had
        # deliberately skewed toward — the one whose fill completes a set and
        # turns an open position into a locked profit — was then rejected by
        # the very rule the skew existed to relax.
        #
        # A completing leg is genuinely worth a thinner edge than a fresh one,
        # so the skew may reduce the requirement, but never below half the
        # base: the leg still has to be worth owning on its own.
        floor_edge = max(0.5 * config.MAKER_MIN_LEG_EDGE, required + min(0.0, skew))
        edge = self.leg_edge(fair_value, price)
        if edge < floor_edge:
            return None, "leg_edge_below_floor", (
                f"edge={edge:+.3f} < {floor_edge:+.3f} "
                f"(fv={fair_value:.3f} bid={price:.3f})"
            )
        return price, "", f"bid {price:.3f} edge={edge:+.3f} fv={fair_value:.3f}"

    def price_pair(
        self,
        *,
        fv_yes: float,
        fv_no: float,
        book_yes: dict | None,
        book_no: dict | None,
        inventory_skew: float = 0.0,
    ) -> tuple[dict, dict]:
        """Price both legs and enforce the pair constraint.

        ``inventory_skew > 0`` means we are already long YES: it pushes the
        YES bid down and the NO bid up, because filling NO completes a set and
        turns the open position into a locked profit.

        Returns ``(result, detail)`` where ``result`` carries either both
        prices or a skip code.
        """
        # Positive skew (long YES) -> pay less for YES, more for NO.
        skew_ticks = max(-config.MAKER_MAX_SKEW_TICKS,
                         min(config.MAKER_MAX_SKEW_TICKS, inventory_skew))
        skew = skew_ticks * config.MAKER_TICK

        bid_yes, code_y, detail_y = self._price_leg(
            fair_value=fv_yes, book=book_yes, skew=skew)
        bid_no, code_n, detail_n = self._price_leg(
            fair_value=fv_no, book=book_no, skew=-skew)

        if bid_yes is None and bid_no is None:
            return {"skip": code_y if code_y else code_n,
                    "detail": f"YES[{detail_y}] NO[{detail_n}]"}, {}
        if bid_yes is None or bid_no is None:
            # Fall through to the single-leg path; the caller decides whether
            # a one-sided quote is acceptable for this market.
            missing = "YES" if bid_yes is None else "NO"
            return {"skip": f"pair_leg_unpriceable_{missing}",
                    "detail": f"YES[{detail_y}] NO[{detail_n}]"}, {}

        # The pair lock. Independent leg pricing implies it (see module
        # docstring) but tick rounding and the price ceiling can nudge the
        # sum, so it is asserted here rather than assumed. Shave the weaker
        # leg -- the one contributing less edge -- until it holds.
        pair_edge = 1.0 - (bid_yes + bid_no)
        required_pair = config.MAKER_PAIR_MIN_EDGE
        while pair_edge < required_pair:
            edge_y = self.leg_edge(fv_yes, bid_yes)
            edge_n = self.leg_edge(fv_no, bid_no)
            if edge_y <= edge_n:
                bid_yes = booklib.tick_down(bid_yes - config.MAKER_TICK)
            else:
                bid_no = booklib.tick_down(bid_no - config.MAKER_TICK)
            if bid_yes < config.MAKER_MIN_LEG_BID or bid_no < config.MAKER_MIN_LEG_BID:
                return {"skip": "pair_cannot_lock",
                        "detail": (f"pair edge {pair_edge:+.3f} < {required_pair:+.3f} and "
                                   f"a leg hit the {config.MAKER_MIN_LEG_BID:.2f} floor "
                                   f"(yes={bid_yes:.3f} no={bid_no:.3f})")}, {}
            new_edge = 1.0 - (bid_yes + bid_no)
            if new_edge <= pair_edge:
                return {"skip": "pair_cannot_lock",
                        "detail": f"pair edge stuck at {pair_edge:+.3f}"}, {}
            pair_edge = new_edge

        return {
            "yes_bid": bid_yes,
            "no_bid": bid_no,
            "pair_edge": pair_edge,
        }, {
            "yes": detail_y,
            "no": detail_n,
        }

    # ── Strategy entry point ─────────────────────────────────────────────────

    async def evaluate(
        self,
        market: dict,
        learned: dict,
        state,
        spot_price: float = None,
        books: dict | None = None,
    ) -> Optional[TradeSignal]:
        asset = market.get("asset", "?")
        market_id = market.get("market_id", "")
        learned = learned or {}
        secs_to_close = float(market.get("secs_to_close") or 0.0)
        engine = str(market.get("engine") or "AMM").upper()

        # Passive orders only exist on a CLOB. LIMIT/GTC against an AMM is not
        # a thing, and sending it was a major source of zero execution.
        if engine != "CLOB":
            note_reject(learned, "MAKER", "engine_not_clob", str(engine))
            return None

        if secs_to_close <= 0:
            note_reject(learned, "MAKER", "market_closed", f"secs={secs_to_close:.0f}")
            return None
        if secs_to_close < config.MAKER_MIN_SECS_TO_CLOSE:
            note_reject(learned, "MAKER", "too_close_to_settle",
                        f"secs={secs_to_close:.0f} < {config.MAKER_MIN_SECS_TO_CLOSE}")
            return None
        if secs_to_close > config.MAKER_MAX_SECS_TO_CLOSE:
            note_reject(learned, "MAKER", "too_far_from_settle",
                        f"secs={secs_to_close:.0f} > {config.MAKER_MAX_SECS_TO_CLOSE}")
            return None

        books = books or {}
        book_yes = books.get(market.get("yes_id") or "")
        book_no = books.get(market.get("no_id") or "")

        if not booklib.is_usable(book_yes) and not booklib.is_usable(book_no):
            note_reject(learned, "MAKER", "book_unavailable", "neither side readable")
            return None
        for label, ob in (("YES", book_yes), ("NO", book_no)):
            if ob and booklib.book_is_stale(ob):
                note_reject(learned, "MAKER", "book_stale", f"{label} side")
                return None

        fv_pair = fair_value_pair(asset, market, state, spot_price)
        if fv_pair is None:
            note_reject(learned, "MAKER", "no_fair_value",
                        f"spot={spot_price} threshold={market.get('threshold')}")
            return None
        fv_yes, fv_no = fv_pair

        inventory = self.inventory_skew(market_id)
        result, detail = self.price_pair(
            fv_yes=fv_yes, fv_no=fv_no,
            book_yes=book_yes, book_no=book_no,
            inventory_skew=inventory,
        )

        if result.get("skip"):
            code = result["skip"]
            note_reject(learned, "MAKER", code, result.get("detail", "")[:200])
            log.debug(f"MAKER SKIP {asset} — {code}: {result.get('detail', '')}")
            # A pair we cannot price may still be worth quoting one side of,
            # but only when the single-leg path is enabled for this account.
            if not config.MAKER_ALLOW_SINGLE_LEG:
                return None
            return self._single_leg(
                market, learned, asset, fv_yes, fv_no,
                book_yes, book_no, inventory,
            )

        bid_yes = result["yes_bid"]
        bid_no = result["no_bid"]
        pair_edge = result["pair_edge"]
        size = config.MAKER_LEG_SIZE_PCT

        log.info(
            f"MAKER PAIR {asset} | fv={fv_yes:.3f}/{fv_no:.3f} "
            f"bid {bid_yes:.3f}/{bid_no:.3f} pair_edge={pair_edge:+.3f} "
            f"skew={inventory:+.0f} secs={secs_to_close:.0f}"
        )

        return TradeSignal(
            strategy="MAKER",
            event_id=market["event_id"],
            market_id=market_id,
            asset=asset,
            timeframe=market.get("timeframe", ""),
            outcome="BOTH",
            outcome_id=market.get("yes_id", ""),
            certainty=min(0.95, max(fv_yes, fv_no)),
            win_prob=max(fv_yes, fv_no),
            market_price=max(bid_yes, bid_no),
            size_pct=size,
            reason=(
                f"MAKER PAIR bid {bid_yes:.3f}/{bid_no:.3f} "
                f"lock={pair_edge:+.3f} fv={fv_yes:.3f}/{fv_no:.3f}"
            ),
            title=market.get("title", ""),
            edge_at_entry=pair_edge,
            legs=[
                QuoteLeg("YES", market.get("yes_id", ""), bid_yes, size, fv_yes),
                QuoteLeg("NO", market.get("no_id", ""), bid_no, size, fv_no),
            ],
        )

    def _single_leg(
        self, market, learned, asset, fv_yes, fv_no,
        book_yes, book_no, inventory,
    ) -> Optional[TradeSignal]:
        """Quote one side when the other cannot be priced.

        Strictly worse than the pair -- no lock, full directional risk, one
        third of the liquidity-reward score -- so it is behind a flag and it
        reports itself honestly in the reason string.
        """
        if inventory > 0:
            # Already long YES; only NO can improve the book.
            options = [("NO", fv_no, book_no, -inventory * config.MAKER_TICK)]
        elif inventory < 0:
            options = [("YES", fv_yes, book_yes, inventory * config.MAKER_TICK)]
        else:
            options = [
                ("YES", fv_yes, book_yes, 0.0),
                ("NO", fv_no, book_no, 0.0),
            ]

        best = None
        for outcome, fv, ob, skew in options:
            if fv < MIN_QUOTABLE_FV or fv > MAX_QUOTABLE_FV:
                continue
            price, code, detail = self._price_leg(fair_value=fv, book=ob, skew=skew)
            if price is None:
                note_reject(learned, "MAKER", f"single_leg_{code}", f"{outcome}: {detail}")
                continue
            edge = self.leg_edge(fv, price)
            if best is None or edge > best[0]:
                best = (edge, outcome, fv, price, market.get(
                    "yes_id" if outcome == "YES" else "no_id", ""))

        if best is None:
            return None
        edge, outcome, fv, price, outcome_id = best
        size = config.MAKER_LEG_SIZE_PCT
        log.info(
            f"MAKER SINGLE {asset} | {outcome} fv={fv:.3f} bid={price:.3f} "
            f"edge={edge:+.3f} (one-sided: no pair lock)"
        )
        return TradeSignal(
            strategy="MAKER",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=market.get("timeframe", ""),
            outcome=outcome,
            outcome_id=outcome_id,
            certainty=min(0.95, fv),
            win_prob=fv,
            market_price=price,
            size_pct=size,
            reason=f"MAKER {outcome} single-leg bid={price:.3f} edge={edge:+.3f} (no pair lock)",
            title=market.get("title", ""),
            edge_at_entry=edge,
            legs=[QuoteLeg(outcome, outcome_id, price, size, fv)],
        )

    # ── Quote lifecycle ──────────────────────────────────────────────────────

    def inventory_skew(self, market_id: str) -> float:
        """Ticks of skew: positive means we are already long YES.

        Driven by fills on this market, so it survives a restart only as long
        as the quote does -- which is correct. Inventory is a property of what
        we hold, and anything longer-lived belongs to the risk book.
        """
        info = self.open_quotes.get(market_id)
        if not info:
            return 0.0
        return float(info.get("inventory", 0.0))

    def track_quote(self, market_id: str, legs: list[dict], spot: float,
                    fv_yes: float = 0.0) -> None:
        """Record a placed quote so it can be requoted or cancelled."""
        self.open_quotes[market_id] = {
            "legs": {leg["outcome"]: leg for leg in legs},
            "placed_at": time.time(),
            "spot": float(spot or 0.0),
            "fv_yes": float(fv_yes or 0.0),
            "inventory": self.open_quotes.get(market_id, {}).get("inventory", 0.0),
        }

    def record_fill(self, market_id: str, outcome: str, shares: float) -> None:
        """A leg filled: skew the next quote toward completing the set."""
        info = self.open_quotes.setdefault(market_id, {
            "legs": {}, "placed_at": time.time(), "spot": 0.0,
            "fv_yes": 0.0, "inventory": 0.0,
        })
        signed = float(shares) if str(outcome).upper() == "YES" else -float(shares)
        info["inventory"] = float(info.get("inventory", 0.0)) + signed
        log.info(
            f"MAKER fill {market_id} {outcome} {shares:.2f}sh → "
            f"inventory {info['inventory']:+.2f}"
        )

    def should_requote(self, market_id: str, spot: float = None) -> bool:
        """True when the quote is stale relative to the oracle or the clock."""
        info = self.open_quotes.get(market_id)
        if not info:
            return False
        if spot and info.get("spot"):
            if abs(spot - info["spot"]) / info["spot"] > config.MAKER_REQUOTE_THRESHOLD:
                return True
        if time.time() - info.get("placed_at", 0) > config.MAKER_QUOTE_MAX_AGE_SEC:
            return True
        return False

    def is_expired(self, market_id: str) -> bool:
        info = self.open_quotes.get(market_id)
        if not info:
            return False
        return time.time() - info.get("placed_at", 0) > config.MAKER_ORDER_TIMEOUT

    def order_ids(self, market_id: str) -> list[str]:
        info = self.open_quotes.get(market_id)
        if not info:
            return []
        return [leg.get("order_id") for leg in info.get("legs", {}).values()
                if leg.get("order_id")]

    def drop(self, market_id: str) -> None:
        self.open_quotes.pop(market_id, None)

    async def cancel_all(self, client, market_id: str = None) -> int:
        """Cancel both legs of a quote together.

        Cancelling one leg of a two-sided quote and leaving the other resting
        is how a market maker acquires an unintended position: the surviving
        leg is now a directional order nobody is hedging.
        """
        targets = (
            {market_id: self.open_quotes[market_id]}
            if market_id and market_id in self.open_quotes
            else dict(self.open_quotes)
        )
        cancelled = 0
        for mid, info in list(targets.items()):
            ids = [leg.get("order_id") for leg in info.get("legs", {}).values()
                   if leg.get("order_id")]
            for order_id in ids:
                try:
                    await client.cancel_order(order_id)
                    cancelled += 1
                except Exception as exc:
                    log.warning(f"MAKER cancel failed {order_id} on {mid}: {exc}")
            if ids:
                log.info(f"MAKER cancelled {len(ids)} leg(s) on {mid}")
            # Keep inventory: a leg may already have filled, and the next
            # quote must still skew toward completing that set.
            self.open_quotes.pop(mid, None)
        return cancelled


# Singleton used by the executor and the main loop.
maker_strategy = MakerStrategy()
