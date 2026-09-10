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
import telegram_bot
import strategy
from config import ARB_MAX_SIZE_NGN, CURRENCY, MIN_PAYOUT_RATIO, FEE_FLOOR

log = logging.getLogger("executor")

active_markets: list[dict] = []
_tg_app = None

_FX_ASSETS = ["EURUSD", "GBPUSD", "XAUUSD"]
_market_engine_cache: dict[str, str] = {}
_market_min_cache:    dict[str, float] = {}
_trade_cooldown:      dict[tuple[str, str], float] = {}
TRADE_COOLDOWN_SEC = 60
MIN_TRADE_NGN      = 100.0


def _cooldown_key(chat_id: str, market_id: str) -> tuple[str, str]:
    # A market-wide key caused user A's order to silence every other user for
    # 60 seconds in this multi-user service.
    return str(chat_id), market_id

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
        return
    if strategy.global_state.systemic_halt_until > time.time():
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — systemic halt active")
        return
    is_hedge = "PAIR_HEDGE" in getattr(sig, "reason", "")
    if risk.already_in(sig.market_id, asset=sig.asset, is_hedge=is_hedge):
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — already in/pending on {sig.market_id}")
        return
    last = _trade_cooldown.get(_cooldown_key(chat_id, sig.market_id), 0.0)
    remaining = TRADE_COOLDOWN_SEC - (time.time() - last)
    if remaining > 0:
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


