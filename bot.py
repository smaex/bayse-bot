"""
Multi-user trading bot — one server, all users via Telegram.
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# pyrefly: ignore [missing-import]
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
import health
from risk import RiskManager
from client import BayseClient
from config import (TELEGRAM_TOKEN, CURRENCY, SCAN_INTERVAL_SECONDS,
                    SYSTEMIC_RISK_HALT_MINS, EXIT_EV_THRESHOLD, MIN_EXIT_TIME_REMAINING,
                    TAKE_PROFIT_GAIN_PCT, TAKE_PROFIT_MIN_SECS_REMAINING)
from strategies.utils import win_probability, realized_vol_hourly

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

def _safe_get_all_active():
    if hasattr(database, "get_all_active"):
        return database.get_all_active()
    if hasattr(database, "get_all_users"):
        try:
            return [u for u in database.get_all_users() if u.get("is_active", 1) == 1]
        except Exception:
            pass
    return []

# ── Shared state ──────────────────────────────────────────────────────────────
active_markets:    list[dict]             = []
_user_clients:     dict[str, BayseClient] = {}
_user_risks:       dict[str, RiskManager] = {}
_user_daily:       dict[str, dict]        = {}
_last_balance:          dict[str, float] = {}
_pending_balance_event: dict[str, float] = {}  # chat_id -> suspected new balance, awaiting confirmation
_last_resolution_time:  dict[str, float] = {}  # chat_id -> epoch of most recently resolved trade
_low_bal_notified: dict[str, str]         = {}
_systemic_alert:   dict[str, bool]        = {}
_scan_client:      BayseClient | None     = None
_tg_app                                   = None
_owns_singleton                           = False

_last_market_eval: dict[str, float] = {}
_user_eval_locks: dict[str, asyncio.Lock] = {}
_background_tasks: set[asyncio.Task] = set()

_active_users_cache:      list[dict] = []
_active_users_cache_time: float      = 0.0
_CACHE_TTL                           = 30.0

_BALANCE_EVENT_MIN_NGN = 200
_BALANCE_EVENT_MIN_PCT = 0.05
_MIN_VIABLE_BALANCE    = 500


async def _supervise(name: str, factory):
    """Restart a forever-loop if it crashes or returns unexpectedly."""
    backoff = 1.0
    while True:
        try:
            health.touch(f"task:{name}", state="running")
            await factory()
            raise RuntimeError("background loop returned unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            health.fail(f"task:{name}", exc, state="restarting")
            log.exception("Background task %s crashed; restart in %.1fs", name, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)


def _start_supervised(name: str, factory) -> asyncio.Task:
    task = asyncio.create_task(_supervise(name, factory), name=f"supervisor:{name}")
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _get_client(user: dict) -> BayseClient:
    cid = user["chat_id"]
    if cid not in _user_clients:
        _user_clients[cid] = BayseClient(user["public_key"], user["secret_key"])
    return _user_clients[cid]


def _get_risk(chat_id: str) -> RiskManager:
    if chat_id not in _user_risks:
        _user_risks[chat_id] = RiskManager()
    return _user_risks[chat_id]


def _session_date() -> str:
    return datetime.now(ZoneInfo(config.TRADING_TIMEZONE)).date().isoformat()


def _daily(chat_id: str, balance: float, settings: dict) -> dict:
    today = _session_date()
    ds = _user_daily.get(chat_id)
    if not ds or ds.get("date") != today:
        ds = settings.get("daily_state", {})
        if ds.get("date") != today:
            old_target_hit = ds.get("target_hit", False)
            ds = {"date": today, "start_balance": balance, "target_hit": False}
            settings["daily_state"] = ds
            # Automatically unpause if paused due to yesterday's daily target or drawdown
            if old_target_hit or settings.get("paused_reason") in (
                "daily_target", "daily_loss_limit", "drawdown"
            ):
                settings["paused"] = False
                settings.pop("paused_reason", None)
                risk = _user_risks.get(chat_id)
                if risk:
                    risk.paused = False
                    risk.peak_balance = balance
                    risk._dd_breach_since = 0.0
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
    if not user.get("public_key") or not user.get("secret_key"):
        health.fail(f"user:{chat_id}", "API credentials unavailable")
        log.error(f"[{chat_id}] User not started: encrypted API credentials are unavailable")
        return
    client = _get_client(user)
    if _scan_client is None:
        _scan_client = client

    settings = user.get("settings", {})
    # Persist user preferences unchanged, but enforce global safety ceilings at
    # evaluation/execution time. This keeps settings honest without allowing an
    # old aggressive profile to bypass a new operator risk policy.

    risk = _get_risk(chat_id)
    if not risk.open_positions:
        # Recovery must finish before evaluations start; the old fire-and-forget
        # load created a restart race where the bot could double-enter a market.
        unresolved = await asyncio.to_thread(database.get_all_unresolved, chat_id)
        for t in unresolved:
            mid = t.get("market_id")
            if not mid:
                continue
            key = mid if mid not in risk.open_positions else f"{mid}:{t.get('outcome')}:{t.get('order_id')}"
            risk.add_position(key, {
                "market_id": mid,
                "trade_id": t["trade_id"], "event_id": t["event_id"],
                "order_id": t.get("order_id"),
                "outcome": t["outcome"], "outcome_id": t["outcome_id"],
                "entry_price": t["entry_price"], "amount_ngn": t["amount_ngn"],
                "filled_quantity": float(t.get("filled_quantity") or 0.0),
                "strategy": t["strategy"], "asset": t["asset"],
                "timeframe": t["timeframe"],
                "confirmed_filled": float(t.get("filled_quantity") or 0.0) > 0,
            })

    if chat_id not in _user_tasks or _user_tasks[chat_id].done():
        _user_tasks[chat_id] = asyncio.create_task(
            _supervise_user_loop(chat_id), name=f"user:{chat_id}"
        )
        is_paused = settings.get("paused", False)
        mode      = settings.get("mode", "balanced")
        log.info(f"[{chat_id}] Trading loop started | mode={mode} | paused={is_paused}")

_user_tasks: dict[str, asyncio.Task] = {}


async def _supervise_user_loop(chat_id: str):
    backoff = 1.0
    while True:
        try:
            await _user_loop(chat_id)
            return  # clean return means the user was deactivated
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            health.fail(f"user:{chat_id}", exc)
            log.exception(f"[{chat_id}] User loop crashed; restart in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)


async def _user_loop(chat_id: str):
    """5-second housekeeping loop per user.

    Fast 5s cycle (was 30s) is needed for the take-profit exit logic:
    if a position gains >35% in the final 5 minutes, we need to catch
    that window before the candle closes or a reversal wipes the gain.
    The evaluation itself is cheap (no network calls) so 5s is safe.
    """
    strategy.set_user_context(chat_id)
    iter_count   = 0
    last_log_min = -1  # track last minute we logged status

    while True:
        await asyncio.sleep(5)
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
        health.touch(f"user:{chat_id}", equity=round(equity, 2))
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

        # ── Deposit / withdrawal detection (debounced + quiet-state guard) ────
        # QUIET-STATE GUARD: if any position is open, a market is locked for
        # execution, or a trade was resolved in the last 60 s, the exchange
        # balance and our risk.deployed() total are temporarily out of sync.
        # Acting on a balance change during this window causes false deposit /
        # withdrawal alerts. We skip this cycle and let everything settle.
        _recent_resolution = time.time() - _last_resolution_time.get(chat_id, 0.0) < 60
        _positions_active  = bool(risk.open_positions or risk.pending_markets)
        if _positions_active or _recent_resolution:
            # Balance isn't stable yet — don't update the baseline either,
            # so the comparison stays valid once trading goes quiet.
            pass
        else:
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
                            # Update peak_balance so drawdown tracking stays accurate,
                            # but do NOT update start_balance — daily profit target is
                            # locked to the balance at the START of the day, not moving
                            # targets every time the user deposits or wins.
                            risk.peak_balance = equity
                            _last_balance[chat_id] = equity
                            if _tg_app:
                                await telegram_bot.notify_deposit_detected(_tg_app, chat_id, delta, "NGN")
                        else:
                            log.info(f"[{chat_id}] WITHDRAWAL detected ₦{delta:,.0f} | new balance ₦{equity:,.0f}")
                            # Adjust peak_balance to prevent false drawdown pauses on withdrawals.
                            # Again, do NOT touch start_balance — daily target stays fixed.
                            risk.peak_balance = max(0.0, risk.peak_balance + delta)
                            _last_balance[chat_id] = equity
                            if _tg_app:
                                await telegram_bot.send_message(
                                    _tg_app, chat_id,
                                    f"💸 *Withdrawal detected* — ₦{abs(delta):,.0f} removed\n"
                                    f"New balance: ₦{equity:,.2f}\n"
                                    f"_(Daily profit target unchanged — based on start-of-day balance)_",
                                    parse_mode="Markdown",
                                )
                        _pending_balance_event.pop(chat_id, None)
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

        # ── Position Exit / Soft Stop-Loss (ALWAYS RUNS FIRST) ───────────────
        # Must run at the absolute start of every loop cycle!
        # Even if trading is paused, low balance, daily target hit, or in drawdown,
        # existing open positions must ALWAYS be actively monitored, stopped-out on reversals, or profit-locked!
        try:
            await _evaluate_and_exit_positions(chat_id, client, risk, settings)
        except Exception as exit_err:
            log.error(f"[{chat_id}] Position exit eval error: {exit_err}", exc_info=True)

        # ── Paused check ───────────────────────────────────────────────────
        if settings.get("paused"):
            if iter_count % 6 == 0:   # log every 3 minutes when paused
                log.info(f"[{chat_id}] PAUSED — skipping evaluation")
            continue

        # ── Low balance guard ──────────────────────────────────────────────
        if equity < _MIN_VIABLE_BALANCE:
            today = _session_date()
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
        session_date = _session_date()
        profit = await asyncio.to_thread(
            database.get_daily_resolved_pnl,
            chat_id, session_date, config.TRADING_TIMEZONE,
        )
        target = _daily_target(settings, day["start_balance"])

        # Sync ground-truth values onto risk so is_in_strict_mode() actually
        # works. risk.daily_target was never assigned anywhere before this —
        # it stayed at its 0.0 default permanently, silently disabling the
        # "tighten up near daily target" safety check with no error at all.
        risk.daily_target       = target
        risk.daily_realized_pnl = profit
        risk.last_reset_date    = session_date

        daily_loss_pct = min(
            max(float(settings.get("daily_loss_limit_pct", config.DEFAULT_DAILY_LOSS_LIMIT_PCT)), 0.1),
            config.MAX_DAILY_LOSS_LIMIT_PCT,
        )
        daily_loss_limit = day["start_balance"] * daily_loss_pct / 100.0
        if profit <= -daily_loss_limit:
            settings["paused"] = True
            settings["paused_reason"] = "daily_loss_limit"
            risk.paused = True
            await asyncio.to_thread(database.update_settings, chat_id, settings)
            log.warning(
                f"[{chat_id}] DAILY LOSS STOP ₦{profit:+,.0f} <= -₦{daily_loss_limit:,.0f}"
            )
            if _tg_app:
                await telegram_bot.send_message(
                    _tg_app, chat_id,
                    f"🛑 *Daily loss limit reached* — ₦{profit:+,.0f}. "
                    "New entries are paused; open positions remain monitored.",
                    parse_mode="Markdown",
                )
            continue

        if target > 0 and profit >= target and not day["target_hit"]:
            day["target_hit"] = True
            settings["daily_state"] = day
            settings["paused"]       = True
            settings["paused_reason"] = "daily_target"
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
            settings["paused_reason"] = "drawdown"
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
        _active_users_cache      = await asyncio.to_thread(_safe_get_all_active)
        _active_users_cache_time = time.time()

        await _evaluate_single_user(user, penalty=0.0)


async def _evaluate_and_exit_positions(chat_id: str, client, risk, settings: dict):
    """
    Soft model-based exit: re-evaluate every open position using the diffusion
    model's updated win probability. If EV drops below EXIT_EV_THRESHOLD
    (default -15%), the thesis is mathematically wrong — exit the position.

    This runs every 5s inside _user_loop (was 30s). It does NOT use a hard price-based
    stop-loss (that's suboptimal for binary options that settle at 0 or 1).
    Instead, it compares the model's estimated win probability against the
    current market price to determine if holding is still +EV.
    Also fires a take-profit exit if the position gained >35% and <5 mins remain.
    """
    if not risk.open_positions:
        return

    positions_to_exit = []
    stale_positions = []

    for position_key, pos in list(risk.open_positions.items()):
        market_id = pos.get("market_id") or position_key
        market = next((m for m in active_markets if m["market_id"] == market_id), None)

        # ── CRITICAL FIX: If the market rotated out of active_markets ─────────
        # (new candle started, scanner replaced the old market_id), we MUST still
        # evaluate the position. Use stored position data + live Binance spot feed.
        # Without this, the exit engine silently skips the position and it rides
        # all the way to resolution at 0.00 or 1.00 with zero protection!
        asset       = pos.get("asset", "")
        outcome     = pos.get("outcome", "YES")
        entry_price = float(pos.get("entry_price") or 0.5)
        amount_ngn  = float(pos.get("amount_ngn") or 100.0)
        direct_price, direct_time = feeds_direct.get_direct_price(asset)
        if direct_price and time.time() - direct_time <= config.FEED_STALE_SEC:
            spot_price = direct_price
        elif time.time() - feeds.spot_updated_at.get(asset, 0.0) <= config.FEED_STALE_SEC:
            spot_price = feeds.spot.get(asset)
        else:
            spot_price = None

        # Retrieve market metadata from active scanner or stored position dictionary
        threshold   = (market.get("threshold") if market else None) or pos.get("threshold")
        closing_date = (market.get("closing_date") if market else "") or pos.get("closing_date", "")
        secs        = market.get("secs_to_close", 0) if market else (scanner._seconds_to_close(closing_date) if closing_date else 0)

        if not market:
            # Market has rotated out — if candle has fully elapsed (secs <= 0), clean up stale positions
            if secs <= 0:
                age_secs = time.time() - pos.get("placed_at", 0)
                if age_secs > 960:  # 16 minutes
                    log.warning(
                        f"[{chat_id}] Cleaning stale resolved position on {market_id} "
                        f"(age={age_secs:.0f}s, asset={asset}, strategy={pos.get('strategy')})"
                    )
                    stale_positions.append(position_key)
                continue

        # Don't try to exit in the final 45 seconds — settlement/oracle resolution
        # risk makes exit prices unreliable and the market is about to close anyway
        if secs < MIN_EXIT_TIME_REMAINING:
            continue

        if not threshold or not spot_price:
            continue

        # Re-estimate win probability using the diffusion model
        dist_pct = (spot_price - threshold) / threshold
        rv = realized_vol_hourly(asset, strategy.global_state)
        w_est = win_probability(dist_pct, secs, asset, sigma_override=rv)

        # If we hold NO, our win prob is the probability price stays BELOW threshold
        if outcome == "NO":
            w_est = 1.0 - w_est

        # Current market price for our held outcome (use live price or estimated thesis value)
        if market:
            current_price = market.get("yes_price", 0.5) if outcome == "YES" else market.get("no_price", 0.5)
        else:
            current_price = entry_price

        # Dynamic Thesis Value based on continuous spot diffusion:
        # If orderbook quotes are lagging, w_est gives the true statistical fair value of the position
        effective_val = w_est

        # Track the highest price/valuation reached during the position's life
        pos["peak_price"] = max(pos.get("peak_price", entry_price), current_price, effective_val)
        peak_price = pos["peak_price"]

        # ── Proactive Threat Warning Nudge (Spot Compression Alert) ───────────
        # If spot compresses to within 0.08% of strike and threat has not been alerted yet:
        is_threatened = (outcome == "YES" and dist_pct < 0.0008) or (outcome == "NO" and dist_pct > -0.0008)
        if is_threatened and not pos.get("threat_alerted") and _tg_app:
            pos["threat_alerted"] = True
            try:
                tf = pos.get("timeframe", "15min")
                strat = pos.get("strategy", "?")
                threat_msg = (
                    f"⚠️ *POSITION THREAT WARNING*\n\n"
                    f"Strategy: *{strat}* | *{asset} {tf}* (*{outcome}*)\n"
                    f"Strike Distance: *{dist_pct:+.3%}* (compressing!)\n"
                    f"Time Remaining: *{secs:.0f}s*\n"
                    f"Status: *Proactively cancelled resting orders & armed emergency SL*"
                )
                asyncio.create_task(telegram_bot.send_message(_tg_app, chat_id, threat_msg, parse_mode="Markdown"))
            except Exception:
                pass

        # ── 1. DYNAMIC TAKE-PROFIT & TRAILING PROFIT LOCK ─────────────────────
        # Locks in profit whenever:
        # A) Live price or diffusion model shows >= +15% profit with < 450s remaining
        # B) Position peaked >= +15% profit, and spot/price is now declining (trailing lock)
        gain_pct = max((current_price - entry_price) / entry_price if entry_price > 0 else 0.0,
                       (effective_val - entry_price) / entry_price if entry_price > 0 else 0.0)
        peak_gain_pct = (peak_price - entry_price) / entry_price if entry_price > 0 else 0.0
        dropped_from_peak = (peak_price - max(current_price, effective_val)) / peak_price if peak_price > 0 else 0.0

        if secs < 450 and gain_pct >= 0.15:
            positions_to_exit.append({
                "market_id": market_id,
                "position_key": position_key,
                "pos": pos,
                "market": market,
                "w_est": w_est,
                "current_price": current_price,
                "ev_hold": gain_pct,
                "exit_reason": "TAKE_PROFIT",
            })
            continue

        if peak_gain_pct >= 0.12 and dropped_from_peak >= 0.08 and max(current_price, effective_val) >= entry_price:
            positions_to_exit.append({
                "market_id": market_id,
                "position_key": position_key,
                "pos": pos,
                "market": market,
                "w_est": w_est,
                "current_price": current_price,
                "ev_hold": gain_pct,
                "exit_reason": "REVERSAL_EXIT",
            })
            continue

        # ── 2. DYNAMIC REAL-TIME STOP-LOSS (SPOT INVALIDATION) ─────────────────
        # Instantly dumps the position if:
        # A) Spot price crosses to the losing side of threshold by >= 0.005%
        # B) Win probability (w_est) drops below 40% (thesis mathematically broken)
        # C) Current loss >= 15%
        adverse_flip = (outcome == "YES" and dist_pct < -0.00005) or (outcome == "NO" and dist_pct > +0.00005)
        thesis_broken = (w_est < 0.40)
        loss_pct = (entry_price - current_price) / entry_price if entry_price > 0 else 0.0
        # MAKER Late-Candle Protection: cancel resting limit orders in the final 5 mins (<300s) to prevent adverse selection dumps
        is_maker_late = (pos.get("strategy") == "MAKER" and secs < 300 and not pos.get("confirmed_filled"))

        if adverse_flip or thesis_broken or is_maker_late or (loss_pct >= 0.15 and current_price >= 0.05):
            positions_to_exit.append({
                "market_id": market_id,
                "position_key": position_key,
                "pos": pos,
                "market": market,
                "w_est": w_est,
                "current_price": current_price,
                "ev_hold": w_est - 0.5,
                "exit_reason": "STOP_LOSS",
            })

    # Execute exits
    for exit_info in positions_to_exit:
        pos = exit_info["pos"]
        market = exit_info["market"]
        market_id = exit_info["market_id"]
        position_key = exit_info["position_key"]
        w_est = exit_info["w_est"]
        current_price = exit_info["current_price"]
        ev_hold = exit_info["ev_hold"]
        exit_reason = exit_info.get("exit_reason", "STOP_LOSS")

        outcome_id = pos.get("outcome_id", "")
        event_id = pos.get("event_id", "")
        amount_ngn = pos.get("amount_ngn", 0)
        entry_price = pos.get("entry_price", 0)

        if not outcome_id or not event_id:
            continue

        if exit_reason == "TAKE_PROFIT":
            log.info(
                f"[{chat_id}] TAKE-PROFIT SIGNAL | {pos.get('strategy', '?')} {pos.get('asset', '?')} "
                f"{pos.get('outcome', '?')} | entry={entry_price:.3f} now={current_price:.3f} "
                f"gain={ev_hold:+.1%} | locking profit"
            )
        elif exit_reason == "REVERSAL_EXIT":
            log.info(
                f"[{chat_id}] REVERSAL PROTECTION SIGNAL | {pos.get('strategy', '?')} {pos.get('asset', '?')} "
                f"{pos.get('outcome', '?')} | entry={entry_price:.3f} peak={pos.get('peak_price', entry_price):.3f} "
                f"now={current_price:.3f} | locking remaining gain {ev_hold:+.1%} before candle dump"
            )
        else:
            log.info(
                f"[{chat_id}] EXIT SIGNAL | {pos.get('strategy', '?')} {pos.get('asset', '?')} "
                f"{pos.get('outcome', '?')} | w_est={w_est:.1%} price={current_price:.3f} "
                f"EV={ev_hold:+.1%} < {EXIT_EV_THRESHOLD:.0%} | "
                f"entry={entry_price:.3f} → now={current_price:.3f}"
            )

        try:
            # Step 1: If this was a maker LIMIT order, CANCEL the open buy order on the exchange first!
            # If the order is still resting on the book when spot dumps, someone will dump INTO our buy order.
            # Cancelling it immediately stops us from getting filled at the worst possible time!
            maker_order_id = pos.get("order_id")
            if maker_order_id:
                try:
                    await client.cancel_order(maker_order_id)
                    log.info(f"[{chat_id}] EXIT: Cancelled resting LIMIT order {maker_order_id} on {market_id}")
                except Exception as ce:
                    # Order might already be filled or expired, which is normal
                    log.debug(f"[{chat_id}] Cancel resting order notice: {ce}")

            # Confirm that a resting maker order actually filled before trying
            # to sell it. A placed GTC order is not a position.
            confirmed_qty = float(pos.get("filled_quantity") or 0.0)
            if maker_order_id and not pos.get("confirmed_filled"):
                order_state = await client.get_order(maker_order_id)
                confirmed_qty = client.parse_filled_shares(order_state)
                if confirmed_qty <= 0:
                    risk.remove_position(position_key)
                    trade_id = pos.get("trade_id")
                    if trade_id:
                        await asyncio.to_thread(database.resolve_trade, trade_id, None, 0.0)
                    log.info(f"[{chat_id}] Cancelled unfilled maker order {maker_order_id}")
                    continue
                confirmed_entry = float(
                    order_state.get("avgFillPrice")
                    or order_state.get("price")
                    or entry_price
                )
                confirmed_fee = float(order_state.get("fee") or 0.0)
                confirmed_cost = (
                    confirmed_qty * confirmed_entry
                    * config.CURRENCY_BASE_MULTIPLIER
                    + confirmed_fee
                )
                pos["confirmed_filled"] = True
                pos["filled_quantity"] = confirmed_qty
                pos["entry_price"] = confirmed_entry
                pos["amount_ngn"] = confirmed_cost
                entry_price = confirmed_entry
                amount_ngn = confirmed_cost
                trade_id = pos.get("trade_id")
                if trade_id:
                    await asyncio.to_thread(
                        database.update_trade_fill,
                        trade_id, confirmed_cost, confirmed_qty, confirmed_entry,
                    )

            # Bayse SELL amount is desired currency proceeds, not a number of
            # shares. Reconcile against the exchange portfolio before sending.
            portfolio_pos = await client.get_position(outcome_id)
            available_shares = float(
                (portfolio_pos or {}).get("availableBalance")
                or (portfolio_pos or {}).get("balance")
                or confirmed_qty
                or 0.0
            )
            exchange_sell_price = float(
                (portfolio_pos or {}).get("sellPrice") or current_price or 0.0
            )
            current_value = float((portfolio_pos or {}).get("currentValue") or 0.0)
            if current_value <= 0 and available_shares > 0 and exchange_sell_price > 0:
                fee_rate = float((market or {}).get("fee_rate", 0.02))
                current_value = (
                    available_shares * exchange_sell_price
                    * config.CURRENCY_BASE_MULTIPLIER
                    * (1.0 - fee_rate * max(1.0 - exchange_sell_price, config.FEE_FLOOR))
                )
            sell_amount = round(current_value * 0.995, 2)
            min_sell = float((market or {}).get("minimum_order_amount", 100.0))
            if available_shares <= 0 or sell_amount < min_sell:
                log.warning(
                    f"[{chat_id}] EXIT deferred for {market_id}: position value "
                    f"₦{sell_amount:,.2f} is below sell minimum ₦{min_sell:,.0f}"
                )
                continue

            sell_quote = await client.get_quote(
                event_id, market_id, outcome_id, "SELL", sell_amount, CURRENCY
            )
            quote_qty = float(sell_quote.get("quantity") or 0.0)
            if (
                sell_quote.get("completeFill") is not True
                or quote_qty <= 0
                or quote_qty > available_shares * 1.001
            ):
                log.warning(f"[{chat_id}] EXIT quote cannot liquidate safely on {market_id}")
                continue

            # Step 2: Sell held shares at market with calibrated slippage.
            # - TAKE_PROFIT: 0.05 (5%) — refuse to give away locked gains to wide AMM spreads
            # - REVERSAL_EXIT: 0.08 (8%) — profit protection before candle dump
            # - STOP_LOSS: 0.20 (20%) — urgent but bounded; an 80% allowance
            #   converted a protective exit into an effectively uncontrolled market dump.
            if exit_reason == "TAKE_PROFIT":
                exit_slippage = 0.05
            elif exit_reason == "REVERSAL_EXIT":
                exit_slippage = 0.08
            else:
                exit_slippage = 0.20

            resp = await client.place_order(
                event_id=event_id, market_id=market_id,
                outcome_id=outcome_id, side="SELL",
                amount=sell_amount, order_type="MARKET",
                currency=CURRENCY, max_slippage=exit_slippage,
            )
            order = resp.get("order") or resp.get("clobOrder") or resp.get("ammOrder") or resp
            order_id = order.get("id") or order.get("orderId") or order.get("order_id")

            sold_shares = client.parse_filled_shares(order)
            if sold_shares <= 0:
                log.warning(f"[{chat_id}] EXIT order {order_id} returned zero confirmed fill; keeping position")
                continue
            sell_price = float(order.get("avgFillPrice") or order.get("price") or current_price)
            gross_proceeds = sold_shares * sell_price * config.CURRENCY_BASE_MULTIPLIER
            explicit_proceeds = order.get("proceeds") or order.get("netProceeds")
            if explicit_proceeds is not None:
                proceeds = float(explicit_proceeds)
            elif order.get("fee") is not None:
                proceeds = gross_proceeds - float(order["fee"])
            else:
                fee_rate = float((market or {}).get("fee_rate", 0.02))
                proceeds = gross_proceeds * (
                    1.0 - fee_rate * max(1.0 - sell_price, config.FEE_FLOOR)
                )
            original_shares = float(pos.get("filled_quantity") or available_shares or sold_shares)
            sold_fraction = min(1.0, sold_shares / original_shares) if original_shares > 0 else 1.0
            cost_sold = amount_ngn * sold_fraction
            pnl = proceeds - cost_sold

            log.info(
                f"[{chat_id}] EXIT FILLED | {pos.get('strategy', '?')} {pos.get('asset', '?')} "
                f"@ {sell_price:.4f} | PnL ≈ ₦{pnl:+,.0f} | reason={exit_reason} | order={order_id}"
            )

            # Update only the quantity actually sold. A partial FAK fill must
            # not make the remaining exchange holding disappear from risk.
            remaining_shares = max(0.0, original_shares - sold_shares)
            remaining_cost = max(0.0, amount_ngn - cost_sold)
            fully_exited = remaining_shares <= max(1e-8, original_shares * 0.001)
            trade_id = pos.get("trade_id")
            if fully_exited:
                risk.remove_position(position_key)
            else:
                pos["filled_quantity"] = remaining_shares
                pos["amount_ngn"] = remaining_cost
                log.warning(
                    f"[{chat_id}] PARTIAL EXIT | {remaining_shares:.4f} shares remain on {market_id}"
                )
            risk.current_free_cash += proceeds
            risk.add_pnl(pnl)

            # Resolve a fully closed trade, or persist the remaining cost basis
            # so settlement cannot count already-sold shares a second time.
            if trade_id:
                try:
                    if fully_exited:
                        await asyncio.to_thread(database.resolve_trade, trade_id, pnl > 0, pnl)
                    else:
                        await asyncio.to_thread(
                            database.update_trade_remaining,
                            trade_id, remaining_cost, remaining_shares, pnl,
                        )
                except Exception as db_err:
                    log.error(f"[{chat_id}] EXIT DB reconciliation failed: {db_err}")

            # Notify user — differentiate take-profit from stop-loss
            if _tg_app:
                emoji = "🟢" if pnl >= 0 else "🔴"
                try:
                    if exit_reason == "TAKE_PROFIT":
                        gain_pct = ev_hold
                        tg_msg = (
                            f"{emoji} *Take-Profit Exit* 💰\n"
                            f"Strategy: {pos.get('strategy', '?')}\n"
                            f"Asset: {pos.get('asset', '?')} {pos.get('outcome', '')}\n"
                            f"Entry: {entry_price:.3f} → Exit: {sell_price:.3f}\n"
                            f"Gain: {gain_pct:+.1%} locked in before close\n"
                            f"PnL: ₦{pnl:+,.0f}"
                        )
                    elif exit_reason == "REVERSAL_EXIT":
                        gain_pct = ev_hold
                        tg_msg = (
                            f"{emoji} *Reversal Protection Exit* 🛡️\n"
                            f"Strategy: {pos.get('strategy', '?')}\n"
                            f"Asset: {pos.get('asset', '?')} {pos.get('outcome', '')}\n"
                            f"Entry: {entry_price:.3f} (Peak: {pos.get('peak_price', entry_price):.3f}) → Exit: {sell_price:.3f}\n"
                            f"Protected Gain: {gain_pct:+.1%} locked before candle dump\n"
                            f"PnL: ₦{pnl:+,.0f}"
                        )
                    else:
                        salvaged_cash = max(0.0, amount_ngn + pnl)
                        saved_loss = max(0.0, amount_ngn - abs(pnl))
                        tg_msg = (
                            f"🛡️ *Emergency Stop-Loss (Capital Salvaged)*\n\n"
                            f"Strategy: *{pos.get('strategy', '?')}* | *{pos.get('asset', '?')}* (*{pos.get('outcome', '')}*)\n"
                            f"Entry: *{entry_price:.3f}* → Exit: *{sell_price:.3f}*\n"
                            f"Salvaged Cash: *₦{salvaged_cash:,.2f}* (saved ~₦{saved_loss:,.2f} of max loss)\n"
                            f"Realized PnL: *-₦{abs(pnl):,.2f}* (capital protected)"
                        )
                    await telegram_bot.send_message(
                        _tg_app, chat_id, tg_msg, parse_mode="Markdown",
                    )
                except Exception:
                    pass

        except Exception as e:
            err_str = str(e).lower()
            if "insufficient shares" in err_str or "insufficient balance" in err_str:
                # Could be either:
                # A) True phantom: LIMIT order was never filled (shares = 0)
                # B) Market already resolved: shares were redeemed by the exchange before we could sell
                # Check the order fill status before corrupting the trade record.
                order_id = pos.get("order_id")
                filled_size = 0.0
                if order_id:
                    try:
                        od = await client.get_order(order_id)
                        filled_size = client.parse_filled_shares(od)
                    except Exception as oe:
                        log.debug(f"[{chat_id}] get_order check: {oe}")

                risk.remove_position(position_key)

                if filled_size <= 0:
                    # Genuine phantom — LIMIT order was never filled.
                    log.warning(
                        f"[{chat_id}] EXIT failed — phantom/unfilled LIMIT position on {market_id} "
                        f"(filledSize=0). Removing from tracker. Error: {e}"
                    )
                    trade_id = pos.get("trade_id")
                    if trade_id:
                        try:
                            await asyncio.to_thread(database.resolve_trade, trade_id, None, 0.0)
                        except Exception:
                            pass
                    if _tg_app:
                        try:
                            await telegram_bot.notify_unfilled(
                                _tg_app, chat_id,
                                pos.get("strategy", "MAKER"),
                                pos.get("asset", "?"),
                                pos.get("timeframe", ""),
                                pos.get("outcome", ""),
                                pos.get("amount_ngn", 0),
                            )
                        except Exception as ne:
                            log.warning(f"[{chat_id}] notify_unfilled (phantom exit) failed: {ne}")
                else:
                    # Order WAS filled but market resolved before we could exit.
                    # Do NOT touch resolved_at/won here — resolution_monitor will
                    # process this correctly via get_unresolved → get_event → get_order.
                    log.info(
                        f"[{chat_id}] EXIT failed on resolved market {market_id} "
                        f"(filledSize={filled_size:.2f}) — deferring to resolution_monitor. Error: {e}"
                    )
            else:
                log.error(f"[{chat_id}] EXIT order failed for {market_id}: {e}", exc_info=True)

    # Clean up stale/expired positions that rotated out of active_markets
    for stale_mid in stale_positions:
        risk.remove_position(stale_mid)


async def _evaluate_single_user(user: dict, trigger_asset: str = None, penalty: float = 0.0):
    """Serialize evaluations per user and drop redundant feed-triggered work."""
    chat_id = user["chat_id"]
    lock = _user_eval_locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        return False
    async with lock:
        return await _evaluate_single_user_locked(user, trigger_asset, penalty)


async def _evaluate_single_user_locked(user: dict, trigger_asset: str = None, penalty: float = 0.0):
    chat_id  = user["chat_id"]
    client   = _user_clients.get(chat_id)
    risk     = _user_risks.get(chat_id)
    if not client or not risk:
        return

    # Always re-fetch settings from DB — the user dict passed in may be a
    # stale cached copy with paused=True even after /resume was called.
    fresh_user = await asyncio.to_thread(database.get_user, chat_id, force_fresh=True)
    if not fresh_user or not fresh_user.get("is_active"):
        return
    settings = fresh_user.get("settings", {})
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
    learned["open_positions"] = {
        key: dict(value) for key, value in risk.open_positions.items()
    }
    if risk.peak_balance > 0:
        learned["drawdown_pct"] = (risk.peak_balance - equity) / risk.peak_balance

    user_assets = settings.get("assets",     config.ALL_ASSETS)
    raw_tfs     = settings.get("timeframes",  ["15min", "5min"])
    requested_strats = settings.get("strategies", config.DEFAULT_STRATEGIES)
    user_strats = [s for s in requested_strats if s in config.PERMITTED_STRATEGIES]
    blocked = sorted(set(requested_strats) - set(user_strats))
    if blocked:
        log.warning(f"[{chat_id}] Strategies blocked by global safety policy: {blocked}")
    max_exp = min(
        settings.get("maxexposure", 20.0) / 100.0,
        config.MAX_PORTFOLIO_EXPOSURE,
    )

    # Normalise timeframe strings (5m → 5min)
    user_tfs = []
    for tf in raw_tfs:
        c = tf.lower().replace("min", "").replace("m", "")
        user_tfs.append(c + "min" if c in ("5", "15") else tf)

    suspended = learned.get("suspended_strategies", [])
    if suspended:
        log.warning(f"[{chat_id}] Strategies SUSPENDED by learner: {suspended}")
    learned["strategies"] = [s for s in user_strats if s not in suspended]

    return await _evaluate_markets(
        chat_id, settings, client, risk, equity, free_cash,
        learned, max_exp, user_assets, user_tfs,
        trigger_asset=trigger_asset, penalty=penalty,
    )


async def _evaluate_markets(chat_id, settings, client, risk, equity, free_cash,
                             learned, max_exp, user_assets, user_tfs,
                             trigger_asset=None, penalty=0.0):
    try:
        all_signals = []
        evaluated   = 0
        skipped_no_spot = 0
        skipped_status  = 0
        skipped_asset   = 0
        skipped_tf      = 0
        skipped_halted  = 0
        skipped_trigger = 0
        skipped_stale_feed = 0
        now = time.time()
        for market in active_markets:
            if market.get("status") != "open":
                skipped_status += 1
                continue
            if market["asset"] not in user_assets:
                skipped_asset += 1
                continue
            if trigger_asset and market["asset"] != trigger_asset:
                skipped_trigger += 1
                continue
            if market["timeframe"] not in user_tfs:
                skipped_tf += 1
                continue
            if strategy.is_halted(market["asset"]):
                skipped_halted += 1
                continue
            evaluated += 1
            # Use a fresh independent oracle for crypto probability models,
            # while strategies such as FRONTRUN can still inspect the Bayse
            # relay separately. Never trade on an indefinitely cached tick.
            asset = market["asset"]
            relay_price = feeds.spot.get(asset)
            relay_time = feeds.spot_updated_at.get(asset, 0.0)
            direct_price, direct_time = feeds_direct.get_direct_price(asset)
            is_crypto = asset in {"BTC", "ETH", "SOL"}

            if is_crypto and config.REQUIRE_DIRECT_ORACLE:
                if not direct_price or now - direct_time > config.FEED_STALE_SEC:
                    skipped_stale_feed += 1
                    continue
                spot_price = direct_price
            else:
                spot_price = relay_price
                if not spot_price or now - relay_time > config.FEED_STALE_SEC:
                    skipped_stale_feed += 1
                    continue
            if not spot_price:
                skipped_no_spot += 1
                continue
            sigs = await strategies.evaluate_all(
                market, learned, strategy.global_state, spot_price=spot_price
            )
            all_signals.extend(sigs)

        if all_signals:
            by_strat = {}
            for s in all_signals:
                by_strat[s.strategy] = by_strat.get(s.strategy, 0) + 1
            log.info(
                f"[{chat_id}] {len(all_signals)} signal(s) from {evaluated} markets | "
                f"breakdown: {by_strat}"
            )
        else:
            # Always log market evaluation — critical for debugging
            spot_summary = {a: round(feeds.spot[a], 2) for a in user_assets if feeds.spot.get(a)}
            strats_eval  = learned.get("strategies", [])  # BUG FIX: user_strats is not in scope here
            log.info(
                f"[{chat_id}] 0 signals | total={len(active_markets)} markets | evaluated={evaluated} | "
                f"strats={strats_eval} | "
                f"skip_status={skipped_status} skip_asset={skipped_asset} "
                f"skip_tf={skipped_tf} skip_trigger={skipped_trigger} skip_halted={skipped_halted} "
                f"no_spot={skipped_no_spot} stale_feed={skipped_stale_feed} | "
                f"user_assets={user_assets} user_tfs={user_tfs} | spot={spot_summary}"
            )

        final = strategies.merge_signals(all_signals, strategy.global_state)
        for sig in final:
            if sig.strategy == "ARB":
                await executor.execute_arb(chat_id, sig, client, risk, equity, free_cash, settings)
            elif sig.strategy == "MIDMARKET_MAKER":
                await executor.execute_midmarket_maker(chat_id, sig, client, risk, equity, free_cash, settings)
            else:
                await executor.execute_trade(chat_id, sig, client, risk, settings, equity, free_cash)
        health.touch("evaluation", chat_id=chat_id, markets=evaluated, signals=len(final))
        return True
    except Exception as e:
        health.fail("evaluation", e, chat_id=chat_id)
        log.error(f"[{chat_id}] Market eval error: {e}", exc_info=True)
        return False


# ── Shared scan loop ──────────────────────────────────────────────────────────

async def _scan_loop():
    global active_markets
    while True:
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        if not _scan_client:
            continue
        try:
            active_markets = await scanner.scan_all(_scan_client)
            health.touch("scanner", markets=len(active_markets))
            telegram_bot._active_markets = active_markets
            executor.init_executor(active_markets, _tg_app)
            log.info(f"Scan: {len(active_markets)} markets")
            feeds.restart_bayse_feed(active_markets, _on_market_update)
            try:
                import shadow_tracker
                shadow_tracker.on_market_scan(active_markets)
            except Exception as se:
                log.debug(f"Shadow tracker scan hook: {se}")
        except Exception as e:
            health.fail("scanner", e)
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
    # 250ms debounce: provides sub-second event-driven reaction to spot/book moves
    if now - _last_market_eval.get(asset, 0) < 0.25:
        return
    _last_market_eval[asset] = now

    global _active_users_cache, _active_users_cache_time
    if not _active_users_cache or (now - _active_users_cache_time) > _CACHE_TTL:
        _active_users_cache      = await asyncio.to_thread(_safe_get_all_active)
        _active_users_cache_time = now

    if _active_users_cache:
        results = await asyncio.gather(
            *(
                _evaluate_single_user(user, asset, penalty=penalty)
                for user in _active_users_cache
            ),
            return_exceptions=True,
        )
        if any(result is True for result in results):
            global _last_successful_eval
            _last_successful_eval = time.time()


def _on_market_update(market_id: str, prices: dict):
    market = next((m for m in active_markets if m["market_id"] == market_id), None)
    if not market:
        return
    asset = market.get("asset", "")
    if asset == "BTC":
        # Uses the OLD (pre-update) yes_price as the move-detection baseline —
        # this must happen BEFORE we write the new price below.
        strategy.record_btc_move(market, prices.get("yes", market["yes_price"]))

    # Commit live price updates to market state in real time
    new_yes = prices.get("yes")
    new_no  = prices.get("no")
    if new_yes is not None and new_no is not None:
        ny, nn = float(new_yes), float(new_no)
        if 0.01 <= ny <= 0.99 and 0.01 <= nn <= 0.99:
            market["yes_price"] = ny
            market["no_price"]  = nn
        try:
            import shadow_tracker
            shadow_tracker.on_price_update(market_id, prices)
        except Exception:
            pass

    asyncio.create_task(_evaluate_all_users_for_asset(asset, penalty=0.0))


# ── Heartbeat ─────────────────────────────────────────────────────────────────

async def _heartbeat_loop():
    log.info("Heartbeat evaluation loop started (30s)")
    while True:
        try:
            await asyncio.sleep(30)
            if not active_markets:
                continue
            global _active_users_cache, _active_users_cache_time, _last_successful_eval
            now = time.time()
            if not _active_users_cache or (now - _active_users_cache_time) > _CACHE_TTL:
                _active_users_cache      = await asyncio.to_thread(_safe_get_all_active)
                _active_users_cache_time = now
            results = await asyncio.gather(
                *(_evaluate_single_user(user, penalty=0.0) for user in _active_users_cache),
                return_exceptions=True,
            )
            if any(result is True for result in results):
                _last_successful_eval = time.time()
            health.touch("heartbeat", users=len(_active_users_cache))
        except Exception as e:
            log.error(f"Heartbeat error: {e}")


# ── Feed watchdog + dead-man switch ───────────────────────────────────────────
_FEED_STALE_ALERT_SEC = 120  # Alert if no price update in 2 minutes
_last_feed_alert: float = 0.0
_last_successful_eval: float = time.time()
_DEAD_MAN_THRESHOLD = 300  # 5 minutes with no evaluation = alert

async def _feed_watchdog():
    """Runs every 30s. Alerts via Telegram if feeds go stale or bot is dead."""
    global _last_feed_alert, _last_successful_eval
    while True:
        await asyncio.sleep(30)
        now = time.time()
        health.touch("watchdog")

        # Check feed staleness
        stale_assets = []
        for asset in ["BTC", "ETH", "SOL"]:
            _, t = feeds_direct.get_direct_price(asset)
            age = now - t if t else now - feeds_direct._startup_time
            if age > _FEED_STALE_ALERT_SEC:
                stale_assets.append(f"{asset} ({age:.0f}s)")

        if stale_assets and (now - _last_feed_alert) > 300:
            _last_feed_alert = now
            msg = f"⚠️ FEED WATCHDOG: Stale prices — {', '.join(stale_assets)}"
            log.warning(msg)
            if _tg_app:
                for user_id in list(_user_clients.keys()):
                    try:
                        await telegram_bot.send_message(_tg_app, user_id, msg)
                    except Exception:
                        pass

        # Check if active_markets is empty
        if not active_markets and (now - _last_feed_alert) > 300:
            _last_feed_alert = now
            log.warning("FEED WATCHDOG: active_markets is EMPTY — scanner returned no markets")

        # Dead-man switch
        if (now - _last_successful_eval) > _DEAD_MAN_THRESHOLD and (now - _last_feed_alert) > 300:
            _last_feed_alert = now
            msg = f"🚨 DEAD MAN SWITCH: No successful evaluation in {int(now - _last_successful_eval)}s!"
            log.error(msg)
            if _tg_app:
                for user_id in list(_user_clients.keys()):
                    try:
                        await telegram_bot.send_message(_tg_app, user_id, msg)
                    except Exception:
                        pass

# ── Self-ping ─────────────────────────────────────────────────────────────────

async def _self_ping_loop():
    url = (os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not url:
        # Keep the supervised optional task quiet instead of returning and
        # triggering an endless restart loop.
        await asyncio.Event().wait()
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
    global _tg_app, active_markets, _scan_client, _owns_singleton

    config.validate()
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set")

    if hasattr(database, "init_db"):
        database.init_db()
    elif hasattr(database, "_init_pool"):
        database._init_pool()

    if hasattr(database, "force_acquire_singleton_lock"):
        if not database.force_acquire_singleton_lock():
            log.critical("Could not acquire singleton lock. Exiting.")
            return
        _owns_singleton = True
        log.info("Singleton lease acquired.")
        health.touch("singleton_lock")

    server_task = asyncio.create_task(
        server.start_server(port=int(os.getenv("PORT", "8080"))),
        name="http-server",
    )
    _background_tasks.add(server_task)
    server_task.add_done_callback(_background_tasks.discard)
    _start_supervised("self_ping", _self_ping_loop)

    async def _lock_heartbeat():
        while True:
            await asyncio.sleep(12)
            if hasattr(database, "heartbeat_singleton_lock"):
                if not await asyncio.to_thread(database.heartbeat_singleton_lock):
                    health.fail("singleton_lock", "lease ownership lost")
                    log.critical("Lost singleton lease — self-terminating.")
                    os._exit(1)
                health.touch("singleton_lock")
    _start_supervised("singleton_heartbeat", _lock_heartbeat)

    _tg_app = telegram_bot.build_app()
    telegram_bot.inject(
        user_clients=_user_clients, user_risks=_user_risks,
        user_daily=_user_daily, active_markets=active_markets,
        start_user_fn=start_user,
    )

    import random
    await asyncio.sleep(random.uniform(2, 8))

    try:
        await _tg_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"Telegram webhook cleanup failed: {e}")

    await _tg_app.initialize()
    await _tg_app.start()
    try:
        await _tg_app.updater.start_polling(drop_pending_updates=True)
        health.touch("telegram")
    except Exception as e:
        # Do not advertise a healthy process with a dead control plane.
        health.fail("telegram", e)
        raise RuntimeError(f"Telegram polling failed to start: {e}") from e

    executor.init_executor(active_markets, _tg_app)
    await strategy.load_memory()

    _start_supervised("price_feeds", lambda: feeds.start_feeds(on_price=_on_spot_price))
    _start_supervised("direct_websocket", feeds_direct.binance_feed)
    _start_supervised("direct_rest", feeds_direct.binance_rest_fallback)
    _start_supervised(
        "resolution_monitor",
        lambda: learner.resolution_monitor(_user_clients, _user_risks, _tg_app),
    )
    _start_supervised("daily_learning", lambda: learner.daily_learning_loop(_tg_app))
    _start_supervised("heartbeat", _heartbeat_loop)
    _start_supervised("scanner_loop", _scan_loop)
    _start_supervised("dashboard", _dashboard_loop)
    _start_supervised("feed_watchdog", _feed_watchdog)

    # ── Reconnect existing users with CORRECT status message ─────────────────
    existing = await asyncio.to_thread(_safe_get_all_active)
    log.info(f"Reconnecting {len(existing)} existing user(s)")

    for user in existing:
        cid      = user["chat_id"]
        settings = user.get("settings", {})
        is_paused = settings.get("paused", False)
        mode      = settings.get("mode", "balanced")

        await start_user(cid)
        if cid not in _user_clients:
            try:
                await telegram_bot.send_message(
                    _tg_app, cid,
                    "⚠️ Your saved API credentials could not be loaded. Trading is disabled; use /rekey.",
                )
            except Exception:
                log.warning(f"[{cid}] Could not send credential recovery notice")
            continue

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
        health.touch("scanner", markets=len(active_markets))
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

    health.touch("bot")
    health.set_ready(True)
    log.info("Bot startup complete; readiness enabled")
    while True:
        await asyncio.sleep(5)
        _refresh_timers()
        health.touch("bot")
        if server_task.done():
            error = server_task.exception()
            raise RuntimeError(f"HTTP server stopped unexpectedly: {error}")
        if not _tg_app.updater.running:
            raise RuntimeError("Telegram polling stopped unexpectedly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        health.set_ready(False)
        if _owns_singleton and hasattr(database, "release_singleton_lock"):
            database.release_singleton_lock()
