"""
Multi-user trading bot — one server, all users managed through Telegram.

Architecture:
  Shared:    spot price feeds, active markets, news signals
  Per-user:  BayseClient, RiskManager, daily profit tracker, trade records
  Keep-alive: aiohttp web server on PORT + self-ping every 13 minutes
"""

import asyncio
import logging
import os
import sys
from datetime import date

from aiohttp import web, ClientSession, ClientTimeout

import database
import feeds
import news as news_mod
import scanner
import strategy
import learner
import telegram_bot
from risk import RiskManager
from client import BayseClient
from config import (
    TELEGRAM_TOKEN, CURRENCY, SCAN_INTERVAL_SECONDS, ARB_MAX_SIZE_NGN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # Hide Telegram API tokens from logs
log = logging.getLogger("bot")

# ── Shared state ───────────────────────────────────────────────────────────────
active_markets: list[dict]          = []
_user_clients:  dict[str, BayseClient]  = {}
_user_risks:    dict[str, RiskManager]  = {}
_user_daily:    dict[str, dict]         = {}  # {chat_id: {date, start_balance, target_hit}}
_user_tasks:    dict[str, asyncio.Task] = {}
_last_balance:  dict[str, float]        = {}  # for deposit/withdrawal detection
_scan_client:   BayseClient | None = None
_tg_app        = None

# Minimum change to be considered a deposit/withdrawal (not just trade P&L noise)
_BALANCE_EVENT_MIN_NGN = 500
_BALANCE_EVENT_MIN_PCT = 0.08   # 8% of balance


def _get_client(user: dict) -> BayseClient:
    cid = user["chat_id"]
    if cid not in _user_clients:
        _user_clients[cid] = BayseClient(user["public_key"], user["secret_key"])
    return _user_clients[cid]


def _get_risk(chat_id: str) -> RiskManager:
    if chat_id not in _user_risks:
        _user_risks[chat_id] = RiskManager()
    return _user_risks[chat_id]


def _daily(chat_id: str, balance: float, settings: dict) -> dict:
    today = date.today().isoformat()
    ds = settings.get("daily_state", {})
    if ds.get("date") != today:
        ds = {"date": today, "start_balance": balance, "target_hit": False}
        settings["daily_state"] = ds
        database.update_settings(chat_id, settings)
    _user_daily[chat_id] = ds  # keep in-memory cache in sync
    return ds


def _daily_target(settings: dict, start_balance: float) -> float:
    absolute = settings.get("daily_target_ngn", 0)
    if absolute > 0:
        return float(absolute)
    # daily_multiplier is a percentage — 10 means 10% of starting balance
    return start_balance * settings.get("daily_multiplier", 10) / 100


# ── User lifecycle ─────────────────────────────────────────────────────────────

def start_user(chat_id: str):
    """Launch a trading loop for a user. Called on setup completion and restart."""
    global _scan_client
    user = database.get_user(chat_id)
    if not user:
        return
    client = _get_client(user)
    if _scan_client is None:
        _scan_client = client

    # ── Reconstruct open positions from DB ─────────────────────────────────────
    # After a restart, risk.open_positions is empty.  Without this, the exposure
    # cap is bypassed until trades resolve — the bot could over-deploy capital.
    risk = _get_risk(chat_id)
    if not risk.open_positions:
        unresolved = database.get_all_unresolved(chat_id)
        for trade in unresolved:
            mid = trade.get("market_id")
            if mid and mid not in risk.open_positions:
                risk.add_position(mid, {
                    "trade_id":    trade["trade_id"],
                    "event_id":    trade["event_id"],
                    "outcome":     trade["outcome"],
                    "outcome_id":  trade["outcome_id"],
                    "entry_price": trade["entry_price"],
                    "amount_ngn":  trade["amount_ngn"],
                    "strategy":    trade["strategy"],
                    "asset":       trade["asset"],
                    "timeframe":   trade["timeframe"],
                })
        if unresolved:
            log.info(
                f"[{chat_id}] Reconstructed {len(unresolved)} open positions from DB "
                f"(deployed ₦{risk.deployed():,.0f})"
            )

    if chat_id not in _user_tasks or _user_tasks[chat_id].done():
        _user_tasks[chat_id] = asyncio.create_task(_user_loop(chat_id))
        log.info(f"Trading loop started for {chat_id}")


async def _user_loop(chat_id: str):
    """Per-user async trading loop — runs every 10 seconds."""
    strategy.set_user_context(chat_id)   # tags every strategy log line with this user
    sent_news: set[str] = set()
    iter_count = 0

    while True:
        await asyncio.sleep(10)
        iter_count += 1
        user = database.get_user(chat_id)
        if not user or not user.get("is_active"):
            log.info(f"[{chat_id}] trading loop exiting (user inactive)")
            break
        settings = user["settings"]
        if settings.get("paused"):
            if iter_count % 18 == 0:  # log every 3 minutes while paused
                log.info(f"[{chat_id}] trading paused — send /resume to restart")
            continue
        if iter_count % 18 == 0:  # heartbeat every 3 minutes
            log.info(f"[{chat_id}] loop alive (iter={iter_count}, spot={feeds.spot})")

        client = _get_client(user)
        risk   = _get_risk(chat_id)

        try:
            free_cash = await client.get_balance_ngn()
        except Exception as e:
            log.warning(f"[{chat_id}] balance fetch failed: {e}")
            continue

        equity = free_cash + risk.deployed()
        risk.update_peak(equity)

        # ── Deposit / withdrawal detection ─────────────────────────────────────
        last_bal = _last_balance.get(chat_id)
        if last_bal is not None:
            delta     = equity - last_bal
            threshold = max(_BALANCE_EVENT_MIN_NGN, last_bal * _BALANCE_EVENT_MIN_PCT)
            if delta > threshold:
                # Deposit detected — re-anchor so it doesn't look like profit
                log.info(f"[{chat_id}] Deposit detected: ₦{delta:+,.0f} (₦{last_bal:,.0f} → ₦{equity:,.0f})")
                day_now = _user_daily.get(chat_id, {})
                day_now["start_balance"] = equity
                settings["daily_state"]  = day_now
                database.update_settings(chat_id, settings)
                risk.peak_balance = equity
                _user_daily[chat_id] = day_now
                if _tg_app:
                    await telegram_bot.notify_deposit_detected(
                        _tg_app, chat_id, delta, "NGN"
                    )
            elif delta < -threshold:
                # Withdrawal detected — re-anchor peak so drawdown isn't triggered
                log.info(f"[{chat_id}] Withdrawal detected: ₦{delta:,.0f} (₦{last_bal:,.0f} → ₦{equity:,.0f})")
                day_now = _user_daily.get(chat_id, {})
                day_now["start_balance"] = equity
                settings["daily_state"]  = day_now
                database.update_settings(chat_id, settings)
                risk.peak_balance = equity
                _user_daily[chat_id] = day_now
                if _tg_app:
                    await telegram_bot.send_message(
                        _tg_app, chat_id,
                        f"💸 *Withdrawal detected* — ₦{abs(delta):,.0f} removed\n\n"
                        f"New balance: ₦{equity:,.2f}\n"
                        f"Drawdown baseline reset. Trading continues from here.",
                        parse_mode="Markdown",
                    )
        _last_balance[chat_id] = equity

        # ── Daily target ───────────────────────────────────────────────────────
        day          = _daily(chat_id, equity, settings)
        profit_today = equity - day["start_balance"]
        target       = _daily_target(settings, day["start_balance"])
        if target > 0 and profit_today >= target and not day["target_hit"]:
            day["target_hit"] = True
            settings["daily_state"] = day
            settings["paused"] = True
            database.update_settings(chat_id, settings)
            if _tg_app:
                await telegram_bot.send_message(
                    _tg_app, chat_id,
                    f"🎯 *Daily target reached!*\n\n"
                    f"Profit today: ₦{profit_today:+,.0f}\n"
                    f"Target: ₦{target:,.0f}\n\n"
                    f"Trading paused until midnight. /resume to override.",
                    parse_mode="Markdown",
                )
            continue

        # ── Drawdown check ─────────────────────────────────────────────────────
        if not risk.check_drawdown(equity):
            dd = (risk.peak_balance - equity) / risk.peak_balance
            settings["paused"] = True
            database.update_settings(chat_id, settings)
            if _tg_app:
                await telegram_bot.notify_drawdown(_tg_app, chat_id, equity, risk.peak_balance, dd)
            continue

        # ── News notifications ─────────────────────────────────────────────────
        for sig in news_mod.active_signals:
            key = f"{sig.source}:{sig.headline[:40]}"
            if key not in sent_news and sig.strength() > 0.4:
                sent_news.add(key)
                if _tg_app:
                    await telegram_bot.notify_news(
                        _tg_app, chat_id,
                        sig.headline, sig.direction, sig.assets, sig.strength(),
                    )

        # ── Signal evaluation ──────────────────────────────────────────────────
        user_assets  = settings.get("assets",     ["BTC", "ETH", "SOL"])
        user_tfs     = settings.get("timeframes",  ["5min", "15min", "1h"])
        user_strats  = settings.get("strategies",  ["SNIPE", "CORRELATE", "ARB", "NEWS"])
        learned      = settings.get("learned",     {})
        suspended    = learned.get("suspended_strategies", [])
        active_strats = [s for s in user_strats if s not in suspended]
        max_exp      = settings.get("maxexposure", 30.0) / 100.0

        try:
            for market in active_markets:
                if market.get("status") != "open":
                    continue
                if market["asset"] not in user_assets:
                    continue
                if market["timeframe"] not in user_tfs:
                    continue

                ws = feeds.market_prices.get(market["market_id"])
                if ws:
                    market["yes_price"] = ws["yes"]
                    market["no_price"]  = ws["no"]

                signals = strategy.evaluate(market, strategies=active_strats, learned=learned)
                for sig in signals:
                    if sig.strategy == "ARB":
                        await _execute_arb(chat_id, sig, client, equity, free_cash, settings)
                    elif not risk.already_in(sig.market_id):
                        await _execute_trade(
                            chat_id, sig, client, risk, equity, free_cash, settings, learned, max_exp
                        )
                    break  # best signal per market per tick
        except Exception as e:
            log.error(f"[{chat_id}] market eval error (iter={iter_count}): {e}", exc_info=True)


_FX_ASSETS = {"EURUSD", "GBPUSD", "EURGBP", "XAUUSD"}


async def _execute_trade(chat_id, sig, client, risk, equity, free_cash, settings, learned, max_exp):
    mult     = learned.get("size_multipliers", {}).get(sig.strategy, 1.0)
    base_pct = settings.get("risk_pct", 3.0) / 100.0
    min_t    = settings.get("mintrade",  100)
    max_t    = settings.get("maxtrade",  500_000)

    # FX trades are sized at 50% of normal — the diffusion edge is thinner on FX
    # (lower vol = smaller absolute move advantage), so smaller positions reduce
    # loss impact while wins still compound at full certainty-scaled rate.
    fx_factor = 0.5 if sig.asset in _FX_ASSETS else 1.0

    # Scale position size by signal certainty: a 35%-certain trade uses 35% of base risk,
    # a 99%-certain trade uses 99%. High-conviction signals earn larger positions automatically.
    raw_pct = base_pct * mult * sig.certainty * fx_factor
    amount  = equity * min(raw_pct, 0.05)   # hard cap at 5% regardless
    amount  = max(min_t, min(max_t, amount))

    # Safety Guard: Ensure mintrade does not force a massively oversized trade
    hard_cap = equity * 0.15
    if amount > hard_cap:
        log.warning(
            f"[{chat_id}] REJECTED {sig.strategy} | {sig.asset} — "
            f"Trade amount ₦{amount:,.0f} exceeds 15% bankroll hard cap (₦{hard_cap:,.0f}). "
            f"Consider lowering mintrade."
        )
        return

    if amount > free_cash:
        return

    if not risk.can_trade(equity, amount, max_exp):
        return

    log.info(
        f"[{chat_id}] PLACING {sig.strategy} | {sig.asset} {sig.timeframe} {sig.outcome} "
        f"@ {sig.market_price:.3f} certainty={sig.certainty:.0%} ₦{amount:,.0f}"
    )

    try:
        resp = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=sig.outcome_id, side="BUY", amount=amount,
            order_type="MARKET", max_slippage=0.05, currency=CURRENCY,
        )
        order        = resp.get("order", resp)
        filled_price = float(order.get("price", sig.market_price) or sig.market_price)
        bayse_order_id = order.get("id") or order.get("orderId") or order.get("order_id")

        log.info(
            f"[{chat_id}] PLACED {sig.strategy} | {sig.asset} {sig.timeframe} {sig.outcome} "
            f"filled={filled_price:.3f} ₦{amount:,.0f} trade_id pending"
        )

        market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
        spot_vs_thresh = 0.0
        if market and market.get("threshold") and feeds.spot.get(sig.asset):
            spot_vs_thresh = (feeds.spot[sig.asset] - market["threshold"]) / market["threshold"]

        trade_id = database.record_trade(
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


async def _execute_arb(chat_id, sig, client, equity, free_cash, settings):
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not market:
        return

    # Re-fetch live prices from WebSocket cache — signal may be stale
    ws = feeds.market_prices.get(sig.market_id)
    yes_p = ws["yes"] if ws else market["yes_price"]
    no_p  = ws["no"]  if ws else market["no_price"]
    if yes_p <= 0 or no_p <= 0:  # prices not yet received from WS — skip
        return
    if yes_p + no_p >= 0.99:  # tight buffer — any sum ≥0.99 is not worth the slippage
        return

    min_t     = settings.get("mintrade", 100)
    max_t     = settings.get("maxtrade", 500_000)

    # Each leg must meet Bayse's minimum order size independently
    budget     = min(ARB_MAX_SIZE_NGN, max_t, equity * 0.05)
    
    if budget > free_cash:
        budget = free_cash
        
    min_budget = min_t / min(yes_p, no_p)
    if budget / (yes_p + no_p) < min_budget:
        budget = min_budget * (yes_p + no_p)

    max_pairs = int(budget / (yes_p + no_p))
    if max_pairs * yes_p < min_t or max_pairs * no_p < min_t:
        return  # can't meet platform minimum on both legs — skip

    profit_est = max_pairs * (1.00 - yes_p - no_p)
    log.info(f"[{chat_id}] ARB {sig.asset}: {max_pairs} pairs → est ₦{profit_est:,.2f}")

    amount_yes = round(max_pairs * yes_p, 2)
    amount_no  = round(max_pairs * no_p, 2)

    yes_ok = False
    try:
        # ── Leg 1: buy YES ─────────────────────────────────────────────────────
        await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["yes_id"], side="BUY",
            amount=amount_yes, order_type="MARKET", currency=CURRENCY,
        )
        yes_ok = True

        # Re-check prices between legs — someone may have front-run us
        ws2 = feeds.market_prices.get(sig.market_id)
        if ws2:
            live_sum = ws2["yes"] + ws2["no"]
            if live_sum >= 0.99:
                log.warning(
                    f"[{chat_id}] ARB {sig.asset}: price moved between legs "
                    f"(sum now {live_sum:.3f}). Rolling back YES leg."
                )
                try:
                    await client.place_order(
                        event_id=sig.event_id, market_id=sig.market_id,
                        outcome_id=market["yes_id"], side="SELL",
                        amount=amount_yes, order_type="MARKET", currency=CURRENCY,
                    )
                except Exception as re:
                    log.error(f"[{chat_id}] ARB rollback sell failed: {re}")
                return

        # ── Leg 2: buy NO ──────────────────────────────────────────────────────
        await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=market["no_id"], side="BUY",
            amount=amount_no, order_type="MARKET", currency=CURRENCY,
        )

        # ── Leg 3: burn for ₦1.00 per pair ─────────────────────────────────────
        await client.burn_shares(sig.market_id, max_pairs, CURRENCY)
        if _tg_app:
            await telegram_bot.notify_arb(_tg_app, chat_id, sig, max_pairs, profit_est)

    except Exception as e:
        log.error(f"[{chat_id}] ARB failed {sig.market_id}: {e}")
        # If YES leg succeeded but NO or burn failed → sell YES back
        if yes_ok:
            log.warning(f"[{chat_id}] ARB rolling back YES leg after failure")
            try:
                await client.place_order(
                    event_id=sig.event_id, market_id=sig.market_id,
                    outcome_id=market["yes_id"], side="SELL",
                    amount=amount_yes, order_type="MARKET", currency=CURRENCY,
                )
                log.info(f"[{chat_id}] ARB YES leg rolled back successfully")
            except Exception as re:
                log.error(f"[{chat_id}] ARB rollback sell ALSO failed: {re}")


