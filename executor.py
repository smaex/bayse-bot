"""
Trade executor — places orders, records trades, handles AMM + CLOB routing.

Key fixes vs previous version:
  - Fee floor corrected to 0.3 (was 0.5)
  - Always MARKET orders — Bayse CLOB has no book depth
  - Telegram notification fires BEFORE DB write — user always notified even if DB fails
  - Float sanitization before every DB write — prevents PostgreSQL REAL underflow
    from subnormal GARCH/Kalman values (e.g. 9.4e-64 crashes psycopg2)
"""

import asyncio
import logging
import math
import re
import time
from typing import Optional

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
_trade_cooldown:      dict[str, float] = {}
TRADE_COOLDOWN_SEC = 60
MIN_TRADE_NGN      = 100.0


def init_executor(markets, tg_app):
    global active_markets, _tg_app
    active_markets = markets
    _tg_app        = tg_app


# ── Float sanitisation ────────────────────────────────────────────────────────

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


# ── Engine detection ──────────────────────────────────────────────────────────

async def _infer_engine(client, market: dict) -> str:
    mid = market.get("market_id", "")
    if mid in _market_engine_cache:
        return _market_engine_cache[mid]
    try:
        ob   = await asyncio.wait_for(
            client.get_orderbook(market["event_id"], mid), timeout=0.5
        )
        bids = ob.get("bids") or ob.get("yes", {}).get("bids") or []
        asks = ob.get("asks") or ob.get("yes", {}).get("asks") or []
        engine = "CLOB" if (bids or asks) else "AMM"
    except Exception:
        engine = "AMM"
    _market_engine_cache[mid] = engine
    return engine


def _effective_fee(fee_rate: float, price: float) -> float:
    """Bayse fee formula: fee = feeRate × max(1 - price, 0.3)"""
    return fee_rate * max(1.0 - price, FEE_FLOOR)


# ── Main trade execution ──────────────────────────────────────────────────────