async def _execute_logic(
    chat_id: str, sig, client, risk, settings: dict,
    equity: float, free_cash: float, *, is_hedge: bool = False,
):
    is_oracle_arb = (sig.strategy == "ORACLE_ARB")
    is_maker      = (sig.strategy == "MAKER")
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
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — strict mode (near daily target), certainty {sig.certainty:.0%} < 40%")
        return

    # ── Sizing (Kelly / Conviction) ────────────────────────────────────────
    kelly_pct = getattr(sig, "size_pct", 0.0)
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

    # ── Hard risk budget ────────────────────────────────────────────────
    # `risk_pct` is a ceiling, not a suggestion. Previously Kelly-sized signals
    # bypassed it, and the ₦100 platform minimum could force a 20% bet on a
    # ₦500 account. If the minimum order does not fit the risk budget, skip.
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
        "safe": 0.01,
        "balanced": 0.02,
        "aggressive": 0.03,
        "full_send": 0.05,
        "custom": 0.05,
    }.get(mode, 0.02)
    allowed_pct = min(user_risk, mode_cap_pct)
    final_pct = min(raw_pct, allowed_pct)

    market_meta = next(
        (m for m in active_markets if m.get("market_id") == sig.market_id), None
    )
    market_min = float((market_meta or {}).get("minimum_order_amount") or MIN_TRADE_NGN)
    effective_min = max(MIN_TRADE_NGN, float(min_t), market_min)
    effective_max = max(0.0, float(max_t))
    hard_cap = min(equity * allowed_pct, effective_max, free_cash)

    if hard_cap < effective_min:
        min_equity = effective_min / allowed_pct if allowed_pct > 0 else float("inf")
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — platform/user minimum "
            f"₦{effective_min:,.0f} exceeds the {allowed_pct:.1%} risk budget "
            f"₦{hard_cap:,.0f} (equity needed ≈ ₦{min_equity:,.0f})"
        )
        return

    amount = min(equity * final_pct, hard_cap)
    if amount < effective_min:
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

    # ── ORACLE_ARB: skip EV and price ceiling — certainty is the gate ──────
    if is_oracle_arb:
        log.info(
            f"[{chat_id}] ORACLE_ARB FAST PATH | {sig.asset} {sig.outcome} "
            f"certainty={sig.certainty:.0%} entry_price={sig.market_price:.3f} ₦{amount:,.0f}"
        )
        # Fall through directly to order placement
    else:
        # ── Engine detection ────────────────────────────────────────────────
        market   = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
        declared = market.get("engine") if market else None
        if declared:
            engine = declared
        elif market:
            engine = await _infer_engine(client, market)
        else:
            engine = "AMM"

        # ── EV check with pre-trade Quote ───────────────────────────────────
        target_margin = {
            "safe": 0.03, "balanced": 0.01, "aggressive": 0.00,
            "full_send": 0.00, "custom": 0.01,
        }.get(mode, 0.01)

        is_probe = sig.certainty < sig.mode_floor
        if is_probe:
            # Live-money exploration turns uncertainty into losses. Shadow
            # tracking can collect calibration data without placing an order.
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
                    log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — quote does not confirm a complete fill")
                    return
                if quote.get("tradeGoesOverMaxLiability") is True:
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
                        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — no profitable size (quoted_price={quote_price:.3f} EV={ev:.2%})")
                        return
            except Exception as e:
                # A stale displayed price is not an executable price. Trading
                # through a quote outage converts unknown slippage into risk.
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
                log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — worst-case EV {ev:+.1%} < {target_margin:.0%} (worst_price={worst_case_p:.3f})")
                return

        # ── Global entry price ceiling ───────────────────────────────────────
        # DATA-DRIVEN (Audit Aug 13-23 2026): entries >= 0.80 → -₦314 net loss at 57% win rate.
        # Buying at 0.85 means: +₦7 net on a WIN (after fees), -₦100 on a LOSS.
        # You need a 93%+ win rate just to break even. That never happens.
        # Hard cap at 0.75 — SNIPE_MAX_MARKET_PRICE is now 0.65, so this is a
        # final backstop that catches any unexpected rounding or overrides.
        if quote_price > 0.75:
            log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — price {quote_price:.3f} > 0.75 ceiling (fee drag kills EV above 0.75)")
            return

    # ── Shared exposure check ──────────────────────────────────────────────
    market   = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not is_oracle_arb:
        # Engine may not be set if we went the oracle_arb path
        if not market:
            engine = "AMM"
        elif not declared:
            engine = await _infer_engine(client, market)
    engine = str(engine or "AMM").upper()

    # ORACLE_ARB used to bypass the quote and EV gates entirely. It is still a
    # probabilistic trade before close, so verify the executable AMM price.
    if is_oracle_arb and engine == "AMM":
        try:
            quote = await client.get_quote(
                sig.event_id, sig.market_id, sig.outcome_id,
                "BUY", amount, CURRENCY,
            )
            if quote.get("completeFill") is False or quote.get("tradeGoesOverMaxLiability") is True:
                return
            quote_price = (
                _quote_effective_buy_price(quote)
                or float(quote.get("price") or sig.market_price)
            )
            oracle_cap = min(0.75, sig.win_prob / 1.03)
            if quote_price > oracle_cap:
                log.info(
                    f"[{chat_id}] SKIP ORACLE_ARB {sig.asset} — executable price "
                    f"{quote_price:.3f} > cap {oracle_cap:.3f}"
                )
                return
        except Exception as exc:
            # A latency trade without a fresh quote is not safe.
            log.warning(f"[{chat_id}] SKIP ORACLE_ARB {sig.asset} — quote failed: {exc}")
            return

    if not risk.can_trade(equity, amount, max_exp):
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — exposure cap "
            f"(deployed=₦{risk.deployed():,.0f}, +₦{amount:,.0f} > {max_exp:.0%} of ₦{equity:,.0f})"
        )
        return

    # ── Correlated crypto exposure cap ─────────────────────────────────────
    if not is_oracle_arb and risk.has_correlated_open_position(sig.asset, sig.outcome, sig.timeframe, certainty=sig.certainty):
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
            log.info(
                f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — market min ₦{cached_min:,.0f} "
                f"exceeds max_trade(₦{max_t:,.0f}) or free_cash(₦{free_cash:,.0f})"
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
            return

    # ── Strict Price-Capped Execution ─────────────────────────────────────
    # MAKER: places a passive LIMIT GTC order to capture the spread.
    # All taker strategies (SNIPE, ORACLE_ARB, CORRELATE, FRONTRUN): use LIMIT FAK
    # (Fill-And-Kill / IOC) with a strict price ceiling passed directly to the exchange.
    # This physically FORBIDS the exchange from ever filling orders at 0.990 or 0.830!
    fee_rate      = _get_market_fee(sig.market_id)
    slip_map      = {"safe": 0.008, "balanced": 0.015, "aggressive": 0.020, "full_send": 0.025, "custom": 0.015}
    slippage      = slip_map.get(mode, 0.015)
    max_valid     = 0.99
    order_type    = "LIMIT" if engine == "CLOB" else "MARKET"
    post_only     = False

    if is_maker:
        if engine != "CLOB":
            log.info(f"[{chat_id}] SKIP MAKER {sig.asset} — passive orders require CLOB")
            return
        time_in_force = "GTC"
        limit_price   = sig.market_price
        post_only     = True  # never accidentally cross and pay taker fees
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
        strategy_cap = (
            config.SNIPE_MAX_MARKET_PRICE
            if sig.strategy == "SNIPE" else 0.75
        )
        cap = min(strategy_cap, dynamic_cap, max_valid)
        if cap <= 0.01:
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
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — "
                    "CLOB orderbook timestamp is stale or invalid"
                )
                _trade_cooldown[
                    _cooldown_key(chat_id, sig.market_id)
                ] = time.time()
                return
            asks = ob.get("asks", [])
            if not asks:
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"CLOB orderbook has no asks (zero liquidity)"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
                return

            best_ask = float(asks[0]["price"])
            if best_ask > cap:
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"best ask {best_ask:.3f} > cap {cap:.3f}"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
                return

            # Verify EV against the real fill price on the book. CLOB BUY fees
            # reduce shares received, so use p/(1-fee_fraction), not p*(1+fee).
            effective_ask = _clob_buy_effective_price(best_ask, fee_rate)
            ev_at_ask = sig.win_prob / effective_ask - 1.0
            if ev_at_ask < target_margin:
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"best ask {best_ask:.3f} EV {ev_at_ask:+.1%} < target {target_margin:.0%}"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
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
                log.info(
                    f"[{chat_id}] SKIP {sig.strategy} {sig.asset} {sig.outcome} — "
                    f"insufficient resting depth (available ₦{avail_ngn:,.0f} < min ₦{min_trade_req:,.0f})"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
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
            # A displayed midpoint is not executable liquidity. Never place a
            # taker order when the CLOB book cannot be verified.
            log.warning(
                f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — "
                f"CLOB orderbook unavailable: {obe}"
            )
            _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
            return
    else:
        # AMM markets execute immediately and do not have a resting book.
        # Bayse documents MARKET/FAK for this engine; LIMIT/FAK caused rejects.
        time_in_force = "FAK"
        limit_price = None

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

        # For LIMIT (MAKER) orders, the order is placed as a passive bid.
        # Track it for requoting and record as pending.
        if is_maker:
            order_id = order.get("id") or order.get("orderId") or order.get("order_id")
            if order_id:
                import feeds_direct
                binance_now, _ = feeds_direct.get_direct_price(sig.asset)
                _get_maker().track_order(
                    market_id    = sig.market_id,
                    order_id     = order_id,
                    placed_price = limit_price,
                    binance_price= binance_now,
                    amount       = amount,
                    outcome_id   = sig.outcome_id,
                    asset        = sig.asset,
                )
                log.info(
                    f"[{chat_id}] MAKER LIMIT PLACED | {sig.asset} {sig.outcome} "
                    f"@ {limit_price:.3f} ₦{amount:,.0f} | order={order_id} | rtt={rtt_ms:.0f}ms"
                )
                if _tg_app:
                    try:
                        await telegram_bot.notify_trade(
                            _tg_app, chat_id, sig, amount, engine="CLOB_LIMIT"
                        )
                    except Exception as ne:
                        log.error(f"[{chat_id}] Limit order notification failed: {ne}")

                # Record limit order in DB and risk manager so resolution_monitor tracks settlement & sends WIN/LOSS notifications
                spot_vs_thresh = 0.0
                if market and market.get("threshold") and feeds.spot.get(sig.asset):
                    spot_vs_thresh = (feeds.spot[sig.asset] - market["threshold"]) / market["threshold"]

                try:
                    trade_id = await asyncio.to_thread(
                        database.record_trade,
                        chat_id=chat_id,
                        strategy=sig.strategy, asset=sig.asset, timeframe=sig.timeframe,
                        outcome=sig.outcome, outcome_id=sig.outcome_id,
                        market_id=sig.market_id, event_id=sig.event_id, order_id=order_id,
                        entry_price=_safe_float(limit_price),
                        amount_ngn=_safe_float(amount),
                        certainty=_safe_float(sig.certainty),
                        secs_to_close=_safe_float(market["secs_to_close"] if market else 0),
                        spot_vs_threshold_pct=_safe_float(spot_vs_thresh),
                        market_price_at_entry=_safe_float(limit_price),
                        engine="CLOB_LIMIT",
                        filled_quantity=0.0,
                    )
                    risk.add_position(sig.market_id, {
                        "market_id":   sig.market_id,
                        "trade_id":    trade_id,    "event_id":   sig.event_id,
                        "order_id":    order_id,
                        "outcome":     sig.outcome, "outcome_id": sig.outcome_id,
                        "entry_price": limit_price, "amount_ngn": amount,
                        "filled_quantity": 0.0, "confirmed_filled": False,
                        "strategy":    sig.strategy, "asset":      sig.asset,
                        "timeframe":   sig.timeframe,
                        "threshold":   market.get("threshold") if market else getattr(sig, "threshold", None),
                        "closing_date": market.get("closing_date") if market else "",
                        "placed_at":   time.time(),
                    })
                except Exception as db_err:
                    log.error(f"[{chat_id}] MAKER DB record failed: {db_err}; cancelling untracked order")
                    try:
                        await client.cancel_order(order_id)
                    except Exception as cancel_error:
                        log.critical(
                            f"[{chat_id}] UNTRACKED MAKER ORDER {order_id}; cancellation failed: {cancel_error}"
                        )

                _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
            else:
                log.warning(f"[{chat_id}] MAKER order placed but no order_id returned")
            return  # Limit order is tracked in DB and will be resolved by resolution_monitor

        shares_filled = client.parse_filled_shares(order)
        filled_price  = float(order.get("avgFillPrice") or order.get("price") or quote_price)
        order_id      = order.get("id") or order.get("orderId") or order.get("order_id")
        order_status  = str(order.get("status") or "").lower()

        # ── FAK / Zero-Fill Reasoning (Order is a Request, Not a Result) ────────
        # If the order was killed, rejected, or cancelled with 0 shares filled,
        # it is a Zero-Fill. Do NOT manufacture a phantom position or deduct cash.
        if shares_filled <= 0:
            if order_status in ("cancelled", "killed", "rejected", "expired") or time_in_force == "FAK":
                log.info(
                    f"[{chat_id}] ⚪ ZERO FILL (FAK killed/cancelled) | {sig.strategy} {sig.asset} "
                    f"order={order_id} status={order_status} rtt={rtt_ms:.0f}ms | liquidity moved away"
                )
                _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
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
                    _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
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

    except Exception as e:
        err = str(e)
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
        _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()
        return

    # ── Notify FIRST — trade has happened on Bayse ────────────────────────
    # Always notify before DB write. If DB fails, user still knows about the trade.
    if _tg_app:
        try:
            await telegram_bot.notify_trade(
                _tg_app, chat_id, sig, actual_ngn, engine=engine
            )
        except Exception as ne:
            log.error(f"[{chat_id}] Notification failed: {ne}")

    # ── Record in DB ──────────────────────────────────────────────────────
    # Sanitise all floats before writing — prevents PostgreSQL REAL underflow
    # from subnormal GARCH/Kalman values (e.g. 9.4e-64 crashes psycopg2).
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
    except Exception as db_err:
        trade_id = None
        log.critical(
            f"[{chat_id}] DB record failed for {sig.asset} {sig.strategy}: {db_err}"
            f" — order={order_id} executed; retaining an in-memory position for exit protection"
        )
        if _tg_app:
            await telegram_bot.send_message(
                _tg_app, chat_id,
                f"🚨 Trade {order_id} executed but the database write failed. "
                "The bot is tracking it in memory; check the Bayse portfolio before restarting.",
            )

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
    _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()


def _get_market_fee(market_id: str) -> float:
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    return market.get("fee_rate", 0.02) if market else 0.02


# ── ARB execution ─────────────────────────────────────────────────────────────

# ARB gets its OWN lock namespace, independent of risk.pending_markets/
# open_positions. Those are shared by SNIPE/FRONTRUN/CORRELATE for directional
# exposure tracking — but ARB's mint/burn arbitrage doesn't economically
# conflict with a directional position on the same market (different
# mechanism, but it still has execution/cancellation risk and separate accounting). Sharing the
# lock meant ARB was almost permanently starved out: confirmed in production,
# 40 consecutive "already in/pending" skips and zero actual attempts in one
# session, simply because SNIPE had open positions on the same BTC/ETH/SOL
# markets ARB also targets. ARB only needs protection against racing against
# ITSELF (the original concurrent-execution bug from session 2).
_arb_pending: set[tuple[str, str]] = set()


async def execute_arb(chat_id: str, sig, client, risk, equity: float, free_cash: float, settings: dict):
    if not config.LIVE_TRADING:
        log.info(f"[{chat_id}] DRY RUN ARB {sig.asset} — LIVE_TRADING=false")
        return
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not market or str(market.get("engine") or "").upper() != "CLOB":
        log.debug(f"[{chat_id}] ARB SKIP {sig.asset} — active CLOB market not found")
        return

    arb_key = _cooldown_key(chat_id, sig.market_id)
    if arb_key in _arb_pending:
        log.info(f"[{chat_id}] ARB SKIP {sig.asset} — already pending on {sig.market_id}")
        return
    last = _trade_cooldown.get(_cooldown_key(chat_id, sig.market_id), 0.0)
    if time.time() - last < TRADE_COOLDOWN_SEC:
        return

    _arb_pending.add(arb_key)
    try:
        await _execute_arb_logic(chat_id, sig, client, market, equity, free_cash, risk, settings)
    finally:
        _arb_pending.discard(arb_key)
        _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()


async def _execute_arb_logic(
    chat_id: str, sig, client, market: dict, equity: float, free_cash: float,
    risk, settings: dict | None = None,
):
    """
    Safe ARB execution using actual filled-share counts for burn sizing.
    Fetches quotes for both YES and NO outcomes before trading to improve
    profitability, then sizes the burn from real order responses (not pre-trade
    estimates) and rolls back both legs atomically on any failure.
    """
    yes_p = market["yes_price"]
    no_p  = market["no_price"]

    # ── Extreme-price guard ───────────────────────────────────────────────
    # Only block genuinely broken/zero markets (< 2 cents). The old 0.08 floor
    # was killing arb on near-resolved markets like YES=0.95 NO=0.03 — the most
    # profitable arbs. The leg-size check below (amount < MIN_TRADE_NGN) handles
    # any cases where a cheap side makes the trade uneconomical.
    if min(yes_p, no_p) < 0.02:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — near-zero price market "
            f"(yes={yes_p:.3f} no={no_p:.3f}), likely broken/settled market"
        )
        return

    # ── Budget allocation ─────────────────────────────────────────────────
    min_leg = max(MIN_TRADE_NGN, float(settings.get("mintrade", MIN_TRADE_NGN))) if settings else MIN_TRADE_NGN
    max_exp = min(
        float((settings or {}).get("maxexposure", 10.0)) / 100.0,
        config.MAX_PORTFOLIO_EXPOSURE,
    )
    arb_risk_pct = min(float((settings or {}).get("risk_pct", 1.0)) / 100.0, 0.02)
    budget = min(ARB_MAX_SIZE_NGN, free_cash, equity * arb_risk_pct)
    if budget < min_leg * 2.0 or not risk.can_trade(equity, budget, max_exp):
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — two-leg minimum ₦{min_leg*2:,.0f} "
            f"does not fit risk budget ₦{budget:,.0f}"
        )
        return

    total_p = yes_p + no_p
    amount_yes = round(budget * (yes_p / total_p), 2)
    amount_no  = round(budget * (no_p  / total_p), 2)

    if amount_yes < min_leg or amount_no < min_leg:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — leg sizes too small "
            f"(yes=₦{amount_yes:,.0f} no=₦{amount_no:,.0f}, min=₦{min_leg:,.0f})"
        )
        return

    # ── Fetch pre-trade quotes ───────────────────────────────────────────
    try:
        quote_yes = await client.get_quote(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["yes_id"], side="BUY", amount=amount_yes,
            currency=CURRENCY
        )
        quote_no = await client.get_quote(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["no_id"], side="BUY", amount=amount_no,
            currency=CURRENCY
        )
    except Exception as q_err:
        log.warning(f"[{chat_id}] ARB SKIP {sig.asset} — failed to get pre-trade quotes: {q_err}")
        return

    if quote_yes.get("completeFill") is not True or quote_no.get("completeFill") is not True:
        log.info(f"[{chat_id}] ARB SKIP {sig.asset} — both legs lack confirmed complete-fill quotes")
        return
    if quote_yes.get("tradeGoesOverMaxLiability") or quote_no.get("tradeGoesOverMaxLiability"):
        return

    q_yes_p = float(quote_yes.get("price") or yes_p)
    q_no_p  = float(quote_no.get("price") or no_p)
    total_q_p = q_yes_p + q_no_p

    from config import ARB_TRIGGER
    if total_q_p > ARB_TRIGGER:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — sum of quotes {total_q_p:.3f} "
            f"exceeds trigger {ARB_TRIGGER:.3f} (yes_quote={q_yes_p:.3f} no_quote={q_no_p:.3f})"
        )
        return

    # ── Pre-trade share estimate (for profitability gate only) ──────────────
    # BUG FIX: in NGN mode 1 share costs price×100 NGN, so the fallback must
    # divide by 100. The old code divided only by price, producing a share count
    # 100× too large — burn_shares then failed with "insufficient shares".
    _sdiv = 100.0 if CURRENCY == "NGN" else 1.0
    est_yes = float(quote_yes.get("quantity") or (amount_yes / (q_yes_p * _sdiv)))
    est_no  = float(quote_no.get("quantity")  or (amount_no  / (q_no_p  * _sdiv)))

    est_burn = min(est_yes, est_no) * 0.97
    quote_fees = float(quote_yes.get("fee") or 0.0) + float(quote_no.get("fee") or 0.0)
    pair_cost_est = est_burn * total_q_p * config.CURRENCY_BASE_MULTIPLIER
    profit_est = (
        est_burn * config.CURRENCY_BASE_MULTIPLIER
        - pair_cost_est
        - quote_fees
    )

    if est_burn < 1.0:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — est matched inventory {est_burn:.2f} shares below 1"
        )
        return

    if profit_est < 20.0:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — profit too thin "
            f"(burn≈{est_burn:.1f} gap={1.0-total_q_p:.4f} est=₦{profit_est:.2f} < ₦20 min)"
        )
        return

    log.info(
        f"[{chat_id}] ARB PLACING {sig.asset} | "
        f"yes=₦{amount_yes:.0f}(≈{est_yes:.1f}sh) "
        f"no=₦{amount_no:.0f}(≈{est_no:.1f}sh) "
        f"est_burn≈{est_burn:.1f} est_profit=₦{profit_est:.2f}"
    )

    # ── Order execution — sized from ACTUAL fills, not quotes ───────────────
    # Upgrade: Place BOTH legs in parallel via asyncio.gather to eliminate execution latency gap.
    # Sized from real filled share counts with atomic dual-leg rollback.
    yes_shares_filled = 0.0
    no_shares_filled  = 0.0
    yes_order: dict = {}
    no_order: dict = {}
    yes_ok = False
    no_ok  = False

    try:
        t_yes = client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["yes_id"], side="BUY",
            amount=amount_yes, order_type="LIMIT", currency=CURRENCY,
            price=min(0.99, q_yes_p + 0.003), time_in_force="FOK",
        )
        t_no = client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["no_id"], side="BUY",
            amount=amount_no, order_type="LIMIT", currency=CURRENCY,
            price=min(0.99, q_no_p + 0.003), time_in_force="FOK",
        )

        results = await asyncio.gather(t_yes, t_no, return_exceptions=True)
        resp_yes, resp_no = results[0], results[1]

        if isinstance(resp_yes, Exception):
            log.warning(f"[{chat_id}] ARB YES leg order failed: {resp_yes}")
        else:
            yes_order = resp_yes.get("order") or resp_yes.get("clobOrder") or resp_yes.get("ammOrder") or resp_yes
            yes_shares_filled = client.parse_filled_shares(yes_order)
            yes_ok = yes_shares_filled > 0
            if not yes_ok:
                log.warning(f"[{chat_id}] ARB YES leg returned no confirmed fill")

        if isinstance(resp_no, Exception):
            log.warning(f"[{chat_id}] ARB NO leg order failed: {resp_no}")
        else:
            no_order = resp_no.get("order") or resp_no.get("clobOrder") or resp_no.get("ammOrder") or resp_no
            no_shares_filled = client.parse_filled_shares(no_order)
            no_ok = no_shares_filled > 0
            if not no_ok:
                log.warning(f"[{chat_id}] ARB NO leg returned no confirmed fill")

        if not (yes_ok and no_ok):
            failed_leg = "NO" if yes_ok else ("YES" if no_ok else "BOTH")
            raise RuntimeError(f"Parallel ARB leg failure: {failed_leg} leg failed")

        # Burn actual filled pairs (subtract tiny epsilon to avoid precision errors)
        burn_qty = round(min(yes_shares_filled, no_shares_filled) - 0.001, 4)
        if burn_qty < 1.0:
            raise ValueError(
                f"burn_qty {burn_qty:.4f} too small "
                f"(yes_filled={yes_shares_filled:.2f} no_filled={no_shares_filled:.2f})"
            )

        # Bayse burn request quantity is a wallet amount, while its response
        # quantity is normalized shares. In NGN, one pair redeems for ₦100.
        burn_wallet_amount = burn_qty * config.CURRENCY_BASE_MULTIPLIER
        burn_response = await client.burn_shares(sig.market_id, burn_wallet_amount, CURRENCY)
        proceeds = float(burn_response.get("proceeds") or burn_wallet_amount)

        yes_price_filled = float(yes_order.get("avgFillPrice") or yes_order.get("price") or q_yes_p)
        no_price_filled = float(no_order.get("avgFillPrice") or no_order.get("price") or q_no_p)
        yes_fee = float(yes_order.get("fee") or 0.0)
        no_fee = float(no_order.get("fee") or 0.0)
        matched_cost = (
            burn_qty * (yes_price_filled + no_price_filled) * config.CURRENCY_BASE_MULTIPLIER
            + yes_fee * (burn_qty / yes_shares_filled)
            + no_fee * (burn_qty / no_shares_filled)
        )
        profit = proceeds - matched_cost
        if profit <= 0:
            log.error(f"[{chat_id}] ARB burn completed with non-positive matched PnL: ₦{profit:.2f}")
        else:
            log.info(f"[{chat_id}] ARB ✅ {sig.asset} | {burn_qty:.3f} pairs | ₦{profit:+,.2f}")

        trade_id = await asyncio.to_thread(
            database.record_trade,
            chat_id=chat_id, strategy="ARB", asset=sig.asset,
            timeframe=sig.timeframe, outcome="ARB", outcome_id="burn",
            market_id=sig.market_id, event_id=sig.event_id,
            entry_price=_safe_float(total_q_p),
            amount_ngn=_safe_float(matched_cost),
            certainty=_safe_float(sig.certainty), secs_to_close=0,
            filled_quantity=_safe_float(burn_qty),
        )
        await asyncio.to_thread(database.resolve_trade, trade_id, profit > 0, profit)
        if _tg_app:
            await telegram_bot.notify_arb(_tg_app, chat_id, sig, burn_qty, profit)

    except Exception as e:
        log.error(f"[{chat_id}] ARB error: {e}")

        # ── Atomic dual-leg rollback ──────────────────────────────────────
        # Handles all 3 asymmetric scenarios:
        #   1. YES-only filled → sell YES shares
        #   2. NO-only filled  → sell NO shares
        #   3. Both filled but burn failed → sell BOTH legs to clear all exposure
        yes_rb = False
        no_rb  = False

        if yes_ok and not no_ok:
            try:
                await client.place_order(
                    sig.event_id, sig.market_id, market["yes_id"],
                    "SELL", _sell_proceeds_for_shares(yes_shares_filled, q_yes_p, market.get("fee_rate", 0.02)), "MARKET", currency=CURRENCY,
                )
                yes_rb = True
                log.info(f"[{chat_id}] ARB rollback ✅ YES sold ({yes_shares_filled:.3f}sh)")
            except Exception as re_:
                log.critical(f"[{chat_id}] ARB ROLLBACK YES FAILED — manual action needed: {re_}")

        elif not yes_ok and no_ok:
            try:
                await client.place_order(
                    sig.event_id, sig.market_id, market["no_id"],
                    "SELL", _sell_proceeds_for_shares(no_shares_filled, q_no_p, market.get("fee_rate", 0.02)), "MARKET", currency=CURRENCY,
                )
                no_rb = True
                log.info(f"[{chat_id}] ARB rollback ✅ NO sold ({no_shares_filled:.3f}sh)")
            except Exception as re_:
                log.critical(f"[{chat_id}] ARB ROLLBACK NO FAILED — manual action needed: {re_}")

        elif yes_ok and no_ok:
            # Both legs filled but burn failed — must clear both
            try:
                await client.place_order(
                    sig.event_id, sig.market_id, market["yes_id"],
                    "SELL", _sell_proceeds_for_shares(yes_shares_filled, q_yes_p, market.get("fee_rate", 0.02)), "MARKET", currency=CURRENCY,
                )
                yes_rb = True
                log.info(f"[{chat_id}] ARB rollback ✅ YES sold ({yes_shares_filled:.3f}sh)")
            except Exception as re_:
                log.critical(f"[{chat_id}] ARB ROLLBACK YES FAILED — YES exposure remains: {re_}")
            try:
                await client.place_order(
                    sig.event_id, sig.market_id, market["no_id"],
                    "SELL", _sell_proceeds_for_shares(no_shares_filled, q_no_p, market.get("fee_rate", 0.02)), "MARKET", currency=CURRENCY,
                )
                no_rb = True
                log.info(f"[{chat_id}] ARB rollback ✅ NO sold ({no_shares_filled:.3f}sh)")
            except Exception as re_:
                log.critical(f"[{chat_id}] ARB ROLLBACK NO FAILED — NO exposure remains: {re_}")

        fully_rb = (
            (yes_ok and not no_ok and yes_rb) or
            (not yes_ok and no_ok and no_rb) or
            (yes_ok and no_ok and yes_rb and no_rb)
        )

        try:
            trade_id = await asyncio.to_thread(
                database.record_trade,
                chat_id=chat_id, strategy="ARB", asset=sig.asset,
                timeframe=sig.timeframe, outcome="ARB", outcome_id="burn_failed",
                market_id=sig.market_id, event_id=sig.event_id,
                entry_price=_safe_float(total_q_p),
                amount_ngn=_safe_float(amount_yes + amount_no),
                certainty=_safe_float(sig.certainty), secs_to_close=0,
            )
            est_loss = 0.0 if fully_rb else -(amount_yes + amount_no)
            await asyncio.to_thread(database.resolve_trade, trade_id, False, est_loss)
        except Exception as db_err:
            log.error(f"[{chat_id}] ARB failure could not be recorded: {db_err}")