# ── Shared scan loop ───────────────────────────────────────────────────────────

async def _scan_loop():
    global active_markets
    while True:
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        if not _scan_client:
            continue
        try:
            active_markets = await scanner.scan_all(_scan_client)
            telegram_bot._active_markets = active_markets
            log.info(f"Rescanned: {len(active_markets)} markets")
            feeds.restart_bayse_feed(active_markets, _on_market_update)
        except Exception as e:
            log.warning(f"Scan failed: {e}")


def _refresh_timers():
    for m in active_markets:
        m["secs_to_close"] = scanner._seconds_to_close(m.get("closing_date", ""))


def _on_spot_price(asset: str, price: float):
    strategy.update_price_history(asset, price)
    log.debug(f"Spot {asset}: {price:,.4f}")


def _on_market_update(market_id: str, prices: dict):
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    if market and market.get("asset") == "BTC":
        strategy.record_btc_move(market, prices.get("yes", market["yes_price"]))


# ── Keep-alive web server ──────────────────────────────────────────────────────

async def _keep_alive_server():
    async def _ping(_req):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", _ping)
    app.router.add_get("/ping", _ping)

    port   = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Keep-alive server on port {port}")


async def _self_ping_loop():
    """Hit our own /ping every 13 minutes to prevent idle shutdown."""
    url = (
        os.environ.get("APP_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or ""
    ).rstrip("/")
    if not url:
        return
    log.info(f"Self-ping active → {url}/ping every 13 min")
    await asyncio.sleep(60)   # let the server start first
    async with ClientSession() as session:
        while True:
            await asyncio.sleep(780)  # 13 minutes
            try:
                async with session.get(
                    f"{url}/ping", timeout=ClientTimeout(total=10)
                ) as r:
                    log.debug(f"Self-ping {r.status}")
            except Exception as e:
                log.debug(f"Self-ping failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    global _tg_app, active_markets, _scan_client

    if not TELEGRAM_TOKEN:
        log.error("Set TELEGRAM_TOKEN in .env (or Render env vars)")
        sys.exit(1)

    log.info("=== Bayse Bot Starting (multi-user) ===")
    database.init_db()

    # Telegram
    _tg_app = telegram_bot.build_app()
    telegram_bot.inject(
        user_clients=_user_clients,
        user_risks=_user_risks,
        user_daily=_user_daily,
        active_markets=active_markets,
        start_user_fn=start_user,
    )
    await _tg_app.initialize()
    await _tg_app.start()
    await _tg_app.updater.start_polling()
    log.info("Telegram bot running")

    asyncio.create_task(_keep_alive_server())
    asyncio.create_task(_self_ping_loop())

    asyncio.create_task(feeds.start_feeds(on_price=_on_spot_price))
    asyncio.create_task(news_mod.start_news_feeds())
    asyncio.create_task(learner.resolution_monitor(_user_clients, _user_risks, _tg_app))
    asyncio.create_task(learner.daily_learning_loop(_tg_app))
    asyncio.create_task(_scan_loop())

    # Reconnect all existing users and notify them
    existing_users = database.get_all_active()
    for user in existing_users:
        cid = user["chat_id"]
        start_user(cid)
        if _scan_client is None:
            _scan_client = _get_client(user)
        try:
            await telegram_bot.send_message(
                _tg_app, cid,
                "🔄 *Bot restarted — you're still connected.*\n\n"
                "Your settings and trade history are intact. Trading resumes now.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # Initial market scan + start Bayse WebSocket for real-time prices
    if _scan_client:
        active_markets = await scanner.scan_all(_scan_client)
        telegram_bot._active_markets = active_markets
        log.info(f"Initial scan: {len(active_markets)} markets")
        feeds.restart_bayse_feed(active_markets, _on_market_update)
        asyncio.create_task(scanner.discover_series(_scan_client))

    # Wait for first spot prices
    for _ in range(20):
        if len(feeds.spot) >= 2:
            break
        await asyncio.sleep(1)
    log.info(f"Spot prices: {feeds.spot}")

    # Refresh market timers every 5 seconds
    while True:
        await asyncio.sleep(5)
        _refresh_timers()

    await _tg_app.updater.stop()
    await _tg_app.stop()
    await _tg_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
