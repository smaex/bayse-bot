"""
Trade executor — places orders, records trades, handles AMM + CLOB routing.

Key fixes:
  - Fee floor corrected to 0.3 (was 0.5)
  - Removed CLOB maker/GTC logic — maker orders at 1% below market never fill
    on prediction markets with fixed expiry. Now uses:
      AMM  → MARKET order (as before)
      CLOB → LIMIT at market price with FAK (fills immediately as taker, or cancels cleanly)
  - Zero-fill assumption only applies to AMM, never to CLOB
  - AMM fill parsing checks 'quantity' first (AMM field), then 'filledSize' (CLOB field)
"""

import asyncio
import logging
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

# Engine type cache: market_id → "AMM" | "CLOB"
_market_engine_cache: dict[str, str] = {}

# Per-market minimum NGN cache (from exchange error messages)
_market_min_cache: dict[str, float] = {}

# Per-market trade cooldown: market_id → last trade timestamp
_trade_cooldown: dict[str, float] = {}
TRADE_COOLDOWN_SEC = 60

# Bayse platform minimum trade (confirmed ₦100)
MIN_TRADE_NGN = 100.0


def init_executor(markets, tg_app):
    global active_markets, _tg_app
    active_markets = markets
    _tg_app        = tg_app


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
    """Entry point — locks the market and delegates to _execute_logic."""
    if strategy.global_state.systemic_halt_until > time.time():
        return
    if risk.already_in(sig.market_id):
        return
    last = _trade_cooldown.get(sig.market_id, 0.0)
    if time.time() - last < TRADE_COOLDOWN_SEC:
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

    # ── Strict mode: near daily target, only take high-conviction signals ──
    if risk.is_in_strict_mode() and sig.certainty < 0.70:
        return

    # ── Conviction sizing ──
    if sig.certainty >= 0.90:
        tier = 2.0
    elif sig.certainty >= 0.70:
        tier = 1.5
    elif sig.certainty >= 0.55:
        tier = 1.0
    else:
        tier = 0.5

    fx_factor = 0.5 if sig.asset in _FX_ASSETS else 1.0
    raw_pct   = user_risk * tier * mult * fx_factor

    # Conviction booster at extreme certainty
    if sig.certainty >= 0.95:
        raw_pct *= 1.5

    # Alpha decay shield — uses hasattr so it degrades gracefully if not in db
    if hasattr(database, "get_alpha_trend"):
        decay = await asyncio.to_thread(
            database.get_alpha_trend, chat_id, sig.strategy, sig.asset
        )
        if decay < 0.85:
            raw_pct *= 0.5

    # Probation sizing
    if risk.is_on_probation():
        raw_pct *= 0.25

    # ── Compute NGN amount ──
    if equity < 3_000:
        amount = MIN_TRADE_NGN
    else:
        capped = min(raw_pct, user_risk * 3.0)
        amount = max(min_t, min(max_t, equity * capped))

    # Hard cap: lower of 5% equity and user maxtrade
    hard_cap = min(max(MIN_TRADE_NGN, equity * 0.05), max_t)
    if amount > hard_cap:
        amount = hard_cap

    effective_min = max(MIN_TRADE_NGN, min_t)
    if amount < effective_min:
        return
    if amount > free_cash:
        return

    # ── EV check ──
    fee_rate = _get_market_fee(sig.market_id)
    eff_fee  = _effective_fee(fee_rate, sig.market_price)
    ev       = sig.win_prob * (1.0 - eff_fee) / sig.market_price - 1.0

    # Discovery probe: low-certainty signal → tiny ₦100 data collection trade
    is_probe = sig.certainty >= 0.35 and sig.certainty < sig.mode_floor
    if is_probe:
        amount = MIN_TRADE_NGN
    elif ev < 0.01:
        log.info(f"[{chat_id}] SKIP {sig.strategy} {sig.asset} — EV {ev:+.1%}")
        return

    if sig.market_price > 0.88:
        return

    if equity >= 3_000 and not risk.can_trade(equity, amount, max_exp):
        return

    # Pre-flight: cached market minimum
    cached_min = _market_min_cache.get(sig.market_id, 0.0)
    if cached_min > 0 and amount < cached_min:
        return

    # ── Engine detection ──
    market   = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    declared = market.get("engine") if market else None
    if declared:
        engine = declared
    elif market:
        engine = await _infer_engine(client, market)
    else:
        engine = "AMM"

    # ── Order routing ─────────────────────────────────────────────────────────
    #
    # CRITICAL: Do NOT use GTC LIMIT orders on prediction markets.
    # A maker bid 1% below market price will NEVER fill before market close.
    # The order gets submitted → sits open → market closes → full refund.
    # Net result: zero trade, wasted cooldown, phantom position in DB.
    #
    # Correct approach:
    #   AMM  → MARKET order (AMM prices everything at current curve)
    #   CLOB → LIMIT at exact market price with FAK
    #          (crosses the spread immediately as taker, or cancels cleanly)
    # ─────────────────────────────────────────────────────────────────────────

    slip_map  = {"safe": 0.003, "balanced": 0.005, "aggressive": 0.01, "full_send": 0.02}
    slippage  = slip_map.get(mode, 0.005)
    max_valid = (1.0 - eff_fee) / 1.01

    if engine == "AMM":
        order_type    = "MARKET"
        time_in_force = "FAK"
        limit_price   = round(min(sig.market_price * (1.0 + slippage), max_valid), 3)
    else:
        # CLOB: limit AT market price with FAK so it fills immediately (taker)
        # or cancels instantly if the book has moved — never lingers as GTC
        order_type    = "LIMIT"
        time_in_force = "FAK"
        limit_price   = round(min(sig.market_price * (1.0 + slippage), max_valid), 3)

    log.info(
        f"[{chat_id}] PLACING {sig.strategy} {sig.asset} {sig.timeframe} "
        f"{sig.outcome} | {engine} {order_type}/{time_in_force} "
        f"₦{amount:,.0f} @ {sig.market_price:.3f} | certainty={sig.certainty:.0%}"
    )

    try:
        resp  = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=sig.outcome_id, side="BUY",
            amount=amount, order_type=order_type,
            price=limit_price if order_type == "LIMIT" else None,
            max_slippage=slippage, currency=CURRENCY,
            time_in_force=time_in_force,
        )
        order = resp.get("order") or resp.get("clobOrder") or resp.get("ammOrder") or resp

        # AMM uses 'quantity'; CLOB uses 'filledSize' / 'sharesMatched'
        shares_filled = client.parse_filled_shares(order)
        filled_price  = float(order.get("avgFillPrice") or order.get("price") or limit_price)
        order_id      = order.get("id") or order.get("orderId") or order.get("order_id")

        if shares_filled <= 0:
            if engine == "AMM":
                # AMM rarely returns fill details — estimate from intent if order_id present
                if order_id:
                    shares_filled = (
                        amount / (filled_price * 100.0)
                        if CURRENCY == "NGN" else amount / filled_price
                    )
                    log.warning(f"[{chat_id}] AMM zero-fill estimate for {sig.asset}")
                else:
                    log.info(f"[{chat_id}] AMM order rejected — no order_id returned")
                    return
            else:
                # CLOB FAK order didn't cross the spread — book moved, no fill
                # This is a clean no-trade, cooldown still applies to avoid hammering
                log.info(f"[{chat_id}] CLOB FAK not filled for {sig.asset} — market moved")
                _trade_cooldown[sig.market_id] = time.time()
                return

        actual_ngn = shares_filled * filled_price * (100.0 if CURRENCY == "NGN" else 1.0)

        spot_vs_thresh = 0.0
        if market and market.get("threshold") and feeds.spot.get(sig.asset):
            spot_vs_thresh = (feeds.spot[sig.asset] - market["threshold"]) / market["threshold"]

        trade_id = await asyncio.to_thread(
            database.record_trade,
            chat_id=chat_id,
            strategy=sig.strategy, asset=sig.asset, timeframe=sig.timeframe,
            outcome=sig.outcome, outcome_id=sig.outcome_id,
            market_id=sig.market_id, event_id=sig.event_id, order_id=order_id,
            entry_price=filled_price, amount_ngn=actual_ngn,
            certainty=sig.certainty,
            secs_to_close=market["secs_to_close"] if market else 0,
            spot_vs_threshold_pct=spot_vs_thresh,
            momentum_at_entry=getattr(sig, "momentum_at_entry", 0.0),
            regime_at_entry=getattr(sig, "regime_at_entry", 0.0),
            edge_at_entry=getattr(sig, "edge_at_entry", 0.0),
            realized_vol_at_entry=getattr(sig, "realized_vol_at_entry", 0.0),
            market_price_at_entry=sig.market_price,
            slippage_ngn=(
                ((filled_price / sig.market_price) - 1.0) * actual_ngn
                if sig.market_price > 0 else 0
            ),
            engine=engine,
        )

        risk.add_position(sig.market_id, {
            "trade_id":    trade_id,   "event_id":   sig.event_id,
            "outcome":     sig.outcome, "outcome_id": sig.outcome_id,
            "entry_price": filled_price, "amount_ngn": actual_ngn,
            "strategy":    sig.strategy, "asset":      sig.asset,
            "timeframe":   sig.timeframe,
        })
        risk.current_free_cash -= actual_ngn
        _trade_cooldown[sig.market_id] = time.time()

        log.info(
            f"[{chat_id}] ✅ FILLED | {sig.strategy} {sig.asset} {sig.timeframe} "
            f"{sig.outcome} @ {filled_price:.4f} ₦{actual_ngn:,.0f} | id={trade_id}"
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
        return

    if _tg_app:
        try:
            await telegram_bot.notify_trade(
                _tg_app, chat_id, sig, actual_ngn,
                engine=engine, execution_style=order_type
            )
        except Exception as ne:
            log.error(f"[{chat_id}] Notification failed: {ne}")


def _get_market_fee(market_id: str) -> float:
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    return market.get("fee_rate", 0.02) if market else 0.02


# ── ARB execution ─────────────────────────────────────────────────────────────

async def execute_arb(chat_id: str, sig, client, equity: float, free_cash: float, settings: dict):
    """Buy both YES and NO then burn pairs — risk-free when YES+NO < 1.00."""
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not market:
        return

    budget = min(ARB_MAX_SIZE_NGN, free_cash * 0.10)
    if budget < MIN_TRADE_NGN * 2:
        return

    yes_p = market["yes_price"]
    no_p  = market["no_price"]

    amount_yes = round(budget * (yes_p / (yes_p + no_p)), 2)
    amount_no  = round(budget - amount_yes, 2)

    if amount_yes < MIN_TRADE_NGN or amount_no < MIN_TRADE_NGN:
        return

    yes_shares = 0.0
    no_shares  = 0.0
    yes_ok     = False

    try:
        # Leg 1 — YES
        resp_yes   = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["yes_id"], side="BUY",
            amount=amount_yes, order_type="MARKET", currency=CURRENCY,
        )
        order_yes  = resp_yes.get("order") or resp_yes
        yes_shares = client.parse_filled_shares(order_yes)
        if yes_shares <= 0:
            yes_shares = amount_yes / yes_p
        yes_ok = True

        # Leg 2 — NO
        resp_no   = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["no_id"], side="BUY",
            amount=amount_no, order_type="MARKET", currency=CURRENCY,
        )
        order_no  = resp_no.get("order") or resp_no
        no_shares = client.parse_filled_shares(order_no)
        if no_shares <= 0:
            no_shares = amount_no / no_p

        # Burn matching pairs
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
                entry_price=yes_p + no_p, amount_ngn=budget,
                certainty=1.0, secs_to_close=0,
            )
            await asyncio.to_thread(database.resolve_trade, trade_id, True, profit)
            if _tg_app:
                await telegram_bot.notify_arb(_tg_app, chat_id, sig, burn_qty, profit)

    except Exception as e:
        log.error(f"[{chat_id}] ARB error: {e}")
        if yes_ok and yes_shares > 0:
            try:
                await client.place_order(
                    sig.event_id, sig.market_id, market["yes_id"],
                    "SELL", amount_yes, "MARKET", currency=CURRENCY,
                )
                log.info(f"[{chat_id}] ARB rollback OK")
            except Exception as re_:
                log.critical(f"[{chat_id}] ARB ROLLBACK FAILED: {re_}")
