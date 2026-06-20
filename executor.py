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
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — systemic halt active")
        return
    if risk.already_in(sig.market_id):
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — already in/pending on {sig.market_id}")
        return
    last = _trade_cooldown.get(sig.market_id, 0.0)
    remaining = TRADE_COOLDOWN_SEC - (time.time() - last)
    if remaining > 0:
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — cooldown {remaining:.0f}s left on {sig.market_id}")
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

# ARB gets its OWN lock namespace, independent of risk.pending_markets/
# open_positions. Those are shared by SNIPE/FRONTRUN/CORRELATE for directional
# exposure tracking — but ARB's mint/burn arbitrage doesn't economically
# conflict with a directional position on the same market (different
# mechanism, genuinely risk-free, no shared capital accounting). Sharing the
# lock meant ARB was almost permanently starved out: confirmed in production,
# 40 consecutive "already in/pending" skips and zero actual attempts in one
# session, simply because SNIPE had open positions on the same BTC/ETH/SOL
# markets ARB also targets. ARB only needs protection against racing against
# ITSELF (the original concurrent-execution bug from session 2).
_arb_pending: set[str] = set()


async def execute_arb(chat_id: str, sig, client, risk, equity: float, free_cash: float, settings: dict):
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not market:
        log.debug(f"[{chat_id}] ARB SKIP {sig.asset} — market {sig.market_id} not found in active list")
        return

    if sig.market_id in _arb_pending:
        log.info(f"[{chat_id}] ARB SKIP {sig.asset} — already pending on {sig.market_id}")
        return
    last = _trade_cooldown.get(sig.market_id, 0.0)
    if time.time() - last < TRADE_COOLDOWN_SEC:
        return

    _arb_pending.add(sig.market_id)
    try:
        await _execute_arb_logic(chat_id, sig, client, market, free_cash)
    finally:
        _arb_pending.discard(sig.market_id)
        _trade_cooldown[sig.market_id] = time.time()


