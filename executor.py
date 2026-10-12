"""Trade executor for fail-closed AMM and CLOB order routing."""

import asyncio
import logging
import math
import re
import time
import uuid
from datetime import datetime
from typing import Optional

import config
import database
import feeds
import scanner
import stall
import telegram_bot
import strategy
import dataclasses

from config import CURRENCY, FEE_FLOOR, MIN_PAYOUT_RATIO
from strategies import book as booklib
from risk import position_is_filled, share_quantities_match

log = logging.getLogger("executor")

active_markets: list[dict] = []
_tg_app = None

_FX_ASSETS = ["EURUSD", "GBPUSD", "XAUUSD"]
_market_engine_cache: dict[str, str] = {}
_market_min_cache:    dict[str, float] = {}
_trade_cooldown:      dict[tuple[str, str, str], float] = {}
TRADE_COOLDOWN_SEC = 60
MIN_TRADE_NGN      = 100.0


def _cooldown_key(chat_id: str, market_id: str, strategy: str = "") -> tuple[str, str, str]:
    # A market-wide key caused user A's order to silence every other user for
    # 60 seconds in this multi-user service.
    #
    # The strategy is part of the key for the same reason, one level down:
    # MAKER re-quotes inside a single candle (60s timeout, 0.10% oracle move),
    # and each placement stamped the *market* cooldown. That let passive
    # liquidity provision repeatedly silence a directional taker on the very
    # same market — the exact "no taker entries" drought this key caused.
    return str(chat_id), market_id, str(strategy or "").upper()


def _stall_skip(chat_id: str, sig, code: str, detail: str = "") -> None:
    """Record why an otherwise-valid signal never became an order.

    Telemetry only — the decision to skip stays exactly as the risk logic made
    it. This exists because a dry-run flag, a balance too small for the platform
    minimum, and a genuinely edge-free market all looked the same from outside.
    """
    try:
        # One call: note_order records the attempt *and* the exec: counter.
        # Calling stall.reject here as well counted every skip twice, so every
        # executor-outcome number in /why was double the real count.
        stall.note_order(chat_id, getattr(sig, "strategy", "?"), placed=False,
                         reason=code, detail=detail)
    except Exception:
        pass


async def _entry_database_ready(chat_id: str, sig) -> bool:
    """Fail closed on new entries when the durable trade ledger is unavailable."""
    try:
        ready = await asyncio.to_thread(database.check_connection)
    except Exception as exc:
        ready = False
        detail = str(exc)[:160]
    else:
        detail = "database health check returned false"
    if ready is True:
        return True
    _stall_skip(chat_id, sig, "database_unavailable", detail)
    log.error(
        f"[{chat_id}] SKIP {getattr(sig, 'strategy', '?')} "
        f"{getattr(sig, 'asset', '?')} — durable trade ledger is unavailable"
    )
    return False


# MAKER singleton — needed to track open limit orders for requoting.
# Imported lazily to avoid circular imports.
_maker_strategy = None

def _get_maker():
    global _maker_strategy
    if _maker_strategy is None:
        from strategies.maker import maker_strategy
        _maker_strategy = maker_strategy
    return _maker_strategy


def init_executor(markets, tg_app):
    global active_markets, _tg_app
    active_markets = markets
    _tg_app        = tg_app


# ── Float sanitisation ────────────────────────────────────────────────────────