# ── Active Mid-Market Market Making Execution ─────────────────────────────────

async def execute_midmarket_maker(
    chat_id: str, sig, client, risk, equity: float, free_cash: float, settings: dict
):
    """
    Executes simultaneous dual-sided resting limit orders near mid-market
    on dislocated or wide orderbooks to capture locked arbitrage spread.
    """
    if not config.LIVE_TRADING:
        log.info(f"[{chat_id}] DRY RUN MIDMARKET_MAKER {sig.asset} — LIVE_TRADING=false")
        return
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not market or str(market.get("engine") or "").upper() != "CLOB":
        return

    last = _trade_cooldown.get(_cooldown_key(chat_id, sig.market_id), 0.0)
    if time.time() - last < TRADE_COOLDOWN_SEC:
        return

    if not hasattr(sig, "converged_with") or len(sig.converged_with) < 4:
        return

    bid_yes, bid_no, yes_id, no_id = sig.converged_with[:4]
    min_leg = max(MIN_TRADE_NGN, float(settings.get("mintrade", MIN_TRADE_NGN))) if settings else MIN_TRADE_NGN

    pair_amount = min_leg * 2.0
    max_exp = min(float(settings.get("maxexposure", 10.0)) / 100.0, config.MAX_PORTFOLIO_EXPOSURE)
    risk_cap = equity * min(float(settings.get("risk_pct", 1.0)) / 100.0, 0.02)
    if free_cash < pair_amount or pair_amount > risk_cap or not risk.can_trade(equity, pair_amount, max_exp):
        log.info(
            f"[{chat_id}] MIDMARKET_MAKER SKIP: ₦{pair_amount:,.0f} pair does not fit "
            f"free cash/risk/exposure budget (risk cap ₦{risk_cap:,.0f})"
        )
        return

    amount_leg = min_leg
    _trade_cooldown[_cooldown_key(chat_id, sig.market_id)] = time.time()

    try:
        t0 = time.time()
        batch = await client.place_batch_orders(
            [
                {
                    "outcomeId": yes_id, "side": "BUY", "type": "LIMIT",
                    "amount": amount_leg, "currency": CURRENCY,
                    "price": bid_yes, "timeInForce": "GTC", "postOnly": True,
                    "stpMode": "CANCEL_OLDEST", "clientOrderId": "mid-yes",
                },
                {
                    "outcomeId": no_id, "side": "BUY", "type": "LIMIT",
                    "amount": amount_leg, "currency": CURRENCY,
                    "price": bid_no, "timeInForce": "GTC", "postOnly": True,
                    "stpMode": "CANCEL_OLDEST", "clientOrderId": "mid-no",
                },
            ],
            idempotency_key=uuid.uuid4().hex,
        )
        results = batch.get("results", []) if isinstance(batch, dict) else []
        order_yes = results[0].get("order", {}) if len(results) > 0 and results[0].get("success") else {}
        order_no = results[1].get("order", {}) if len(results) > 1 and results[1].get("success") else {}
        rtt_ms = (time.time() - t0) * 1000

        id_yes = order_yes.get("id") or order_yes.get("orderId") if isinstance(order_yes, dict) else None
        id_no  = order_no.get("id") or order_no.get("orderId") if isinstance(order_no, dict) else None

        if not id_yes and not id_no:
            log.warning(f"[{chat_id}] MIDMARKET_MAKER dual orders failed: yes={order_yes}, no={order_no}")
            return
        if bool(id_yes) != bool(id_no):
            orphan_id = id_yes or id_no
            log.warning(
                f"[{chat_id}] MIDMARKET_MAKER only one resting leg was accepted; "
                f"cancelling orphan {orphan_id}"
            )
            try:
                await client.cancel_order(orphan_id)
            except Exception as cancel_error:
                log.critical(f"[{chat_id}] Failed to cancel orphan maker order {orphan_id}: {cancel_error}")
            return

        log.info(
            f"[{chat_id}] MIDMARKET_MAKER DUAL ORDERS PLACED | {sig.asset} {sig.timeframe} | "
            f"YES@{bid_yes:.3f} (id={id_yes}) + NO@{bid_no:.3f} (id={id_no}) | "
            f"Locked Spread=+{sig.edge_at_entry:.1%} | rtt={rtt_ms:.0f}ms"
        )

        if _tg_app:
            try:
                await telegram_bot.notify_midmarket(_tg_app, chat_id, sig, bid_yes, bid_no, amount_leg)
            except Exception as te:
                log.warning(f"[{chat_id}] Telegram notify midmarket error: {te}")

        # Record each placed leg in DB & risk manager
        for oid, bid_p, outcome, token_id in [
            (id_yes, bid_yes, "YES", yes_id),
            (id_no, bid_no, "NO", no_id),
        ]:
            if oid:
                try:
                    trade_id = await asyncio.to_thread(
                        database.record_trade,
                        chat_id=chat_id,
                        strategy="MIDMARKET_MAKER",
                        asset=sig.asset,
                        timeframe=sig.timeframe,
                        outcome=outcome,
                        outcome_id=token_id,
                        market_id=sig.market_id,
                        event_id=sig.event_id,
                        order_id=oid,
                        entry_price=_safe_float(bid_p),
                        amount_ngn=_safe_float(amount_leg),
                        certainty=0.95,
                        secs_to_close=_safe_float(market.get("secs_to_close", 0)),
                        spot_vs_threshold_pct=0.0,
                        market_price_at_entry=_safe_float(bid_p),
                        engine="CLOB_LIMIT",
                        filled_quantity=0.0,
                    )
                    risk.add_position(sig.market_id + f"_{outcome}", {
                        "market_id": sig.market_id,
                        "trade_id": trade_id, "event_id": sig.event_id,
                        "order_id": oid, "outcome": outcome, "outcome_id": token_id,
                        "entry_price": bid_p, "amount_ngn": amount_leg,
                        "filled_quantity": 0.0, "confirmed_filled": False,
                        "strategy": "MIDMARKET_MAKER", "asset": sig.asset,
                        "timeframe": sig.timeframe,
                        "threshold": market.get("threshold"),
                        "closing_date": market.get("closing_date", ""),
                        "placed_at": time.time(),
                    })
                except Exception as dbe:
                    log.error(f"[{chat_id}] DB record midmarket leg failed: {dbe}; cancelling {oid}")
                    try:
                        await client.cancel_order(oid)
                    except Exception as cancel_error:
                        log.critical(f"[{chat_id}] Untracked midmarket order {oid}: {cancel_error}")

        # Start 45s adverse selection watchdog
        if id_yes and id_no:
            asyncio.create_task(
                _midmarket_watchdog(client, chat_id, sig.market_id, id_yes, id_no, bid_yes, bid_no)
            )

    except Exception as e:
        log.error(f"[{chat_id}] execute_midmarket_maker error: {e}", exc_info=True)


