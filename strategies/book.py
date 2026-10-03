"""
Order-book primitives and Bayse settlement economics.

Everything here is pure: no network, no clock reads beyond an injected ``now``,
no database, no global state. Both execution legs -- TAKER and MAKER -- price
off this one module so a fee, a rounding rule or a staleness rule can never
drift between the leg that measures an edge and the leg that captures it.

Conventions (verified against docs.bayse.markets)
------------------------------------------------
* Prices are probability units in (0, 1). One share of the winning outcome
  settles to ``1.0 * CURRENCY_BASE_MULTIPLIER`` (N100.00 in NGN).
* Taker fee: ``fee = feeRate * C * P * max(1 - P, 0.5)``. As a fraction of
  notional that is ``feeRate * max(1 - P, 0.5)``.
* CLOB BUY: the fee reduces *shares received*, so the wallet cost per net
  share is ``P / (1 - f)`` -- not ``P * (1 + f)``. Getting this backwards
  understates cost by ``f**2``; small here, catastrophic if the rate rises.
* CLOB SELL: the fee reduces *proceeds*, so net per share is ``P * (1 - f)``.
* **Makers pay no fee on CLOB.** A resting order that is filled has no fee on
  either side. This is the single largest economic fact in the whole system
  and it is why MAKER exists.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

import config

log = logging.getLogger("strategies.book")

# Prices outside this band are not executable on Bayse (documented 0.01-0.99)
# and are never a real quote: they are a broken or empty book.
MIN_VALID_PRICE = 0.01
MAX_VALID_PRICE = 0.99


# ── Level hygiene ─────────────────────────────────────────────────────────────

def level_price(level) -> Optional[float]:
    """A single book level's price, or None if the level is unusable."""
    try:
        if isinstance(level, dict):
            raw = level.get("price")
        else:
            raw = level[0]
        price = float(raw)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(price):
        return None
    if not (MIN_VALID_PRICE <= price <= MAX_VALID_PRICE):
        return None
    return price


def level_quantity(level) -> float:
    """A level's size, tolerating the shape differences across Bayse routes."""
    try:
        if isinstance(level, dict):
            raw = level.get("quantity", level.get("size"))
        else:
            raw = level[1]
        qty = float(raw)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(qty) or qty <= 0:
        return 0.0
    return qty


def clean_side(levels: Iterable) -> list[tuple[float, float]]:
    """Valid ``(price, quantity)`` pairs from one side of a book."""
    out: list[tuple[float, float]] = []
    for level in levels or []:
        price = level_price(level)
        if price is None:
            continue
        qty = level_quantity(level)
        if qty <= 0:
            continue
        out.append((price, qty))
    return out


def best_bid(book: dict | None) -> Optional[float]:
    """Highest bid, or None when the bid side is empty/unusable."""
    bids = clean_side((book or {}).get("bids"))
    return max(p for p, _ in bids) if bids else None


def best_ask(book: dict | None) -> Optional[float]:
    """Lowest ask, or None when the ask side is empty/unusable."""
    asks = clean_side((book or {}).get("asks"))
    return min(p for p, _ in asks) if asks else None


def mid(book: dict | None) -> Optional[float]:
    bid, ask = best_bid(book), best_ask(book)
    if bid is None and ask is None:
        return None
    if bid is None:
        return ask
    if ask is None:
        return bid
    return (bid + ask) / 2.0


def spread(book: dict | None) -> Optional[float]:
    bid, ask = best_bid(book), best_ask(book)
    if bid is None or ask is None:
        return None
    return ask - bid


def depth(book: dict | None, side: str = "bids") -> float:
    """Total quantity resting on one side."""
    return sum(q for _, q in clean_side((book or {}).get(side)))


def is_usable(book: dict | None) -> bool:
    """A book with at least one executable level on at least one side."""
    return bool(clean_side((book or {}).get("bids")) or clean_side((book or {}).get("asks")))


# ── Staleness ─────────────────────────────────────────────────────────────────

