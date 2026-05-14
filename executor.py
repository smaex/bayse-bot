import asyncio
import logging
from typing import Optional
import database
import feeds
import scanner
import telegram_bot
import strategy
import comparative_analysis
from config import ARB_MAX_SIZE_NGN, CURRENCY, MIN_PAYOUT_RATIO

log = logging.getLogger("executor")

# These will be initialized by bot.py
active_markets = []
_tg_app = None
_FX_ASSETS = ["EURUSD", "GBPUSD", "EURGBP", "XAUUSD"]

def init_executor(markets, tg_app):
    global active_markets, _tg_app
    active_markets = markets
    _tg_app = tg_app

async def execute_trade(chat_id, sig, client, risk, settings, equity, free_cash):
    """decide and execute a single-sided trade"""
    mode = settings.get("mode", "balanced")
    min_t    = settings.get("mintrade", 100)
    max_t    = settings.get("maxtrade", 500_000)
    max_exp  = settings.get("maxexposure", 30.0) / 100.0
    learned  = settings.get("learned", {})
    mult     = learned.get("size_multipliers", {}).get(sig.strategy, 1.0)

    # ── Trailing Profit Shield (Strict Mode) ──
    if risk.is_in_strict_mode():
        # Once 80% of daily target is reached, only take 'God Tier' signals (>= 0.70)
        if sig.certainty < 0.70:
            log.info(f"[{chat_id}] SKIPPED — Strict Mode active (Gain Shield), certainty {sig.certainty:.2f} < 0.70")
            return

    # ── Conviction Sizing (Tiered Risk) ──────────────────────────────────────
    # Scales risk based on certainty: 1% (Low) -> 4% (High)
    if sig.certainty >= 0.90:
        base_pct = 0.04
    elif sig.certainty >= 0.70:
        base_pct = 0.03
    elif sig.certainty >= 0.55:
        base_pct = 0.02
    else:
        base_pct = 0.01  # Minimum (0.45 - 0.55 range)

    # Apply external multipliers (FX factor, ML learned multipliers)
    fx_factor = 0.5 if sig.asset in _FX_ASSETS else 1.0
    raw_pct = base_pct * mult * fx_factor

    # ── Conviction Booster (Extreme Certainty) ──
    if sig.certainty >= 0.95:
        conviction_mult = 2.0  # Slightly more conservative than 5x with new tiered base
        raw_pct *= conviction_mult
        log.info(f"[{chat_id}] 🔥 CONVICTION BOOSTER: Boosting size by {conviction_mult}x (Certainty {sig.certainty:.0%})")


    # ── Alpha Decay Shield (Anti-Bleed) ──
    # If the edge magnitude is shrinking over the last 10 trades, reduce size.
    decay_mult = await asyncio.to_thread(database.get_alpha_trend, chat_id, sig.strategy, sig.asset)
    if decay_mult < 0.85:
        raw_pct *= 0.5
        log.info(f"[{chat_id}] 🛡️ ALPHA DECAY SHIELD: Slashing size by 50% (Decay Factor: {decay_mult:.2f})")

    # ── Probationary Sizing ──
    if risk.is_on_probation():
        probation_mult = 0.25
        raw_pct *= probation_mult
        log.info(f"[{chat_id}] PROBATION ACTIVE: Slashing size by {1-probation_mult:.0%}")

    # ── Micro-Account Handling (For ₦2,000 starts) ─────────────────────────
    if equity < 3000:
        # On tiny accounts, we can't trade < ₦100. 
        # We force ₦100 but keep the strategy conviction filters.
        amount = 100.0
        log.info(f"[{chat_id}] Micro-Account Mode: forcing ₦100 minimum viable size")
    else:
        amount  = equity * min(raw_pct, 0.10)
        amount  = max(min_t, min(max_t, amount))

    hard_cap = equity * 0.08
    if amount > hard_cap:
        log.info(
            f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — "
            f"Trade ₦{amount:,.0f} exceeds 8% hard cap (₦{hard_cap:,.0f})."
        )
        return

    if amount > free_cash:
        log.info(f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — Insufficient free cash (Need ₦{amount:,.0f}, Have ₦{free_cash:,.0f})")
        return

    if not risk.can_trade(equity, amount, max_exp):
        log.info(f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — Risk limit or max exposure reached")
        return
    # ── Discovery Probe ──
    is_probe = False
    # Probes: Small trades to collect data even if certainty is modest
    if learned.get("discovery_mode") or (sig.certainty < 0.50 and mode != "safe"):
        is_probe = True
        amount = 100.0
        log.info(f"[{chat_id}] DISCOVERY PROBE: Sizing down to ₦100 for Bayesian data collection.")

    fee_rate = settings.get("fee_rate", 0.04)
    est_net_payout = (1.0 - fee_rate) / sig.market_price
    if est_net_payout < 1.0 + MIN_PAYOUT_RATIO:
        log.info(f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — est. net payout {(est_net_payout - 1.0):.1%} < {MIN_PAYOUT_RATIO:.1%} minimum")
        return

    # ── Dynamic Payout Hurdle (Flexibility Layer) ──
    # We adjust the "Minimum Profit Buffer" based on the user's selected mode.
    # Safe Mode demands 10%; Balanced 6%; Aggressive 3%; Full Send 1%.
    mode_hurdle = {"safe": 0.10, "balanced": 0.06, "aggressive": 0.03, "full_send": 0.01}.get(mode, 0.06)
    current_min_payout = mode_hurdle
    
    # Conviction Boost: If we are >90% sure, we are extra flexible on price
    if sig.certainty >= 0.90:
        current_min_payout = min(current_min_payout, 0.02)
        log.debug(f"[{chat_id}] Conviction Boost: Hurdle lowered to {current_min_payout:.1%}")

    max_allowed_price = (1.0 - fee_rate) / (1.0 + current_min_payout)
    is_fx = sig.asset in _FX_ASSETS
    
    # ── Adaptive Slippage ──
    slip_map = {"safe": 0.002, "balanced": 0.005, "aggressive": 0.01, "full_send": 0.025}
    base_slip = slip_map.get(mode, 0.005)
    
    # Volatility multiplier
    from strategies.utils import realized_vol_hourly
    from strategies.base import global_state
    import config
    current_vol = realized_vol_hourly(sig.asset, global_state)
    base_vol = config.ASSET_HOURLY_VOL.get(sig.asset, 0.022)
    vol_mult = current_vol / base_vol
    adaptive_slip = base_slip * vol_mult
    
    # ── Engine Detection ──
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    engine = market.get("engine", "AMM") if market else "AMM"

    # ── Hybrid Maker/Taker Routing ──
    is_maker = False
    time_in_force = "FAK"
    
    # Toxicity Check: Disable MAKER if volatility is elevated
    if engine == "CLOB" and vol_mult <= 1.5:
        normalized_threshold = 0.005 * vol_mult
        if abs(sig.momentum_at_entry) < normalized_threshold:
            is_maker = True
            limit_price = min(sig.market_price * 0.98, max_allowed_price)
            time_in_force = "GTC"
    
    if not is_maker:
        limit_price = min(sig.market_price * (1.0 + adaptive_slip), max_allowed_price)

    execution_style = "MAKER" if is_maker else "TAKER"
    log.info(
        f"[{chat_id}] PLACING {sig.strategy} | {sig.asset} ({engine} {execution_style}) "
        f"price={sig.market_price:.3f} limit={limit_price:.3f} slip={adaptive_slip:.2%} "
        f"certainty={sig.certainty:.0%} ₦{amount:,.0f}"
    )

    try:
        # ── Slippage Shield (Alpha Capture Phase) ──
        # Check recent performance. If slippage > 1.5%, reduce size by 50%
        avg_slip = await asyncio.to_thread(database.get_avg_slippage, sig.asset, sig.strategy)
        if avg_slip > 0.015:
            log.warning(f"[{chat_id}] SLIPPAGE SHIELD ACTIVE: Reducing size for {sig.asset} ({avg_slip:.1%} avg slip)")
            amount = amount * 0.5

        # Place the order
        resp = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=sig.outcome_id, side="BUY", amount=amount,
            order_type="LIMIT", price=limit_price, currency=CURRENCY,
            time_in_force=time_in_force
        )
        order = resp.get("order", resp)
        shares_filled = float(order.get("shares", order.get("quantity", 0)) or 0)
        
        # ── Order Chaser (CLOB only) ──
        # If CLOB and zero fill, try one more time at a slightly more aggressive price (if still profitable)
        if engine == "CLOB" and not is_maker and shares_filled <= 0:
            chase_price = min(limit_price * 1.002, max_allowed_price)
            if chase_price > limit_price:
                log.info(f"[{chat_id}] CLOB CHASE: First order missed. Retrying at {chase_price:.3f}")
                resp = await client.place_order(
                    event_id=sig.event_id, market_id=sig.market_id,
                    outcome_id=sig.outcome_id, side="BUY", amount=amount,
                    order_type="LIMIT", price=chase_price, currency=CURRENCY,
                )
                order = resp.get("order", resp)
                shares_filled = float(order.get("shares", order.get("quantity", 0)) or 0)

        if shares_filled <= 0:
            log.info(f"[{chat_id}] {sig.asset} order not filled (price moved away)")
            return

        filled_price = float(order.get("price", limit_price) or limit_price)
        bayse_order_id = order.get("id") or order.get("orderId") or order.get("order_id")

        spot_vs_thresh = 0.0
        if market and market.get("threshold") and feeds.spot.get(sig.asset):
            spot_vs_thresh = (feeds.spot[sig.asset] - market["threshold"]) / market["threshold"]

        trade_id = await asyncio.to_thread(
            database.record_trade,
            chat_id=chat_id, strategy=sig.strategy, asset=sig.asset,
            timeframe=sig.timeframe, outcome=sig.outcome, outcome_id=sig.outcome_id,
            market_id=sig.market_id, event_id=sig.event_id, order_id=bayse_order_id,
            entry_price=filled_price, amount_ngn=amount, certainty=sig.certainty,
            secs_to_close=market["secs_to_close"] if market else 0,
            spot_vs_threshold_pct=spot_vs_thresh,
            momentum_at_entry=sig.momentum_at_entry,
            regime_at_entry=sig.regime_at_entry,
            edge_at_entry=sig.edge_at_entry,
            realized_vol_at_entry=sig.realized_vol_at_entry,
            market_price_at_entry=sig.market_price,
            slippage_ngn=((filled_price / sig.market_price) - 1.0) * amount if sig.market_price > 0 else 0,
            poly_price_at_entry=await comparative_analysis.get_comparative_price(sig.asset, market.get("threshold", 0)) if market else None
        )

        risk.add_position(sig.market_id, {
            "trade_id": trade_id, "event_id": sig.event_id,
            "outcome": sig.outcome, "outcome_id": sig.outcome_id,
            "entry_price": filled_price, "amount_ngn": amount,
            "strategy": sig.strategy, "asset": sig.asset, "timeframe": sig.timeframe,
        })

        if _tg_app:
            await telegram_bot.notify_trade(_tg_app, chat_id, sig, amount)

    except Exception as e:
        log.error(f"[{chat_id}] order failed {sig.market_id}: {e}")

async def execute_arb(chat_id, sig, client, equity, free_cash, settings):
    """Executes a market-neutral arbitrage"""
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not market: return

    budget = min(ARB_MAX_SIZE_NGN, free_cash * 0.10)
    if budget < 200: return

    try:
        yes_p = market["yes_price"]; no_p = market["no_price"]
        amount_yes = round(budget * (yes_p / (yes_p + no_p)), 2)
        amount_no = round(budget * (no_p / (yes_p + no_p)), 2)
        
        quote = await client.get_quote(sig.event_id, sig.market_id, market["yes_id"], "BUY", amount_yes, CURRENCY)
        if float(quote.get("sharesMatched", 0)) <= 0: return
    except Exception: return

    yes_ok = False; yes_shares = 0
    try:
        batch_orders = [
            {
                "outcomeId": market["yes_id"],
                "side": "BUY",
                "type": "MARKET",
                "amount": amount_yes,
                "currency": CURRENCY,
                "timeInForce": "FAK"
            },
            {
                "outcomeId": market["no_id"],
                "side": "BUY",
                "type": "MARKET",
                "amount": amount_no,
                "currency": CURRENCY,
                "timeInForce": "FAK"
            }
        ]
        
        batch_resp = await client.batch_place_orders(batch_orders)
        results = batch_resp.get("results", [])
        if len(results) < 2:
            raise RuntimeError("Invalid batch response")
            
        yes_res = results[0]
        no_res = results[1]
        
        yes_ok = yes_res.get("success", False)
        if yes_ok:
            y_ord = yes_res.get("order", {})
            yes_shares = float(y_ord.get("shares", y_ord.get("quantity", 0)) or 0)
            
        no_ok = no_res.get("success", False)
        no_shares = 0
        if no_ok:
            n_ord = no_res.get("order", {})
            no_shares = float(n_ord.get("shares", n_ord.get("quantity", 0)) or 0)
            
        if not yes_ok or not no_ok:
            raise RuntimeError(f"Batch incomplete. YES ok: {yes_ok}, NO ok: {no_ok}")

        burn_pairs = min(yes_shares, no_shares)
        if burn_pairs > 0:
            await client.burn_shares(sig.market_id, burn_pairs, CURRENCY)
            profit = burn_pairs - (amount_yes + amount_no)
            log.info(f"[{chat_id}] ARB SUCCESS | {sig.asset} | {burn_pairs:.2f} pairs | Profit: ₦{profit:,.2f}")
            
            trade_id = await asyncio.to_thread(
                database.record_trade,
                chat_id=chat_id, strategy="ARB", asset=sig.asset, timeframe=sig.timeframe,
                outcome="ARB", outcome_id="burn", market_id=sig.market_id, event_id=sig.event_id,
                order_id=str(burn_pairs), entry_price=yes_p + no_p, amount_ngn=budget,
                certainty=1.0, secs_to_close=0, spot_vs_threshold_pct=0.0
            )
            await asyncio.to_thread(database.resolve_trade, trade_id, won=True, pnl_ngn=profit)
            if _tg_app: await telegram_bot.notify_arb(_tg_app, chat_id, sig, burn_pairs, profit)

    except Exception as e:
        log.error(f"[{chat_id}] ARB error: {e}")
        if yes_ok and yes_shares > 0:
            try:
                await client.place_order(sig.event_id, sig.market_id, market["yes_id"], "SELL", amount_yes, "MARKET", currency=CURRENCY)
                log.info(f"[{chat_id}] ARB ROLLBACK SUCCESS")
            except Exception as re:
                log.critical(f"[{chat_id}] ARB ROLLBACK FAILED: {re}")