async def _midmarket_watchdog(client, chat_id: str, market_id: str, id_yes: str, id_no: str, bid_yes: float, bid_no: float):
    """
    Guards against adverse selection in dual-sided market making:
    After 45s, checks if only one leg was filled.
    If one leg filled and the other is still resting, cancels the resting order
    to prevent directional run-away drift into trending moves.
    """
    await asyncio.sleep(45.0)
    try:
        o_yes, o_no = await asyncio.gather(
            client.get_order(id_yes),
            client.get_order(id_no),
            return_exceptions=True,
        )
        status_yes = str(o_yes.get("status", "")).lower() if isinstance(o_yes, dict) else "unknown"
        status_no  = str(o_no.get("status", "")).lower() if isinstance(o_no, dict) else "unknown"

        filled_yes = status_yes in ("filled", "completed")
        filled_no  = status_no in ("filled", "completed")

        if filled_yes and filled_no:
            log.info(f"[{chat_id}] 🏆 MIDMARKET DUAL FILL SUCCESS on {market_id}! +{(1.0-(bid_yes+bid_no)):.1%} locked")
        elif filled_yes and not filled_no:
            log.warning(f"[{chat_id}] ⚠️ MIDMARKET partial fill: YES filled, NO unfilled. Cancelling NO order {id_no}")
            try:
                await client.cancel_order(id_no)
            except Exception as ce:
                log.error(f"[{chat_id}] Failed to cancel unhedged NO order: {ce}")
        elif filled_no and not filled_yes:
            log.warning(f"[{chat_id}] ⚠️ MIDMARKET partial fill: NO filled, YES unfilled. Cancelling YES order {id_yes}")
            try:
                await client.cancel_order(id_yes)
            except Exception as ce:
                log.error(f"[{chat_id}] Failed to cancel unhedged YES order: {ce}")
    except Exception as we:
        log.error(f"[{chat_id}] Midmarket watchdog error: {we}")
