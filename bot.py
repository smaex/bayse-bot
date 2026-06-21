"""
Multi-user trading bot — one server, all users via Telegram.
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
from risk import RiskManager
from client import BayseClient
from config import TELEGRAM_TOKEN, CURRENCY, SCAN_INTERVAL_SECONDS, SYSTEMIC_RISK_HALT_MINS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Ghost killer — must run before Telegram polling starts ────────────────────
import ghost_kill
ghost_kill.kill_ghosts()

log = logging.getLogger("bot")

# ── Shared state ──────────────────────────────────────────────────────────────
active_markets:    list[dict]             = []
_user_clients:     dict[str, BayseClient] = {}
_user_risks:       dict[str, RiskManager] = {}
_user_daily:       dict[str, dict]        = {}
_last_balance:     dict[str, float]       = {}
_pending_balance_event: dict[str, float]  = {}  # chat_id -> suspected new balance, awaiting confirmation
_low_bal_notified: dict[str, str]         = {}
_systemic_alert:   dict[str, bool]        = {}
_scan_client:      BayseClient | None     = None
_tg_app                                   = None

_last_market_eval: dict[str, float] = {}

_active_users_cache:      list[dict] = []
_active_users_cache_time: float      = 0.0
_CACHE_TTL                           = 30.0

_BALANCE_EVENT_MIN_NGN = 200
_BALANCE_EVENT_MIN_PCT = 0.05
_MIN_VIABLE_BALANCE    = 500


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
    ds = _user_daily.get(chat_id)
    if not ds or ds.get("date") != today:
        ds = settings.get("daily_state", {})
        if ds.get("date") != today:
            ds = {"date": today, "start_balance": balance, "target_hit": False}
            settings["daily_state"] = ds
            asyncio.create_task(asyncio.to_thread(database.update_settings, chat_id, settings))
        _user_daily[chat_id] = ds
    return ds


def _daily_target(settings: dict, start: float) -> float:
    abs_ = settings.get("daily_target_ngn", 0)
    if abs_ > 0:
        return float(abs_)
    return start * settings.get("daily_multiplier", 10) / 100


# ── User lifecycle ────────────────────────────────────────────────────────────

async def start_user(chat_id: str):
    global _scan_client
    user = await asyncio.to_thread(database.get_user, chat_id)
    if not user:
        return
    client = _get_client(user)
    if _scan_client is None:
        _scan_client = client

    settings = user.get("settings", {})
    # Do NOT silently cap risk_pct / maxexposure on restart.
    # Previously this overrode aggressive/full_send mode settings on every deploy,
    # locking users at 2% risk regardless of what they chose via /mode or /set.
    # Hard safety rails live in executor (_execute_logic caps at 5%) and risk.py.

    risk = _get_risk(chat_id)
    if not risk.open_positions:
        async def _load():
            for t in await asyncio.to_thread(database.get_all_unresolved, chat_id):
                mid = t.get("market_id")
                if mid and mid not in risk.open_positions:
                    risk.add_position(mid, {
                        "trade_id": t["trade_id"], "event_id": t["event_id"],
                        "outcome": t["outcome"], "outcome_id": t["outcome_id"],
                        "entry_price": t["entry_price"], "amount_ngn": t["amount_ngn"],
                        "strategy": t["strategy"], "asset": t["asset"],
                        "timeframe": t["timeframe"],
                    })
        asyncio.create_task(_load())

    if chat_id not in _user_tasks or _user_tasks[chat_id].done():
        _user_tasks[chat_id] = asyncio.create_task(_user_loop(chat_id))
        is_paused = settings.get("paused", False)
        mode      = settings.get("mode", "balanced")
        log.info(f"[{chat_id}] Trading loop started | mode={mode} | paused={is_paused}")

_user_tasks: dict[str, asyncio.Task] = {}


async def _user_loop(chat_id: str):
    """30-second housekeeping loop per user."""
    strategy.set_user_context(chat_id)
    iter_count   = 0
    last_log_min = -1  # track last minute we logged status

    while True:
        await asyncio.sleep(30)
        iter_count += 1
        user = await asyncio.to_thread(database.get_user, chat_id)
        if not user or not user.get("is_active"):
            log.info(f"[{chat_id}] User deactivated — stopping loop")
            break
        client   = _get_client(user)
        risk     = _get_risk(chat_id)
        settings = user.get("settings", {})

        try:
            free_cash = await client.get_balance_ngn()
            risk.current_free_cash = free_cash
        except Exception as e:
            log.warning(f"[{chat_id}] Balance fetch failed: {e}")
            continue

        equity = free_cash + risk.deployed()
        risk.update_balance(equity)
        risk.update_peak(equity)

        # ── Structured status log every 5 minutes ─────────────────────────
        current_min = int(time.time() // 60)
        if current_min % 5 == 0 and current_min != last_log_min:
            last_log_min = current_min
            mode     = settings.get("mode", "balanced")
            paused   = settings.get("paused", False)
            n_pos    = len(risk.open_positions)
            deployed = risk.deployed()
            log.info(
                f"[{chat_id}] STATUS | balance=₦{equity:,.0f} | "
                f"mode={mode} | paused={paused} | "
                f"positions={n_pos} | deployed=₦{deployed:,.0f}"
            )

        # ── Deposit / withdrawal detection (debounced) ──────────────────────
        last = _last_balance.get(chat_id)
        if last is not None:
            delta     = equity - last
            threshold = max(_BALANCE_EVENT_MIN_NGN, last * _BALANCE_EVENT_MIN_PCT)
            if abs(delta) > threshold:
                pending = _pending_balance_event.get(chat_id)
                if pending is not None and abs(pending - equity) < threshold * 0.5:
                    # Same large deviation confirmed on a second consecutive
                    # check (~30s later) — treat as real and act on it.
                    if delta > 0:
                        log.info(f"[{chat_id}] DEPOSIT detected +₦{delta:,.0f} | new balance ₦{equity:,.0f}")
                        day = _user_daily.get(chat_id, {})
                        day["start_balance"] = equity
                        settings["daily_state"] = day
                        await asyncio.to_thread(database.update_settings, chat_id, settings)
                        risk.peak_balance = equity
                        _user_daily[chat_id] = day
                        if _tg_app:
                            await telegram_bot.notify_deposit_detected(_tg_app, chat_id, delta, "NGN")
                    else:
                        log.info(f"[{chat_id}] WITHDRAWAL detected ₦{delta:,.0f} | new balance ₦{equity:,.0f}")
                        day = _user_daily.get(chat_id, {})
                        day["start_balance"] = equity
                        settings["daily_state"] = day
                        await asyncio.to_thread(database.update_settings, chat_id, settings)
                        # Do NOT reset risk.peak_balance here — see prior fix notes.
                        _user_daily[chat_id] = day
                        if _tg_app:
                            await telegram_bot.send_message(
                                _tg_app, chat_id,
                                f"💸 *Withdrawal detected* — ₦{abs(delta):,.0f} removed\n"
                                f"New balance: ₦{equity:,.2f}",
                                parse_mode="Markdown",
                            )
                    _pending_balance_event.pop(chat_id, None)
                    _last_balance[chat_id] = equity
                else:
                    # First time seeing this deviation — don't act yet, just
                    # remember it. _last_balance is deliberately NOT updated
                    # here, so the next check still compares against the
                    # last CONFIRMED baseline rather than this unconfirmed one.
                    _pending_balance_event[chat_id] = equity
            else:
                _pending_balance_event.pop(chat_id, None)
                _last_balance[chat_id] = equity
        else:
            _last_balance[chat_id] = equity

        # ── Paused check ───────────────────────────────────────────────────
        if settings.get("paused"):
            if iter_count % 6 == 0:   # log every 3 minutes when paused
                log.info(f"[{chat_id}] PAUSED — skipping evaluation")
            continue

        # ── Low balance guard ──────────────────────────────────────────────
        if equity < _MIN_VIABLE_BALANCE:
            today = date.today().isoformat()
            if _low_bal_notified.get(chat_id) != today:
                _low_bal_notified[chat_id] = today
                log.warning(f"[{chat_id}] LOW BALANCE ₦{equity:,.0f} — trading halted")
                if _tg_app:
                    await telegram_bot.send_message(
                        _tg_app, chat_id,
                        f"⚠️ *Low Balance* — ₦{equity:,.0f}\n"
                        f"Deposit to resume trading (minimum ₦{_MIN_VIABLE_BALANCE:,}).",
                        parse_mode="Markdown",
                    )
            continue

        # ── Daily target ───────────────────────────────────────────────────
        day    = _daily(chat_id, equity, settings)
        profit = equity - day["start_balance"]
        target = _daily_target(settings, day["start_balance"])

        # Sync ground-truth values onto risk so is_in_strict_mode() actually
        # works. risk.daily_target was never assigned anywhere before this —
        # it stayed at its 0.0 default permanently, silently disabling the
        # "tighten up near daily target" safety check with no error at all.
        risk.daily_target      = target
        risk.daily_realized_pnl = profit

        if target > 0 and profit >= target and not day["target_hit"]:
            day["target_hit"] = True
            settings["daily_state"] = day
            settings["paused"]       = True
            await asyncio.to_thread(database.update_settings, chat_id, settings)
            log.info(f"[{chat_id}] DAILY TARGET HIT ₦{profit:+,.0f} — trading paused")
            if _tg_app:
                await telegram_bot.send_message(
                    _tg_app, chat_id,
                    f"🎯 *Daily target reached!* ₦{profit:+,.0f}\n/resume to override.",
                    parse_mode="Markdown",
                )
            continue

        # ── Drawdown check ─────────────────────────────────────────────────
        if not risk.check_drawdown(equity):
            dd = (risk.peak_balance - equity) / risk.peak_balance
            settings["paused"] = True
            await asyncio.to_thread(database.update_settings, chat_id, settings)
            log.warning(f"[{chat_id}] DRAWDOWN STOP {dd:.1%} — trading paused")
            if _tg_app:
                await telegram_bot.notify_drawdown(_tg_app, chat_id, equity, risk.peak_balance, dd)
            continue

        # ── Systemic halt ──────────────────────────────────────────────────
        alert = strategy.check_systemic_risk()
        if alert:
            if not _systemic_alert.get(chat_id):
                _systemic_alert[chat_id] = True
                log.warning(f"[{chat_id}] SYSTEMIC HALT — {alert}")
                if _tg_app:
                    await telegram_bot.send_message(
                        _tg_app, chat_id,
                        f"🚨 *Systemic Risk Alert*\n{alert}\nTrading paused for {SYSTEMIC_RISK_HALT_MINS} min.",
                        parse_mode="Markdown",
                    )
        else:
            _systemic_alert[chat_id] = False

        # ── Refresh user cache ─────────────────────────────────────────────
        global _active_users_cache, _active_users_cache_time
        _active_users_cache      = await asyncio.to_thread(database.get_all_active)
        _active_users_cache_time = time.time()

        await _evaluate_single_user(user, penalty=0.0)


async def _evaluate_single_user(user: dict, trigger_asset: str = None, penalty: float = 0.0):
    chat_id  = user["chat_id"]
    client   = _user_clients.get(chat_id)
    risk     = _user_risks.get(chat_id)
    if not client or not risk:
        return

    settings = user.get("settings", {})
    risk.mode = settings.get("mode", "balanced")
    if settings.get("paused"):
        return

    free_cash = risk.current_free_cash
    if free_cash <= 0:
        try:
            free_cash = await client.get_balance_ngn()
            risk.current_free_cash = free_cash
        except Exception:
            return

    equity = free_cash + risk.deployed()
    if risk.target_hit or risk.max_drawdown_hit:
        return

    learned = await asyncio.to_thread(learner.get_learned_overrides, chat_id)
    if risk.peak_balance > 0:
        learned["drawdown_pct"] = (risk.peak_balance - equity) / risk.peak_balance

    user_assets = settings.get("assets",     config.ALL_ASSETS)
    raw_tfs     = settings.get("timeframes",  ["5min", "15min", "1h"])
    user_strats = settings.get("strategies",  config.ACTIVE_STRATEGIES)
    max_exp     = settings.get("maxexposure", 20.0) / 100.0

    # Normalise timeframe strings (5m → 5min)
    user_tfs = []
    for tf in raw_tfs:
        c = tf.lower().replace("min", "").replace("m", "")
        user_tfs.append(c + "min" if c in ("5", "15") else tf)

    learned["strategies"] = [s for s in user_strats
                              if s not in learned.get("suspended_strategies", [])]

    await _evaluate_markets(chat_id, settings, client, risk, equity, free_cash,
                            learned, max_exp, user_assets, user_tfs,
                            trigger_asset=trigger_asset, penalty=penalty)


async def _evaluate_markets(chat_id, settings, client, risk, equity, free_cash,
                             learned, max_exp, user_assets, user_tfs,
                             trigger_asset=None, penalty=0.0):
    try:
        all_signals = []
        evaluated   = 0
        skipped_no_spot = 0
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
            evaluated += 1
            # Pass spot price explicitly — one consistent value per eval cycle
            spot_price = feeds.spot.get(market["asset"])
            if not spot_price:
                skipped_no_spot += 1
            sigs = await strategies.evaluate_all(
                market, learned, strategy.global_state, spot_price=spot_price
            )
            all_signals.extend(sigs)

        if all_signals:
            log.info(f"[{chat_id}] {len(all_signals)} signal(s) from {evaluated} markets evaluated")
        elif evaluated > 0:
            # Always log when markets exist but no signals — critical for debugging
            spot_summary = {a: round(feeds.spot[a], 2) for a in user_assets if feeds.spot.get(a)}
            log.info(
                f"[{chat_id}] 0 signals | {evaluated} markets evaluated | "
                f"no_spot={skipped_no_spot} | spot={spot_summary}"
            )

        final = strategies.merge_signals(all_signals, strategy.global_state)
        for sig in final:
            if sig.strategy == "ARB":
                await executor.execute_arb(chat_id, sig, client, risk, equity, free_cash, settings)
            else:
                await executor.execute_trade(chat_id, sig, client, risk, settings, equity, free_cash)
    except Exception as e:
        log.error(f"[{chat_id}] Market eval error: {e}", exc_info=True)


# ── Shared scan loop ──────────────────────────────────────────────────────────

async def _scan_loop():
    global active_markets
    while True:
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        if not _scan_client:
            continue
        try:
            active_markets = await scanner.scan_all(_scan_client)
            telegram_bot._active_markets = active_markets
            executor.init_executor(active_markets, _tg_app)
            log.info(f"Scan: {len(active_markets)} markets")
            feeds.restart_bayse_feed(active_markets, _on_market_update)
        except Exception as e:
            log.warning(f"Scan failed: {e}")


def _refresh_timers():
    for m in active_markets:
        m["secs_to_close"] = scanner._seconds_to_close(m.get("closing_date", ""))


def _on_spot_price(asset: str, price: float):
    lag = feeds_direct.check_lag(asset, price)
    if lag["status"] == "stale":
        # Oracle is stale but Bayse relay is live — still update history and
        # evaluate. Strategies use feeds.spot (relay price) directly; we just
        # skip the oracle-cross-check here. Blocking evaluations entirely when
        # the oracle is temporarily offline caused 4-hour trading blackouts.
        strategy.update_price_history(asset, price)
        asyncio.create_task(_evaluate_all_users_for_asset(asset, penalty=0.0))
        return
    penalty = 0.0010 if lag["status"] == "degraded" else 0.0
    strategy.update_price_history(asset, lag["price"])
    recorder.record_spot_tick(asset, lag["price"])
    asyncio.create_task(_evaluate_all_users_for_asset(asset, penalty))


async def _evaluate_all_users_for_asset(asset: str, penalty: float = 0.0):
    now = time.time()
    if now - _last_market_eval.get(asset, 0) < 1.0:
        return
    _last_market_eval[asset] = now

    global _active_users_cache, _active_users_cache_time
    if not _active_users_cache or (now - _active_users_cache_time) > _CACHE_TTL:
        _active_users_cache      = await asyncio.to_thread(database.get_all_active)
        _active_users_cache_time = now

    for user in _active_users_cache:
        asyncio.create_task(_evaluate_single_user(user, asset, penalty=penalty))


def _on_market_update(market_id: str, prices: dict):
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    if not market:
        return
    asset = market.get("asset", "")
    if asset == "BTC":
        # Uses the OLD (pre-update) yes_price as the move-detection baseline —
        # this must happen BEFORE we write the new price below.
        strategy.record_btc_move(market, prices.get("yes", market["yes_price"]))

    # CRITICAL: actually commit the live price update. Previously this never
    # happened — active_markets' yes_price/no_price were only ever refreshed
    # by the next REST scan (every 15s), meaning every strategy was reading
    # stale prices for EV/edge calculations on every tick except the one
    # right after a scan. This is the live source of truth; commit it.
    new_yes = prices.get("yes")
    new_no  = prices.get("no")
    if new_yes is not None and new_no is not None:
        ny, nn = float(new_yes), float(new_no)
        if 0.90 <= (ny + nn) <= 1.05:
            market["yes_price"] = ny
            market["no_price"]  = nn
        # else: malformed tick, leave the last-known-good price in place
        # rather than poisoning the market dict with a bad data point.

    asyncio.create_task(_evaluate_all_users_for_asset(asset, penalty=0.0))


# ── Heartbeat ─────────────────────────────────────────────────────────────────

async def _heartbeat_loop():
    log.info("Heartbeat evaluation loop started (30s)")
    while True:
        try:
            await asyncio.sleep(30)
            if not active_markets:
                continue
            global _active_users_cache, _active_users_cache_time
            now = time.time()
            if not _active_users_cache or (now - _active_users_cache_time) > _CACHE_TTL:
                _active_users_cache      = await asyncio.to_thread(database.get_all_active)
                _active_users_cache_time = now
            for user in _active_users_cache:
                asyncio.create_task(_evaluate_single_user(user, penalty=0.0))
        except Exception as e:
            log.error(f"Heartbeat error: {e}")


# ── Self-ping ─────────────────────────────────────────────────────────────────

async def _self_ping_loop():
    url = (os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not url:
        return
    await asyncio.sleep(60)
    async with ClientSession() as session:
        while True:
            await asyncio.sleep(780)
            try:
                async with session.get(f"{url}/ping", timeout=ClientTimeout(total=10)) as r:
                    log.debug(f"Self-ping {r.status}")
            except Exception:
                pass


# ── Dashboard stats ───────────────────────────────────────────────────────────

async def _dashboard_loop():
    while True:
        try:
            user_stats = []
            for cid, client in _user_clients.items():
                risk = _user_risks.get(cid)
                try:
                    bal = await client.get_balance_ngn()
                except Exception:
                    bal = 0
                user_stats.append({
                    "id":         f"{cid[:4]}...{cid[-4:]}" if len(cid) > 8 else cid,
                    "paused":     risk.paused if risk else True,
                    "balance":    bal,
                    "pnl_today":  0,
                    "mode":       getattr(risk, "mode", "balanced"),
                    "exposure":   (risk.deployed() / bal * 100) if (risk and bal > 0) else 0,
                    "open_count": len(risk.open_positions) if risk else 0,
                })
            oracle_stats = {
                a: {"price": d["price"], "lag": time.time() - d["time"]}
                for a, d in feeds_direct.direct_spot.items()
            }
            server.stats_cache.update({
                "users": user_stats, "oracles": oracle_stats, "last_update": time.time()
            })
        except Exception as e:
            log.error(f"Dashboard update error: {e}")
        await asyncio.sleep(30)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _tg_app, active_markets, _scan_client

    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN not set")
        sys.exit(1)

    log.info("=== Bayse Bot Starting ===")
    database.init_db()

    asyncio.create_task(server.start_server(port=8080))
    asyncio.create_task(_self_ping_loop())

    database.release_singleton_lock()
    if not database.force_acquire_singleton_lock():
        log.critical("Could not acquire singleton lock. Exiting.")
        return
    log.info("Singleton lock acquired.")

    async def _lock_heartbeat():
        while True:
            await asyncio.sleep(12)
            if not await asyncio.to_thread(database.heartbeat_singleton_lock):
                log.critical("Lost singleton lock — self-terminating.")
                os._exit(1)
    asyncio.create_task(_lock_heartbeat())

    _tg_app = telegram_bot.build_app()
    telegram_bot.inject(
        user_clients=_user_clients, user_risks=_user_risks,
        user_daily=_user_daily, active_markets=active_markets,
        start_user_fn=start_user,
    )

    import random
    await asyncio.sleep(random.uniform(2, 8))

    try:
        await _tg_app.bot.set_webhook(url="https://google.com/unused-kick")
        await asyncio.sleep(5)
        await _tg_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"Telegram ghost kick: {e}")

    await _tg_app.initialize()
    await _tg_app.start()
    try:
        await _tg_app.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        log.error(f"Telegram polling start: {e}")

    executor.init_executor(active_markets, _tg_app)
    await strategy.load_memory()

    asyncio.create_task(feeds.start_feeds(on_price=_on_spot_price))
    asyncio.create_task(feeds_direct.binance_feed())
    asyncio.create_task(feeds_direct.binance_rest_fallback())
    asyncio.create_task(learner.resolution_monitor(_user_clients, _user_risks, _tg_app))
    asyncio.create_task(learner.daily_learning_loop(_tg_app))
    asyncio.create_task(_heartbeat_loop())
    asyncio.create_task(_scan_loop())
    asyncio.create_task(_dashboard_loop())

    # ── Reconnect existing users with CORRECT status message ─────────────────
    existing = await asyncio.to_thread(database.get_all_active)
    log.info(f"Reconnecting {len(existing)} existing user(s)")

    for user in existing:
        cid      = user["chat_id"]
        settings = user.get("settings", {})
        is_paused = settings.get("paused", False)
        mode      = settings.get("mode", "balanced")

        await start_user(cid)

        if _scan_client is None:
            _scan_client = _get_client(user)

        # Tell user what state the bot is actually in — not a blanket "resumed"
        try:
            if is_paused:
                await telegram_bot.send_message(
                    _tg_app, cid,
                    f"🔄 *Bot restarted* (update deployed)\n\n"
                    f"⏸ Your trading was *paused* before the restart — "
                    f"it is still paused.\nSend /resume when you're ready.",
                    parse_mode="Markdown",
                )
                log.info(f"[{cid}] Reconnected | mode={mode} | PAUSED — notified")
            else:
                await telegram_bot.send_message(
                    _tg_app, cid,
                    f"🚀 *Bot updated and reconnected.*\n\n"
                    f"Mode: *{mode.title()}* | Trading: *Active*",
                    parse_mode="Markdown",
                )
                log.info(f"[{cid}] Reconnected | mode={mode} | ACTIVE — notified")
        except Exception:
            pass

    if _scan_client:
        active_markets = await scanner.scan_all(_scan_client)
        telegram_bot._active_markets = active_markets
        executor.init_executor(active_markets, _tg_app)
        log.info(f"Initial scan: {len(active_markets)} markets")
        feeds.restart_bayse_feed(active_markets, _on_market_update)
        asyncio.create_task(scanner.discover_series(_scan_client))

    for _ in range(20):
        if len(feeds.spot) >= 2:
            break
        await asyncio.sleep(1)
    log.info(f"Spot prices: {feeds.spot}")

    while True:
        await asyncio.sleep(5)
        _refresh_timers()


if __name__ == "__main__":
    import os
    asyncio.run(main())