async def _execute_arb_logic(chat_id: str, sig, client, market: dict, free_cash: float):
    """
    Safe ARB execution — units-correct, no speculative quote-guessing.

    THE BUG THIS REPLACES: Bayse's contract size is ₦100 per share — proven
    by execute_trade's own working formula `shares = amount/(price*100)`
    and its inverse `ngn = shares*price*100`. The previous ARB rewrite called
    a get_quote() endpoint and guessed at field names ("shares", "shareAmount",
    etc.); when none matched, it fell back to `amount/(price*(1+slippage))` —
    missing the ÷100 entirely. That produced share counts 100x too large
    (₦140 spent registered as "276 shares" instead of the real ~2.76),
    so burn_qty was 100x inflated and burning failed every time with
    "not enough shares" — exactly the error seen twice in production.

    FIX: drop the quote calls (they were guessing at an undocumented schema
    and added latency for zero verified benefit — simpler is more reliable
    here). Use the SAME proven formula as the rest of the codebase, with a
    conservative slippage haircut so the estimate is always a lower bound
    on actual shares received.
    """
    yes_p = market["yes_price"]
    no_p  = market["no_price"]

    # ── Extreme-price guard ───────────────────────────────────────────────
    # Below 0.08, the proportional budget split makes one leg unaffordable
    # at MIN_TRADE_NGN regardless of budget size at this account's balance.
    if min(yes_p, no_p) < 0.08:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — extreme-price market "
            f"(yes={yes_p:.3f} no={no_p:.3f}), unaffordable with current balance"
        )
        return

    # ── Budget allocation ─────────────────────────────────────────────────
    budget  = min(ARB_MAX_SIZE_NGN, free_cash * 0.30)
    total_p = yes_p + no_p
    amount_yes = round(budget * (yes_p / total_p), 2)
    amount_no  = round(budget * (no_p  / total_p), 2)

    if amount_yes < MIN_TRADE_NGN or amount_no < MIN_TRADE_NGN:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — leg sizes too small "
            f"(yes=₦{amount_yes:,.0f} no=₦{amount_no:,.0f}, min=₦{MIN_TRADE_NGN:,.0f})"
        )
        return

    # ── Share estimate — units-correct ────────────────────────────────────
    # shares = NGN_amount / (price * 100). This is the proven contract-size
    # convention already used by execute_trade. A 3% slippage haircut on
    # the price (worst-case actual fill) makes this a conservative LOWER
    # bound on actual shares received, so burn_qty can never exceed real
    # holdings.
    arb_slip = 0.03
    yes_shares = amount_yes / (yes_p * 100.0 * (1.0 + arb_slip))
    no_shares  = amount_no  / (no_p  * 100.0 * (1.0 + arb_slip))

    burn_qty   = min(yes_shares, no_shares)
    # Profit formula (units-verified): burning N pairs returns N*₦100 face
    # value; cost was the sum of both legs. profit = N*(1-total_p)*100.
    profit_est = burn_qty * (1.0 - total_p) * 100.0

    # Require a meaningful profit, not just >0 — each attempt risks real
    # slippage and places two live orders, so a fractional-naira "profit"
    # isn't worth the operational risk.
    if profit_est < 2.0:
        log.info(
            f"[{chat_id}] ARB SKIP {sig.asset} — profit too thin after haircut "
            f"(burn={burn_qty:.3f} gap={1.0-total_p:.4f} est=₦{profit_est:.2f})"
        )
        return

    log.info(
        f"[{chat_id}] ARB PLACING {sig.asset} | "
        f"yes=₦{amount_yes:.0f}({yes_shares:.3f}sh) "
        f"no=₦{amount_no:.0f}({no_shares:.3f}sh) "
        f"burn={burn_qty:.3f} est_profit=₦{profit_est:.2f}"
    )

    yes_ok = False
    try:
        await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["yes_id"], side="BUY",
            amount=amount_yes, order_type="MARKET", currency=CURRENCY,
            max_slippage=arb_slip,
        )
        yes_ok = True

        await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["no_id"], side="BUY",
            amount=amount_no, order_type="MARKET", currency=CURRENCY,
            max_slippage=arb_slip,
        )

        # Burn the conservative estimate — NOT a value parsed from the order
        # response (that's what caused both previous bugs: the response
        # field doesn't reliably represent true share count in this unit
        # convention).
        await client.burn_shares(sig.market_id, burn_qty, CURRENCY)
        profit = burn_qty * (1.0 - total_p) * 100.0
        log.info(f"[{chat_id}] ARB ✅ {sig.asset} | {burn_qty:.3f} pairs | ₦{profit:+,.2f}")

        trade_id = await asyncio.to_thread(
            database.record_trade,
            chat_id=chat_id, strategy="ARB", asset=sig.asset,
            timeframe=sig.timeframe, outcome="ARB", outcome_id="burn",
            market_id=sig.market_id, event_id=sig.event_id,
            entry_price=_safe_float(total_p),
            amount_ngn=_safe_float(amount_yes + amount_no),
            certainty=1.0, secs_to_close=0,
        )
        await asyncio.to_thread(database.resolve_trade, trade_id, True, profit)
        if _tg_app:
            await telegram_bot.notify_arb(_tg_app, chat_id, sig, burn_qty, profit)

    except Exception as e:
        log.error(f"[{chat_id}] ARB error: {e}")
        rollback_ok = False
        if yes_ok:
            try:
                await client.place_order(
                    sig.event_id, sig.market_id, market["yes_id"],
                    "SELL", amount_yes, "MARKET", currency=CURRENCY,
                )
                rollback_ok = True
                log.info(f"[{chat_id}] ARB rollback OK")
            except Exception as re_:
                log.critical(f"[{chat_id}] ARB ROLLBACK FAILED: {re_}")

        try:
            trade_id = await asyncio.to_thread(
                database.record_trade,
                chat_id=chat_id, strategy="ARB", asset=sig.asset,
                timeframe=sig.timeframe, outcome="ARB", outcome_id="burn_failed",
                market_id=sig.market_id, event_id=sig.event_id,
                entry_price=_safe_float(total_p),
                amount_ngn=_safe_float(amount_yes + amount_no),
                certainty=1.0, secs_to_close=0,
            )
            est_loss = -(amount_yes + amount_no) if not rollback_ok else 0.0
            await asyncio.to_thread(database.resolve_trade, trade_id, False, est_loss)
        except Exception as db_err:
            log.error(f"[{chat_id}] ARB failure could not be recorded: {db_err}")
