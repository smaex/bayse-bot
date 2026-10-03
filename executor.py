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

def _sell_proceeds_for_shares(shares: float, price: float, fee_rate: float) -> float:
    """Conservative currency amount to request when liquidating shares.

    Bayse SELL `amount` means desired currency proceeds, not share quantity.
    """
    gross = shares * price * config.CURRENCY_BASE_MULTIPLIER
    return max(0.0, gross * (1.0 - _effective_fee(fee_rate, price)) * 0.98)


def _safe_float(val, default: float = 0.0) -> float:
    """
    Clamp to PostgreSQL REAL range before any DB write.

    Python's float64 can represent values like 9.4e-64 (subnormal for REAL).
    PostgreSQL REAL minimum is ~1.18e-38 and maximum ~3.4e38.
    Subnormal values crash psycopg2 with NumericValueOutOfRange.
    """
    if val is None or not math.isfinite(val):
        return default
    if val != 0.0 and abs(val) < 1e-37:
        return 0.0          # subnormal → zero (safe for REAL)
    if abs(val) > 3.4e38:
        return default      # overflow → default
    return float(val)


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
    reducing fees without guessing from a configured fee rate.
    """
    quantity = float(quote.get("quantity") or 0.0)
    if quantity <= 0:
        return 0.0
    amount = float(quote.get("amount") or 0.0)
    if amount <= 0:
        amount = float(quote.get("costOfShares") or 0.0)
        amount += float(quote.get("fee") or 0.0)
    multiplier = float(
        quote.get("currencyBaseMultiplier")
        or config.CURRENCY_BASE_MULTIPLIER
    )
    return amount / (quantity * multiplier) if amount > 0 else 0.0


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
        if sig.certainty >= 0.90:   tier = 2.0
        elif sig.certainty >= 0.70: tier = 1.5
        elif sig.certainty >= 0.55: tier = 1.0
        else:                       tier = 0.5
        fx_factor = 0.5 if sig.asset in _FX_ASSETS else 1.0
        raw_pct   = user_risk * tier * mult * fx_factor

    if sig.certainty >= 0.95 and kelly_pct == 0.0:
        raw_pct *= 1.5

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
    is_maker = (sig.strategy == "MAKER")
    legs     = sig.ensure_legs()
    mode      = settings.get("mode", "balanced")
    min_t     = settings.get("mintrade", MIN_TRADE_NGN)
    max_t     = settings.get("maxtrade", 5_000)
    max_exp   = min(
        settings.get("maxexposure", 20.0) / 100.0,
        config.MAX_PORTFOLIO_EXPOSURE,
    )
    learned   = settings.get("learned", {})
    mult      = _performance_size_multiplier(learned, sig)
    user_risk = min(
        settings.get("risk_pct", 2.0) / 100.0,
        config.MAX_TRADE_RISK,
    )
    declared  = None
    engine    = "AMM"
    quote_price = float(sig.market_price)
    target_margin = {
        "safe": 0.03, "balanced": 0.01, "aggressive": 0.00,
        "full_send": 0.00, "custom": 0.01,
    }.get(mode, 0.01)

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
        # If the account has sufficient free cash and bankroll for the exchange minimum order (e.g. ₦100),
        # clamp to effective_min so smaller test balances (e.g. ₦1,000–₦4,999) can place ₦100 orders.
        if free_cash >= effective_min and equity >= 500.0:
            log.info(
                f"[{chat_id}] CLAMP TO MINIMUM {sig.strategy} {sig.asset} — risk budget ₦{hard_cap:,.0f} "
                f"bumped to exchange minimum ₦{effective_min:,.0f} (equity=₦{equity:,.0f})"
            )
            amount = effective_min
        else:
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
    else:
        amount = min(equity * final_pct, hard_cap)
        if amount < effective_min:
            if free_cash >= effective_min and equity >= 500.0:
                amount = effective_min
            else:
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

    # ── Engine detection ───────────────────────────────────────────────────
    market   = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    declared = market.get("engine") if market else None
    if declared:
        engine = declared
    elif market:
        engine = await _infer_engine(client, market)
    else:
        engine = "AMM"

    # ── EV check with pre-trade Quote ──────────────────────────────────────
    target_margin = {
        "safe": 0.03, "balanced": 0.01, "aggressive": 0.00,
        "full_send": 0.00, "custom": 0.01,
    }.get(mode, 0.01)

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
            q_price = float(quote.get("price") or sig.market_price)
            q_qty = float(quote.get("quantity") or 0)
            if quote.get("completeFill") is not True or q_qty <= 0:
                _stall_skip(chat_id, sig, "quote_not_complete_fill")
                log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — quote does not confirm a complete fill")
                return
            if quote.get("tradeGoesOverMaxLiability") is True:
                _stall_skip(chat_id, sig, "quote_over_max_liability")
                log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — quote exceeds market liability")
                return

            quote_price = _quote_effective_buy_price(quote)
            if quote_price <= 0:
                # Compatibility fallback for old quote payloads that omit
                # amount/cost but still expose a quoted marginal price.
                fee_rate = _get_market_fee(sig.market_id)
                quote_price = _clob_buy_effective_price(q_price, fee_rate)

            if quote_price <= 0:
                log.warning(f"[{chat_id}] Invalid quote price {quote_price} returned. Skipping EV calculation.")
                return
            ev = sig.win_prob / quote_price - 1.0

            if ev < target_margin:
                log.info(f"[{chat_id}] EV {ev:+.1%} too low at size ₦{amount:,.0f} (price={quote_price:.3f}). Scaling down...")
                scaled_success = False
                for scale in [0.5, 0.25]:
                    scaled_amount = max(MIN_TRADE_NGN, round(amount * scale, -2))
                    if scaled_amount <= MIN_TRADE_NGN or scaled_amount >= amount:
                        scaled_amount = MIN_TRADE_NGN

                    try:
                        scaled_quote = await client.get_quote(
                            event_id=sig.event_id, market_id=sig.market_id,
                            outcome_id=sig.outcome_id, side="BUY", amount=scaled_amount,
                            currency=CURRENCY
                        )
                        if (
                            scaled_quote.get("completeFill") is not True
                            or scaled_quote.get("tradeGoesOverMaxLiability") is True
                        ):
                            continue
                        sq_price = float(scaled_quote.get("price") or sig.market_price)
                        sq_qty = float(scaled_quote.get("quantity") or 0)
                        if sq_qty <= 0:
                            continue
                        scaled_price = _quote_effective_buy_price(
                            scaled_quote
                        )
                        if scaled_price <= 0:
                            fee_rate = _get_market_fee(sig.market_id)
                            scaled_price = _clob_buy_effective_price(
                                sq_price, fee_rate
                            )

                        scaled_ev = sig.win_prob / scaled_price - 1.0
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
        if not is_probe and ev < target_margin:
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

    if not risk.can_trade(equity, amount, max_exp):
        _stall_skip(
            chat_id, sig, "exposure_cap",
            f"filled ₦{risk.deployed_filled():,.0f} + ₦{amount:,.0f} > "
            f"{max_exp:.0%} of ₦{equity:,.0f} (resting ₦{risk.deployed_resting():,.0f} excluded)",
        )
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — exposure cap "
            f"(filled=₦{risk.deployed_filled():,.0f}, +₦{amount:,.0f} > "
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
        existing = sum(
            1 for key, p in risk.open_positions.items()
            if p.get("strategy") == "MAKER"
            and (p.get("market_id", key) == sig.market_id
                 or str(key).startswith(f"{sig.market_id}:"))
        )
        if existing:
            _stall_skip(chat_id, sig, "maker_quote_already_resting",
                        f"{existing} MAKER leg(s) already resting on {sig.market_id}")
            log.info(
                f"[{chat_id}] SKIP MAKER {sig.asset} — a quote is already resting on "
                f"{sig.market_id}"
            )
            return
        # Maker capital budget. Resting quotes reserve wallet funds and a
        # one-sided fill carries real directional risk, so the maker book gets
        # its own ceiling instead of borrowing the directional one.
        maker_notional = sum(
            float(p.get("amount_ngn") or 0.0)
            for p in risk.open_positions.values()
            if p.get("strategy") == "MAKER"
        )
        if maker_notional + amount > equity * config.MAX_MAKER_NOTIONAL_PCT:
            _stall_skip(
                chat_id, sig, "maker_notional_cap",
                f"₦{maker_notional:,.0f} + ₦{amount:,.0f} > "
                f"{config.MAX_MAKER_NOTIONAL_PCT:.0%} of ₦{equity:,.0f}",
            )
            log.info(
                f"[{chat_id}] SKIP MAKER {sig.asset} — maker notional "
                f"₦{maker_notional:,.0f} + ₦{amount:,.0f} exceeds the "
                f"{config.MAX_MAKER_NOTIONAL_PCT:.0%} budget"
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

    # ── Market-specific minimum ───────────────────────────────────────────
    cached_min = _market_min_cache.get(sig.market_id, 0.0)
    if cached_min > 0 and amount < cached_min:
        if cached_min <= hard_cap:
            log.info(
                f"[{chat_id}] BUMP {sig.strategy} {sig.asset} order ₦{amount:,.0f} → "
                f"₦{cached_min:,.0f} (Bayse market minimum)"
            )
            amount = cached_min
        else:
            _stall_skip(chat_id, sig, "market_minimum_exceeds_budget",
                        f"market min ₦{cached_min:,.0f} > max_trade ₦{max_t:,.0f} / free_cash ₦{free_cash:,.0f}")
            log.info(
                f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — market min ₦{cached_min:,.0f} "
                f"exceeds max_trade(₦{max_t:,.0f}) or free_cash(₦{free_cash:,.0f})"
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
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

    elif engine == "CLOB":
        # ── CLOB Taker Execution: Price to match real Order Book Asks ────────
        # On a CLOB, buying into a book requires crossing the spread to the lowest ask.
        # Theoretical midpoint bidding (sig.market_price) always lands below the ask and
        # causes 100% zero-fill FAK cancellations.
        time_in_force = "FAK"
        # Exact CLOB BUY cap: taker fees reduce shares, so effective price is
        # p/(1-fee_fraction). Never impose a minimum cap that can exceed EV.
        fee_fraction = _effective_fee(fee_rate, sig.market_price)
        dynamic_cap = (
            sig.win_prob * (1.0 - fee_fraction) / (1.0 + target_margin)
        )
        strategy_cap = 0.75
        cap = min(strategy_cap, dynamic_cap, max_valid)
        if cap <= 0.01:
            _stall_skip(chat_id, sig, "clob_ev_cap_below_floor",
                        f"cap={cap:.3f} from win_prob={sig.win_prob:.3f} price={sig.market_price:.3f}")
            return

        limit_price = round(
            min(
                sig.market_price + max(0.012, sig.market_price * slippage),
                cap,
            ),
            3,
        )
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
            asks = ob.get("asks", [])
            if not asks:
                _stall_skip(chat_id, sig, "clob_no_asks")
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"CLOB orderbook has no asks (zero liquidity)"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return

            best_ask = float(asks[0]["price"])
            if best_ask > cap:
                _stall_skip(chat_id, sig, "clob_ask_above_cap",
                            f"best_ask={best_ask:.3f} cap={cap:.3f}")
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"best ask {best_ask:.3f} > cap {cap:.3f}"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return

            # Verify EV against the real fill price on the book. CLOB BUY fees
            # reduce shares received, so use p/(1-fee_fraction), not p*(1+fee).
            effective_ask = _clob_buy_effective_price(best_ask, fee_rate)
            ev_at_ask = sig.win_prob / effective_ask - 1.0
            if ev_at_ask < target_margin:
                _stall_skip(chat_id, sig, "clob_ev_at_ask_below_target",
                            f"best_ask={best_ask:.3f} EV={ev_at_ask:+.1%} target={target_margin:.0%}")
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"best ask {best_ask:.3f} EV {ev_at_ask:+.1%} < target {target_margin:.0%}"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                return

            # Price to match the resting ask with 1-2 ticks slippage buffer (up to cap)
            limit_price = round(min(best_ask + 0.003, cap, max_valid), 3)
            log.info(
                f"[{chat_id}] CLOB Book Match: best_ask={best_ask:.3f} -> limit_price={limit_price:.3f} "
                f"(EV={ev_at_ask:+.1%})"
            )

            # ── Depth-Aware Sizing (Zero-Fill Eliminator) ─────────────────────
            avail_ngn = 0.0
            for ask_entry in asks:
                ask_p = float(ask_entry.get("price", 0))
                if ask_p <= limit_price:
                    tot = float(ask_entry.get("total") or 0.0)
                    if tot <= 0:
                        tot = float(ask_entry.get("quantity", 0)) * ask_p * (100.0 if CURRENCY == "NGN" else 1.0)
                    avail_ngn += tot
                else:
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

    # A complete-set take is two orders forming one position, so it cannot go
    # through the single-order path below.
    if len(legs) > 1 and not is_maker:
        return await _place_complete_set_take(
            chat_id, sig, client, risk, settings, legs, amount,
            market=market, fee_rate=fee_rate, slippage=slippage,
            max_valid=max_valid,
        )

    # Every MAKER order goes through the quote path -- a one-leg quote and a
    # two-leg quote differ only in length. The single-order path below assumes
    # one order means one immediate fill means one position, and a resting
    # quote is none of those.
    if is_maker:
        return await _place_maker_quote(
            chat_id, sig, client, risk, settings, legs, amount,
            market=market, equity=equity, max_exp=max_exp,
        )

    execution_price = f"cap={limit_price:.3f}" if limit_price is not None else f"quote={quote_price:.3f}"
    log.info(
        f"[{chat_id}] PLACING {sig.strategy} {sig.asset} {sig.timeframe} "
        f"{sig.outcome} | {order_type}/{time_in_force} "
        f"₦{amount:,.0f} @ {execution_price} (sig={sig.market_price:.3f}) | cert={sig.certainty:.0%}"
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
        order = resp.get("order") or resp.get("clobOrder") or resp.get("ammOrder") or resp

        shares_filled = client.parse_filled_shares(order)
        filled_price  = float(order.get("avgFillPrice") or order.get("price") or quote_price)
        order_id      = order.get("id") or order.get("orderId") or order.get("order_id")
        order_status  = str(order.get("status") or "").lower()

        # ── FAK / Zero-Fill Reasoning (Order is a Request, Not a Result) ────────
        # If the order was killed, rejected, or cancelled with 0 shares filled,
        # it is a Zero-Fill. Do NOT manufacture a phantom position or deduct cash.
        if shares_filled <= 0:
            if order_status in ("cancelled", "killed", "rejected", "expired") or time_in_force == "FAK":
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
                        shares_filled = client.parse_filled_shares(confirmed)
                        if shares_filled > 0:
                            order = confirmed
                            filled_price = float(
                                confirmed.get("avgFillPrice")
                                or confirmed.get("price")
                                or filled_price
                            )
                    except Exception as confirm_error:
                        log.error(f"[{chat_id}] Could not confirm ambiguous order {order_id}: {confirm_error}")
                if shares_filled <= 0:
                    log.critical(
                        f"[{chat_id}] AMBIGUOUS ORDER {order_id} — accepted without a confirmed fill; "
                        "new entries on this market are cooling down for manual reconciliation"
                    )
                    _stall_skip(chat_id, sig, "order_unconfirmed_fill",
                                f"order={order_id} status={order_status}")
                    try:
                        app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                        if app_to_use:
                            await telegram_bot.notify_order_unconfirmed(
                                app_to_use, chat_id, sig.strategy, sig.asset,
                                sig.timeframe, sig.outcome, amount, order_id,
                            )
                    except Exception as ne:
                        log.warning(f"notify_order_unconfirmed failed in executor: {ne}")
                    _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
                    return

        fee_paid = float(order.get("fee") or 0.0)
        total_cost = order.get("totalCost")
        share_cost = order.get("costOfShares") or order.get("cost")
        if total_cost is not None:
            actual_ngn = float(total_cost)
        elif share_cost is not None:
            actual_ngn = float(share_cost) + fee_paid
        else:
            # `amount` is the requested budget and can exceed the cost of a
            # partial FAK fill. Reconstruct cost only from confirmed shares.
            actual_ngn = (
                shares_filled * filled_price * config.CURRENCY_BASE_MULTIPLIER
                + fee_paid
            )

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

    # ── Notify FIRST — trade has happened on Bayse ────────────────────────
    # Always notify before DB write. If DB fails, user still knows about the trade.
    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    if app_to_use:
        try:
            await telegram_bot.notify_trade(
                app_to_use, chat_id, sig, actual_ngn, engine=engine
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
            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
            if app_to_use:
                try:
                    await telegram_bot.send_message(
                        app_to_use, chat_id,
                        f"🚨 Trade {order_id} executed but the database write failed after retries. "
                        "The bot is tracking it in memory; check the Bayse portfolio before restarting.",
                    )
                except Exception:
                    pass

    position_key = (
        f"{sig.market_id}:{sig.outcome}:{order_id}"
        if is_hedge or sig.market_id in risk.open_positions
        else sig.market_id
    )
    risk.add_position(position_key, {
        "market_id":   sig.market_id,
        "trade_id":    trade_id,    "event_id":   sig.event_id,
        "order_id":    order_id,
        "outcome":     sig.outcome, "outcome_id": sig.outcome_id,
        "entry_price": filled_price, "amount_ngn": actual_ngn,
        "filled_quantity": shares_filled, "confirmed_filled": True,
        "strategy":    sig.strategy, "asset":      sig.asset,
        "timeframe":   sig.timeframe,
        "threshold":   market.get("threshold") if market else getattr(sig, "threshold", None),
        "closing_date": market.get("closing_date") if market else "",
        "placed_at":   time.time(),
    })
    risk.current_free_cash -= actual_ngn
    _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()


async def _place_maker_quote(
    chat_id: str, sig, client, risk, settings: dict,
    legs: list, amount: float, *, market: dict | None, equity: float,
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

    for leg in legs:
        try:
            resp = await client.place_order(
                event_id=sig.event_id, market_id=sig.market_id,
                outcome_id=leg.outcome_id, side="BUY",
                amount=amount, order_type="LIMIT",
                price=leg.price, currency=CURRENCY,
                time_in_force="GTC", post_only=True,
                stp_mode="CANCEL_OLDEST",
            )
        except Exception as exc:
            await _unwind_maker_legs(chat_id, client, placed, sig,
                                     reason=f"placement failed: {exc}")
            _stall_skip(chat_id, sig, "maker_place_failed", f"{leg.outcome}: {exc}"[:200])
            log.warning(f"[{chat_id}] MAKER {leg.outcome} placement failed: {exc}")
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return

        order = resp.get("order") or resp.get("clobOrder") or resp
        order_id = order.get("id") or order.get("orderId") or order.get("order_id")
        if not order_id:
            await _unwind_maker_legs(chat_id, client, placed, sig,
                                     reason=f"no order id for {leg.outcome}")
            _stall_skip(chat_id, sig, "maker_place_no_order_id", leg.outcome)
            log.error(f"[{chat_id}] MAKER {leg.outcome} placed with no order id")
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return

        # A post-only order can still cross in flight if the book moved between
        # our read and this call. Treat any immediate fill as a real fill.
        filled = client.parse_filled_shares(order)
        placed.append({
            "outcome":    leg.outcome,
            "outcome_id": leg.outcome_id,
            "order_id":   order_id,
            "price":      float(leg.price),
            "amount":     float(amount),
            "filled":     filled,
        })
        log.info(
            f"[{chat_id}] MAKER LEG PLACED | {sig.asset} {leg.outcome} "
            f"@ {leg.price:.3f} ₦{amount:,.0f} | order={order_id}"
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
        await _unwind_maker_legs(chat_id, client, placed, sig, reason="no leg recorded")
        return

    if tracked < len(placed):
        # A half-tracked quote is worse than no quote: one leg is managed, the
        # other is not. Withdraw it and re-quote cleanly next pass.
        log.error(
            f"[{chat_id}] MAKER quote only {tracked}/{len(placed)} legs recorded — "
            f"cancelling all"
        )
        await _unwind_maker_legs(chat_id, client, placed, sig, reason="partial tracking")
        return

    _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()

    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    if app_to_use:
        try:
            await telegram_bot.notify_trade(
                app_to_use, chat_id, sig, amount * len(placed), engine="CLOB_LIMIT"
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
    is_filled = float(entry.get("filled") or 0.0) > 0
    risk.add_position(leg_key, {
        "market_id":       sig.market_id,
        "trade_id":        trade_id,
        "event_id":        sig.event_id,
        "order_id":        entry["order_id"],
        "outcome":         entry["outcome"],
        "outcome_id":      entry["outcome_id"],
        "entry_price":     entry["price"],
        "amount_ngn":      entry["amount"],
        "filled_quantity": float(entry.get("filled") or 0.0),
        "confirmed_filled": is_filled,
        "strategy":        sig.strategy,
        "asset":           sig.asset,
        "timeframe":       sig.timeframe,
        "threshold":       (market or {}).get("threshold"),
        "closing_date":    (market or {}).get("closing_date", ""),
        "placed_at":       time.time(),
        "quote_leg":       True,
    })
    if is_filled:
        risk.current_free_cash -= float(entry["amount"])
        _get_maker().record_fill(sig.market_id, entry["outcome"], entry["filled"])
    return trade_id


async def _unwind_maker_legs(
    chat_id: str, client, placed: list[dict], sig, *, reason: str
) -> None:
    """Withdraw every leg of a partially-placed quote.

    Cancelling one leg and leaving the other is how a market maker acquires an
    unintended position: the survivor is now a one-sided bet nobody is
    hedging. All or nothing, every time.
    """
    for entry in placed:
        try:
            await client.cancel_order(entry["order_id"])
            log.info(
                f"[{chat_id}] MAKER unwound {entry['outcome']} {entry['order_id']} ({reason})"
            )
        except Exception as exc:
            log.critical(
                f"[{chat_id}] MAKER LEG {entry['order_id']} ({entry['outcome']}) "
                f"could not be cancelled after {reason}: {exc}"
            )
    if placed:
        _get_maker().drop(sig.market_id)


async def _place_complete_set_take(
    chat_id: str, sig, client, risk, settings: dict,
    legs: list, amount: float, *, market: dict | None, fee_rate: float,
    slippage: float, max_valid: float,
) -> None:
    """Take both outcomes of a market whose asks sum below one.

    A complete set settles to exactly 1.00, so paying less than 1.00 for it is
    a locked profit with no forecast involved. Both legs are crossed with FAK
    orders at the ask plus a buffer.

    Placement is sequential, and that is safe *because of the strategy's
    design*: each leg cleared the directional EV gate on its own before this
    was ever signalled, so a partial fill leaves us holding a trade we were
    happy to own -- not a half of something that only worked as a pair. The
    completed set is upside, not a requirement.

    If the second leg fails, we do not sell the first in a panic: it is an
    EV-positive position, it is tracked, and the exit policy manages it.
    """
    placed: list[dict] = []
    total_cost = 0.0

    for leg in legs:
        # Cross the ask with a small buffer, capped so a gap in the book
        # cannot turn a locked spread into an overpay.
        cap = min(leg.price + max(0.012, leg.price * slippage), max_valid)
        try:
            resp = await client.place_order(
                event_id=sig.event_id, market_id=sig.market_id,
                outcome_id=leg.outcome_id, side="BUY",
                amount=amount, order_type="LIMIT", price=cap,
                currency=CURRENCY, time_in_force="FAK",
                max_slippage=slippage, stp_mode="SKIP",
            )
        except Exception as exc:
            _stall_skip(chat_id, sig, "complete_set_leg_failed",
                        f"{leg.outcome}: {exc}"[:200])
            log.warning(f"[{chat_id}] complete-set {leg.outcome} leg failed: {exc}")
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return

        order = resp.get("order") or resp.get("clobOrder") or resp
        shares = client.parse_filled_shares(order)
        if shares <= 0:
            _stall_skip(chat_id, sig, "complete_set_leg_zero_fill",
                        f"{leg.outcome} @ {cap:.3f} did not fill")
            log.info(
                f"[{chat_id}] complete-set {leg.outcome} leg did not fill "
                f"@{cap:.3f}"
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()
            return

        fill_price = float(order.get("avgFillPrice") or order.get("price") or cap)
        cost = shares * fill_price * config.CURRENCY_BASE_MULTIPLIER
        placed.append({
            "outcome":    leg.outcome,
            "outcome_id": leg.outcome_id,
            "order_id":   order.get("id") or order.get("orderId") or order.get("order_id"),
            "price":      fill_price,
            "shares":     shares,
            "amount":     cost,
        })
        total_cost += cost
        log.info(
            f"[{chat_id}] COMPLETE-SET LEG | {sig.asset} {leg.outcome} "
            f"{shares:.2f}sh @ {fill_price:.3f} ₦{cost:,.0f}"
        )

    if len(placed) < 2:
        return

    stall.note_trade(chat_id, market_id=sig.market_id)

    spot_vs_thresh = 0.0
    spot_now = feeds.spot.get(sig.asset) or 0.0
    if market and market.get("threshold") and spot_now:
        spot_vs_thresh = (spot_now - market["threshold"]) / market["threshold"]

    for entry in placed:
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
                secs_to_close=_safe_float((market or {}).get("secs_to_close", 0)),
                spot_vs_threshold_pct=_safe_float(spot_vs_thresh),
                market_price_at_entry=_safe_float(entry["price"]),
                engine="CLOB",
                filled_quantity=_safe_float(entry["shares"]),
            )
        except Exception as db_err:
            log.error(f"[{chat_id}] complete-set DB record failed: {db_err}")
            trade_id = None

        leg_key = f"{sig.market_id}:{entry['outcome']}:{entry['order_id']}"
        risk.add_position(leg_key, {
            "market_id":        sig.market_id,
            "trade_id":         trade_id,
            "event_id":         sig.event_id,
            "order_id":         entry["order_id"],
            "outcome":          entry["outcome"],
            "outcome_id":       entry["outcome_id"],
            "entry_price":      entry["price"],
            "amount_ngn":       entry["amount"],
            "filled_quantity":  entry["shares"],
            "confirmed_filled": True,
            "strategy":         sig.strategy,
            "asset":            sig.asset,
            "timeframe":        sig.timeframe,
            "threshold":        (market or {}).get("threshold"),
            "closing_date":     (market or {}).get("closing_date", ""),
            "placed_at":        time.time(),
            "complete_set_leg": True,
        })

    risk.current_free_cash -= total_cost

    # Burn immediately: a complete set held is a complete set at risk of
    # nothing, but a set burned is cash, and cash does not need managing.
    sets = min(entry["shares"] for entry in placed)
    if sets > 0:
        try:
            resp = await client.burn_shares(sig.market_id, sets, CURRENCY)
            proceeds = float(
                resp.get("amount") or resp.get("proceeds")
                or (sets * 1.0 * config.CURRENCY_BASE_MULTIPLIER)
            )
            pnl = proceeds - total_cost
            risk.current_free_cash += proceeds
            risk.add_pnl(pnl)
            # Capture the trade ids before the legs leave the risk book:
            # looking them up afterwards finds nothing and the rows would stay
            # unresolved forever.
            trade_ids = [
                (risk.open_positions.get(
                    f"{sig.market_id}:{entry['outcome']}:{entry['order_id']}"
                ) or {}).get("trade_id")
                for entry in placed
            ]
            for entry in placed:
                risk.remove_position(
                    f"{sig.market_id}:{entry['outcome']}:{entry['order_id']}"
                )
            for tid in trade_ids:
                if tid:
                    try:
                        await asyncio.to_thread(database.resolve_trade, tid, True, pnl / 2.0)
                    except Exception as db_err:
                        log.error(f"[{chat_id}] burn reconciliation failed: {db_err}")
            log.info(
                f"[{chat_id}] COMPLETE SET LOCKED | {sig.asset} {sig.timeframe} "
                f"{sets:.2f} sets cost ₦{total_cost:,.0f} → ₦{proceeds:,.0f} "
                f"| PnL ₦{pnl:+,.0f}"
            )
            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
            if app_to_use:
                try:
                    await telegram_bot.notify_set_burned(
                        app_to_use, chat_id, sig.asset, sig.timeframe,
                        sets, total_cost, proceeds, pnl, structural=True,
                    )
                except Exception:
                    pass
        except Exception as exc:
            # Burning is the payoff, but failing to burn is not a loss: the set
            # still settles to 1.00. Say so and let the exit policy manage it.
            log.error(
                f"[{chat_id}] complete-set burn failed on {sig.market_id}: {exc} "
                f"— the set still settles to 1.00; managing it as a position"
            )

    _trade_cooldown[_cooldown_key(chat_id, sig.market_id, sig.strategy)] = time.time()


def _get_market_fee(market_id: str) -> float:
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    return market.get("fee_rate", 0.02) if market else 0.02
