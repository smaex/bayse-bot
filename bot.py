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
import time
from datetime import date

from aiohttp import web, ClientSession, ClientTimeout

import database
import feeds
import news as news_mod
import scanner
import strategy
import strategies
import learner
import telegram_bot
import executor
import server
import recorder
import config
import feeds_direct
import comparative_analysis
from risk import RiskManager
from client import BayseClient
from config import (
    TELEGRAM_TOKEN, CURRENCY, SCAN_INTERVAL_SECONDS, ARB_MAX_SIZE_NGN,
    MIN_PAYOUT_RATIO, SYSTEMIC_RISK_HALT_MINS,
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
_low_bal_notified: dict[str, str]       = {}  # chat_id → date last notified (max once/day)
_systemic_alert_sent: dict[str, bool]   = {}  # chat_id → bool
_scan_client:   BayseClient | None = None
_tg_app        = None

_last_spot:        dict[str, float] = {}
_last_market_eval: dict[str, float] = {}
_last_lag_log:     dict[str, float] = {}  # throttle lag warnings to once per minute

# BN-9 FIX: Cache active users in memory — refreshed by 30s housekeeping loop.
# Prevents a DB read on every single price tick (was 4+ reads/second).
_active_users_cache: list[dict] = []
_active_users_cache_time: float = 0.0
_ACTIVE_USERS_CACHE_TTL = 30.0  # seconds

# Minimum change to be considered a deposit/withdrawal (not just trade P&L noise)
_BALANCE_EVENT_MIN_NGN = 200
_BALANCE_EVENT_MIN_PCT = 0.05   # 5% of balance
_MIN_VIABLE_BALANCE    = 1_000  # Bayse minimum trade is ₦100; need ₦1k+ to trade safely


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
    
    # Check in-memory cache first — it might have been updated by deposit logic
    ds = _user_daily.get(chat_id)
    if not ds or ds.get("date") != today:
        # Fallback to settings or create new
        ds = settings.get("daily_state", {})
        if ds.get("date") != today:
            ds = {"date": today, "start_balance": balance, "target_hit": False}
            settings["daily_state"] = ds
            asyncio.create_task(asyncio.to_thread(database.update_settings, chat_id, settings))
        _user_daily[chat_id] = ds
    
    return ds


def _daily_target(settings: dict, start_balance: float) -> float:
    absolute = settings.get("daily_target_ngn", 0)
    if absolute > 0:
        return float(absolute)
    # daily_multiplier is a percentage — 10 means 10% of starting balance
    return start_balance * settings.get("daily_multiplier", 10) / 100


# ── User lifecycle ─────────────────────────────────────────────────────────────

async def start_user(chat_id: str):
    """Launch a trading loop for a user. Called on setup completion and restart."""
    global _scan_client
    user = await asyncio.to_thread(database.get_user, chat_id)
    if not user:
        return
    client = _get_client(user)
    if _scan_client is None:
        _scan_client = client

    # ── Force-Safety Migration ────────────────────────────────────────────────
    # Ensure existing users are moved to the new safer defaults (2% risk, 20% exposure)
    # This prevents the "20% wipeout" even if they haven't manually updated settings.
    settings = user.get("settings", {})
    updated = False
    if settings.get("risk_pct", 3.0) > 2.0:
        settings["risk_pct"] = 2.0
        updated = True
    if settings.get("maxexposure", 30.0) > 20.0:
        settings["maxexposure"] = 20.0
        updated = True
    if updated:
        asyncio.create_task(asyncio.to_thread(database.update_settings, chat_id, settings))
        log.info(f"[{chat_id}] Safety Migration applied: risk_pct=2%, maxexposure=20%")

    # ── Reconstruct open positions from DB ─────────────────────────────────────
    # After a restart, risk.open_positions is empty.  Without this, the exposure
    # cap is bypassed until trades resolve — the bot could over-deploy capital.
    risk = _get_risk(chat_id)
    if not risk.open_positions:
        async def _load_pos():
            unresolved = await asyncio.to_thread(database.get_all_unresolved, chat_id)
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
        asyncio.create_task(_load_pos())

    # ── One-time Startup Warning: Low Balance ──────────────────────────────────
    async def _warn_low_bal():
        try:
            client = _get_client(user)
            bal = await client.get_balance_ngn()
            if bal < _MIN_VIABLE_BALANCE * 2:
                await telegram_bot.send_message(
                    _tg_app, chat_id,
                    f"⚠️ *Small Bankroll Detected (₦{bal:,.0f})*\n\n"
                    f"The bot is active, but because your balance is low, "
                    f"I have enabled **Selective Mode** and **Small Account Mode**.\n\n"
                    f"I will only take the highest-quality trades to protect your "
                    f"capital. Trades may be less frequent until your balance grows.",
                    parse_mode="Markdown"
                )
        except Exception:
            pass

    if chat_id not in _user_tasks or _user_tasks[chat_id].done():
        _user_tasks[chat_id] = asyncio.create_task(_user_loop(chat_id))
        asyncio.create_task(_warn_low_bal())
        log.info(f"Trading loop started for {chat_id}")


async def _user_loop(chat_id: str):
    """Per-user async trading loop — runs every 30 seconds for housekeeping. Signal evaluation is event-driven."""
    strategy.set_user_context(chat_id)   # tags every strategy log line with this user
    sent_news: set[str] = set()
    iter_count = 0

    while True:
        await asyncio.sleep(30)
        iter_count += 1
        user = await asyncio.to_thread(database.get_user, chat_id)
        if not user or not user.get("is_active"):
            log.info(f"[{chat_id}] trading loop exiting (user inactive)")
            break
        client = _get_client(user)
        risk   = _get_risk(chat_id)
        settings = user.get("settings", {})

        try:
            free_cash = await client.get_balance_ngn()
            risk.current_free_cash = free_cash
        except Exception as e:
            log.warning(f"[{chat_id}] balance fetch failed: {e}")
            continue

        equity = free_cash + risk.deployed()
        risk.update_balance(equity)
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
                await asyncio.to_thread(database.update_settings, chat_id, settings)
                risk.peak_balance = equity
                _user_daily[chat_id] = day_now
                if _tg_app:
                    await telegram_bot.notify_deposit_detected(
                        _tg_app, chat_id, delta, "NGN"
                    )
            elif delta < -threshold:
                # Withdrawal detected
                log.info(f"[{chat_id}] Withdrawal detected: ₦{delta:,.0f} (₦{last_bal:,.0f} → ₦{equity:,.0f})")
                day_now = _user_daily.get(chat_id, {})
                day_now["start_balance"] = equity
                settings["daily_state"]  = day_now
                await asyncio.to_thread(database.update_settings, chat_id, settings)
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

        # ── Pause check (moved below housekeeping) ─────────────────────────────
        if settings.get("paused"):
            if iter_count % 18 == 0:  # log every 3 minutes while paused
                log.info(f"[{chat_id}] trading paused — send /resume to restart")
            continue

        if iter_count % 18 == 0:  # heartbeat every 3 minutes
            log.info(f"[{chat_id}] loop alive (iter={iter_count}, spot={feeds.spot})")

        # ── Low balance guard ──────────────────────────────────────────────────
        if equity < _MIN_VIABLE_BALANCE:
            today = date.today().isoformat()
            if _low_bal_notified.get(chat_id) != today:
                _low_bal_notified[chat_id] = today
                log.info(
                    f"[{chat_id}] Balance ₦{equity:,.0f} below minimum "
                    f"₦{_MIN_VIABLE_BALANCE:,} — notifying user to deposit"
                )
                if _tg_app:
                    await telegram_bot.send_message(
                        _tg_app, chat_id,
                        f"⚠️ *Low Balance — Trading Paused*\n\n"
                        f"Your balance is ₦{equity:,.0f}, which is below "
                        f"the ₦{_MIN_VIABLE_BALANCE:,} minimum needed to trade safely.\n\n"
                        f"Bayse requires at least ₦100 per trade, and the bot "
                        f"needs ₦1,000+ to size positions properly.\n\n"
                        f"💰 Please deposit funds to resume trading.\n"
                        f"The bot will start trading automatically once your "
                        f"balance is above ₦{_MIN_VIABLE_BALANCE:,}.",
                        parse_mode="Markdown",
                    )
            continue

        # ── Daily target ───────────────────────────────────────────────────────
        day          = _daily(chat_id, equity, settings)
        profit_today = equity - day["start_balance"]
        target       = _daily_target(settings, day["start_balance"])
        if target > 0 and profit_today >= target and not day["target_hit"]:
            day["target_hit"] = True
            settings["daily_state"] = day
            settings["paused"] = True
            await asyncio.to_thread(database.update_settings, chat_id, settings)
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
            await asyncio.to_thread(database.update_settings, chat_id, settings)
            if _tg_app:
                await telegram_bot.notify_drawdown(_tg_app, chat_id, equity, risk.peak_balance, dd)
            continue

        # ── Systemic Risk Halt ─────────────────────────────────────────────────
        risk_alert = strategy.check_systemic_risk()
        if risk_alert:
            if not _systemic_alert_sent.get(chat_id):
                _systemic_alert_sent[chat_id] = True
                log.warning(f"[{chat_id}] {risk_alert}")
                if _tg_app:
                    await telegram_bot.send_message(
                        _tg_app, chat_id,
                        f"🚨 *SYSTEMIC RISK ALERT*\n\n{risk_alert}\n\n"
                        f"The entire market is experiencing extreme volatility shocks. "
                        f"Trading is automatically paused for {SYSTEMIC_RISK_HALT_MINS} minutes to protect your capital.",
                        parse_mode="Markdown"
                    )
        else:
            if _systemic_alert_sent.get(chat_id):
                _systemic_alert_sent[chat_id] = False
                log.info(f"[{chat_id}] Systemic risk cleared. Resuming.")
        for sig in news_mod.active_signals:
            key = f"{sig.source}:{sig.headline[:40]}"
            if key not in sent_news and sig.strength() > 0.4:
                sent_news.add(key)
                if _tg_app:
                    await telegram_bot.notify_news(
                        _tg_app, chat_id,
                        sig.headline, sig.direction, sig.assets, sig.strength(),
                    )

        # BN-9 FIX: Refresh the in-memory user cache here in the 30s loop
        global _active_users_cache, _active_users_cache_time
        _active_users_cache = await asyncio.to_thread(database.get_all_active)
        _active_users_cache_time = time.time()

        # ── Signal evaluation ──────────────────────────────────────────────────
        user_assets  = settings.get("assets",     ["BTC", "ETH", "SOL"])
        user_tfs     = settings.get("timeframes",  ["5min", "15min", "1h"])
        # BN-2 FIX: Include all strategies in default list
        user_strats  = settings.get("strategies",  ["SNIPE", "CORRELATE", "ARB", "NEWS", "POLY_EDGE", "FRONTRUN", "MARKET_BIAS"])
        learned      = settings.get("learned",     {})
        learned["mode"] = settings.get("mode", "balanced")
        suspended    = learned.get("suspended_strategies", [])
        active_strats = [s for s in user_strats if s not in suspended]
        max_exp      = settings.get("maxexposure", 20.0) / 100.0  # BN-8 FIX: unified default 20%

        await _evaluate_single_user(user, penalty=0.0)

async def _evaluate_single_user(user: dict, trigger_asset: str = None, penalty: float = 0.0):
    """
    Trench-Hardened Evaluation:
    Refreshes balance, checks risk guards, and evaluates markets with an adaptive safety penalty.
    """
    chat_id = user["chat_id"]
    strategy.set_user_context(chat_id)
    
    client  = _user_clients.get(chat_id)
    risk    = _user_risks.get(chat_id)
    
    if not client or not risk:
        return
        
    settings = user.get("settings", {})
    risk.mode = settings.get("mode", "balanced")
    
    if settings.get("paused"):
        return
    
    # 1. Get cached balance
    try:
        # Use cached balance from RiskManager to avoid hitting API rate limits on every tick.
        # The 30s loop refreshes this value periodically.
        free_cash = risk.current_free_cash
        
        # Fallback for startup if cache is empty
        if free_cash <= 0:
            free_cash = await client.get_balance_ngn()
            risk.current_free_cash = free_cash
            
        equity = free_cash + risk.deployed()
        risk.update_balance(equity)
    except Exception as e:
        log.error(f"[{chat_id}] balance refresh error: {e}")
        return

    # 2. Check risk guards (drawdown, target)
    if risk.target_hit or risk.max_drawdown_hit:
        return

    # 3. Evaluate all relevant markets with Adaptive Sizing
    # BN-2 FIX: Full default strategy list including new strategies
    active_strats = settings.get("strategies", ["SNIPE", "CORRELATE", "ARB", "NEWS", "POLY_EDGE", "FRONTRUN", "MARKET_BIAS"])
    learned = await asyncio.to_thread(learner.get_learned_overrides, chat_id)
    if not learned:
        learned = {}
        
    # Inject real-time drawdown state for Drawdown-Adjusted Kelly sizing
    if risk.peak_balance > 0:
        learned["drawdown_pct"] = (risk.peak_balance - equity) / risk.peak_balance
    else:
        learned["drawdown_pct"] = 0.0
    
    # BN-8 FIX: Unified maxexposure default — was 100% here (dangerous), now 20%
    max_exp = settings.get("maxexposure", 20.0) / 100.0
    user_assets = settings.get("assets", config.ALL_ASSETS)
    
    raw_tfs = settings.get("timeframes", ["5min", "15min", "1h"])
    # Clean up shorthand timeframe strings (e.g. 5m -> 5min, 15m -> 15min)
    user_tfs = []
    for tf in raw_tfs:
        tf_clean = tf.lower().replace("min", "").replace("m", "")
        if tf_clean in {"5", "15"}:
            user_tfs.append(tf_clean + "min")
        else:
            user_tfs.append(tf)

    await _evaluate_markets(
        chat_id, settings, client, risk, equity, free_cash, 
        active_strats, learned, max_exp, user_assets, user_tfs, 
        trigger_asset=trigger_asset, penalty=penalty
    )


async def _evaluate_markets(
    chat_id, settings, client, risk, equity, free_cash,
    active_strats, learned, max_exp, user_assets, user_tfs,
    trigger_asset: str = None, penalty: float = 0.0
):
    try:
        all_signals = []
        for market in active_markets:
            if market.get("status") != "open":
                continue
            if market["asset"] not in user_assets:
                continue
            if trigger_asset and market["asset"] != trigger_asset:
                continue
            if market["timeframe"] not in user_tfs:
                continue
            if strategy.is_halted(market["asset"]):
                continue

            # ── Modular Strategy Evaluation ──
            signals = await strategies.evaluate_all(market, learned, strategy.global_state)
            
            for sig in signals:
                if sig.strategy in active_strats:
                    all_signals.append(sig)
            
        # ── Convergence Engine ──
        final_signals = strategies.merge_signals(all_signals, strategy.global_state)
            
        for sig in final_signals:
            if sig.strategy == "ARB":
                await executor.execute_arb(chat_id, sig, client, equity, free_cash, settings)
            else:
                await executor.execute_trade(chat_id, sig, client, risk, settings, equity, free_cash)
    except Exception as e:
        log.error(f"[{chat_id}] market eval error: {e}", exc_info=True)




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
            
            # Record the new market snapshot for backtesting (non-blocking)
            asyncio.create_task(asyncio.to_thread(recorder.record_market_snapshot, active_markets))
        except Exception as e:
            log.warning(f"Scan failed: {e}")


def _refresh_timers():
    for m in active_markets:
        m["secs_to_close"] = scanner._seconds_to_close(m.get("closing_date", ""))


def _on_spot_price(asset: str, price: float):
    # ── Stale Data Guard (Adaptive) ──────────────────────────────────────────
    # We check if the relay is lagging. Instead of just blocking, we try to 
    # use the direct price or apply a safety spread.
    lag = feeds_direct.check_lag(asset, price)
    best_price = lag["price"]
    
    if lag["status"] == "stale":
        log.warning(
            f"⚠️ INFRA GUARD: {asset} STALE. Blocking entry. "
            f"Lag: {lag['lag_sec']:.1f}s, Diff: {lag['diff_pct']:.4%}"
        )
        return
        
    safety_penalty = 0.0
    if lag["status"] == "degraded":
        safety_penalty = 0.0010 
        reason = f"lag {lag['lag_sec']:.1f}s" if lag['lag_sec'] > config.INFRA_DEGRADED_LAG_SEC else f"diff {lag['diff_pct']:.4%}"
        
        # Throttle log to once per minute per asset
        now = time.time()
        if now - _last_lag_log.get(asset, 0) > 60:
            _last_lag_log[asset] = now
            log.info(f"🟡 {asset} {reason} — applying 0.1% safety spread.")

    # Always use the most recent history/recording
    strategy.update_price_history(asset, best_price)
    recorder.record_spot_tick(asset, best_price)  # In-memory buffer, no DB hit
    log.debug(f"Spot {asset}: {best_price:,.4f}")
    
    _last_spot[asset] = best_price
    
    # Trigger high-frequency evaluation instantly on price change
    asyncio.create_task(_evaluate_all_users_for_spot(asset, 0.0, 0.0, penalty=safety_penalty))


async def _evaluate_all_users_for_spot(asset: str, change: float, threshold: float, penalty: float = 0.0):
    now = time.time()
    # BN-1 FIX: Reduced throttle from 3s → 1s. At 3s, most signals on fast-moving
    # assets (BTC/ETH ticking every ~1s) were silently dropped before evaluation.
    if now - _last_market_eval.get(asset, 0) < 1.0:
        return
    _last_market_eval[asset] = now
    
    # BN-9 FIX: Use cached user list instead of hitting DB on every tick.
    # Falls back to fresh DB read if cache is stale (>30s) or empty.
    global _active_users_cache, _active_users_cache_time
    if not _active_users_cache or (now - _active_users_cache_time) > _ACTIVE_USERS_CACHE_TTL:
        _active_users_cache = await asyncio.to_thread(database.get_all_active)
        _active_users_cache_time = now
    
    for user in _active_users_cache:
        asyncio.create_task(_evaluate_single_user(user, asset, penalty=penalty))


def _on_market_update(market_id: str, prices: dict):
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    if not market:
        return
    asset = market.get("asset")
    # Record BTC move for CORRELATE strategy
    if asset == "BTC":
        strategy.record_btc_move(market, prices.get("yes", market["yes_price"]))
    # BN-7 FIX: Also trigger a full evaluation when ANY market price updates.
    # Previously only BTC moves triggered re-evaluation. A market shifting from
    # 0.52 → 0.65 in one tick would not be acted on until the next heartbeat (5s).
    asyncio.create_task(_evaluate_all_users_for_spot(asset, 0.0, 0.0, penalty=0.0))


# ── Keep-alive web server ──────────────────────────────────────────────────────

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

async def _heartbeat_eval_loop():
    """
    World-Class Heartbeat: Evaluates all markets for all users every 5 seconds.
    Ensures we never miss a window even if price moves slowly.
    """
    log.info("🚀 Heartbeat Evaluation Loop started (5s interval)")
    while True:
        try:
            await asyncio.sleep(5)
            if not active_markets:
                continue
                
            users = await asyncio.to_thread(database.get_all_active)
            for user in users:
                # Run evaluation without a specific trigger asset (full scan)
                asyncio.create_task(_evaluate_single_user(user, penalty=0.0))
        except Exception as e:
            log.error(f"Heartbeat loop error: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    global _tg_app, active_markets, _scan_client

    if not TELEGRAM_TOKEN:
        log.error("Set TELEGRAM_TOKEN in .env (or Render env vars)")
        sys.exit(1)

    log.info("=== Bayse Bot Starting (multi-user) ===")
    database.init_db()
    
    # Start Health-Check Server early so Render sees the port as open
    # This prevents Render from killing the instance while it waits for the Ghost Shield lock.
    asyncio.create_task(server.start_server(8080))
    asyncio.create_task(_self_ping_loop())
    
    # ── GHOST SHIELD: Singleton Lock ──
    # During Render deploys, a new instance may start before the old one dies.
    # We wait up to 300s (5 min) for the old instance to release the lock/go stale.
    max_wait = 300
    waited = 0
    while not database.acquire_singleton_lock():
        if waited >= max_wait:
            log.critical(f"🚨 GHOST SHIELD: Could not acquire lock after {max_wait}s. Terminating.")
            return
        if waited % 30 == 0:
            log.info(f"⏳ GHOST SHIELD: Another instance is active. Standing by... ({waited}s)")
        await asyncio.sleep(5)
        waited += 5
    
    log.info("🛡️ GHOST SHIELD: Lock acquired. Starting master process.")
        
    async def _lock_heartbeat():
        while True:
            database.heartbeat_singleton_lock()
            await asyncio.sleep(30)
    asyncio.create_task(_lock_heartbeat())

    # Telegram
    _tg_app = telegram_bot.build_app()
    telegram_bot.inject(
        user_clients=_user_clients,
        user_risks=_user_risks,
        user_daily=_user_daily,
        active_markets=active_markets,
        start_user_fn=start_user,
    )
    import random
    startup_delay = random.uniform(2, 8)
    log.info(f"Bot: Cold start protection — delaying for {startup_delay:.1f}s...")
    await asyncio.sleep(startup_delay)

    # Telegram: NUCLEAR GHOST KICK
    # Forcing a webhook kills all other active 'getUpdates' (polling) sessions immediately
    log.info("Telegram: NUCLEAR KICK — Purging ghost instances via webhook reset...")
    try:
        # Use a validly-formatted URL to pass Telegram's validation
        await _tg_app.bot.set_webhook(url="https://bayse-bot-ghost-kick.render.com/unused")
        await asyncio.sleep(5)
        await _tg_app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Telegram: Ghost instances purged successfully.")
    except Exception as e:
        log.warning(f"Telegram: Ghost-kick failed (likely already clean): {e}")

    await _tg_app.initialize()
    await _tg_app.start()
    # ── Telegram Polling with Conflict-Aware Retry ──
    while True:
        try:
            log.info("Telegram: Starting polling loop...")
            await _tg_app.updater.start_polling(drop_pending_updates=True)
            break 
        except Exception as e:
            if "Conflict" in str(e):
                log.warning("Telegram Conflict: Another instance is polling. Waiting 30s for ghost to die...")
                await asyncio.sleep(30)
            else:
                log.error(f"Telegram Polling Error: {e}")
                break
    log.info("Telegram bot running")

    # Modules

    # Initialize Modules
    executor.init_executor(active_markets, _tg_app)
    await strategy.load_memory()
    # Start feeds
    asyncio.create_task(feeds.start_feeds(on_price=_on_spot_price))
    asyncio.create_task(feeds_direct.binance_feed())
    asyncio.create_task(feeds_direct.binance_rest_fallback())
    asyncio.create_task(feeds_direct.tiingo_fx_feed())
    asyncio.create_task(feeds_direct.tiingo_fx_rest_fallback())
    asyncio.create_task(news_mod.start_news_feeds())
    asyncio.create_task(learner.resolution_monitor(_user_clients, _user_risks, _tg_app))
    asyncio.create_task(learner.daily_learning_loop(_tg_app))
    asyncio.create_task(learner.stagnation_monitor(_tg_app))
    asyncio.create_task(_heartbeat_eval_loop())
    asyncio.create_task(_scan_loop())
    asyncio.create_task(_update_dashboard_stats())
    asyncio.create_task(_polymarket_polling_loop())
    from strategies.optimizer import optimizer
    asyncio.create_task(optimizer.schedule_loop())

    # Reconnect all existing users and notify them
    existing_users = await asyncio.to_thread(database.get_all_active)
    for user in existing_users:
        cid = user["chat_id"]
        await start_user(cid)
        if _scan_client is None:
            _scan_client = _get_client(user)
        try:
            await telegram_bot.send_message(
                _tg_app, cid,
                "🚀 *System Update Complete*\n\n"
                "The Bayse Bot has successfully deployed the latest code update. "
                "Your connection is secure and trading has resumed.",
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


async def _update_dashboard_stats():
    """Periodically push live runtime data to the web dashboard server."""
    while True:
        try:
            user_stats = []
            for cid, client in _user_clients.items():
                risk = _user_risks.get(cid)
                balance = 0
                try:
                    balance = await client.get_balance_ngn()
                except: pass
                
                user_stats.append({
                    "id": f"{cid[:4]}...{cid[-4:]}" if len(cid) > 8 else cid,
                    "paused": risk.paused if risk else True,
                    "balance": balance,
                    "pnl_today": 0, # Placeholder for more complex logic
                    "mode": risk.mode if hasattr(risk, 'mode') else "balanced",
                    "exposure": (sum(p.get('amount_ngn', 0) for p in risk.open_positions.values()) / balance * 100) if (risk and balance > 0) else 0,
                    "open_count": len(risk.open_positions) if risk else 0
                })
            
            # Sync oracles
            oracle_stats = {}
            for asset, data in feeds_direct.direct_spot.items():
                oracle_stats[asset] = {
                    "price": data['price'],
                    "lag": time.time() - data['time']
                }

            server.stats_cache.update({
                "users": user_stats,
                "oracles": oracle_stats,
                "last_update": time.time()
            })
        except Exception as e:
            log.error(f"Dashboard update failed: {e}")
            
        await asyncio.sleep(30)

async def _polymarket_polling_loop():
    """Background task to poll Polymarket every 5 minutes."""
    while True:
        try:
            await comparative_analysis.update_cache()
            await asyncio.sleep(300) # 5 minutes
        except Exception as e:
            log.error(f"Polymarket Polling error: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