def _finite_float(value) -> float | None:
    """Parse a finite number without allowing NaN/Infinity into risk math."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _sell_proceeds_for_shares(shares: float, price: float, fee_rate: float) -> float:
    """Conservative currency amount to request when liquidating shares.

    Bayse SELL `amount` means desired currency proceeds, not share quantity.
    Invalid input returns zero so it cannot become a live order amount.
    """
    shares = _finite_float(shares)
    price = _finite_float(price)
    fee_rate = _finite_float(fee_rate)
    unit = _finite_float(config.CURRENCY_BASE_MULTIPLIER)
    if (
        shares is None or price is None or fee_rate is None or unit is None
        or shares <= 0.0 or not 0.0 < price < 1.0
        or not 0.0 <= fee_rate < 1.0 or unit <= 0.0
    ):
        return 0.0
    gross = shares * price * unit
    proceeds = gross * (1.0 - _effective_fee(fee_rate, price)) * 0.98
    return proceeds if math.isfinite(proceeds) and proceeds > 0.0 else 0.0


def _safe_float(val, default: float = 0.0) -> float:
    """
    Clamp to PostgreSQL REAL range before any DB write.

    Python's float64 can represent values like 9.4e-64 (subnormal for REAL).
    PostgreSQL REAL minimum is ~1.18e-38 and maximum ~3.4e38.
    Subnormal values crash psycopg2 with NumericValueOutOfRange.
    """
    number = _finite_float(val)
    if number is None:
        return default
    if number != 0.0 and abs(number) < 1e-37:
        return 0.0          # subnormal → zero (safe for REAL)
    if abs(number) > 3.4e38:
        return default      # overflow → default
    return number


def _performance_size_multiplier(learned: dict, sig) -> float:
    """Combine strategy-wide and strategy/asset/timeframe loss controls."""
    multipliers = learned.get("size_multipliers", {})
    combo_key = f"{sig.strategy}:{sig.asset}:{sig.timeframe}"
    try:
        strategy_mult = float(multipliers.get(sig.strategy, 1.0))
        combo_mult = float(multipliers.get(combo_key, 1.0))
        combined = strategy_mult * combo_mult
    except (AttributeError, TypeError, ValueError):
        combined = 1.0
    if not math.isfinite(combined):
        combined = 1.0
    return min(1.50, max(0.0, combined))


# ── Engine detection ──────────────────────────────────────────────────────────

async def _infer_engine(client, market: dict) -> str:
    mid = market.get("market_id", "")
    if mid in _market_engine_cache:
        return _market_engine_cache[mid]
    try:
        yes_id = market.get("yes_id") or market.get("outcome1Id")
        if not yes_id:
            return "AMM"
        ob = await asyncio.wait_for(
            client.get_orderbook(yes_id), timeout=0.5
        )
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        engine = "CLOB" if (bids or asks) else "AMM"
    except Exception:
        engine = "AMM"
    _market_engine_cache[mid] = engine
    return engine


def _effective_fee(fee_rate: float, price: float) -> float:
    """Bayse fee as a fraction of fill notional."""
    return fee_rate * max(1.0 - price, FEE_FLOOR)


def _clob_buy_effective_price(price: float, fee_rate: float) -> float:
    """Exact wallet cost per net share for a fee-bearing CLOB BUY."""
    return price / max(1.0 - _effective_fee(fee_rate, price), 1e-9)


def _floor_price_tick(price: float, tick: float = 0.001) -> float:
    """Round a hard BUY ceiling down, never up through the risk limit."""
    try:
        value = float(price)
        tick = float(tick)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or not math.isfinite(tick) or tick <= 0:
        return 0.0
    return math.floor((value + tick * 1e-9) / tick) * tick


def _max_raw_buy_price_for_ev(
    fair_value: float, fee_rate: float, target_margin: float,
    *, max_effective_price: float | None = None, max_raw_price: float = 0.99,
) -> float:
    """Largest exchange tick whose exact fee-inclusive price preserves EV.

    The BUY fee changes with price, so solving with a fee sampled at the signal
    price can leave the final tick just over the model's EV ceiling. Binary
    search the same fee function used for entry accounting, then round down.
    """
    try:
        fair_value = float(fair_value)
        target_margin = max(0.0, float(target_margin))
        max_raw_price = min(0.99, float(max_raw_price))
        configured_max = (
            float(max_effective_price)
            if max_effective_price is not None
            else float(config.TAKER_MAX_EFFECTIVE_PRICE)
        )
    except (TypeError, ValueError):
        return 0.0
    if (
        not math.isfinite(fair_value) or not 0.0 < fair_value < 1.0
        or not math.isfinite(target_margin) or target_margin < 0.0
        or not math.isfinite(max_raw_price) or max_raw_price < 0.01
        or not math.isfinite(configured_max) or configured_max <= 0.01
    ):
        return 0.0

    effective_ceiling = min(configured_max, fair_value / (1.0 + target_margin))
    low, high = 0.01, max_raw_price
    if booklib.effective_buy_price(high, fee_rate) <= effective_ceiling:
        return _floor_price_tick(high)
    for _ in range(64):
        mid = (low + high) / 2.0
        if booklib.effective_buy_price(mid, fee_rate) <= effective_ceiling:
            low = mid
        else:
            high = mid
    return _floor_price_tick(low)


def _level_prices(levels) -> list[float]:
    """Valid (0, 1) prices from a Bayse book side, tolerating malformed levels."""
    prices = []
    for level in levels or []:
        try:
            raw = level.get("price") if isinstance(level, dict) else level[0]
            price = float(raw)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            continue
        if 0.0 < price < 1.0:
            prices.append(price)
    return prices


def _maker_quote_against_book(
    book: dict,
    max_price: float,
    *,
    fair_value: float | None = None,
    min_price: float | None = None,
    tick: float | None = None,
    max_ticks_behind: int | None = None,
) -> tuple[float | None, str, str]:
    """Price one post-only MAKER BUY against the live order book.

    ``max_price`` is the strategy's number: the most MAKER will pay. It is a
    hard ceiling and is never exceeded -- a risk/reward ceiling is not a
    liquidity setting, so no amount of "but it would fill" raises it.

    The mechanics live in :func:`strategies.book.passive_bid_price` so the
    strategy and the executor cannot disagree about what "passive and
    competitive" means. This wrapper exists to keep the executor's historical
    skip-code names, which the drought report and its tests key off.

    ``fair_value`` is diagnostic only: a skip then reports the model-implied
    gross return at the current best bid and ask. It never changes admission
    or pricing.
    """
    price, code, detail = booklib.passive_bid_price(
        book,
        max_price,
        floor=config.MAKER_MIN_LEG_BID if min_price is None else float(min_price),
        tick=tick,
        max_ticks_behind=max_ticks_behind,
    )

    if price is None:
        code = {
            "behind_book": "maker_quote_behind_book",
            "would_cross_book": "maker_would_cross_book",
            "no_passive_price": "maker_no_passive_price",
        }.get(code, f"maker_{code}")
        detail += _model_at_book(book, fair_value)
        return None, code, detail
    return price, "", detail + _model_at_book(book, fair_value)


def _model_at_book(book: dict, fair_value: float | None) -> str:
    """Model-implied gross return at the current best bid and ask.

    Diagnostic text appended to a skip. It exists because "the quote was
    uncompetitive" and "the quote was uncompetitive and the model thought it
    was worth 30% more" are very different operational signals, and only the
    second one argues for revisiting the ceiling.
    """
    try:
        fv = float(fair_value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(fv) or not (0.0 < fv < 1.0):
        return ""
    best_bid, best_ask = booklib.best_bid(book), booklib.best_ask(book)
    parts = []
    if best_bid is not None:
        parts.append(f"at best bid {fv / best_bid - 1.0:+.1%}")
    if best_ask is not None:
        parts.append(f"at best ask {fv / best_ask - 1.0:+.1%}")
    if not parts:
        return ""
    return (
        f"; model FV {fv:.3f} -> gross ROI " + " and ".join(parts)
        + " if filled (model estimate only; before fees/adverse selection)"
    )


def _book_is_stale(
    book: dict, *, max_age: float | None = None, now: float | None = None
) -> bool:
    """Reject an explicitly stale/malformed book timestamp when one is present.

    Bayse's documented order-book level schema does not promise a timestamp,
    so absence cannot safely be interpreted as stale. A supplied timestamp is
    nonetheless enforced and supports seconds, milliseconds, or ISO-8601.
    """
    timestamp = next(
        (
            book.get(key)
            for key in ("timestamp", "updatedAt", "updated_at")
            if book.get(key) is not None
        ),
        None,
    )
    if timestamp is None:
        return False
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
        if updated > 10_000_000_000:
            updated /= 1000.0
        age = (time.time() if now is None else now) - updated
        limit = (
            config.CLOB_MAX_BOOK_AGE_SECONDS
            if max_age is None else max_age
        )
        return age < -1.0 or age > limit
    except (TypeError, ValueError, OverflowError):
        return True


def _quote_effective_buy_price(quote: dict) -> float:
    """Derive wallet cost per normalized share from a Bayse quote.

    Bayse defines BUY ``amount`` as total wallet spend and ``quantity`` as
    shares received. This handles both AMM embedded fees and CLOB share-
    reducing fees without guessing from a configured fee rate. Missing cost
    fields return zero for legacy-payload fallback; malformed/non-finite
    supplied fields return NaN so the caller rejects the quote.
    """
    if not isinstance(quote, dict):
        return math.nan

    numeric_fields = (
        "price", "quantity", "amount", "costOfShares", "fee",
        "currencyBaseMultiplier",
    )
    parsed: dict[str, float] = {}
    for field in numeric_fields:
        raw = quote.get(field)
        if raw is None:
            continue
        value = _finite_float(raw)
        if value is None:
            return math.nan
        parsed[field] = value

    for field in ("price", "quantity", "amount", "costOfShares", "fee"):
        if field in parsed and parsed[field] < 0.0:
            return math.nan
    if "price" in parsed and not 0.0 < parsed["price"] < 1.0:
        return math.nan

    quantity = parsed.get("quantity", 0.0)
    if quantity <= 0.0:
        return 0.0

    amount = parsed.get("amount", 0.0)
    if amount <= 0.0:
        has_cost = "costOfShares" in parsed or "fee" in parsed
        if not has_cost:
            return 0.0
        amount = parsed.get("costOfShares", 0.0) + parsed.get("fee", 0.0)
    if amount <= 0.0:
        return 0.0

    multiplier = parsed.get("currencyBaseMultiplier")
    if multiplier is None:
        multiplier = _finite_float(config.CURRENCY_BASE_MULTIPLIER)
    if multiplier is None or multiplier <= 0.0:
        return math.nan
    effective = amount / (quantity * multiplier)
    return effective if math.isfinite(effective) and effective > 0.0 else math.nan


# ── Main trade execution ──────────────────────────────────────────────────────

async def execute_trade(chat_id: str, sig, client, risk, settings: dict,
                        equity: float, free_cash: float):
    if not config.LIVE_TRADING:
        log.info(f"[{chat_id}] DRY RUN {sig.strategy} {sig.asset} — LIVE_TRADING=false")
        stall.note_state(chat_id, dry_run=True)
        _stall_skip(chat_id, sig, "dry_run_live_trading_false",
                    "signal was valid; LIVE_TRADING=false means no order is ever sent")
        return
    if strategy.global_state.systemic_halt_until > time.time():
        _stall_skip(chat_id, sig, "systemic_halt")
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — systemic halt active")
        return
    is_hedge = "PAIR_HEDGE" in getattr(sig, "reason", "")
    if risk.already_in(sig.market_id, asset=sig.asset, is_hedge=is_hedge,
                       strategy=sig.strategy, outcome=getattr(sig, "outcome", "")):
        _stall_skip(chat_id, sig, "already_in_or_pending")
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — already in/pending on {sig.market_id}")
        return
    last = _trade_cooldown.get(_cooldown_key(chat_id, sig.market_id, sig.strategy), 0.0)
    remaining = TRADE_COOLDOWN_SEC - (time.time() - last)
    if remaining > 0:
        _stall_skip(chat_id, sig, "market_cooldown", f"{remaining:.0f}s left")
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — cooldown {remaining:.0f}s left on {sig.market_id}")
        return

    risk.lock_market(sig.market_id)
    try:
        await _execute_logic(
            chat_id, sig, client, risk, settings, equity, free_cash,
            is_hedge=is_hedge,
        )
    finally:
        risk.unlock_market(sig.market_id)



def _order_has_malformed_financials(order: dict) -> bool:
    """Detect non-finite or out-of-range numeric values in an order payload."""
    if not isinstance(order, dict):
        return True
    fields = (
        "quantity", "shares", "filledSize", "sharesFilled", "sharesMatched",
        "amountMatched", "filledQuantity", "avgFillPrice", "price",
        "totalCost", "amount", "costOfShares", "cost", "fee",
        "proceeds", "netProceeds", "payout",
    )
    for field in fields:
        raw = order.get(field)
        if raw is None:
            continue
        value = _finite_float(raw)
        if value is None:
            return True
        if field in {"avgFillPrice", "price"} and not 0.0 < value < 1.0:
            return True
        if field != "fee" and value < 0.0:
            return True
    return False


def _order_has_malformed_fill_quantity(order: dict) -> bool:
    """A zero parser result is not proof of no fill when fields are malformed."""
    if not isinstance(order, dict):
        return True
    for field in (
        "quantity", "shares", "filledSize", "sharesFilled",
        "sharesMatched", "filledQuantity",
    ):
        raw = order.get(field)
        if raw is None:
            continue
        value = _finite_float(raw)
        if value is None or value < 0.0:
            return True
    return False


def _order_fill_price(order: dict, fallback: float) -> tuple[float | None, bool]:
    """Return a valid fill/limit price and whether the payload needs review."""
    malformed = not isinstance(order, dict)
    for field in ("avgFillPrice", "price"):
        raw = order.get(field) if isinstance(order, dict) else None
        if raw is None:
            continue
        value = _finite_float(raw)
        if value is None or not 0.0 < value < 1.0:
            malformed = True
            continue
        return value, malformed
    value = _finite_float(fallback)
    if value is not None and 0.0 < value < 1.0:
        return value, True
    return None, True


def _order_actual_cost(order: dict, shares: float, fill_price: float) -> float:
    """Best available wallet debit for one confirmed BUY fill, including fees.

    Prefer Bayse's explicit total, then the reported shares-cost plus fee. If
    those fields are absent or malformed, reconstruct from finite confirmed
    shares and price. The caller separately flags malformed payload fields and
    alerts for reconciliation; it never drops an exchange-confirmed fill.
    """
    shares = _finite_float(shares)
    fill_price = _finite_float(fill_price)
    unit = _finite_float(config.CURRENCY_BASE_MULTIPLIER)
    if (
        shares is None or shares <= 0.0 or fill_price is None
        or not 0.0 < fill_price < 1.0 or unit is None or unit <= 0.0
    ):
        return math.nan

    raw_fee = order.get("fee") if isinstance(order, dict) else None
    fee = 0.0 if raw_fee is None else _finite_float(raw_fee)
    if fee is None:
        return math.nan

    if isinstance(order, dict):
        total_cost = order.get("totalCost")
        if total_cost is not None:
            parsed_total = _finite_float(total_cost)
            if parsed_total is not None and parsed_total > 0.0:
                return parsed_total

        for field in ("costOfShares", "cost"):
            share_cost = order.get(field)
            if share_cost is None:
                continue
            parsed_cost = _finite_float(share_cost)
            if parsed_cost is not None and parsed_cost >= 0.0:
                candidate = parsed_cost + fee
                if math.isfinite(candidate) and candidate > 0.0:
                    return candidate

    reconstructed = shares * fill_price * unit + fee
    return reconstructed if math.isfinite(reconstructed) and reconstructed > 0.0 else math.nan


def _complete_set_payout(resp, shares: float) -> tuple[float | None, bool, str]:
    """Reconstruct guaranteed set payout and distrust inconsistent API totals."""
    shares = _finite_float(shares)
    unit = _finite_float(config.CURRENCY_BASE_MULTIPLIER)
    if shares is None or shares <= 0.0 or unit is None or unit <= 0.0:
        return None, True, "invalid matched-share quantity or currency multiplier"
    expected = shares * unit
    if not math.isfinite(expected) or expected <= 0.0:
        return None, True, "guaranteed set payout is non-finite"

    if not isinstance(resp, dict):
        return expected, True, "burn returned a non-object response; used guaranteed payout"
    status = str(resp.get("status") or "").strip().lower()
    failed_statuses = {"failed", "failure", "rejected", "error", "cancelled", "canceled"}
    confirmed_statuses = {"success", "succeeded", "completed", "complete", "burned", "confirmed", "ok"}
    if (
        resp.get("success") is False or resp.get("ok") is False
        or status in failed_statuses or resp.get("error")
        or (status and status not in confirmed_statuses
            and resp.get("success") is not True and resp.get("ok") is not True)
    ):
        return None, True, "burn response does not confirm success; outcome is ambiguous"

    raw = next(
        (resp.get(key) for key in ("amount", "proceeds", "payout")
         if resp.get(key) is not None),
        None,
    )
    if raw is None:
        return expected, True, "burn response omitted payout; used guaranteed set payout"
    reported = _finite_float(raw)
    tolerance = max(0.01, expected * 1e-4)
    if reported is None or reported <= 0.0 or abs(reported - expected) > tolerance:
        return expected, True, "reported burn payout was malformed or inconsistent; used guaranteed set payout"
    return reported, False, ""


def _balanced_pair_amounts(legs, amount: float, *, min_order: float = 0.0,
                           fee_rate: float = 0.0, is_maker: bool = False):
    """Per-leg stakes that buy the SAME number of shares on every leg.

    A set settles on ``min(shares_yes, shares_no)``, so a locked spread needs
    equal quantities on both legs. Spending the same naira amount on a 0.46
    leg and a 0.50 leg buys more shares of the cheaper side; the excess is an
    unhedged directional position that the strategy's sizing, its exemption
    from performance shrinkage and the pair accounting never charged for. The
    most expensive leg therefore sets the share count, and the cheaper leg
    spends less than its ceiling rather than over-buying.

    The stakes are apportioned by the *fee-inclusive* price, because a taker's
    fee is taken out of the shares received: ``eff = price / (1 - fee)`` and
    ``shares = amount / (eff * multiplier)``. For a maker (no fee) the two
    prices are the same number.

    Each leg's order must still clear the exchange's per-order minimum, so a
    set that cannot be balanced inside the pair's two-leg budget raises the
    whole set to the smallest size that clears it, and returns ``None`` when
    even that cannot be afforded. ``None`` means "do not send either leg".
    """
    try:
        amount = float(amount)
        min_order = float(min_order)
        fee_rate = float(fee_rate)
    except (TypeError, ValueError):
        return None
    if (
        not all(math.isfinite(value) for value in (amount, min_order, fee_rate))
        or amount <= 0.0 or min_order < 0.0 or not 0.0 <= fee_rate < 1.0
    ):
        return None
    if len(legs) < 2:
        return [amount] * len(legs)

    effs = []
    for leg in legs:
        price = _finite_float(getattr(leg, "price", None))
        if price is None or not 0.0 < price < 1.0:
            return None
        effective = booklib.effective_buy_price(
            price, fee_rate, is_maker=is_maker
        )
        if not math.isfinite(effective) or effective <= 0.0:
            return None
        effs.append(effective)
    eff_max, eff_min = max(effs), min(effs)

    shares = amount / eff_max
    if min_order > 0 and shares * eff_min < min_order:
        shares = min_order / eff_min
    amounts = [shares * effective for effective in effs]

    # The budget is a per-leg ceiling, not just a cap on the pair's combined
    # notional. Raising a balanced pair to clear the minimum must not silently
    # enlarge its expensive leg beyond the risk-sized per-leg budget.
    if max(amounts) > amount + 1e-6:
        return None
    if sum(amounts) > 2.0 * amount + 1e-6:
        return None
    if min_order > 0 and min(amounts) < min_order - 1e-6:
        return None
    return amounts


@dataclasses.dataclass(frozen=True)
class _Sizing:
    """How much one signal is allowed to commit, and why that is the cap.

    Kept separate from the order-placement flow because this is the number
    that decides whether a mistake is survivable. Everything here is a
    ceiling: the strategy's own size, the mode cap, the account's risk
    setting, the free cash actually available.
    """
    final_pct: float      # fraction of equity this leg may commit
    allowed_pct: float    # the binding risk ceiling, for messaging
    hard_cap: float       # absolute ₦ ceiling for one leg
    effective_min: float  # smallest order the exchange and user accept
    effective_max: float  # largest order the user allows


async def _size_for_signal(
    sig, settings: dict, risk, *, chat_id: str, equity: float, free_cash: float,
    n_legs: int, mode: str, mult: float, user_risk: float,
    min_t: float, max_t: float,
) -> _Sizing:
    """Compute the position size for one signal.

    Extracted from ``_execute_logic`` so the most safety-critical arithmetic in
    the bot can be tested on its own. The rules, in the order they bind:

    * the strategy's own Kelly size, scaled by its settled performance;
    * a conviction tier when the strategy expressed no size of its own;
    * halved when the strategy's realised edge is decaying, or the account is
      on probation;
    * capped by the account's ``risk_pct`` and the mode ceiling -- a ceiling,
      never a suggestion;
    * capped in cash by the free balance divided across the legs, so a
      two-sided quote cannot reserve money the wallet does not have.
    """
    kelly_pct = float(getattr(sig, "size_pct", 0.0) or 0.0)
    if kelly_pct > 0.0:
        raw_pct = kelly_pct * mult
    else:
        # Conviction can only reduce a position, never restore one.
        #
        # This used to scale *up* as well (1.5x above 0.70, 2.0x above 0.90,
        # plus another 1.5x above 0.95). Those multipliers are still there in
        # spirit but they no longer exist here, because they cancelled the
        # learner's performance decay exactly when it mattered: a strategy the
        # learner had halved (mult 0.5) still bet the full risk_pct whenever a
        # signal looked confident, since 2.0 x 1.5 x 0.5 = 1.5 > 1.
        #
        # `mult` is the learner's verdict on this strategy based on settled
        # results, and `certainty` is a heuristic score, not a calibrated
        # probability -- the notification calls it one. Letting a heuristic
        # undo evidence is how a bot sizes up into its own worst strategy.
        tier = 1.0 if sig.certainty >= 0.55 else 0.5
        fx_factor = 0.5 if sig.asset in _FX_ASSETS else 1.0
        raw_pct   = user_risk * tier * mult * fx_factor

    # `risk_pct` is a ceiling, not a suggestion. Previously Kelly-sized signals
    # bypassed it, and the ₦100 platform minimum could force a 20% bet on a
    # ₦500 account.
    raw_pct = min(raw_pct, config.MAX_TRADE_RISK)

    if hasattr(database, "get_alpha_trend"):
        decay = await asyncio.to_thread(
            database.get_alpha_trend, chat_id, sig.strategy, sig.asset
        )
        if decay < 0.85:
            raw_pct *= 0.5

    if risk.is_on_probation():
        raw_pct *= 0.50

    mode_cap_pct = {
        "safe": 0.03,
        "balanced": 0.05,
        "aggressive": 0.08,
        "full_send": 0.10,
        "custom": 0.05,
    }.get(mode, 0.05)
    allowed_pct = min(user_risk, mode_cap_pct)
    final_pct = min(raw_pct, allowed_pct)

    market_meta = next(
        (m for m in active_markets if m.get("market_id") == sig.market_id), None
    )
    market_min = float((market_meta or {}).get("minimum_order_amount") or MIN_TRADE_NGN)
    effective_min = max(MIN_TRADE_NGN, float(min_t), market_min)
    effective_max = max(0.0, float(max_t))
    # ``size_pct`` is per leg. A two-sided quote commits it twice, so each leg
    # may only spend its share of free cash -- otherwise a pair can reserve
    # money the wallet does not have and the second placement fails after the
    # first has already filled.
    per_leg_free_cash = free_cash / max(1, n_legs)
    hard_cap = min(equity * allowed_pct, effective_max, per_leg_free_cash)

    return _Sizing(
        final_pct=final_pct,
        allowed_pct=allowed_pct,
        hard_cap=hard_cap,
        effective_min=effective_min,
        effective_max=effective_max,
    )


async def _execute_logic(
    chat_id: str, sig, client, risk, settings: dict,
    equity: float, free_cash: float, *, is_hedge: bool = False,
):
    if not isinstance(settings, dict):
        _stall_skip(chat_id, sig, "invalid_execution_settings", "settings must be a mapping")
        log.error(f"[{chat_id}] SKIP trade — execution settings are not a mapping")
        return

    numeric_inputs = {
        "equity": _finite_float(equity),
        "free_cash": _finite_float(free_cash),
        "risk_pct": _finite_float(settings.get("risk_pct", 2.0)),
        "maxexposure": _finite_float(settings.get("maxexposure", 20.0)),
        "mintrade": _finite_float(settings.get("mintrade", MIN_TRADE_NGN)),
        "maxtrade": _finite_float(settings.get("maxtrade", 5_000.0)),
        "market_price": _finite_float(getattr(sig, "market_price", None)),
        "win_prob": _finite_float(getattr(sig, "win_prob", None)),
        "certainty": _finite_float(getattr(sig, "certainty", None)),
        "size_pct": _finite_float(getattr(sig, "size_pct", None)),
        "mode_floor": _finite_float(getattr(sig, "mode_floor", 0.0)),
        "min_net_ev": _finite_float(getattr(sig, "min_net_ev", 0.0)),
        "max_trade_risk": _finite_float(config.MAX_TRADE_RISK),
        "max_portfolio_exposure": _finite_float(config.MAX_PORTFOLIO_EXPOSURE),
    }
    invalid_values = [name for name, value in numeric_inputs.items() if value is None]
    if invalid_values:
        _stall_skip(chat_id, sig, "invalid_execution_numeric", ", ".join(invalid_values))
        log.error(f"[{chat_id}] SKIP trade — invalid numeric inputs: {invalid_values}")
        return

    equity = numeric_inputs["equity"]
    free_cash = numeric_inputs["free_cash"]
    risk_pct_setting = numeric_inputs["risk_pct"]
    max_exposure_pct = numeric_inputs["maxexposure"]
    min_t = numeric_inputs["mintrade"]
    max_t = numeric_inputs["maxtrade"]
    signal_price = numeric_inputs["market_price"]
    signal_prob = numeric_inputs["win_prob"]
    signal_certainty = numeric_inputs["certainty"]
    signal_size_pct = numeric_inputs["size_pct"]
    signal_mode_floor = numeric_inputs["mode_floor"]
    signal_min_ev = numeric_inputs["min_net_ev"]
    global_risk_cap = numeric_inputs["max_trade_risk"]
    global_exposure_cap = numeric_inputs["max_portfolio_exposure"]

    if (
        equity <= 0.0 or free_cash < 0.0 or risk_pct_setting <= 0.0
        or max_exposure_pct <= 0.0 or min_t < 0.0 or max_t <= 0.0
        or global_risk_cap <= 0.0 or global_exposure_cap <= 0.0
        or not 0.0 < signal_price < 1.0
        or not 0.0 <= signal_prob <= 1.0
        or not 0.0 <= signal_certainty <= 1.0
        or signal_size_pct < 0.0
        or not 0.0 <= signal_mode_floor <= 1.0
        or signal_min_ev < 0.0
    ):
        _stall_skip(chat_id, sig, "invalid_execution_numeric", "value outside permitted range")
        log.error(f"[{chat_id}] SKIP trade — execution input outside permitted range")
        return

    # Preserve the internal signal contract (downstream logging and persistence
    # expect numeric fields even if a manually constructed signal used strings).
    sig.market_price = signal_price
    sig.win_prob = signal_prob
    sig.certainty = signal_certainty
    sig.size_pct = signal_size_pct
    sig.mode_floor = signal_mode_floor
    sig.min_net_ev = signal_min_ev

    mode = settings.get("mode", "balanced")
    if not isinstance(mode, str) or mode not in {
        "safe", "balanced", "aggressive", "full_send", "custom"
    }:
        _stall_skip(chat_id, sig, "invalid_execution_mode", str(mode))
        log.error(f"[{chat_id}] SKIP trade — unsupported risk mode {mode!r}")
        return
    is_maker = (sig.strategy == "MAKER")
    legs = sig.ensure_legs()
    if not legs:
        _stall_skip(chat_id, sig, "invalid_execution_legs", "signal has no legs")
        return
    for leg in legs:
        leg_price = _finite_float(getattr(leg, "price", None))
        leg_size = _finite_float(getattr(leg, "size_pct", None))
        leg_fair_value = _finite_float(getattr(leg, "fair_value", None))
        if (
            leg_price is None or not 0.0 < leg_price < 1.0
            or leg_size is None or leg_size < 0.0
            or leg_fair_value is None or not 0.0 <= leg_fair_value <= 1.0
            or not getattr(leg, "outcome", None)
            or not getattr(leg, "outcome_id", None)
        ):
            _stall_skip(chat_id, sig, "invalid_execution_legs", "malformed numeric or identity field")
            log.error(f"[{chat_id}] SKIP trade — malformed execution leg for {sig.market_id}")
            return
    market_meta = next(
        (m for m in active_markets if m.get("market_id") == sig.market_id), None
    )
    raw_market_min = (market_meta or {}).get("minimum_order_amount", MIN_TRADE_NGN)
    market_min = _finite_float(raw_market_min)
    if market_min is None or market_min < 0.0:
        _stall_skip(chat_id, sig, "invalid_market_minimum", str(raw_market_min))
        log.error(f"[{chat_id}] SKIP trade — invalid market minimum for {sig.market_id}")
        return

    max_exp = min(max_exposure_pct / 100.0, global_exposure_cap)
    learned = settings.get("learned", {})
    mult = _performance_size_multiplier(learned, sig)
    user_risk = min(risk_pct_setting / 100.0, global_risk_cap)
    declared = None
    engine = "AMM"
    quote_price = signal_price
    target_margin = {
        "safe": 0.03, "balanced": 0.01, "aggressive": 0.00,
        "full_send": 0.00, "custom": 0.01,
    }[mode]

    if risk.is_in_strict_mode() and sig.certainty < 0.40:
        _stall_skip(chat_id, sig, "strict_mode_near_daily_target",
                    f"certainty {sig.certainty:.0%} < 40%")
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — strict mode (near daily target), certainty {sig.certainty:.0%} < 40%")
        return

    sizing = await _size_for_signal(
        sig, settings, risk, chat_id=chat_id,
        equity=equity, free_cash=free_cash, n_legs=len(legs),
        mode=mode, mult=mult, user_risk=user_risk, min_t=min_t, max_t=max_t,
    )
    allowed_pct   = sizing.allowed_pct
    final_pct     = sizing.final_pct
    hard_cap      = sizing.hard_cap
    effective_min = sizing.effective_min
    effective_max = sizing.effective_max

    if hard_cap < effective_min:
        min_equity = effective_min / allowed_pct if allowed_pct > 0 else float("inf")
        _stall_skip(chat_id, sig, "risk_budget_below_platform_minimum",
                    f"min ₦{effective_min:,.0f} > budget ₦{hard_cap:,.0f}; "
                    f"needs ≈ ₦{min_equity:,.0f} equity at {allowed_pct:.1%} risk")
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — platform/user minimum "
            f"₦{effective_min:,.0f} exceeds the {allowed_pct:.1%} risk budget "
            f"₦{hard_cap:,.0f} (equity needed ≈ ₦{min_equity:,.0f})"
        )
        return

    amount = min(equity * final_pct, hard_cap)
    if amount < effective_min:
        _stall_skip(chat_id, sig, "size_below_market_minimum",
                    f"₦{amount:,.0f} < ₦{effective_min:,.0f}")
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — Kelly amount "
            f"₦{amount:,.0f} is below minimum ₦{effective_min:,.0f}"
        )
        return

    if config.TEST_MODE:
        if equity < config.TEST_MIN_BANKROLL:
            log.warning(
                f"[{chat_id}] TEST MODE: bankroll ₦{equity:,.0f} below "
                f"₦{config.TEST_MIN_BANKROLL:,.0f} floor — HALTING all trades"
            )
            return
        amount = min(amount, config.TEST_MAX_TRADE_NGN)
        if amount < effective_min:
            log.info(
                f"[{chat_id}] TEST MODE cap ₦{amount:,.0f} is below market "
                f"minimum ₦{effective_min:,.0f}; skipping"
            )
            return

    # ── Final size ─────────────────────────────────────────────────────────
    # Sizing is closed here, before any budget is checked, so that everything
    # downstream bounds the size that will actually be sent.
    #
    # A market minimum learned from a rejection cannot justify expanding a
    # Kelly-sized order, even if the global hard cap would allow more. Skip
    # rather than undoing performance shrinkage or the strategy's requested size.
    cached_min = _market_min_cache.get(sig.market_id, 0.0)
    if cached_min > 0 and amount < cached_min:
        _stall_skip(
            chat_id, sig, "market_minimum_exceeds_size",
            f"market min ₦{cached_min:,.0f} > sized amount ₦{amount:,.0f} "
            f"(hard cap ₦{hard_cap:,.0f})",
        )
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — learned market minimum "
            f"₦{cached_min:,.0f} exceeds Kelly-sized amount ₦{amount:,.0f}"
        )
        _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
        return

    # Both multi-leg paths need equal share counts -- a set settles on
    # min(shares_yes, shares_no) -- so their per-leg stakes are derived here,
    # and the exposure ceiling is checked against what the whole decision
    # commits rather than one leg of it.
    min_order = max(effective_min, cached_min)
    if len(legs) > 1:
        # `fee_rate` is only known below (it is resolved with the engine), but
        # the taker fee is a property of the market and is needed here to
        # apportion the stakes by what a share actually costs. Resolve it now;
        # the later read is the same cached value.
        leg_amounts = _balanced_pair_amounts(
            legs, amount, min_order=min_order,
            fee_rate=_get_market_fee(sig.market_id), is_maker=is_maker,
        )
        if leg_amounts is None:
            _stall_skip(
                chat_id, sig, "pair_below_order_minimum",
                f"no balanced set fits ₦{amount:,.0f}/leg above ₦{min_order:,.0f}/order "
                f"at {[round(float(leg.price), 3) for leg in legs]}",
            )
            log.info(
                f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — a balanced "
                f"(equal-share) set needs more than ₦{2 * amount:,.0f} to clear "
                f"the ₦{min_order:,.0f} per-order minimum on every leg"
            )
            return
    else:
        leg_amounts = [amount]
    committed = sum(leg_amounts)

    # A resting MAKER quote is not filled exposure (risk.deployed_filled) and
    # is charged at the per-leg budget against the directional ceiling, with
    # its own maker budget below. A TAKER complete set is two immediate FAK
    # orders: its capital becomes a position at once, so it is charged in full.
    exposure_amount = amount if is_maker else committed
    if not risk.can_trade(equity, exposure_amount, max_exp):
        _stall_skip(
            chat_id, sig, "exposure_cap",
            f"filled ₦{risk.deployed_filled():,.0f} + ₦{exposure_amount:,.0f} > "
            f"{max_exp:.0%} of ₦{equity:,.0f}",
        )
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — exposure cap "
            f"(filled=₦{risk.deployed_filled():,.0f}, +₦{exposure_amount:,.0f} > "
            f"{max_exp:.0%} of ₦{equity:,.0f})"
        )
        return

    # ── Engine detection ───────────────────────────────────────────────────
    market   = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    declared = market.get("engine") if market else None
    if declared:
        engine = declared
    elif market:
        engine = await _infer_engine(client, market)
    else:
        engine = "AMM"

    is_complete_set_taker = len(legs) > 1 and not is_maker
    if is_complete_set_taker and str(engine or "").upper() != "CLOB":
        _stall_skip(chat_id, sig, "complete_set_requires_clob", f"engine={engine}")
        log.info(
            f"[{chat_id}] SKIP TAKER {sig.asset} — complete-set execution requires CLOB, "
            f"not {engine}"
        )
        return

    fee_rate = _get_market_fee(sig.market_id)
    if not math.isfinite(fee_rate) or not 0.0 <= fee_rate < 1.0:
        _stall_skip(chat_id, sig, "invalid_market_fee", str(fee_rate))
        log.error(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — invalid market fee")
        return

    # ── EV check with pre-trade Quote ──────────────────────────────────────
    target_margin = {
        "safe": 0.03, "balanced": 0.01, "aggressive": 0.00,
        "full_send": 0.00, "custom": 0.01,
    }.get(mode, 0.01)
    if str(sig.strategy).upper() == "TAKER":
        if signal_min_ev <= 0.0:
            signal_min_ev = config.TAKER_MIN_NET_EV_DEFAULT * {
                "safe": 1.6, "balanced": 1.0, "aggressive": 0.7,
                "full_send": 0.5, "custom": 1.0,
            }[mode]
        target_margin = max(target_margin, signal_min_ev)

    is_probe = sig.certainty < sig.mode_floor
    if is_probe:
        # Live-money exploration turns uncertainty into losses. A signal below
        # the account's own conviction floor is not a trade.
        _stall_skip(chat_id, sig, "below_mode_floor",
                    f"{sig.certainty:.1%} < {sig.mode_floor:.1%}")
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — certainty "
            f"{sig.certainty:.1%} below mode floor {sig.mode_floor:.1%}"
        )
        return

    quote_price = sig.market_price
    if engine == "AMM":
        try:
            quote = await client.get_quote(
                event_id=sig.event_id, market_id=sig.market_id,
                outcome_id=sig.outcome_id, side="BUY", amount=amount,
                currency=CURRENCY
            )
            if not isinstance(quote, dict):
                _stall_skip(chat_id, sig, "invalid_quote_payload", "quote is not an object")
                return
            raw_q_price = quote.get("price")
            q_price = signal_price if raw_q_price is None else _finite_float(raw_q_price)
            raw_q_qty = quote.get("quantity")
            q_qty = 0.0 if raw_q_qty is None else _finite_float(raw_q_qty)
            if (
                q_price is None or not 0.0 < q_price < 1.0
                or q_qty is None or q_qty < 0.0
            ):
                _stall_skip(chat_id, sig, "invalid_quote_numeric", "price or quantity is malformed")
                log.warning(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — quote has invalid price/quantity")
                return
            if quote.get("completeFill") is not True or q_qty <= 0:
                _stall_skip(chat_id, sig, "quote_not_complete_fill")
                log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — quote does not confirm a complete fill")
                return
            if quote.get("tradeGoesOverMaxLiability") is True:
                _stall_skip(chat_id, sig, "quote_over_max_liability")
                log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — quote exceeds market liability")
                return

            quote_price = _quote_effective_buy_price(quote)
            if not math.isfinite(quote_price):
                _stall_skip(chat_id, sig, "invalid_quote_numeric", "non-finite quote cost fields")
                log.warning(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — quote cost is malformed")
                return
            if quote_price <= 0:
                # Compatibility fallback for legacy payloads that omit
                # amount/cost fields but still expose an executable price.
                quote_price = _clob_buy_effective_price(q_price, fee_rate)

            if not math.isfinite(quote_price) or quote_price <= 0:
                _stall_skip(chat_id, sig, "invalid_quote_price", str(quote_price))
                log.warning(f"[{chat_id}] Invalid quote price {quote_price} returned. Skipping EV calculation.")
                return
            ev = signal_prob / quote_price - 1.0
            if not math.isfinite(ev):
                _stall_skip(chat_id, sig, "invalid_quote_ev", str(ev))
                return

            if ev < target_margin:
                log.info(f"[{chat_id}] EV {ev:+.1%} too low at size ₦{amount:,.0f} (price={quote_price:.3f}). Scaling down...")
                scaled_success = False
                for scale in [0.5, 0.25]:
                    scaled_amount = max(effective_min, round(amount * scale, -2))
                    if scaled_amount >= amount:
                        continue

                    try:
                        scaled_quote = await client.get_quote(
                            event_id=sig.event_id, market_id=sig.market_id,
                            outcome_id=sig.outcome_id, side="BUY", amount=scaled_amount,
                            currency=CURRENCY
                        )
                        if not isinstance(scaled_quote, dict):
                            continue
                        if (
                            scaled_quote.get("completeFill") is not True
                            or scaled_quote.get("tradeGoesOverMaxLiability") is True
                        ):
                            continue
                        raw_sq_price = scaled_quote.get("price")
                        sq_price = signal_price if raw_sq_price is None else _finite_float(raw_sq_price)
                        raw_sq_qty = scaled_quote.get("quantity")
                        sq_qty = 0.0 if raw_sq_qty is None else _finite_float(raw_sq_qty)
                        if (
                            sq_price is None or not 0.0 < sq_price < 1.0
                            or sq_qty is None or sq_qty <= 0.0
                        ):
                            continue
                        scaled_price = _quote_effective_buy_price(scaled_quote)
                        if not math.isfinite(scaled_price):
                            continue
                        if scaled_price <= 0:
                            scaled_price = _clob_buy_effective_price(
                                sq_price, fee_rate
                            )
                        if not math.isfinite(scaled_price) or scaled_price <= 0.0:
                            continue

                        scaled_ev = signal_prob / scaled_price - 1.0
                        if not math.isfinite(scaled_ev):
                            continue
                        if scaled_ev >= target_margin:
                            log.info(
                                f"[{chat_id}] Sizing down success! ₦{amount:,.0f} → ₦{scaled_amount:,.0f} "
                                f"(EV={scaled_ev:.2%}, price={scaled_price:.3f})"
                            )
                            amount = scaled_amount
                            quote_price = scaled_price
                            ev = scaled_ev
                            scaled_success = True
                            break
                    except Exception as q_err:
                        log.debug(f"Sizing down quote failed for size ₦{scaled_amount}: {q_err}")

                if not scaled_success:
                    _stall_skip(chat_id, sig, "no_profitable_size", f"EV {ev:.2%}")
                    log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — no profitable size (quoted_price={quote_price:.3f} EV={ev:.2%})")
                    return
        except Exception as e:
            # A stale displayed price is not an executable price. Trading
            # through a quote outage converts unknown slippage into risk.
            _stall_skip(chat_id, sig, "quote_request_failed", str(e)[:120])
            log.warning(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — executable quote failed: {e}")
            return
    elif is_complete_set_taker:
        # A structural pair has two complementary fair values, so comparing
        # max(win_prob) with max(price) is not a valid EV test. The complete-set
        # executor re-reads both books and re-checks each leg plus the combined
        # lock before it sends either order.
        pass
    else:
        # CLOB or probe: evaluate EV using worst-case price (inclusive of spread buffer & fees)
        fee_rate = _get_market_fee(sig.market_id)
        slip_map_ev = {"safe": 0.008, "balanced": 0.015, "aggressive": 0.020, "full_send": 0.025, "custom": 0.015}
        slip_ev = slip_map_ev.get(mode, 0.015) if not is_maker else 0.0
        taker_buf = max(0.012, sig.market_price * slip_ev) if not is_maker else 0.0
        worst_case_p = min(sig.market_price + taker_buf, 0.99)
        effective_worst_p = _clob_buy_effective_price(
            worst_case_p, fee_rate
        )
        ev = sig.win_prob / effective_worst_p - 1.0
        # No "probe" exemption here: a signal below its own conviction floor
        # returned above, so by this point every arrival has cleared it. The
        # exemption the old code implied (`not is_probe`) could never be False
        # and only made the gate look conditional when it was not.
        if ev < target_margin:
            _stall_skip(chat_id, sig, "worst_case_ev_below_margin", f"{ev:+.1%} < {target_margin:.0%}")
            log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — worst-case EV {ev:+.1%} < {target_margin:.0%} (worst_price={worst_case_p:.3f})")
            return

    # ── Global entry price ceiling ───────────────────────────────────────
    # DATA-DRIVEN (Audit Aug 13-23 2026): entries >= 0.80 → -₦314 net loss at 57% win rate.
    # Buying at 0.85 means: +₦7 net on a WIN (after fees), -₦100 on a LOSS.
    # You need a 93%+ win rate just to break even. That never happens.
    # Hard cap at 0.75 — TAKER_MAX_EFFECTIVE_PRICE is 0.65, so this is a
    # final backstop that catches any unexpected rounding or overrides.
    if quote_price > 0.75:
        _stall_skip(chat_id, sig, "price_above_ev_ceiling", f"{quote_price:.3f} > 0.75")
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — price {quote_price:.3f} > 0.75 ceiling (fee drag kills EV above 0.75)")
        return

    # ── Shared exposure check ──────────────────────────────────────────────
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    engine = str(engine or "AMM").upper()

    exposure_amount = committed if is_complete_set_taker else amount
    if not risk.can_trade(equity, exposure_amount, max_exp):
        _stall_skip(
            chat_id, sig, "exposure_cap",
            f"filled ₦{risk.deployed_filled():,.0f} + ₦{exposure_amount:,.0f} > "
            f"{max_exp:.0%} of ₦{equity:,.0f} (resting ₦{risk.deployed_resting():,.0f} excluded)",
        )
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — exposure cap "
            f"(filled=₦{risk.deployed_filled():,.0f}, +₦{exposure_amount:,.0f} > "
            f"{max_exp:.0%} of ₦{equity:,.0f}; resting=₦{risk.deployed_resting():,.0f})"
        )
        return

    # ── MAKER quote-pair guard ────────────────────────────────────────────
    # One quote per market. A second quote on a market we are already quoting
    # is either a duplicate of the first (wasted wallet reservation) or a
    # conflicting price from a different model reading, and neither should be
    # sent. Per-asset limiting is what let a single resting quote on BTC block
    # a better opportunity elsewhere on BTC.
    if is_maker:
        market_legs = [
            p for key, p in risk.open_positions.items()
            if str(p.get("strategy") or "").upper() in config.MAKER_STRATEGIES
            and (p.get("market_id", key) == sig.market_id
                 or str(key).startswith(f"{sig.market_id}:"))
        ]
        resting = [p for p in market_legs if not position_is_filled(p)]
        if resting:
            _stall_skip(chat_id, sig, "maker_quote_already_resting",
                        f"{len(resting)} MAKER leg(s) already resting on {sig.market_id}")
            log.info(
                f"[{chat_id}] SKIP MAKER {sig.asset} — a quote is already resting on "
                f"{sig.market_id}"
            )
            return
        # A leg that has already filled is a position, not a quote, so it must
        # not block re-quoting the market outright -- blocking there is what
        # made a half-filled pair impossible to finish, because the only order
        # that could complete it was never sent. What it *does* restrict is
        # what the new quote may be: it has to include the opposite side, or
        # it is simply adding to the side we already hold.
        held_outcomes = {
            str(p.get("outcome") or "").upper() for p in market_legs
        }
        if market_legs:
            wanted = {
                str(getattr(leg, "outcome", "") or "").upper() for leg in legs
            }
            if not (wanted - held_outcomes):
                _stall_skip(
                    chat_id, sig, "maker_quote_would_double_filled_side",
                    f"holding {sorted(o for o in held_outcomes if o)}; "
                    f"legs {sorted(o for o in wanted if o)}",
                )
                log.info(
                    f"[{chat_id}] SKIP MAKER {sig.asset} — a filled leg is already "
                    f"held on {sig.market_id} and this quote only re-bids it"
                )
                return
        # Maker capital budget. Resting quotes reserve wallet funds and a
        # one-sided fill carries real directional risk, so the maker book gets
        # its own ceiling instead of borrowing the directional one.
        maker_notional = 0.0
        for position in risk.open_positions.values():
            if position.get("strategy") != "MAKER":
                continue
            position_amount = _finite_float(position.get("amount_ngn", 0.0))
            if position_amount is None or position_amount < 0.0:
                maker_notional = math.inf
                break
            maker_notional += position_amount
        # `committed` is every leg of this decision, not one leg of it: a
        # two-sided quote reserves twice the per-leg budget, and a budget that
        # counts one leg lets the maker book hold twice its own ceiling.
        if maker_notional + committed > equity * config.MAX_MAKER_NOTIONAL_PCT:
            _stall_skip(
                chat_id, sig, "maker_notional_cap",
                f"₦{maker_notional:,.0f} + ₦{committed:,.0f} > "
                f"{config.MAX_MAKER_NOTIONAL_PCT:.0%} of ₦{equity:,.0f}",
            )
            log.info(
                f"[{chat_id}] SKIP MAKER {sig.asset} — maker notional "
                f"₦{maker_notional:,.0f} + ₦{committed:,.0f} exceeds the "
                f"{config.MAX_MAKER_NOTIONAL_PCT:.0%} budget"
            )
            return

        # A one-sided quote is directional risk with none of the pair's
        # guarantee, and it gets its own, tighter ceiling. The knob existed and
        # was validated at startup, but nothing read it, so the only limit on a
        # one-sided quote was the whole maker budget. `_single_leg` is enabled
        # by default, so that path is reachable on any market whose other side
        # is priced through the model's ceiling -- which, on an
        # arbitrage-free book, is most of them.
        if len(legs) < 2:
            unpaired = risk.maker_unpaired_notional()
            if unpaired + amount > equity * config.MAX_MAKER_UNPAIRED_PCT:
                _stall_skip(
                    chat_id, sig, "maker_unpaired_cap",
                    f"₦{unpaired:,.0f} + ₦{amount:,.0f} > "
                    f"{config.MAX_MAKER_UNPAIRED_PCT:.0%} of ₦{equity:,.0f}",
                )
                log.info(
                    f"[{chat_id}] SKIP MAKER {sig.asset} — one-sided quote would put "
                    f"₦{unpaired + amount:,.0f} of the ₦{equity:,.0f} account into "
                    f"unhedged maker risk (cap "
                    f"{config.MAX_MAKER_UNPAIRED_PCT:.0%})"
                )
                return

    # ── Correlated crypto exposure cap ─────────────────────────────────────
    if risk.has_correlated_open_position(sig.asset, sig.outcome, sig.timeframe, certainty=sig.certainty, strategy=sig.strategy):
        _stall_skip(chat_id, sig, "correlated_asset_cap",
                    f"same-direction {sig.asset} {sig.outcome} already open on {sig.timeframe}")
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
            f"correlated crypto position already open in same direction on {sig.timeframe} (certainty={sig.certainty:.2f} < 0.65)"
        )
        return

    # ── Strict Price-Capped Execution ─────────────────────────────────────
    # MAKER places passive LIMIT GTC orders to capture the spread; TAKER uses
    # LIMIT FAK (Fill-And-Kill / IOC) with a strict price ceiling passed
    # directly to the exchange. That physically forbids the exchange from ever
    # filling a taker order at 0.990 or 0.830.
    fee_rate      = _get_market_fee(sig.market_id)
    slip_map      = {"safe": 0.008, "balanced": 0.015, "aggressive": 0.020, "full_send": 0.025, "custom": 0.015}
    slippage      = slip_map.get(mode, 0.015)
    max_valid     = 0.99
    order_type    = "LIMIT" if engine == "CLOB" else "MARKET"
    post_only     = False

    if is_maker:
        if engine != "CLOB":
            _stall_skip(chat_id, sig, "maker_requires_clob_engine", f"engine={engine}")
            log.info(f"[{chat_id}] SKIP MAKER {sig.asset} — passive orders require CLOB")
            return
        time_in_force = "GTC"
        post_only     = True  # never accidentally cross and pay taker fees
        cooldown_key  = _cooldown_key(chat_id, sig.market_id, sig.strategy)

        # ── Re-verify every leg against ONE fresh book ─────────────────────
        # The strategy priced this quote off the snapshot it was handed. By the
        # time we are here the book may have moved, so each leg is re-priced
        # with the same function the strategy used -- never a second opinion --
        # and the pair lock is re-asserted on the prices we are about to send.
        # That last step matters: two independently-valid prices are not a
        # locked spread unless they still sum below one *now*.
        outcome_ids = [leg.outcome_id for leg in legs if leg.outcome_id]
        book_error = ""
        try:
            books = await asyncio.wait_for(
                client.get_orderbooks(outcome_ids, depth=5), timeout=2.5
            )
        except Exception as obe:
            books, book_error = {}, str(obe)[:120]

        usable = {oid: b for oid, b in (books or {}).items()
                  if isinstance(b, dict) and booklib.is_usable(b)}
        if any(oid not in usable for oid in outcome_ids):
            # No fresh market state, no order -- the same rule every entry
            # obeys, applied to both legs rather than just the first.
            code = "maker_book_stale" if usable else "maker_book_unavailable"
            _stall_skip(chat_id, sig, code,
                        book_error or f"{len(outcome_ids) - len(usable)}/{len(outcome_ids)} "
                                      f"outcome book(s) unreadable")
            log.info(
                f"[{chat_id}] SKIP MAKER {sig.asset} — {code.replace('_', ' ')}"
                + (f": {book_error}" if book_error else "")
            )
            _trade_cooldown[cooldown_key] = time.time()
            return

        stale = [oid for oid, b in usable.items() if booklib.book_is_stale(b)]
        if stale:
            _stall_skip(chat_id, sig, "maker_book_stale", f"{len(stale)} leg(s) stale")
            log.info(f"[{chat_id}] SKIP MAKER {sig.asset} — order book timestamp is stale")
            _trade_cooldown[cooldown_key] = time.time()
            return

        repriced: list = []
        for leg in legs:
            price, book_skip, book_detail = _maker_quote_against_book(
                usable.get(leg.outcome_id) or {}, leg.price, fair_value=leg.fair_value
            )
            if price is None:
                _stall_skip(chat_id, sig, book_skip, f"{leg.outcome}: {book_detail}")
                log.info(
                    f"[{chat_id}] SKIP MAKER {sig.asset} {leg.outcome} — {book_detail}"
                )
                # One book check per market per cooldown window, not per signal.
                _trade_cooldown[cooldown_key] = time.time()
                return
            if abs(price - leg.price) > 1e-9:
                log.info(
                    f"[{chat_id}] MAKER re-priced {sig.asset} {leg.outcome} "
                    f"{leg.price:.3f} -> {price:.3f} ({book_detail})"
                )
                # QuoteLeg is frozen: a leg is a quote that was priced, and a
                # quote that silently mutates is how the risk book and the
                # order end up disagreeing about what was sent.
                leg = dataclasses.replace(leg, price=price)
            repriced.append(leg)
        legs = repriced

        if len(legs) > 1:
            # The stakes were apportioned from the prices the strategy sent;
            # the quote is about to go out at the re-priced ones. Rebalance on
            # the legs actually being sent, or the equal-share property the
            # lock depends on is only approximate.
            rebalanced = _balanced_pair_amounts(
                legs, amount, min_order=min_order, fee_rate=fee_rate, is_maker=True,
            )
            if rebalanced is None:
                _stall_skip(chat_id, sig, "pair_below_order_minimum",
                            f"re-priced legs cannot clear ₦{min_order:,.0f}/order "
                            f"inside ₦{2 * amount:,.0f}")
                _trade_cooldown[cooldown_key] = time.time()
                return
            leg_amounts = rebalanced

            lock = 1.0 - sum(float(leg.price) for leg in legs)
            if lock < config.MAKER_PAIR_MIN_EDGE:
                prices = "/".join(f"{leg.outcome}@{leg.price:.3f}" for leg in legs)
                _stall_skip(
                    chat_id, sig, "maker_pair_no_longer_locks",
                    f"{prices} locks {lock:+.3f} < {config.MAKER_PAIR_MIN_EDGE:+.3f}",
                )
                log.info(
                    f"[{chat_id}] SKIP MAKER {sig.asset} — pair no longer locks "
                    f"({prices}, lock={lock:+.3f})"
                )
                _trade_cooldown[cooldown_key] = time.time()
                return

        limit_price = max(float(leg.price) for leg in legs)

    elif is_complete_set_taker:
        # The structural route must inspect both outcome books together in
        # `_place_complete_set_take`; a single-outcome best ask is not enough to
        # authorize a two-leg position.
        time_in_force = "FAK"
        limit_price = None
    elif engine == "CLOB":
        # ── CLOB Taker Execution: Price to match real Order Book Asks ────────
        # On a CLOB, buying into a book requires crossing the spread to the lowest ask.
        # Theoretical midpoint bidding (sig.market_price) always lands below the ask and
        # causes 100% zero-fill FAK cancellations.
        time_in_force = "FAK"
        # The fee changes with price, so invert the exact fee-inclusive EV
        # function rather than sampling the fee at the stale signal price.
        strategy_cap = 0.75
        cap = _max_raw_buy_price_for_ev(
            sig.win_prob, fee_rate, target_margin,
            max_effective_price=config.TAKER_MAX_EFFECTIVE_PRICE,
            max_raw_price=min(strategy_cap, max_valid),
        )
        if cap < 0.01:
            _stall_skip(chat_id, sig, "clob_ev_cap_below_floor",
                        f"cap={cap:.3f} from win_prob={sig.win_prob:.3f} "
                        f"target={target_margin:.1%}")
            return

        limit_price = _floor_price_tick(min(
            sig.market_price + max(0.012, sig.market_price * slippage), cap,
        ))
        try:
            ob = await asyncio.wait_for(
                client.get_orderbook(sig.outcome_id, depth=5), timeout=1.5
            )
            if _book_is_stale(ob):
                _stall_skip(chat_id, sig, "clob_book_stale")
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — "
                    "CLOB orderbook timestamp is stale or invalid"
                )
                _trade_cooldown[
                    _cooldown_key(chat_id, sig.market_id, sig.strategy)
                ] = time.time()
                return
            ask_levels = (ob or {}).get("asks", [])
            asks = booklib.clean_side(ask_levels)
            best_ask = booklib.best_ask(ob)
            if not asks or best_ask is None:
                _stall_skip(chat_id, sig, "clob_no_asks")
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"CLOB orderbook has no asks (zero liquidity)"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return

            if best_ask > cap:
                _stall_skip(chat_id, sig, "clob_ask_above_cap",
                            f"best_ask={best_ask:.3f} cap={cap:.3f}")
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"best ask {best_ask:.3f} > cap {cap:.3f}"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return

            # Verify EV against both the live ask and the highest price our
            # order can actually fill at. CLOB BUY fees reduce shares received,
            # so use p/(1-fee_fraction), not p*(1+fee).
            effective_ask = _clob_buy_effective_price(best_ask, fee_rate)
            ev_at_ask = sig.win_prob / effective_ask - 1.0
            if str(sig.strategy).upper() == "TAKER" and not (
                config.TAKER_MIN_EFFECTIVE_PRICE - 1e-9
                <= effective_ask
                <= config.TAKER_MAX_EFFECTIVE_PRICE + 1e-9
            ):
                _stall_skip(chat_id, sig, "clob_effective_price_outside_band",
                            f"ask={best_ask:.3f} effective={effective_ask:.3f}")
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return
            if ev_at_ask < target_margin:
                _stall_skip(chat_id, sig, "clob_ev_at_ask_below_target",
                            f"best_ask={best_ask:.3f} EV={ev_at_ask:+.1%} target={target_margin:.1%}")
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"best ask {best_ask:.3f} EV {ev_at_ask:+.1%} < target {target_margin:.1%}"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return

            # The order's maximum payable price has to preserve the same EV as
            # the live ask. Flooring to the CLOB tick prevents a round-up through
            # the exact price cap.
            limit_price = _floor_price_tick(min(best_ask + 0.003, cap, max_valid))
            if limit_price + 1e-9 < best_ask:
                _stall_skip(chat_id, sig, "clob_ask_above_cap",
                            f"best_ask={best_ask:.3f} limit={limit_price:.3f}")
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return
            effective_limit = _clob_buy_effective_price(limit_price, fee_rate)
            ev_at_limit = sig.win_prob / effective_limit - 1.0
            if ev_at_limit + 1e-9 < target_margin:
                _stall_skip(chat_id, sig, "clob_ev_at_limit_below_target",
                            f"limit={limit_price:.3f} EV={ev_at_limit:+.1%} target={target_margin:.1%}")
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return
            log.info(
                f"[{chat_id}] CLOB Book Match: best_ask={best_ask:.3f} -> limit_price={limit_price:.3f} "
                f"(EV at ask={ev_at_ask:+.1%}, at limit={ev_at_limit:+.1%})"
            )

            # ── Depth-Aware Sizing (Zero-Fill Eliminator) ─────────────────────
            # Notional comes from the share count and the documented
            # ``price * base multiplier`` cost, not from the book's ``total``
            # field: ``total`` carries no unit in the payload, and taking an
            # unscaled one as naira understates NGN depth by the 100x base
            # multiplier -- real liquidity then looks like no liquidity.
            avail_ngn = 0.0
            for ask_entry in sorted(
                ask_levels,
                key=lambda level: booklib.level_price(level) or float("inf"),
            ):
                ask_p = booklib.level_price(ask_entry)
                if ask_p is None:
                    continue
                if ask_p <= limit_price:
                    avail_ngn += booklib.level_notional(ask_entry)
                elif ask_p > limit_price:
                    break

            min_trade_req = float(settings.get("mintrade", 100.0))
            if avail_ngn < min_trade_req:
                _stall_skip(chat_id, sig, "clob_depth_below_minimum",
                            f"available ₦{avail_ngn:,.0f} < min ₦{min_trade_req:,.0f}")
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"insufficient resting depth (available ₦{avail_ngn:,.0f} < min ₦{min_trade_req:,.0f})"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return

            # Clip order size to resting liquidity so FAK orders fill with 100% reliability
            if amount > avail_ngn:
                clipped = max(min_trade_req, round(avail_ngn * 0.95, 2))
                log.info(
                    f"[{chat_id}] Depth-Aware Sizing: clipped order ₦{amount:,.0f} -> ₦{clipped:,.0f} "
                    f"to match resting book depth (₦{avail_ngn:,.0f})"
                )
                amount = clipped
        except Exception as obe:
            _stall_skip(chat_id, sig, "clob_book_unavailable", str(obe)[:120])
            # A displayed midpoint is not executable liquidity. Never place a
            # taker order when the CLOB book cannot be verified.
            log.warning(
                f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — "
                f"CLOB orderbook unavailable: {obe}"
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return
    else:
        # AMM markets execute immediately and do not have a resting book.
        # Bayse documents MARKET/FAK for this engine; LIMIT/FAK caused rejects.
        time_in_force = "FAK"
        limit_price = None

    # Do not send a new exchange write unless the durable trade ledger is
    # reachable. This check runs only after a signal passed pricing, book, and
    # risk gates (not on every market tick), and protects cached user settings
    # from turning a database outage into an unrecorded live entry.
    if not await _entry_database_ready(chat_id, sig):
        _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
        return

    # A complete-set take is two orders forming one position, so it cannot go
    # through the single-order path below.
    if is_complete_set_taker:
        return await _place_complete_set_take(
            chat_id, sig, client, risk, settings, legs, amount,
            market=market, fee_rate=fee_rate, slippage=slippage,
            max_valid=max_valid, min_order=min_order,
            target_margin=target_margin, equity=equity,
            free_cash=free_cash, max_exposure=max_exp,
        )

    # Every MAKER order goes through the quote path -- a one-leg quote and a
    # two-leg quote differ only in length. The single-order path below assumes
    # one order means one immediate fill means one position, and a resting
    # quote is none of those.
    if is_maker:
        return await _place_maker_quote(
            chat_id, sig, client, risk, settings, legs, leg_amounts,
            market=market, equity=equity, max_exp=max_exp,
        )

    execution_price = f"cap={limit_price:.3f}" if limit_price is not None else f"quote={quote_price:.3f}"
    log.info(
        f"[{chat_id}] PLACING {sig.strategy} {sig.asset} {sig.timeframe} "
        f"{sig.outcome} | {order_type}/{time_in_force} "
        f"₦{amount:,.0f} @ {execution_price} (sig={sig.market_price:.3f}) | cert={sig.certainty:.0%}"
    )
    # An execution the operator cannot see coming is indistinguishable from no
    # execution at all until it fills. Say it is being sent, then report what
    # the exchange did with it.
    await _notify_order_sent(
        chat_id, sig, amount=amount,
        price=limit_price if limit_price is not None else quote_price,
        engine=engine,
    )

    try:
        t_order_start = time.time()
        resp  = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=sig.outcome_id, side="BUY",
            amount=amount, order_type=order_type,
            price=limit_price,
            max_slippage=slippage,
            currency=CURRENCY,
            time_in_force=time_in_force,
            post_only=post_only,
            stp_mode="CANCEL_OLDEST" if is_maker else "SKIP",
        )
        rtt_ms = (time.time() - t_order_start) * 1000.0
        if not isinstance(resp, dict):
            _stall_skip(chat_id, sig, "order_unconfirmed_fill", "non-object API response")
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, "unknown", "non-object API response"
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return
        order = resp.get("order") or resp.get("clobOrder") or resp.get("ammOrder") or resp
        if not isinstance(order, dict):
            _stall_skip(chat_id, sig, "order_unconfirmed_fill", "malformed order object")
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, "unknown", "malformed order object"
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return

        try:
            parsed_shares = _finite_float(client.parse_filled_shares(order) or 0.0)
        except Exception:
            parsed_shares = None
        malformed_fill_quantity = (
            parsed_shares is None or parsed_shares < 0.0
            or _order_has_malformed_fill_quantity(order)
        )
        shares_filled = max(0.0, parsed_shares) if parsed_shares is not None else 0.0
        filled_price, price_needs_review = _order_fill_price(order, quote_price)
        payload_needs_review = _order_has_malformed_financials(order)
        order_id = order.get("id") or order.get("orderId") or order.get("order_id")
        order_status = str(order.get("status") or "").lower()

        # ── FAK / Zero-Fill Reasoning (Order is a Request, Not a Result) ────────
        # If the order was killed, rejected, or cancelled with 0 shares filled,
        # it is a Zero-Fill. Do NOT manufacture a phantom position or deduct cash.
        if shares_filled <= 0:
            if (
                order_status in ("cancelled", "canceled", "killed", "rejected", "expired")
                and not malformed_fill_quantity
                and not _order_has_malformed_fill_quantity(order)
            ):
                _stall_skip(chat_id, sig, "zero_fill_fak_killed",
                            f"order={order_id} status={order_status or 'unknown'}")
                log.info(
                    f"[{chat_id}] ⚪ ZERO FILL (FAK killed/cancelled) | {sig.strategy} {sig.asset} "
                    f"order={order_id} status={order_status} rtt={rtt_ms:.0f}ms | liquidity moved away"
                )
                try:
                    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                    if app_to_use:
                        await telegram_bot.notify_unfilled(
                            app_to_use, chat_id, sig.strategy,
                            sig.asset, sig.timeframe,
                            sig.outcome, amount,
                        )
                except Exception as ne:
                    log.warning(f"notify_unfilled failed in executor: {ne}")
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return
            else:
                # Never manufacture a position from the requested amount. If
                # Bayse accepted an order but omitted fill confirmation, query
                # it once; otherwise fail closed and leave an explicit alert.
                if order_id:
                    try:
                        confirmed = await client.get_order(order_id)
                        confirmed_order = (
                            confirmed.get("order") or confirmed.get("clobOrder") or confirmed
                        ) if isinstance(confirmed, dict) else {}
                        if not isinstance(confirmed_order, dict):
                            confirmed_order = {}
                        try:
                            confirmed_shares = _finite_float(
                                client.parse_filled_shares(confirmed_order) or 0.0
                            )
                        except Exception:
                            confirmed_shares = None
                        malformed_fill_quantity = (
                            confirmed_shares is None or confirmed_shares < 0.0
                            or _order_has_malformed_fill_quantity(confirmed_order)
                        )
                        shares_filled = (
                            max(0.0, confirmed_shares)
                            if confirmed_shares is not None else 0.0
                        )
                        order_status = str(confirmed_order.get("status") or order_status).lower()
                        if shares_filled > 0.0:
                            order = confirmed_order
                            filled_price, confirmed_price_review = _order_fill_price(
                                confirmed_order, quote_price
                            )
                            price_needs_review = price_needs_review or confirmed_price_review
                            payload_needs_review = (
                                payload_needs_review
                                or _order_has_malformed_financials(confirmed_order)
                            )
                            order_status = str(order.get("status") or order_status).lower()
                    except Exception as confirm_error:
                        log.error(f"[{chat_id}] Could not confirm ambiguous order {order_id}: {confirm_error}")
                if (
                    shares_filled <= 0
                    and order_status in {
                        "cancelled", "canceled", "killed", "rejected", "expired",
                    }
                    and not malformed_fill_quantity
                    and not _order_has_malformed_fill_quantity(order)
                ):
                    _stall_skip(
                        chat_id, sig, "zero_fill_fak_killed",
                        f"order={order_id} status={order_status}",
                    )
                    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                    if app_to_use:
                        try:
                            await telegram_bot.notify_unfilled(
                                app_to_use, chat_id, sig.strategy,
                                sig.asset, sig.timeframe, sig.outcome, amount,
                            )
                        except Exception as notify_error:
                            log.warning(f"notify_unfilled failed in executor: {notify_error}")
                    _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                    return
                if shares_filled <= 0:
                    _stall_skip(
                        chat_id, sig, "order_unconfirmed_fill",
                        f"order={order_id} status={order_status}",
                    )
                    await _pause_for_order_reconciliation(
                        chat_id, settings, risk, sig, order_id,
                        f"no confirmed share quantity; status={order_status or 'unknown'}",
                    )
                    _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                    return

        # `amount` is the requested budget and can exceed the cost of a
        # partial FAK fill, so the debit is rebuilt from confirmed shares and
        # the fee the exchange reported for this fill. If the exchange's
        # financial fields are unusable, preserve the confirmed shares and
        # reserve the full requested budget until a human reconciles the fill.
        if filled_price is None:
            filled_price = signal_price
            price_needs_review = True
        payload_needs_review = (
            payload_needs_review or _order_has_malformed_financials(order)
        )
        actual_ngn = _order_actual_cost(order, shares_filled, filled_price)
        if not math.isfinite(actual_ngn) or actual_ngn <= 0.0:
            actual_ngn = amount
            payload_needs_review = True

        spot_vs_thresh = 0.0
        if market and market.get("threshold") and feeds.spot.get(sig.asset):
            spot_vs_thresh = (feeds.spot[sig.asset] - market["threshold"]) / market["threshold"]

        log.info(
            f"[{chat_id}] ✅ FILLED | {sig.strategy} {sig.asset} {sig.timeframe} "
            f"{sig.outcome} @ {filled_price:.4f} ₦{actual_ngn:,.0f} | order={order_id} "
            f"rtt={rtt_ms:.0f}ms (shares={shares_filled:.2f})"
        )
        # Exchange-confirmed: this is the only point where a taker becomes a
        # trade for the drought clock and for /why's NO_CONFIRMED_FILL check.
        # It is also a real ORDER PLACEMENT — without this, an account whose
        # takers all fill still reported 0 orders placed and /why mislabelled
        # it EXECUTION_BLOCKED.
        stall.note_order(chat_id, sig.strategy, placed=True, reason="filled")
        stall.note_trade(chat_id, market_id=sig.market_id)

    except Exception as e:
        err = str(e)
        _stall_skip(chat_id, sig, "order_rejected_by_exchange", err[:150])
        status_match = re.match(r"API\s+(\d{3})\b", err)
        definite_rejection = bool(
            status_match and int(status_match.group(1)) in {400, 401, 403, 404, 422}
        )
        if not definite_rejection:
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, "unknown",
                f"BUY request raised after dispatch; exchange outcome is ambiguous: {err[:140]}",
            )
        try:
            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
            if app_to_use:
                await telegram_bot.notify_order_rejected(
                    app_to_use, chat_id, sig.strategy, sig.asset,
                    sig.timeframe, sig.outcome, amount, err,
                )
        except Exception as ne:
            log.warning(f"notify_order_rejected failed in executor: {ne}")
        m = re.search(r'Minimum buy amount is [A-Z]+ ([\d,]+(?:\.\d+)?)', err)
        if m:
            market_min = float(m.group(1).replace(",", ""))
            _market_min_cache[sig.market_id] = market_min
            log.info(f"[{chat_id}] Market min ₦{market_min:,.0f} cached for {sig.market_id}")
        else:
            log.error(f"[{chat_id}] Order failed {sig.market_id}: {e}", exc_info=True)
        # Always set cooldown on failure — prevents an infinite tight retry
        # loop hammering the same broken market every tick (this was the
        # root cause of trades silently dying for hours with no visible error).
        _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
        return

    # The exchange has confirmed a real fill. Put it into the in-memory risk
    # book before any Telegram or database await so a slow/failing dependency
    # cannot leave live shares invisible to exits and exposure checks.
    position_key = (
        f"{sig.market_id}:{sig.outcome}:{order_id}"
        if is_hedge or sig.market_id in risk.open_positions
        else sig.market_id
    )
    risk.add_position(position_key, {
        "market_id": sig.market_id,
        "trade_id": None,
        "event_id": sig.event_id,
        "order_id": order_id,
        "outcome": sig.outcome,
        "outcome_id": sig.outcome_id,
        "entry_price": filled_price,
        "amount_ngn": actual_ngn,
        "filled_quantity": shares_filled,
        "confirmed_filled": True,
        "strategy": sig.strategy,
        "asset": sig.asset,
        "timeframe": sig.timeframe,
        "threshold": market.get("threshold") if market else getattr(sig, "threshold", None),
        "closing_date": market.get("closing_date") if market else "",
        "placed_at": time.time(),
    })
    risk.current_free_cash -= actual_ngn
    if malformed_fill_quantity:
        risk.open_positions[position_key]["exit_reconciliation_pending"] = True
        await _pause_for_order_reconciliation(
            chat_id, settings, risk, sig, order_id,
            "confirmed BUY response contains malformed or conflicting share quantities",
        )
    _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
    if payload_needs_review or price_needs_review:
        reasons = []
        if payload_needs_review:
            reasons.append("malformed/non-finite order financial fields")
        if price_needs_review:
            reasons.append("fill price reconstructed from the executable quote")
        await _notify_fill_reconciliation(chat_id, sig, order_id, "; ".join(reasons))

    # ── Notify FIRST — trade has happened on Bayse ────────────────────────
    # Always notify before DB write. If DB fails, user still knows about the trade.
    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    try:
        await telegram_bot.notify_trade(
            app_to_use, chat_id, sig, actual_ngn, engine=engine,
            fill_price=filled_price, shares=shares_filled,
        )
    except Exception as ne:
        log.error(f"[{chat_id}] Notification failed: {ne}")

    # ── Record in DB ──────────────────────────────────────────────────────
    # Sanitise all floats before writing — prevents PostgreSQL REAL underflow
    # from subnormal GARCH/Kalman values (e.g. 9.4e-64 crashes psycopg2).
    trade_id = None
    for db_attempt in range(3):
        try:
            trade_id = await asyncio.to_thread(
                database.record_trade,
                chat_id=chat_id,
                strategy=sig.strategy, asset=sig.asset, timeframe=sig.timeframe,
                outcome=sig.outcome, outcome_id=sig.outcome_id,
                market_id=sig.market_id, event_id=sig.event_id, order_id=order_id,
                entry_price=_safe_float(filled_price),
                amount_ngn=_safe_float(actual_ngn),
                certainty=_safe_float(sig.certainty),
                secs_to_close=_safe_float(market["secs_to_close"] if market else 0),
                spot_vs_threshold_pct=_safe_float(spot_vs_thresh),
                momentum_at_entry=_safe_float(getattr(sig, "momentum_at_entry", 0.0)),
                regime_at_entry=_safe_float(getattr(sig, "regime_at_entry", 0.0)),
                edge_at_entry=_safe_float(getattr(sig, "edge_at_entry", 0.0)),
                realized_vol_at_entry=_safe_float(getattr(sig, "realized_vol_at_entry", 0.0)),
                market_price_at_entry=_safe_float(sig.market_price),
                slippage_ngn=_safe_float(
                    ((filled_price / sig.market_price) - 1.0) * actual_ngn
                    if sig.market_price > 0 else 0
                ),
                engine=engine,
                filled_quantity=_safe_float(shares_filled),
            )
            break
        except Exception as db_err:
            if db_attempt < 2:
                await asyncio.sleep(0.5 * (db_attempt + 1))
                continue
            log.critical(
                f"[{chat_id}] DB record failed for {sig.asset} {sig.strategy}: {db_err}"
                f" — order={order_id} executed; retaining an in-memory position for exit protection"
            )

    if trade_id:
        risk.open_positions[position_key]["trade_id"] = trade_id
    else:
        risk.open_positions[position_key]["ledger_reconciliation_pending"] = True
        await _pause_for_order_reconciliation(
            chat_id, settings, risk, sig, order_id,
            "confirmed BUY fill has no durable database trade row",
        )


async def _place_maker_quote(
    chat_id: str, sig, client, risk, settings: dict,
    legs: list, leg_amounts: list, *, market: dict | None, equity: float,
    max_exp: float,
) -> None:
    """Place both legs of a two-sided MAKER quote and track them as one quote.

    Why sequential rather than batch: the response shape for per-order results
    in Bayse's batch endpoint is not something this code can verify, and an
    order that gets placed but never tracked is the worst outcome available --
    it holds wallet funds with no record, no requote and no cancel. Two
    well-understood calls beat one whose failure mode is an orphan.

    Sequential placement is safe *because of the strategy's design*, not in
    spite of it: each leg clears the directional EV gate on its own, so if the
    second leg fails after the first has filled we are left holding a trade we
    were willing to own anyway. We then cancel the first rather than leave a
    quote half-built -- a lone leg is a directional order nobody hedged.
    """
    placed: list[dict] = []

    for index, leg in enumerate(legs):
        # Each leg's own stake. A two-sided set must buy the SAME number of
        # shares on both sides, or the "lock" covers only the smaller quantity
        # and the remainder is an unhedged directional position -- which is
        # what paying the same naira for a cheaper side used to do (see
        # `_balanced_pair_amounts`).
        leg_amount = float(leg_amounts[index]) if index < len(leg_amounts) else float(leg_amounts[-1])
        try:
            resp = await client.place_order(
                event_id=sig.event_id, market_id=sig.market_id,
                outcome_id=leg.outcome_id, side="BUY",
                amount=leg_amount, order_type="LIMIT",
                price=leg.price, currency=CURRENCY,
                time_in_force="GTC", post_only=True,
                stp_mode="CANCEL_OLDEST",
            )
        except Exception as exc:
            await _unwind_maker_legs(
                chat_id, client, placed, sig,
                reason=f"placement failed: {exc}", risk=risk, settings=settings,
            )
            _stall_skip(chat_id, sig, "maker_place_failed", f"{leg.outcome}: {exc}"[:200])
            log.warning(f"[{chat_id}] MAKER {leg.outcome} placement failed: {exc}")
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, "unknown",
                f"MAKER {leg.outcome} request outcome is ambiguous: {str(exc)[:160]}",
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return

        order = (
            (resp.get("order") or resp.get("clobOrder") or resp)
            if isinstance(resp, dict) else {}
        )
        if not isinstance(order, dict):
            order = {}
        order_id = order.get("id") or order.get("orderId") or order.get("order_id")
        if not order_id:
            await _unwind_maker_legs(
                chat_id, client, placed, sig,
                reason=f"no order id for {leg.outcome}", risk=risk, settings=settings,
            )
            _stall_skip(chat_id, sig, "maker_place_no_order_id", leg.outcome)
            log.error(f"[{chat_id}] MAKER {leg.outcome} placed with no order id")
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, "unknown",
                f"MAKER {leg.outcome} response has no reconcilable order id",
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return

        # A post-only order can still cross in flight if the book moved between
        # our read and this call. Keep its live remainder under management too.
        try:
            filled = _finite_float(client.parse_filled_shares(order) or 0.0)
        except Exception:
            filled = None
        fill_status = str(order.get("status") or "").strip().lower()
        terminal_status = fill_status in {
            "filled", "completed", "cancelled", "canceled", "expired",
            "rejected", "killed",
        }
        if filled is None or filled < 0.0:
            _stall_skip(
                chat_id, sig, "maker_malformed_fill_quantity",
                f"{leg.outcome} order={order_id}",
            )
            placed.append({
                "outcome": leg.outcome,
                "outcome_id": leg.outcome_id,
                "order_id": order_id,
                "price": float(leg.price),
                "amount": leg_amount,
                "filled": 0.0,
                "order_active": True,
                "fill_status": fill_status,
                "fill_review": True,
            })
            await _unwind_maker_legs(
                chat_id, client, placed, sig,
                reason="malformed placement fill quantity",
                risk=risk, settings=settings,
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return
        fill_price, fill_price_review = _order_fill_price(order, leg.price)
        if fill_price is None:
            fill_price = float(leg.price)
            fill_price_review = True
        filled_cost = (
            filled * fill_price * config.CURRENCY_BASE_MULTIPLIER
            if filled > 0.0 else 0.0
        )
        if not math.isfinite(filled_cost) or (filled > 0.0 and filled_cost <= 0.0):
            filled_cost = leg_amount
            fill_price_review = True
        if terminal_status and filled <= 0.0:
            _stall_skip(chat_id, sig, "maker_zero_fill", f"{leg.outcome} status={fill_status}")
            await _unwind_maker_legs(
                chat_id, client, placed, sig,
                reason=f"{leg.outcome} ended without a fill",
                risk=risk, settings=settings,
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return
        placed.append({
            "outcome":       leg.outcome,
            "outcome_id":    leg.outcome_id,
            "order_id":      order_id,
            "price":         fill_price if filled > 0.0 else float(leg.price),
            # While any remainder is live the exchange can still reserve the
            # full stake. After a terminal full fill, track its actual debit.
            "amount":        filled_cost if terminal_status and filled > 0.0 else leg_amount,
            "filled":        filled,
            "order_active":  not terminal_status,
            "fill_status":   fill_status,
            "fill_review":   fill_price_review,
        })
        log.info(
            f"[{chat_id}] MAKER LEG PLACED | {sig.asset} {leg.outcome} "
            f"@ {leg.price:.3f} ₦{leg_amount:,.0f} | order={order_id}"
        )

    # Track the quote as a unit so requote and cancel always act on both legs.
    spot_now = feeds.spot.get(sig.asset) or 0.0
    _get_maker().track_quote(
        sig.market_id, placed, spot=spot_now,
        fv_yes=next((leg.fair_value for leg in legs if leg.outcome == "YES"), 0.0),
    )

    # A resting post-only quote is an order, NOT a trade: nothing has been
    # executed yet. Recording it as a trade reset the drought clock every time
    # MAKER re-quoted, so an account could go hours with zero fills and still
    # report HEALTHY. Confirmed fills call note_trade instead (bot.py).
    stall.note_order(chat_id, sig.strategy, placed=True, reason="clob_limit_resting")

    spot_vs_thresh = 0.0
    if market and market.get("threshold") and spot_now:
        spot_vs_thresh = (spot_now - market["threshold"]) / market["threshold"]

    tracked = 0
    for entry in placed:
        trade_id = await _record_maker_leg(
            chat_id, sig, client, risk, entry,
            market=market, spot_vs_thresh=spot_vs_thresh,
        )
        if trade_id:
            tracked += 1

    if tracked == 0:
        # Nothing recorded means nothing is being watched. An order that holds
        # wallet funds with no DB row and no risk entry is unreconcileable, so
        # withdraw the whole quote rather than leave it resting.
        log.error(f"[{chat_id}] MAKER quote placed but no leg recorded — cancelling all")
        await _unwind_maker_legs(
            chat_id, client, placed, sig, reason="no leg recorded",
            risk=risk, settings=settings,
        )
        return

    if tracked < len(placed):
        # A half-tracked quote is worse than no quote: one leg is managed, the
        # other is not. Withdraw it and re-quote cleanly next pass.
        log.error(
            f"[{chat_id}] MAKER quote only {tracked}/{len(placed)} legs recorded — "
            f"cancelling all"
        )
        await _unwind_maker_legs(
            chat_id, client, placed, sig, reason="partial tracking",
            risk=risk, settings=settings,
        )
        return

    _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()

    quote_amount = sum(float(entry["amount"]) for entry in placed)
    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    if app_to_use:
        try:
            await telegram_bot.notify_trade(
                app_to_use, chat_id, sig, quote_amount, engine="CLOB_LIMIT"
            )
        except Exception as ne:
            log.error(f"[{chat_id}] MAKER quote notification failed: {ne}")


async def _record_maker_leg(
    chat_id: str, sig, client, risk, entry: dict, *,
    market: dict | None, spot_vs_thresh: float,
) -> str | None:
    """Persist one resting leg and register it with the risk book."""
    trade_id = None
    for db_attempt in range(3):
        try:
            trade_id = await asyncio.to_thread(
                database.record_trade,
                chat_id=chat_id,
                strategy=sig.strategy, asset=sig.asset, timeframe=sig.timeframe,
                outcome=entry["outcome"], outcome_id=entry["outcome_id"],
                market_id=sig.market_id, event_id=sig.event_id,
                order_id=entry["order_id"],
                entry_price=_safe_float(entry["price"]),
                amount_ngn=_safe_float(entry["amount"]),
                certainty=_safe_float(sig.certainty),
                secs_to_close=_safe_float(market["secs_to_close"] if market else 0),
                spot_vs_threshold_pct=_safe_float(spot_vs_thresh),
                market_price_at_entry=_safe_float(entry["price"]),
                engine="CLOB_LIMIT",
                filled_quantity=_safe_float(entry.get("filled") or 0.0),
            )
            break
        except Exception as db_err:
            if db_attempt < 2:
                await asyncio.sleep(0.5 * (db_attempt + 1))
                continue
            log.error(f"[{chat_id}] MAKER DB record failed: {db_err}")
            return None
    if not trade_id:
        return None

    # Keys include outcome and order id: a single market can carry a filled
    # taker position AND two maker legs, and keying by market id alone let one
    # silently overwrite another, dropping it from exit management.
    leg_key = f"{sig.market_id}:{entry['outcome']}:{entry['order_id']}"
    filled = _finite_float(entry.get("filled") or 0.0)
    if filled is None or filled < 0.0:
        filled = 0.0
    is_filled = filled > 0.0
    risk.add_position(leg_key, {
        "market_id":       sig.market_id,
        "trade_id":        trade_id,
        "event_id":        sig.event_id,
        "order_id":        entry["order_id"],
        "outcome":         entry["outcome"],
        "outcome_id":      entry["outcome_id"],
        "entry_price":     entry["price"],
        "amount_ngn":      entry["amount"],
        "filled_quantity": filled,
        "confirmed_filled": is_filled,
        "order_active":    bool(entry.get("order_active", not is_filled)),
        "strategy":        sig.strategy,
        "asset":           sig.asset,
        "timeframe":       sig.timeframe,
        "threshold":       (market or {}).get("threshold"),
        "closing_date":    (market or {}).get("closing_date", ""),
        "placed_at":       time.time(),
        "quote_leg":       True,
    })
    amount = _finite_float(entry.get("amount"))
    if amount is None or amount < 0.0:
        risk.remove_position(leg_key)
        return None
    # Bayse reserves GTC order cash on acceptance. Debit once at placement;
    # later fill polling only changes the position classification and cost.
    risk.current_free_cash -= amount
    if is_filled:
        _get_maker().record_fill(sig.market_id, entry["outcome"], filled)
        stall.note_trade(chat_id, market_id=sig.market_id)
        if entry.get("fill_review"):
            await _notify_fill_reconciliation(
                chat_id, sig, entry.get("order_id"),
                "immediate MAKER fill price was reconstructed from its limit",
            )
    return trade_id


async def _unwind_maker_legs(
    chat_id: str, client, placed: list[dict], sig, *, reason: str,
    risk, settings: dict,
) -> None:
    """Cancel and reconcile every leg before discarding a MAKER quote.

    A failed placement/ledger write does not make a Bayse order disappear. Keep
    any fill or possibly-live remainder visible in the risk book, and pause if
    the exchange cannot prove the order is terminal.
    """
    terminal_statuses = {
        "filled", "completed", "cancelled", "canceled", "expired",
        "rejected", "killed",
    }
    cancel_attempted = False
    unresolved = False

    for entry in placed:
        order_id = entry.get("order_id")
        if not order_id:
            unresolved = True
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, "unknown",
                f"MAKER leg has no order id after {reason}",
            )
            continue

        try:
            await client.cancel_order(order_id)
            cancel_attempted = True
        except Exception as cancel_error:
            log.warning(
                f"[{chat_id}] MAKER cancel request for {order_id} failed; "
                f"checking exchange state: {cancel_error}"
            )
            cancel_attempted = True

        key = f"{sig.market_id}:{entry.get('outcome', '?')}:{order_id}"
        position = risk.open_positions.get(key)
        try:
            state = await client.get_order(order_id)
            if not isinstance(state, dict):
                raise ValueError("post-cancel order query returned a non-object")
            status = str(state.get("status") or "").strip().lower()
            try:
                exchange_shares = _finite_float(
                    client.parse_filled_shares(state) or 0.0
                )
            except Exception:
                exchange_shares = None
            initial_shares = _finite_float(entry.get("filled") or 0.0)
            if position is not None:
                position_shares = _finite_float(
                    position.get("filled_quantity") or 0.0
                )
                if position_shares is None or position_shares < 0.0:
                    raise ValueError("tracked MAKER fill quantity is malformed")
                initial_shares = max(initial_shares or 0.0, position_shares)
            if (
                exchange_shares is None or exchange_shares < 0.0
                or initial_shares is None or initial_shares < 0.0
            ):
                raise ValueError("post-cancel MAKER fill quantity is malformed")

            shares = max(initial_shares, exchange_shares)
            terminal = status in terminal_statuses
            if status in {"filled", "completed"} and shares <= 0.0:
                raise ValueError("exchange reports a filled order without shares")
            price, price_needs_review = _order_fill_price(
                state, entry.get("price")
            )
            if shares > 0.0 and price is None:
                raise ValueError("confirmed MAKER fill has no valid price")
            if price is None:
                price = _finite_float(entry.get("price"))
            if price is None or not 0.0 < price < 1.0:
                raise ValueError("MAKER order limit price is malformed")
            cost = shares * price * config.CURRENCY_BASE_MULTIPLIER
            if shares > 0.0 and (not math.isfinite(cost) or cost <= 0.0):
                raise ValueError("confirmed MAKER fill cost is malformed")
        except Exception as reconcile_error:
            unresolved = True
            # The placement response is still evidence of any immediate fill.
            # Retain that lower bound and the full accepted reservation; never
            # turn a query failure into an assumed zero-fill cancellation.
            known_shares = _finite_float(entry.get("filled") or 0.0)
            if known_shares is None or known_shares < 0.0:
                known_shares = 0.0
            known_price = _finite_float(entry.get("price"))
            if known_price is None or not 0.0 < known_price < 1.0:
                known_price = 0.5
            known_cost = known_shares * known_price * config.CURRENCY_BASE_MULTIPLIER
            if position is None:
                reserved = _finite_float(entry.get("amount"))
                reserved = reserved if reserved is not None and reserved >= 0.0 else 0.0
                position = {
                    "market_id": sig.market_id,
                    "trade_id": entry.get("trade_id"),
                    "event_id": sig.event_id,
                    "order_id": order_id,
                    "outcome": entry.get("outcome"),
                    "outcome_id": entry.get("outcome_id"),
                    "entry_price": known_price,
                    "amount_ngn": known_cost if known_shares > 0.0 else reserved,
                    "filled_quantity": known_shares,
                    "confirmed_filled": known_shares > 0.0,
                    "order_active": True,
                    "pending_cancel": True,
                    "strategy": sig.strategy,
                    "asset": sig.asset,
                    "timeframe": sig.timeframe,
                    "placed_at": time.time(),
                    "maker_ledger_missing": not bool(entry.get("trade_id")),
                    "ledger_reconciliation_pending": (
                        known_shares > 0.0 and not bool(entry.get("trade_id"))
                    ),
                }
                risk.add_position(key, position)
                current_cash = _finite_float(risk.current_free_cash)
                if current_cash is not None:
                    risk.current_free_cash = current_cash - reserved
                if known_shares > 0.0:
                    stall.note_trade(chat_id, market_id=sig.market_id)
                    _get_maker().record_fill(
                        sig.market_id, entry.get("outcome", ""), known_shares
                    )
            else:
                position["order_active"] = True
                position["pending_cancel"] = True
                position["maker_ledger_missing"] = not bool(position.get("trade_id"))
                position["ledger_reconciliation_pending"] = bool(
                    position.get("filled_quantity") and not position.get("trade_id")
                )
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, order_id,
                f"MAKER cancel/fill reconciliation failed after {reason}: "
                f"{str(reconcile_error)[:140]}",
            )
            continue

        if terminal and shares <= 0.0:
            if position is not None:
                trade_id = position.get("trade_id")
                if trade_id:
                    try:
                        await asyncio.to_thread(
                            database.resolve_trade, trade_id, None, 0.0
                        )
                    except Exception as db_error:
                        log.error(
                            f"[{chat_id}] cancelled MAKER row {trade_id} "
                            f"could not be settled: {db_error}"
                        )
                risk.remove_position(key)
            log.info(
                f"[{chat_id}] MAKER unwound {entry.get('outcome')} {order_id} "
                f"({reason}; confirmed zero fill)"
            )
            continue

        position_was_tracked = position is not None
        previous_shares = _finite_float(entry.get("filled") or 0.0) or 0.0
        if position_was_tracked:
            tracked_shares = _finite_float(
                position.get("filled_quantity") or 0.0
            ) or 0.0
            previous_shares = max(previous_shares, tracked_shares)
        newly_filled = (
            max(0.0, shares - previous_shares)
            if position_was_tracked else shares
        )
        if position is None:
            reserved = _finite_float(entry.get("amount"))
            if reserved is None or reserved < 0.0:
                unresolved = True
                await _pause_for_order_reconciliation(
                    chat_id, settings, risk, sig, order_id,
                    "MAKER accepted reservation is malformed",
                )
                continue
            position = {
                "market_id": sig.market_id,
                "trade_id": entry.get("trade_id"),
                "event_id": sig.event_id,
                "order_id": order_id,
                "outcome": entry.get("outcome"),
                "outcome_id": entry.get("outcome_id"),
                "entry_price": price,
                "amount_ngn": cost if shares > 0.0 else reserved,
                "filled_quantity": shares,
                "confirmed_filled": shares > 0.0,
                "order_active": not terminal,
                "strategy": sig.strategy,
                "asset": sig.asset,
                "timeframe": sig.timeframe,
                "placed_at": time.time(),
                "maker_ledger_missing": not bool(entry.get("trade_id")),
                "ledger_reconciliation_pending": (
                    shares > 0.0 and not bool(entry.get("trade_id"))
                ),
            }
            risk.add_position(key, position)
            current_cash = _finite_float(risk.current_free_cash)
            if current_cash is not None:
                risk.current_free_cash = current_cash - reserved
        else:
            position["entry_price"] = price
            position["amount_ngn"] = cost if shares > 0.0 else position.get("amount_ngn", 0.0)
            position["filled_quantity"] = shares
            position["confirmed_filled"] = shares > 0.0
            position["order_active"] = not terminal

        position["filled_quantity"] = shares
        position["confirmed_filled"] = shares > 0.0
        position["order_active"] = not terminal
        if terminal:
            position.pop("pending_cancel", None)
        else:
            position["pending_cancel"] = True
            unresolved = True

        trade_id = position.get("trade_id")
        pause_reasons = []
        if trade_id and shares > 0.0:
            try:
                await asyncio.to_thread(
                    database.update_trade_fill, trade_id, cost, shares, price
                )
            except Exception as db_error:
                log.critical(
                    f"[{chat_id}] MAKER fill {order_id} remains in memory; "
                    f"ledger update failed: {db_error}"
                )
                pause_reasons.append(
                    "confirmed MAKER fill could not be persisted to its ledger row"
                )
        elif shares > 0.0:
            position["maker_ledger_missing"] = True
            position["ledger_reconciliation_pending"] = True
            unresolved = True
            pause_reasons.append(
                "confirmed MAKER fill has no durable ledger trade row"
            )

        if newly_filled > 0.0:
            stall.note_trade(chat_id, market_id=sig.market_id)
            _get_maker().record_fill(sig.market_id, entry.get("outcome", ""), newly_filled)
        if price_needs_review and shares > 0.0:
            await _notify_fill_reconciliation(
                chat_id, sig, order_id,
                "MAKER unwind fill price reconstructed from posted limit",
            )
        if not terminal:
            pause_reasons.append(
                f"MAKER order remains live after cancel request ({reason})"
            )
        if pause_reasons:
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, order_id, "; ".join(pause_reasons),
            )
        if terminal:
            log.info(
                f"[{chat_id}] MAKER unwound {entry.get('outcome')} {order_id} "
                f"({reason}; terminal status={status}, shares={shares:.4f})"
            )

    if cancel_attempted:
        try:
            risk.current_free_cash = await client.get_balance_ngn()
        except Exception as balance_error:
            log.warning(
                f"[{chat_id}] free-cash refresh after MAKER unwind failed: "
                f"{balance_error}"
            )

    if placed and not unresolved:
        _get_maker().drop(sig.market_id)


async def _place_complete_set_take(
    chat_id: str, sig, client, risk, settings: dict,
    legs: list, amount: float, *, market: dict | None, fee_rate: float,
    slippage: float, max_valid: float, min_order: float,
    target_margin: float, equity: float, free_cash: float,
    max_exposure: float,
) -> None:
    """Take both outcomes of a CLOB market after revalidating the pair live.

    Each order is a FAK at a hard price cap. Both fresh books, both standalone
    EVs, the combined locked edge, available depth, and the full pair budget are
    checked before the first order is sent. A confirmed first-leg fill is
    persisted and put in the risk book before the second order is attempted, so
    a second-leg failure cannot leave an invisible exchange position.
    """
    cooldown_key = _cooldown_key(chat_id, sig.market_id, sig.strategy)
    if len(legs) != 2:
        _stall_skip(chat_id, sig, "complete_set_invalid_leg_count", str(len(legs)))
        _trade_cooldown[cooldown_key] = time.time()
        return

    outcome_ids = [str(getattr(leg, "outcome_id", "") or "") for leg in legs]
    if not all(outcome_ids) or len(set(outcome_ids)) != 2:
        _stall_skip(chat_id, sig, "complete_set_invalid_outcome_ids")
        _trade_cooldown[cooldown_key] = time.time()
        return

    # A burn consumes market-level outcome balances, not shares tagged by
    # strategy/order. Do not burn this set while another position or resting
    # order on the market is tracked; otherwise the TAKER rows could be removed
    # while another strategy's now-consumed shares remain in risk/DB.
    existing = [
        (key, pos) for key, pos in risk.open_positions.items()
        if (pos.get("market_id") or key) == sig.market_id
        or str(key).startswith(f"{sig.market_id}:")
    ]
    if existing:
        _stall_skip(
            chat_id, sig, "complete_set_market_has_existing_positions",
            f"{len(existing)} other market position/order(s) would share burn balances",
        )
        _trade_cooldown[cooldown_key] = time.time()
        return

    # A structural take is only as current as its less-visible leg. Read both
    # books in one request; a one-sided refresh can turn a lock into a loss.
    try:
        books = await asyncio.wait_for(
            client.get_orderbooks(outcome_ids, depth=5), timeout=2.5
        )
    except Exception as exc:
        _stall_skip(chat_id, sig, "complete_set_book_unavailable", str(exc)[:160])
        log.warning(f"[{chat_id}] complete-set fresh book read failed: {exc}")
        _trade_cooldown[cooldown_key] = time.time()
        return

    usable = {
        outcome_id: book
        for outcome_id, book in (books or {}).items()
        if isinstance(book, dict) and booklib.is_usable(book)
    }
    if any(outcome_id not in usable for outcome_id in outcome_ids):
        _stall_skip(
            chat_id, sig, "complete_set_book_unavailable",
            f"{len(outcome_ids) - len(usable)}/{len(outcome_ids)} outcome books unreadable",
        )
        _trade_cooldown[cooldown_key] = time.time()
        return
    stale = [outcome_id for outcome_id in outcome_ids if booklib.book_is_stale(usable[outcome_id])]
    if stale:
        _stall_skip(chat_id, sig, "complete_set_book_stale", f"{len(stale)} leg(s) stale")
        _trade_cooldown[cooldown_key] = time.time()
        return

    fresh_legs: list = []
    asks: list[float] = []
    effective_asks: list[float] = []
    for leg in legs:
        ask = booklib.best_ask(usable[leg.outcome_id])
        if ask is None:
            _stall_skip(chat_id, sig, "complete_set_no_ask", str(leg.outcome))
            _trade_cooldown[cooldown_key] = time.time()
            return
        try:
            fair = float(leg.fair_value)
            effective = booklib.effective_buy_price(ask, fee_rate, is_maker=False)
            ev = fair / effective - 1.0 if effective > 0 else float("-inf")
        except (TypeError, ValueError, ZeroDivisionError):
            fair, effective, ev = 0.0, float("inf"), float("-inf")
        if (
            effective < config.TAKER_MIN_EFFECTIVE_PRICE - 1e-9
            or effective > config.TAKER_MAX_EFFECTIVE_PRICE + 1e-9
            or ev + 1e-9 < target_margin
        ):
            _stall_skip(
                chat_id, sig, "complete_set_live_leg_ev_failed",
                f"{leg.outcome} ask={ask:.3f} effective={effective:.3f} "
                f"EV={ev:+.1%} target={target_margin:.1%}",
            )
            log.info(
                f"[{chat_id}] SKIP TAKER complete set — live {leg.outcome} "
                f"EV {ev:+.1%} < {target_margin:.1%} or price outside band"
            )
            _trade_cooldown[cooldown_key] = time.time()
            return
        fresh_legs.append(dataclasses.replace(leg, price=ask))
        asks.append(ask)
        effective_asks.append(effective)

    live_lock_edge = 1.0 - sum(effective_asks)
    if live_lock_edge + 1e-9 < config.COMPLETE_SET_TAKER_MIN_EDGE:
        _stall_skip(
            chat_id, sig, "complete_set_live_edge_below_floor",
            f"edge={live_lock_edge:+.4f} needs>={config.COMPLETE_SET_TAKER_MIN_EDGE:+.4f}",
        )
        _trade_cooldown[cooldown_key] = time.time()
        return

    # The limit itself must preserve both the per-leg EV floor and the
    # direction-independent lock. The exact asks are the safe fallback when a
    # slippage buffer would spend too much of the pair edge.
    limit_prices: list[float] = []
    for leg, ask in zip(fresh_legs, asks):
        ev_cap = _max_raw_buy_price_for_ev(
            leg.fair_value, fee_rate, target_margin,
            max_effective_price=config.TAKER_MAX_EFFECTIVE_PRICE,
            max_raw_price=min(max_valid, 0.99),
        )
        proposed = min(
            ask + max(0.012, ask * slippage), ev_cap, max_valid,
        )
        limit_price = _floor_price_tick(proposed)
        if limit_price + 1e-9 < ask:
            _stall_skip(
                chat_id, sig, "complete_set_ev_cap_below_ask",
                f"{leg.outcome} ask={ask:.3f} cap={limit_price:.3f}",
            )
            _trade_cooldown[cooldown_key] = time.time()
            return
        limit_prices.append(limit_price)

    def _caps_preserve_pair(prices: list[float]) -> bool:
        effective_caps = [
            booklib.effective_buy_price(price, fee_rate, is_maker=False)
            for price in prices
        ]
        per_leg_ok = all(
            (float(leg.fair_value) / effective - 1.0) + 1e-9 >= target_margin
            and effective <= config.TAKER_MAX_EFFECTIVE_PRICE + 1e-9
            for leg, effective in zip(fresh_legs, effective_caps)
        )
        pair_edge = 1.0 - sum(effective_caps)
        return per_leg_ok and pair_edge + 1e-9 >= config.COMPLETE_SET_TAKER_MIN_EDGE

    if not _caps_preserve_pair(limit_prices):
        # Both current asks have already passed the live standalone-EV and pair
        # edge checks. Matching exactly at those ticks preserves the lock while
        # still refusing any worse price if the book moves again.
        ask_caps = [_floor_price_tick(ask) for ask in asks]
        if (
            any(cap + 1e-9 < ask for cap, ask in zip(ask_caps, asks))
            or not _caps_preserve_pair(ask_caps)
        ):
            _stall_skip(chat_id, sig, "complete_set_caps_no_longer_lock")
            _trade_cooldown[cooldown_key] = time.time()
            return
        limit_prices = ask_caps

    # Bound each leg by the depth available at its actual limit. Use the thinner
    # side as the pair's budget, then rebalance at the fresh prices so both
    # orders still target the same net share count and both clear the exchange
    # minimum. This also means a depth clip cannot leave stale per-leg stakes.
    depth_budgets: list[float] = []
    for leg, cap in zip(fresh_legs, limit_prices):
        book = usable[leg.outcome_id]
        available = sum(
            booklib.level_notional(level)
            for level in (book.get("asks") or [])
            if (booklib.level_price(level) is not None)
            and booklib.level_price(level) <= cap + 1e-9
        )
        depth_budgets.append(max(0.0, available))
    depth_budget = min([float(amount), *depth_budgets])
    if depth_budget + 1e-9 < min_order:
        _stall_skip(
            chat_id, sig, "complete_set_depth_below_minimum",
            f"pair budget ₦{depth_budget:,.0f} < ₦{min_order:,.0f}/leg",
        )
        _trade_cooldown[cooldown_key] = time.time()
        return

    leg_amounts = _balanced_pair_amounts(
        fresh_legs, depth_budget, min_order=min_order,
        fee_rate=fee_rate, is_maker=False,
    )
    if leg_amounts is None:
        _stall_skip(
            chat_id, sig, "pair_below_order_minimum",
            f"fresh pair cannot clear ₦{min_order:,.0f}/order within ₦{depth_budget:,.0f}/leg",
        )
        _trade_cooldown[cooldown_key] = time.time()
        return
    committed = sum(leg_amounts)
    if committed > free_cash + 1e-6:
        _stall_skip(
            chat_id, sig, "complete_set_free_cash_cap",
            f"pair ₦{committed:,.0f} > free cash ₦{free_cash:,.0f}",
        )
        _trade_cooldown[cooldown_key] = time.time()
        return
    if not risk.can_trade(equity, committed, max_exposure):
        _stall_skip(
            chat_id, sig, "exposure_cap",
            f"filled ₦{risk.deployed_filled():,.0f} + ₦{committed:,.0f} > "
            f"{max_exposure:.0%} of ₦{equity:,.0f}",
        )
        _trade_cooldown[cooldown_key] = time.time()
        return

    log.info(
        f"[{chat_id}] PLACING TAKER COMPLETE SET {sig.asset} {sig.timeframe} "
        f"| asks={asks[0]:.3f}/{asks[1]:.3f} caps={limit_prices[0]:.3f}/{limit_prices[1]:.3f} "
        f"| budgets=₦{leg_amounts[0]:,.0f}/₦{leg_amounts[1]:,.0f} "
        f"| lock={live_lock_edge:+.1%}"
    )
    await _notify_order_sent(
        chat_id, sig, amount=committed, price=None, engine="CLOB",
        legs=fresh_legs,
    )

    placed: list[dict] = []
    failure_reason = ""
    ledger_record_failed = False
    spot_vs_thresh = 0.0
    spot_now = feeds.spot.get(sig.asset) or 0.0
    threshold = (market or {}).get("threshold")
    if threshold and spot_now:
        spot_vs_thresh = (spot_now - threshold) / threshold

    for index, (leg, leg_amount, cap) in enumerate(
        zip(fresh_legs, leg_amounts, limit_prices)
    ):
        leg_amount = float(leg_amount)
        try:
            resp = await client.place_order(
                event_id=sig.event_id, market_id=sig.market_id,
                outcome_id=leg.outcome_id, side="BUY",
                amount=leg_amount, order_type="LIMIT", price=cap,
                currency=CURRENCY, time_in_force="FAK",
                max_slippage=slippage, stp_mode="SKIP",
            )
        except Exception as exc:
            failure_reason = (
                f"{leg.outcome} order request errored; its fill is unconfirmed: "
                f"{str(exc)[:160]}"
            )
            _stall_skip(chat_id, sig, "complete_set_leg_failed", failure_reason)
            log.warning(f"[{chat_id}] complete-set {leg.outcome} leg failed: {exc}")
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, "unknown", failure_reason
            )
            break

        order = (
            (resp.get("order") or resp.get("clobOrder") or resp)
            if isinstance(resp, dict) else {}
        )
        if not isinstance(order, dict):
            order = {}
        order_id = order.get("id") or order.get("orderId") or order.get("order_id")
        order_status = str(order.get("status") or "").lower()
        try:
            shares = _finite_float(client.parse_filled_shares(order) or 0.0)
        except Exception:
            shares = 0.0
        if shares is None:
            shares = 0.0
        malformed_fill_quantity = _order_has_malformed_fill_quantity(order)

        if shares <= 0.0:
            # A FAK response without confirmed quantity is not a tracked
            # position. If the exchange returned a non-terminal/ambiguous
            # status, query once before reporting that no fill is confirmed.
            terminal_zero_fill = (
                order_status in {
                    "cancelled", "canceled", "killed", "rejected", "expired",
                }
                and not _order_has_malformed_fill_quantity(order)
            )
            if order_id and not terminal_zero_fill:
                try:
                    confirmed = await client.get_order(order_id)
                    confirmed_order = (
                        confirmed.get("order") or confirmed.get("clobOrder") or confirmed
                    ) if isinstance(confirmed, dict) else {}
                    if not isinstance(confirmed_order, dict):
                        confirmed_order = {}
                    confirmed_shares = _finite_float(
                        client.parse_filled_shares(confirmed_order) or 0.0
                    )
                    order_status = str(confirmed_order.get("status") or order_status).lower()
                    if confirmed_shares is not None and confirmed_shares > 0.0:
                        order = confirmed_order
                        shares = confirmed_shares
                        malformed_fill_quantity = _order_has_malformed_fill_quantity(order)
                except Exception as confirm_error:
                    log.error(
                        f"[{chat_id}] Could not confirm complete-set order {order_id}: "
                        f"{confirm_error}"
                    )
            if shares <= 0.0:
                terminal_zero_fill = (
                    order_status in {
                        "cancelled", "canceled", "killed", "rejected", "expired",
                    }
                    and not _order_has_malformed_fill_quantity(order)
                )
                failure_reason = (
                    f"{leg.outcome} @ {cap:.3f} has no exchange-confirmed fill "
                    f"(status={order_status or 'unknown'})"
                )
                _stall_skip(chat_id, sig, "complete_set_leg_zero_fill", failure_reason)
                log.info(f"[{chat_id}] {failure_reason}")
                if terminal_zero_fill:
                    if not placed:
                        app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                        try:
                            await telegram_bot.notify_unfilled(
                                app_to_use, chat_id, sig.strategy, sig.asset,
                                sig.timeframe, leg.outcome, leg_amount,
                            )
                        except Exception as notify_error:
                            log.warning(f"notify_unfilled failed: {notify_error}")
                else:
                    await _pause_for_order_reconciliation(
                        chat_id, settings, risk, sig, order_id or "unknown",
                        failure_reason,
                    )
                break

        fill_price, price_needs_review = _order_fill_price(order, asks[index])
        payload_needs_review = _order_has_malformed_financials(order)
        if fill_price is None:
            fill_price = asks[index]
            price_needs_review = True
        actual_cost = _order_actual_cost(order, shares, fill_price)
        if not math.isfinite(actual_cost) or actual_cost <= 0.0:
            # The fill is real, but its cost fields are not. Reserve the order's
            # requested budget rather than understate capital at risk.
            actual_cost = leg_amount
            payload_needs_review = True
        tracking_id = str(order_id or f"local-{uuid.uuid4().hex}")
        entry = {
            "outcome": leg.outcome,
            "outcome_id": leg.outcome_id,
            "order_id": order_id,
            "tracking_id": f"{sig.market_id}:{leg.outcome}:{tracking_id}",
            "price": fill_price,
            "signal_price": leg.price,
            "shares": shares,
            "amount": actual_cost,
            "requested_amount": leg_amount,
            "fill_reconciliation_pending": malformed_fill_quantity,
        }
        placed.append(entry)
        stall.note_order(chat_id, sig.strategy, placed=True, reason="filled")
        stall.note_trade(chat_id, market_id=sig.market_id)

        # Add the exchange-confirmed position to risk synchronously before any
        # network notification or database await. A slow Telegram API must not
        # leave a real fill temporarily invisible to entry/exit controls.
        risk_key = entry["tracking_id"]
        risk.add_position(risk_key, {
            "market_id": sig.market_id,
            "trade_id": None,
            "event_id": sig.event_id,
            "order_id": order_id,
            "outcome": leg.outcome,
            "outcome_id": leg.outcome_id,
            "entry_price": fill_price,
            "amount_ngn": actual_cost,
            "filled_quantity": shares,
            "confirmed_filled": True,
            "strategy": sig.strategy,
            "asset": sig.asset,
            "timeframe": sig.timeframe,
            "threshold": (market or {}).get("threshold"),
            "closing_date": (market or {}).get("closing_date", ""),
            "placed_at": time.time(),
            "complete_set_leg": True,
            "exit_reconciliation_pending": malformed_fill_quantity,
        })
        risk.current_free_cash -= actual_cost
        if malformed_fill_quantity:
            failure_reason = (
                f"{leg.outcome} fill has malformed/conflicting quantity fields; "
                "no additional leg or burn will be attempted"
            )
            for pair_entry in placed:
                tracked_position = risk.open_positions.get(pair_entry["tracking_id"])
                if tracked_position is not None:
                    tracked_position["exit_reconciliation_pending"] = True
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, order_id,
                failure_reason,
            )
        if risk.current_free_cash < -1e-6:
            log.critical(
                f"[{chat_id}] TAKER complete-set actual debit exceeded tracked free cash by "
                f"₦{abs(risk.current_free_cash):,.2f}"
            )
        if payload_needs_review or price_needs_review:
            reasons = []
            if payload_needs_review:
                reasons.append("malformed/non-finite order financial fields")
            if price_needs_review:
                reasons.append("fill price reconstructed from the executable ask")
            await _notify_fill_reconciliation(
                chat_id, sig, order_id, "; ".join(reasons)
            )

        pair_matched = (
            share_quantities_match(placed[0]["shares"], shares)
            if len(placed) == 2 else None
        )
        app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
        try:
            await telegram_bot.notify_taker_fill(
                app_to_use, chat_id, sig.asset, sig.timeframe, leg.outcome,
                shares, fill_price, actual_cost, complete_set=True,
                leg_number=len(placed), pair_matched=pair_matched,
            )
        except Exception as notify_error:
            log.error(f"[{chat_id}] TAKER fill notification failed: {notify_error}")

        trade_id = None
        for db_attempt in range(3):
            try:
                trade_id = await asyncio.to_thread(
                    database.record_trade,
                    chat_id=chat_id,
                    strategy=sig.strategy, asset=sig.asset, timeframe=sig.timeframe,
                    outcome=leg.outcome, outcome_id=leg.outcome_id,
                    market_id=sig.market_id, event_id=sig.event_id,
                    order_id=order_id,
                    entry_price=_safe_float(fill_price),
                    amount_ngn=_safe_float(actual_cost),
                    certainty=_safe_float(sig.certainty),
                    secs_to_close=_safe_float((market or {}).get("secs_to_close", 0)),
                    spot_vs_threshold_pct=_safe_float(spot_vs_thresh),
                    momentum_at_entry=_safe_float(getattr(sig, "momentum_at_entry", 0.0)),
                    regime_at_entry=_safe_float(getattr(sig, "regime_at_entry", 0.0)),
                    edge_at_entry=_safe_float(getattr(sig, "edge_at_entry", 0.0)),
                    realized_vol_at_entry=_safe_float(
                        getattr(sig, "realized_vol_at_entry", 0.0)
                    ),
                    market_price_at_entry=_safe_float(leg.price),
                    slippage_ngn=_safe_float(
                        ((fill_price / leg.price) - 1.0) * actual_cost
                        if leg.price > 0 else 0.0
                    ),
                    engine="CLOB",
                    filled_quantity=_safe_float(shares),
                )
                break
            except Exception as db_err:
                if db_attempt < 2:
                    await asyncio.sleep(0.5 * (db_attempt + 1))
                    continue
                log.critical(
                    f"[{chat_id}] complete-set DB record failed for {order_id}: {db_err}; "
                    "the confirmed position remains tracked in memory"
                )

        if trade_id:
            risk.open_positions[risk_key]["trade_id"] = trade_id
        else:
            for pair_entry in placed:
                tracked_position = risk.open_positions.get(pair_entry["tracking_id"])
                if tracked_position is not None:
                    tracked_position["ledger_reconciliation_pending"] = True
            risk.open_positions[risk_key]["ledger_reconciliation_pending"] = True
            ledger_record_failed = True
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, order_id,
                "confirmed complete-set BUY fill has no durable database trade row",
            )
        entry["trade_id"] = trade_id
        if ledger_record_failed or malformed_fill_quantity:
            break
        log.info(
            f"[{chat_id}] COMPLETE-SET LEG FILLED | {sig.asset} {leg.outcome} "
            f"{shares:.4f}sh @ {fill_price:.3f} ₦{actual_cost:,.0f} "
            f"| order={order_id or 'unknown'}"
        )

    if ledger_record_failed:
        _trade_cooldown[cooldown_key] = time.time()
        return

    if any(entry.get("fill_reconciliation_pending") for entry in placed):
        _trade_cooldown[cooldown_key] = time.time()
        return

    if len(placed) < len(legs):
        if placed:
            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
            try:
                await telegram_bot.notify_taker_pair_partial(
                    app_to_use, chat_id, sig.asset, sig.timeframe,
                    placed, failure_reason,
                )
            except Exception as notify_error:
                log.error(f"[{chat_id}] TAKER partial-set notification failed: {notify_error}")
        _trade_cooldown[cooldown_key] = time.time()
        return

    # The burn consumes the matched quantity and the current database/risk rows
    # are whole-fill records. Do not burn an imbalanced pair and then discard
    # the excess shares: leave both fills tracked until they can be managed or
    # resolved without losing quantity.
    if not share_quantities_match(placed[0]["shares"], placed[1]["shares"]):
        imbalance = (
            f"complete-set fills are imbalanced "
            f"({placed[0]['shares']:.4f} vs {placed[1]['shares']:.4f} shares); "
            "both positions remain tracked and were not burned"
        )
        log.critical(f"[{chat_id}] {imbalance}")
        await _pause_for_order_reconciliation(
            chat_id, settings, risk, sig,
            ",".join(str(entry.get("order_id") or "unknown") for entry in placed),
            imbalance,
        )
        _trade_cooldown[cooldown_key] = time.time()
        return

    sets = min(entry["shares"] for entry in placed)
    try:
        resp = await client.burn_shares(sig.market_id, sets, CURRENCY)
        proceeds, payout_needs_review, payout_reason = _complete_set_payout(resp, sets)
        if proceeds is None:
            for entry in placed:
                position = risk.open_positions.get(entry["tracking_id"])
                if position is not None:
                    position["burn_reconciliation_pending"] = True
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, f"burn:{sig.market_id}", payout_reason
            )
            _trade_cooldown[cooldown_key] = time.time()
            return
        if payout_needs_review:
            await _notify_fill_reconciliation(
                chat_id, sig, f"burn:{sig.market_id}", payout_reason
            )

        costs = [_finite_float(entry.get("amount")) for entry in placed]
        current_cash = _finite_float(risk.current_free_cash)
        if (
            any(cost is None or cost <= 0.0 for cost in costs)
            or current_cash is None
        ):
            for entry in placed:
                position = risk.open_positions.get(entry["tracking_id"])
                if position is not None:
                    position["burn_reconciliation_pending"] = True
            await _pause_for_order_reconciliation(
                chat_id, settings, risk, sig, f"burn:{sig.market_id}",
                "burn succeeded but tracked cost/free-cash values are malformed",
            )
            _trade_cooldown[cooldown_key] = time.time()
            return

        total_cost = sum(costs)
        pnl = proceeds - total_cost
        if not math.isfinite(total_cost) or total_cost <= 0.0 or not math.isfinite(pnl):
            raise ValueError("complete-set cost or realized PnL is invalid")
        risk.current_free_cash = current_cash + proceeds
        risk.add_pnl(pnl)
        for entry in placed:
            risk.remove_position(entry["tracking_id"])
        for entry, leg_cost in zip(placed, costs):
            if entry.get("trade_id"):
                try:
                    leg_pnl = proceeds / len(placed) - leg_cost
                    await asyncio.to_thread(
                        database.resolve_trade, entry["trade_id"], leg_pnl >= 0.0, leg_pnl
                    )
                except Exception as db_err:
                    log.error(f"[{chat_id}] burn reconciliation failed: {db_err}")
        log.info(
            f"[{chat_id}] COMPLETE SET LOCKED | {sig.asset} {sig.timeframe} "
            f"{sets:.4f} sets cost ₦{total_cost:,.0f} → ₦{proceeds:,.0f} "
            f"| PnL ₦{pnl:+,.0f}"
        )
        app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
        try:
            await telegram_bot.notify_set_burned(
                app_to_use, chat_id, sig.asset, sig.timeframe,
                sets, total_cost, proceeds, pnl, structural=True,
            )
        except Exception as notify_error:
            log.warning(f"[{chat_id}] complete-set burn notification failed: {notify_error}")
    except Exception as exc:
        for entry in placed:
            position = risk.open_positions.get(entry["tracking_id"])
            if position is not None:
                position["burn_reconciliation_pending"] = True
        await _pause_for_order_reconciliation(
            chat_id, settings, risk, sig, f"burn:{sig.market_id}",
            f"burn request outcome is ambiguous: {exc}",
        )
        log.error(
            f"[{chat_id}] complete-set burn failed on {sig.market_id}: {exc}; "
            "both legs remain tracked pending manual reconciliation"
        )

    _trade_cooldown[cooldown_key] = time.time()


async def _notify_order_sent(
    chat_id: str, sig, *, amount: float,
    price: float | None = None, engine: str = "", legs: list | None = None,
) -> None:
    """Tell the operator an order is on its way to the exchange.

    Fires before the order is sent, so the message describes an *execution
    attempt* rather than a result -- the fill, resting or zero-fill notice
    still follows from wherever the order actually lands. Best-effort by
    design: a Telegram failure must never delay or block a live order.
    """
    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    if not app_to_use:
        return
    try:
        await telegram_bot.notify_executing(
            app_to_use, chat_id, sig.strategy, sig.asset,
            getattr(sig, "timeframe", ""), getattr(sig, "outcome", ""),
            amount, price=price, engine=engine, legs=legs,
        )
    except Exception as exc:
        log.warning(f"[{chat_id}] execution notification failed: {exc}")


async def _pause_for_order_reconciliation(
    chat_id: str, settings: dict, risk, sig, order_id, reason: str
) -> None:
    """Pause the account until an ambiguous exchange order is reconciled."""
    risk.paused = True
    if hasattr(risk, "pause_reason"):
        risk.pause_reason = "order_reconciliation"
    settings["paused"] = True
    settings["paused_reason"] = "order_reconciliation"
    try:
        await asyncio.to_thread(database.update_settings, chat_id, settings)
    except Exception as exc:
        log.critical(f"[{chat_id}] Could not persist reconciliation pause: {exc}")
    message = (
        f"🚨 Trading paused: order {order_id or 'unknown'} for "
        f"{getattr(sig, 'asset', '?')} needs manual reconciliation. "
        f"Verify the Bayse orders/portfolio before resuming. Reason: {reason[:160]}"
    )
    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    if app_to_use:
        try:
            await telegram_bot.send_message(app_to_use, chat_id, message)
        except Exception as exc:
            log.error(f"[{chat_id}] reconciliation-pause alert failed: {exc}")
    log.critical(f"[{chat_id}] {message}")


async def _notify_fill_reconciliation(chat_id: str, sig, order_id, reason: str) -> None:
    """Alert when an exchange-confirmed fill required estimated accounting."""
    message = (
        f"🚨 Fill reconciliation required for order {order_id or 'unknown'} "
        f"({getattr(sig, 'asset', '?')} {getattr(sig, 'strategy', '?')}). "
        f"The fill remains tracked conservatively; verify shares and cash debit "
        f"in Bayse before restarting. Reason: {reason[:180]}"
    )
    log.critical(f"[{chat_id}] {message}")
    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    if app_to_use:
        try:
            await telegram_bot.send_message(app_to_use, chat_id, message)
        except Exception as exc:
            log.error(f"[{chat_id}] fill-reconciliation alert failed: {exc}")


def _get_market_fee(market_id: str) -> float:
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    raw_fee = market.get("fee_rate", 0.02) if market else 0.02
    fee_rate = _finite_float(raw_fee)
    if fee_rate is None or not 0.0 <= fee_rate < 1.0:
        return math.nan
    return fee_rate