def book_age_sec(book: dict, *, now: float | None = None) -> Optional[float]:
    """Age of a book snapshot in seconds, or None when no timestamp is given.

    Bayse's documented level schema does not promise a timestamp, so *absence
    cannot safely be read as stale*. Callers must fail closed on ``None`` only
    if they have an independent freshness signal; otherwise they treat it as
    unknown-but-usable, which is the documented contract.
    """
    if not isinstance(book, dict):
        return None
    timestamp = next(
        (book.get(key) for key in ("timestamp", "updatedAt", "updated_at")
         if book.get(key) is not None),
        None,
    )
    if timestamp is None:
        return None
    try:
        if isinstance(timestamp, (int, float)):
            updated = float(timestamp)
        else:
            value = str(timestamp).strip()
            try:
                updated = float(value)
            except ValueError:
                updated = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ).timestamp()
        if updated > 10_000_000_000:  # milliseconds
            updated /= 1000.0
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, (now if now is not None else _now()) - updated)


def book_is_stale(
    book: dict, *, max_age: float | None = None, now: float | None = None
) -> bool:
    """True only when a supplied timestamp proves the book is too old.

    A book with no timestamp is *not* stale by this rule -- it is undated. A
    malformed timestamp is treated as stale, because a book we cannot date is
    a book we cannot trust for a price we are about to post.
    """
    if not isinstance(book, dict) or not book:
        return True
    age = book_age_sec(book, now=now)
    if age is None:
        return False
    limit = config.CLOB_MAX_BOOK_AGE_SECONDS if max_age is None else float(max_age)
    return age > limit


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


# ── Fees ──────────────────────────────────────────────────────────────────────

def taker_fee_fraction(price: float, fee_rate: float) -> float:
    """Bayse taker fee as a fraction of fill notional.

    ``fee / (C * P) = feeRate * max(1 - P, 0.5)``.
    """
    price = min(max(float(price), 0.0), 1.0)
    return float(fee_rate) * max(1.0 - price, config.FEE_FLOOR)


def maker_fee_fraction(price: float, fee_rate: float) -> float:
    """Makers pay nothing on Bayse CLOB. Kept explicit so callers cannot forget.

    It takes the same arguments as :func:`taker_fee_fraction` on purpose: a
    caller that swaps one for the other stays correct, which is not true of a
    bare ``0.0`` sprinkled through pricing code.
    """
    return 0.0


def effective_buy_price(price: float, fee_rate: float, *, is_maker: bool = False) -> float:
    """Wallet cost per *net* share for a BUY at ``price``.

    Taker: the fee eats shares, so ``cost_per_share = P / (1 - f)``.
    Maker: no fee, so the cost is the price.
    """
    price = float(price)
    if is_maker:
        return price
    f = taker_fee_fraction(price, fee_rate)
    if f >= 1.0:
        return float("inf")
    return price / (1.0 - f)


def effective_sell_proceeds(
    price: float, shares: float, fee_rate: float, *, is_maker: bool = False
) -> float:
    """Net currency received for selling ``shares`` at ``price``.

    Taker: the fee comes out of proceeds, so ``shares * P * (1 - f)``.
    Maker: no fee.
    """
    price, shares = float(price), float(shares)
    if is_maker:
        return price * shares
    return price * shares * (1.0 - taker_fee_fraction(price, fee_rate))


def payout_per_share(shares: float = 1.0) -> float:
    """Currency paid out per winning share at settlement."""
    return shares * 1.0 * config.CURRENCY_BASE_MULTIPLIER


def notional(shares: float, price: float) -> float:
    """Currency value of ``shares`` at ``price``."""
    return shares * price * config.CURRENCY_BASE_MULTIPLIER


# ── Size-aware execution price ────────────────────────────────────────────────