async def execute_trade(chat_id: str, sig, client, risk, settings: dict,
                        equity: float, free_cash: float):
    if strategy.global_state.systemic_halt_until > time.time():
        log.debug(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — systemic halt active")
        return
    if risk.already_in(sig.market_id):
        log.debug(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — already in/pending on {sig.market_id}")
        return
    last = _trade_cooldown.get(sig.market_id, 0.0)
    remaining = TRADE_COOLDOWN_SEC - (time.time() - last)
    if remaining > 0:
        log.debug(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — cooldown {remaining:.0f}s left on {sig.market_id}")
        return

    risk.lock_market(sig.market_id)
    try:
        await _execute_logic(chat_id, sig, client, risk, settings, equity, free_cash)
    finally:
        risk.unlock_market(sig.market_id)


async def _execute_logic(chat_id: str, sig, client, risk, settings: dict,
                          equity: float, free_cash: float):
    mode      = settings.get("mode", "balanced")
    min_t     = settings.get("mintrade", MIN_TRADE_NGN)
    max_t     = settings.get("maxtrade", 5_000)
    max_exp   = settings.get("maxexposure", 20.0) / 100.0
    learned   = settings.get("learned", {})
    mult      = learned.get("size_multipliers", {}).get(sig.strategy, 1.0)
    user_risk = min(settings.get("risk_pct", 2.0), 5.0) / 100.0

    if risk.is_in_strict_mode() and sig.certainty < 0.70:
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — strict mode (near daily target), certainty {sig.certainty:.0%} < 70%")
        return

    # ── Conviction sizing ──────────────────────────────────────────────────
    if sig.certainty >= 0.90:   tier = 2.0
    elif sig.certainty >= 0.70: tier = 1.5
    elif sig.certainty >= 0.55: tier = 1.0
    else:                       tier = 0.5

    fx_factor = 0.5 if sig.asset in _FX_ASSETS else 1.0
    raw_pct   = user_risk * tier * mult * fx_factor

    if sig.certainty >= 0.95:
        raw_pct *= 1.5

    if hasattr(database, "get_alpha_trend"):
        decay = await asyncio.to_thread(
            database.get_alpha_trend, chat_id, sig.strategy, sig.asset
        )
        if decay < 0.85:
            raw_pct *= 0.5

    if risk.is_on_probation():
        raw_pct *= 0.25

    # ── NGN amount ─────────────────────────────────────────────────────────
    if equity < 3_000:
        amount = MIN_TRADE_NGN
    else:
        capped = min(raw_pct, user_risk * 3.0)
        amount = max(min_t, min(max_t, equity * capped))

    hard_cap = min(max(MIN_TRADE_NGN, equity * 0.05), max_t)
    if amount > hard_cap:
        amount = hard_cap

    effective_min = max(MIN_TRADE_NGN, min_t)
    if amount < effective_min or amount > free_cash:
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — amount ₦{amount:,.0f} "
            f"outside bounds (min=₦{effective_min:,.0f}, free_cash=₦{free_cash:,.0f})"
        )
        return

    # ── EV check ───────────────────────────────────────────────────────────
    fee_rate = _get_market_fee(sig.market_id)
    eff_fee  = _effective_fee(fee_rate, sig.market_price)
    ev       = sig.win_prob * (1.0 - eff_fee) / sig.market_price - 1.0

    is_probe = sig.certainty >= 0.35 and sig.certainty < sig.mode_floor
    if is_probe:
        amount = MIN_TRADE_NGN
    elif ev < 0.01:
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — EV {ev:+.1%} (market_price={sig.market_price:.3f} win_prob={sig.win_prob:.2%})")
        return

    # Ceiling must match SNIPE_MAX_MARKET_PRICE (0.90) — was 0.88 which killed
    # valid signals that already passed SNIPE's own EV gate at prices 0.88-0.90.
    if sig.market_price > 0.90:
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — market_price {sig.market_price:.3f} > 0.90 ceiling")
        return

    if equity >= 3_000 and not risk.can_trade(equity, amount, max_exp):
        log.info(
            f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — exposure cap "
            f"(deployed=₦{risk.deployed():,.0f}, +₦{amount:,.0f} > {max_exp:.0%} of ₦{equity:,.0f})"
        )
        return

    # ── Market-specific minimum ───────────────────────────────────────────
    # CRITICAL FIX: previously this silently returned forever with NO log and
    # NO cooldown — once a market's real Bayse minimum exceeded our computed
    # amount, every future signal for that market_id died instantly and
    # invisibly, in a tight infinite retry loop (signal fires every ~1s,
    # dies silently, fires again). This was very likely why trades stopped
    # entirely for hours despite signals firing continuously.
    cached_min = _market_min_cache.get(sig.market_id, 0.0)
    if cached_min > 0 and amount < cached_min:
        if cached_min <= max_t and cached_min <= free_cash:
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
            _trade_cooldown[sig.market_id] = time.time()  # prevent infinite tight retry loop
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

    # ── Always MARKET orders ───────────────────────────────────────────────
    # AMM always fills. Bayse CLOB has no book depth — LIMIT orders
    # (even FAK) find no counterparty and get refunded immediately.
    # MARKET orders route to the AMM curve which always provides liquidity.
    slip_map      = {"safe": 0.003, "balanced": 0.005, "aggressive": 0.01, "full_send": 0.02}
    slippage      = slip_map.get(mode, 0.005)
    max_valid     = (1.0 - eff_fee) / 1.01
    order_type    = "MARKET"
    time_in_force = "FAK"
    limit_price   = round(min(sig.market_price * (1.0 + slippage), max_valid), 3)

    log.info(
        f"[{chat_id}] PLACING {sig.strategy} {sig.asset} {sig.timeframe} "
        f"{sig.outcome} | {order_type}/{time_in_force} "
        f"₦{amount:,.0f} @ {sig.market_price:.3f} | certainty={sig.certainty:.0%}"
    )

    try:
        resp  = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=sig.outcome_id, side="BUY",
            amount=amount, order_type=order_type,
            price=None,
            max_slippage=slippage, currency=CURRENCY,
            time_in_force=time_in_force,
        )
        order = resp.get("order") or resp.get("clobOrder") or resp.get("ammOrder") or resp

        shares_filled = client.parse_filled_shares(order)
        filled_price  = float(order.get("avgFillPrice") or order.get("price") or limit_price)
        order_id      = order.get("id") or order.get("orderId") or order.get("order_id")

        if shares_filled <= 0:
            if order_id:
                shares_filled = (
                    amount / (filled_price * 100.0)
                    if CURRENCY == "NGN" else amount / filled_price
                )
                log.warning(f"[{chat_id}] AMM zero-fill estimate for {sig.asset}")
            else:
                log.info(f"[{chat_id}] Order rejected — no order_id returned")
                return

        actual_ngn = shares_filled * filled_price * (100.0 if CURRENCY == "NGN" else 1.0)

        spot_vs_thresh = 0.0
        if market and market.get("threshold") and feeds.spot.get(sig.asset):
            spot_vs_thresh = (feeds.spot[sig.asset] - market["threshold"]) / market["threshold"]

        log.info(
            f"[{chat_id}] ✅ FILLED | {sig.strategy} {sig.asset} {sig.timeframe} "
            f"{sig.outcome} @ {filled_price:.4f} ₦{actual_ngn:,.0f} | order={order_id}"
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
        _trade_cooldown[sig.market_id] = time.time()
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
        )
    except Exception as db_err:
        log.error(
            f"[{chat_id}] DB record failed for {sig.asset} {sig.strategy}: {db_err}"
            f" — trade DID execute on Bayse (order={order_id}) but is not in DB"
        )
        _trade_cooldown[sig.market_id] = time.time()
        return

    risk.add_position(sig.market_id, {
        "trade_id":    trade_id,    "event_id":   sig.event_id,
        "outcome":     sig.outcome, "outcome_id": sig.outcome_id,
        "entry_price": filled_price, "amount_ngn": actual_ngn,
        "strategy":    sig.strategy, "asset":      sig.asset,
        "timeframe":   sig.timeframe,
    })
    risk.current_free_cash -= actual_ngn
    _trade_cooldown[sig.market_id] = time.time()


def _get_market_fee(market_id: str) -> float:
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    return market.get("fee_rate", 0.02) if market else 0.02


# ── ARB execution ─────────────────────────────────────────────────────────────

async def execute_arb(chat_id: str, sig, client, risk, equity: float, free_cash: float, settings: dict):
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not market:
        log.debug(f"[{chat_id}] ARB SKIP {sig.asset} — market {sig.market_id} not found in active list")
        return

    # CRITICAL FIX: ARB previously had NO real lock, only a cooldown timestamp
    # set at the very end of the function. Two overlapping evaluation triggers
    # (price tick + heartbeat + market update all fire independently) could
    # both pass the cooldown check and call execute_arb concurrently on the
    # SAME market before either had set the cooldown — racing on the same
    # capital and shares. This produced "you do not have enough shares for
    # this trade" on the burn, then an "insufficient shares balance" failure
    # on the rollback too, because both legs got bought twice while only one
    # set of shares actually existed. This lock makes ARB execution mutually
    # exclusive per-market, exactly like execute_trade already does.
    if risk.already_in(sig.market_id):
        log.debug(f"[{chat_id}] ARB SKIP {sig.asset} — already in/pending on {sig.market_id}")
        return
    last = _trade_cooldown.get(sig.market_id, 0.0)
    if time.time() - last < TRADE_COOLDOWN_SEC:
        return

    risk.lock_market(sig.market_id)
    try:
        await _execute_arb_logic(chat_id, sig, client, market, free_cash)
    finally:
        risk.unlock_market(sig.market_id)
        _trade_cooldown[sig.market_id] = time.time()


async def _execute_arb_logic(chat_id: str, sig, client, market: dict, free_cash: float):
    # ARB is risk-free (mint/burn — guaranteed profit, no directional exposure),
    # so it deserves a much higher allocation than directional strategies.
    # Old 10% fraction made ARB mathematically dead below ~₦2,000 free cash
    # (budget never cleared the 2x MIN_TRADE_NGN floor needed for both legs),
    # silently and permanently disabling ARB for smaller accounts with no log.
    budget = min(ARB_MAX_SIZE_NGN, free_cash * 0.30)
    if budget < MIN_TRADE_NGN * 2:
        # Try harder before giving up — ARB has zero directional risk,
        # so using most of free_cash here is safe if it clears the floor.
        budget = min(free_cash * 0.90, MIN_TRADE_NGN * 2.5)
    if budget < MIN_TRADE_NGN * 2:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — free_cash ₦{free_cash:,.0f} too small "
            f"for the ₦{MIN_TRADE_NGN*2:,.0f} two-leg minimum"
        )
        return

    yes_p = market["yes_price"]
    no_p  = market["no_price"]

    amount_yes = round(budget * (yes_p / (yes_p + no_p)), 2)
    amount_no  = round(budget - amount_yes, 2)

    if amount_yes < MIN_TRADE_NGN or amount_no < MIN_TRADE_NGN:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — leg sizes too small "
            f"(yes=₦{amount_yes:,.0f} no=₦{amount_no:,.0f}, min=₦{MIN_TRADE_NGN:,.0f})"
        )
        return

    # Explicit slippage cap — must match the conservative estimate formula
    # below, so the "worst case" we calculate for is the same worst case
    # Bayse's API actually enforces.
    arb_slippage = 0.03

    yes_shares = 0.0
    no_shares  = 0.0
    yes_ok     = False

    try:
        resp_yes = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["yes_id"], side="BUY",
            amount=amount_yes, order_type="MARKET", currency=CURRENCY,
            max_slippage=arb_slippage,
        )
        order_yes  = resp_yes.get("order") or resp_yes
        yes_shares = client.parse_filled_shares(order_yes)
        if yes_shares <= 0:
            # CRITICAL FIX: previously used amount/yes_p — an optimistic estimate
            # that ignores AMM price impact/slippage. If the real fill price was
            # worse than quoted (which it usually is for any size on an AMM),
            # this estimate OVERSTATES actual shares received. Burning more
            # shares than we actually hold is exactly what caused "you do not
            # have enough shares for this trade". Using price*(1+max_slippage)
            # as the denominator guarantees this estimate is a LOWER bound on
            # actual shares received (assuming Bayse honors max_slippage),
            # leaving a small unburned residual instead of a hard failure.
            yes_shares = amount_yes / (yes_p * (1.0 + arb_slippage))
        yes_ok = True

        resp_no = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["no_id"], side="BUY",
            amount=amount_no, order_type="MARKET", currency=CURRENCY,
            max_slippage=arb_slippage,
        )
        order_no  = resp_no.get("order") or resp_no
        no_shares = client.parse_filled_shares(order_no)
        if no_shares <= 0:
            no_shares = amount_no / (no_p * (1.0 + arb_slippage))

        burn_qty = min(yes_shares, no_shares)
        if burn_qty > 0:
            await client.burn_shares(sig.market_id, burn_qty, CURRENCY)
            profit = burn_qty - (amount_yes + amount_no)
            log.info(f"[{chat_id}] ARB ✅ {sig.asset} | {burn_qty:.2f} pairs | ₦{profit:+,.2f}")

            trade_id = await asyncio.to_thread(
                database.record_trade,
                chat_id=chat_id, strategy="ARB", asset=sig.asset,
                timeframe=sig.timeframe, outcome="ARB", outcome_id="burn",
                market_id=sig.market_id, event_id=sig.event_id,
                entry_price=_safe_float(yes_p + no_p),
                amount_ngn=_safe_float(budget),
                certainty=1.0, secs_to_close=0,
            )
            await asyncio.to_thread(database.resolve_trade, trade_id, True, profit)
            if _tg_app:
                await telegram_bot.notify_arb(_tg_app, chat_id, sig, burn_qty, profit)

    except Exception as e:
        log.error(f"[{chat_id}] ARB error: {e}")
        rollback_ok = False
        if yes_ok and yes_shares > 0:
            try:
                await client.place_order(
                    sig.event_id, sig.market_id, market["yes_id"],
                    "SELL", amount_yes, "MARKET", currency=CURRENCY,
                )
                rollback_ok = True
                log.info(f"[{chat_id}] ARB rollback OK")
            except Exception as re_:
                log.critical(f"[{chat_id}] ARB ROLLBACK FAILED: {re_}")

        # CRITICAL FIX: a failed burn/rollback previously vanished completely —
        # nothing was ever written to the trades table, so the loss never
        # showed up in /trades, /analysis, or the learner's win-rate math.
        # Real money was spent on both legs; record it as a loss so it's
        # visible and the learner can actually account for it.
        try:
            trade_id = await asyncio.to_thread(
                database.record_trade,
                chat_id=chat_id, strategy="ARB", asset=sig.asset,
                timeframe=sig.timeframe, outcome="ARB", outcome_id="burn_failed",
                market_id=sig.market_id, event_id=sig.event_id,
                entry_price=_safe_float(yes_p + no_p),
                amount_ngn=_safe_float(amount_yes + amount_no),
                certainty=1.0, secs_to_close=0,
            )
            # Best-effort loss estimate: full cost if rollback failed too,
            # else just the rollback's own slippage cost (small, unknown — log only).
            est_loss = -(amount_yes + amount_no) if not rollback_ok else 0.0
            await asyncio.to_thread(database.resolve_trade, trade_id, False, est_loss)
        except Exception as db_err:
            log.error(f"[{chat_id}] ARB failure could not even be recorded to DB: {db_err}")
        # bug as the directional executor (no cooldown meant the same ARB
        # signal could re-fire every tick with no log trace).
        _trade_cooldown[sig.market_id] = time.time()
