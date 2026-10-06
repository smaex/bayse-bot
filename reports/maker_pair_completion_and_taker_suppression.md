# MAKER pairs could never be finished, and the taker was being suppressed

**Reports addressed (operator, 2026-10-06):**

1. "For the bidirectional trades on MAKER, there seems to be a bug that makes it
   impossible to really know when to enter both legs without the system
   cancelling the trade."
2. "TAKER hasn't fired too."
3. "I'm just getting notifications of filled trades, no notification of when the
   trade is being executed."

**Method:** each mechanism below is reproduced by this repository's own code,
with the sequence of live calls (risk book → orchestrator → executor → quote
lifecycle). The regression suite `tests/test_maker_pair_completion.py` is run
against both trees: **14 of its 20 tests fail on the pre-fix code**. The 6 that
pass are the controls -- the three "withdraw a completion leg that no longer
deserves to rest" cases (which the old code got right for the wrong reason: it
cancelled everything), "a second maker market on the same asset is still
blocked", "a fresh locked pair still beats a taker", and "a quote that only
re-bids a held side is refused". That split is what makes the rest regressions
rather than restatements of the new behaviour.

---

## 1. A half-filled pair was cancelled and could never be re-quoted

A two-sided MAKER quote is one decision made of two orders. When the first order
fills, the second stops being "a quote" — it is the order that completes a set,
and one fill away from a locked, direction-free profit is the entire reason the
strategy exists. Three defects treated it as an ordinary quote instead.

**1a. The standing-quote clock withdrew it.**
`bot._manage_unfilled_maker_orders` cancels both unfilled legs when the quote is
older than `MAKER_ORDER_TIMEOUT` (60s), when the oracle has moved more than
`MAKER_REQUOTE_THRESHOLD` (0.10%), or within `MAKER_MIN_SECS_TO_CLOSE` (45s) of
settlement. `bot._exit_decision` separately returns `CANCEL_RESTING` for any
unfilled maker position below `MAKER_LATE_CANCEL_SECS` (180s). None of these
knew that the sibling had filled, so the pair was pulled apart at the exact
moment it had become worth completing: the filled leg stayed in the book as a
naked directional bet, and the exit policy eventually stopped it out. That is
the account bleed in the report.

**1b. The market was then blocked forever.**
Both `risk.already_in` and the executor's `maker_quote_already_resting` guard
counted *any* tracked MAKER leg on the market as "a quote already resting" —
filled or not. Once the completing order was gone, no new MAKER quote on that
market could ever be sent, so the set was unfinishable by construction.

**1c. The inventory skew was erased with the quote.**
`record_fill` stored the skew as `open_quotes[market_id]["inventory"]`, and every
withdrawal path (`_withdraw_resting_quote`, the cancel branch, `_resolve_unfilled_position`)
popped the whole quote record. The next quote therefore priced as if we held
nothing, re-bid the same side, and turned "one fill from a locked set" into a
second directional bet.

**Fix.**

* `MakerStrategy` now keeps a fill ledger (`self.inventory`) that outlives the
  quote, and `inventory_skew(market_id, positions)` derives the skew from the
  risk book when the live loop passes it — correct across a restart.
* `bot._completion_leg_verdict` re-judges the surviving leg on every pass
  against a **fresh** fair value: it keeps resting while (a) it still nests
  inside `1 − MAKER_PAIR_MIN_EDGE` against the price the sibling actually filled
  at, and (b) the bid still clears the same edge rule that priced it, skew
  included (never below half the base requirement). With no fresh oracle or no
  market data it holds rather than cancels, because the alternative is
  destroying the one order that stands between an open position and a locked
  profit.
* `_exit_decision` gained `completion_leg`, which exempts exactly that order
  from `CANCEL_RESTING`.
* The executor's guard now blocks only *resting* legs and requires a re-quote to
  contain the side we do not hold; `already_in` likewise treats a filled MAKER
  leg as a position, scoped to the market being completed (the "one maker
  position per asset" rule still blocks every other market).

## 2. TAKER was suppressed by resting maker orders

Two mechanisms, both of which the fixed tree removes:

* `risk.already_in` refused a TAKER entry on the opposite side of any tracked
  MAKER leg — including a *resting, unfilled* quote. A two-sided quote always
  has a leg on the "other" side, so for as long as the maker was quoting (most
  of every candle) one of its two orders silently deleted taker entries.
  A quote is an order; only a fill can oppose an entry.
* A duplicate MAKER signal — one the executor would refuse with
  `maker_quote_already_resting` — was still fed into `_resolve_collision`, where
  a MAKER *pair* outranks a TAKER by construction. A signal that could only ever
  be skipped was therefore deleting real taker entries on every market the maker
  happened to be quoting. `evaluate_all` now drops such signals before collision
  resolution.

The deliberate preference itself is unchanged: a fresh locked MAKER pair still
beats a directional taker, and a TAKER complete set still beats a single-leg
maker quote. Gates were not loosened.

## 3. Executions are announced when they are sent

Only fills produced a message. `telegram_bot.notify_executing` and
`executor._notify_order_sent` now send an "Order being executed" notice *before*
the single taker order and before the first leg of a complete-set take; the
existing fill, resting and zero-fill notices follow as before. (MAKER already
announced its two-sided quote at placement via `notify_trade`'s
"resting post-only bid — not filled yet" status line, so it does not double up.)

## Deliberately not changed

* `MAKER_MAX_BID`, `SNIPE_MAX_MARKET_PRICE`, the per-leg edge floor, the EV
  margin, the fee rules and the price bands are untouched. Every fix above
  removes work being thrown away, not a requirement being relaxed.
* A completion leg that a *stale or unreadable* book says is buried is kept:
  a missing price is not evidence, and churn on bad data is worse than a bid
  that may still be first in line. The requote only fires on a readable, fresh
  book where `passive_bid_price` answers `behind_book`.

## Evidence

```
$ .venv/bin/python -m pytest -q        # fixed tree
369 passed in 45.78s                    # 347 before this change

$ git stash push bot.py executor.py risk.py strategies/__init__.py \
      strategies/maker.py telegram_bot.py
$ .venv/bin/python -m pytest tests/test_maker_pair_completion.py -q
14 failed, 6 passed                     # the 6 are the negative controls
```