def walk_asks(book: dict, budget: float) -> tuple[float, float, bool]:
    """Fill a BUY of ``budget`` currency against the ask side.

    Returns ``(avg_price, shares_bought, complete)``. ``complete`` is False
    when the book ran out of liquidity before the whole budget was spent --
    the caller decides whether that is acceptable, but it must decide.
    """
    if budget <= 0:
        return 0.0, 0.0, False
    remaining, cost, shares = float(budget), 0.0, 0.0
    unit = config.CURRENCY_BASE_MULTIPLIER
    for price, qty in sorted(clean_side((book or {}).get("asks")), key=lambda lv: lv[0]):
        if remaining <= 1e-12:
            break
        level_notional = qty * price * unit
        take_notional = min(level_notional, remaining)
        take_shares = take_notional / (price * unit) if price > 0 else 0.0
        cost += take_notional
        shares += take_shares
        remaining -= take_notional
    if shares <= 0:
        return 0.0, 0.0, False
    return cost / (shares * unit), shares, remaining <= 1e-9


def walk_bids(book: dict, shares: float) -> tuple[float, float, bool]:
    """Fill a SELL of ``shares`` against the bid side.

    Returns ``(avg_price, proceeds, complete)`` where ``proceeds`` is gross
    currency before any taker fee.
    """
    if shares <= 0:
        return 0.0, 0.0, False
    remaining, proceeds, filled = float(shares), 0.0, 0.0
    unit = config.CURRENCY_BASE_MULTIPLIER
    for price, qty in sorted(clean_side((book or {}).get("bids")),
                             key=lambda lv: lv[0], reverse=True):
        if remaining <= 1e-12:
            break
        take_shares = min(qty, remaining)
        proceeds += take_shares * price * unit
        filled += take_shares
        remaining -= take_shares
    if filled <= 0:
        return 0.0, 0.0, False
    return proceeds / (filled * unit), proceeds, remaining <= 1e-9


# ── Complete-set mathematics ──────────────────────────────────────────────────

def complete_set_lock_cost(
    yes_price: float, no_price: float, fee_rate: float, *, is_maker: bool = False
) -> float:
    """Cost of one complete set (one YES share + one NO share).

    A complete set always settles to exactly one unit of currency: exactly one
    of the two outcomes resolves true. So the *payout* of a set is risk-free
    and the only question is what it cost.

    Buying both sides at ``yes_price`` and ``no_price`` therefore locks a
    profit iff this returns a value below 1.0, and the locked edge is
    ``1.0 - cost`` per set. Direction, vol and forecast error are irrelevant
    to that number -- which is the whole point.
    """
    return (
        effective_buy_price(yes_price, fee_rate, is_maker=is_maker)
        + effective_buy_price(no_price, fee_rate, is_maker=is_maker)
    )


def complete_set_lock_edge(
    yes_price: float, no_price: float, fee_rate: float, *, is_maker: bool = False
) -> float:
    """Locked profit per complete set, in probability units. Negative = loss."""
    return 1.0 - complete_set_lock_cost(yes_price, no_price, fee_rate, is_maker=is_maker)


# ── Price ladder helpers ──────────────────────────────────────────────────────

def tick_down(price: float, ticks: int = 1, tick: float | None = None) -> float:
    """Round a price down to the exchange grid, never below the minimum."""
    step = config.MAKER_TICK if tick is None else float(tick)
    stepped = math.floor(round(float(price) / step, 6)) * step
    stepped -= (ticks - 1) * step
    return round(max(MIN_VALID_PRICE, stepped), 4)


def tick_up(price: float, ticks: int = 1, tick: float | None = None) -> float:
    """Round a price up to the exchange grid, never above the maximum."""
    step = config.MAKER_TICK if tick is None else float(tick)
    stepped = math.ceil(round(float(price) / step, 6)) * step
    stepped += (ticks - 1) * step
    return round(min(MAX_VALID_PRICE, stepped), 4)


def ticks_behind(price: float, reference: float, tick: float | None = None) -> float:
    """How many ticks ``price`` sits below ``reference`` (negative = above)."""
    step = config.MAKER_TICK if tick is None else float(tick)
    if step <= 0:
        return 0.0
    return round((float(reference) - float(price)) / step, 6)


