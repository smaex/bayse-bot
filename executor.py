import asyncio
import logging
from typing import Optional
import database
import feeds
import scanner
import telegram_bot
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
    base_pct = settings.get("risk_pct", 3.0) / 100.0
    min_t    = settings.get("mintrade", 100)
    max_t    = settings.get("maxtrade", 500_000)
    max_exp  = settings.get("maxexposure", 30.0) / 100.0
    learned  = settings.get("learned", {})
    mult     = learned.get("size_multipliers", {}).get(sig.strategy, 1.0)

    fx_factor = 0.5 if sig.asset in _FX_ASSETS else 1.0
    raw_pct = base_pct * mult * sig.certainty * fx_factor

    if equity < 2000 and sig.strategy != "ARB":
        raw_pct *= 0.5
        log.info(f"[{chat_id}] Small Account Mode: scaling {sig.strategy} size by 0.5x")

    amount  = equity * min(raw_pct, 0.05)
    amount  = max(min_t, min(max_t, amount))

    hard_cap = equity * 0.08
    if amount > hard_cap:
        log.info(
            f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — "
            f"Trade ₦{amount:,.0f} exceeds 8% hard cap (₦{hard_cap:,.0f})."
        )
        return

    if amount > free_cash:
        return

    if not risk.can_trade(equity, amount, max_exp):
        return

    fee_rate = settings.get("fee_rate", 0.04)
    est_net_payout = (1.0 - fee_rate) / sig.market_price
    if est_net_payout < 1.0 + MIN_PAYOUT_RATIO:
        return

    max_allowed_price = (1.0 - fee_rate) / (1.0 + MIN_PAYOUT_RATIO)
    is_fx = sig.asset in _FX_ASSETS
    slip_mult = 1.002 if is_fx else 1.02
    limit_price = min(sig.market_price * slip_mult, max_allowed_price) 

    log.info(
        f"[{chat_id}] PLACING {sig.strategy} | {sig.asset} {sig.timeframe} {sig.outcome} "
        f"@ {sig.market_price:.3f} certainty={sig.certainty:.0%} ₦{amount:,.0f}"
    )

    try:
        resp = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=sig.outcome_id, side="BUY", amount=amount,
            order_type="LIMIT", price=limit_price, currency=CURRENCY,
        )
        order = resp.get("order", resp)
        filled_price = float(order.get("price", limit_price) or limit_price)
        bayse_order_id = order.get("id") or order.get("orderId") or order.get("order_id")

        market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
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
        
        quote = await client.get_quote(sig.event_id, sig.market_id, market["yes_id"], "BUY", amount_yes, CURRENCY)
        if float(quote.get("sharesMatched", 0)) <= 0: return
    except Exception: return

    yes_ok = False; yes_shares = 0
    try:
        resp_yes = await client.place_order(sig.event_id, sig.market_id, market["yes_id"], "BUY", amount_yes, "MARKET", currency=CURRENCY)
        yes_ok = True
        y_ord = resp_yes.get("order", resp_yes)
        yes_shares = float(y_ord.get("shares", y_ord.get("quantity", 0)) or 0)
        if yes_shares <= 0: raise RuntimeError("YES fill 0")
        
        ws = feeds.market_prices.get(sig.market_id)
        if not ws: raise RuntimeError("Feed lost")
        live_no_p = ws["no"]
        amount_no = round(yes_shares * live_no_p, 2)
        
        if (yes_p + live_no_p) >= 0.995: raise RuntimeError("Arb gone")

        resp_no = await client.place_order(sig.event_id, sig.market_id, market["no_id"], "BUY", amount_no, "MARKET", currency=CURRENCY)
        n_ord = resp_no.get("order", resp_no)
        no_shares = float(n_ord.get("shares", n_ord.get("quantity", 0)) or 0)

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