# ── Passive (post-only) pricing ───────────────────────────────────────────────

def passive_bid_price(
    book: dict,
    ceiling: float,
    *,
    floor: float = 0.0,
    tick: float | None = None,
    max_ticks_behind: int | None = None,
) -> tuple[Optional[float], str, str]:
    """Where a post-only BUY can rest, given the most we are willing to pay.

    This is pure book mechanics -- it answers "what price is both passive and
    competitive", never "is this trade worth doing". The value judgement is
    the caller's, expressed as ``ceiling``. Keeping the two apart is what lets
    the strategy and the executor agree: the executor re-runs exactly this
    function against a fresher book and either confirms the strategy's price
    or rejects the order, with no second opinion to disagree with.

    ``ceiling`` is a hard limit and is never exceeded: a risk/reward ceiling
    is not a liquidity setting.

    Returns ``(price, "", detail)`` or ``(None, code, detail)``:
      * ``no_passive_price``  — no price at or above ``floor`` stays passive;
      * ``would_cross_book``  — the only prices available cross the ask, so a
        post-only order would be rejected (or silently turn us into a taker
        paying a fee we never budgeted);
      * ``behind_book``       — a price that is passive but buried: every
        seller hits the bids above it first, so it rests until timeout and
        teaches us nothing.
    """
    step = config.MAKER_TICK if tick is None else float(tick)
    if max_ticks_behind is None:
        max_ticks_behind = config.MAKER_MAX_TICKS_BEHIND_BEST_BID
    floor = max(float(floor or 0.0), MIN_VALID_PRICE)

    bids = clean_side((book or {}).get("bids"))
    asks = clean_side((book or {}).get("asks"))
    best_bid = max(p for p, _ in bids) if bids else None
    best_ask = min(p for p, _ in asks) if asks else None

    if best_bid is None and best_ask is None:
        return None, "no_passive_price", "no readable level on either side"

    price = float(ceiling)
    if best_ask is not None:
        price = min(price, best_ask - step)
    if best_bid is not None:
        # Lead by a tick only when the spread is wide enough that leading is
        # still passive; otherwise join the queue rather than overpay for the
        # same position in it.
        desired = best_bid + step if (
            best_ask is not None and best_ask - best_bid > step + 1e-9
        ) else best_bid
        price = min(price, desired)

    # Round DOWN: rounding up would quietly pay more than the model cleared.
    price = tick_down(price, tick=step)

    book_text = (
        (f"best bid {best_bid:.3f}" if best_bid is not None else "no bids")
        + (f" / best ask {best_ask:.3f}" if best_ask is not None else " / no asks")
    )

    if price < floor - 1e-9:
        code = "would_cross_book" if (best_ask is not None and ceiling >= best_ask - 1e-9) else "no_passive_price"
        return None, code, (
            f"{book_text}: no passive price at or above {floor:.3f} "
            f"(ceiling {ceiling:.3f})"
        )
    if best_ask is not None and price >= best_ask - 1e-9:
        return None, "would_cross_book", (
            f"{book_text}: bid {price:.3f} would cross the ask"
        )
    if best_bid is not None and ticks_behind(price, best_bid, step) > max_ticks_behind:
        return None, "behind_book", (
            f"bid {price:.3f} is {ticks_behind(price, best_bid, step):.0f} tick(s) under "
            f"the {book_text} — it cannot fill before timeout"
        )
    return price, "", f"{book_text} -> bid {price:.3f}"


# ── Sanity ────────────────────────────────────────────────────────────────────

def pair_sum_sane(yes: Optional[float], no: Optional[float],
                  lo: float = 0.80, hi: float = 1.20) -> bool:
    """Guard against a book whose two outcomes are internally inconsistent.

    A valid binary's YES and NO prices straddle parity. When both legs of a
    book are readable but their sum is far from 1.0, one of the two books is
    broken or the market is not the binary we think it is -- and every
    complete-set number computed from it would be fiction.
    """
    if yes is None or no is None:
        return False
    return lo <= (yes + no) <= hi
