"""
Multi-user trading bot — one server, all users via Telegram.
"""

import asyncio
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# pyrefly: ignore [missing-import]
from aiohttp import ClientSession, ClientTimeout

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
import stall
import feeds_direct
import health
import maintenance
from risk import RiskManager, position_is_filled, share_quantities_match
from client import BayseClient
from config import (TELEGRAM_TOKEN, CURRENCY, SCAN_INTERVAL_SECONDS,
                    SYSTEMIC_RISK_HALT_MINS)
from strategies.utils import realized_vol_hourly

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


# Safety stops that are scoped to one trading day and must expire with it.
# A manual pause (no reason, or "manual") is deliberately absent: only the
# operator can lift that one.
_SESSION_PAUSE_REASONS = ("daily_target", "daily_loss_limit", "drawdown")


def _advance_trading_day(chat_id: str, balance: float, settings: dict) -> tuple[dict, str]:
    """Roll the daily record forward and lift *session-scoped* safety pauses.

    Returns ``(day_state, resumed_reason)``. A non-empty ``resumed_reason`` means
    this call re-opened entries that a previous day's stop had closed.

    This must run on every cycle *before* the paused gate in ``_user_loop``.
    It used to live only inside the daily-target section, which sits after
    ``if settings.get("paused"): continue`` — so the moment the bot paused itself
    for a daily loss, a daily target, or drawdown, the code that was supposed to
    release that pause at the next trading day became unreachable. One bad day
    therefore stopped a live account permanently and silently, which is exactly
    the "no trades for two days" failure this now prevents.
    """
    today = _session_date()
    ds = _user_daily.get(chat_id)
    if not ds or ds.get("date") != today:
        ds = settings.get("daily_state", {}) or {}
    if ds.get("date") == today:
        try:
            start_balance = float(ds.get("start_balance") or 0.0)
        except (TypeError, ValueError):
            start_balance = 0.0
        if math.isfinite(start_balance) and start_balance > 0:
            _user_daily[chat_id] = ds
            return ds, ""

        # Recover legacy/corrupt same-day state written before /resume verified
        # that a live balance was available. A zero baseline makes the daily
        # loss budget exactly zero and pauses at PnL ₦+0. Rebase to the fresh
        # equity passed by _user_loop, but do not clear an existing pause; an
        # explicit /resume still owns lifting it.
        try:
            live_equity = float(balance)
        except (TypeError, ValueError):
            live_equity = 0.0
        if math.isfinite(live_equity) and live_equity > 0:
            ds = dict(ds)
            ds["start_balance"] = live_equity
            ds["target_hit"] = False
            ds.setdefault("pnl_baseline", 0.0)
            settings["daily_state"] = ds
            _user_daily[chat_id] = ds
            asyncio.create_task(asyncio.to_thread(database.update_settings, chat_id, settings))
            log.error(
                f"[{chat_id}] Repaired invalid same-day equity baseline to ₦{live_equity:,.2f}; "
                "pause state is unchanged"
            )
            return ds, ""
        _user_daily[chat_id] = ds
        return ds, ""

    old_target_hit = bool(ds.get("target_hit", False))
    previous_date = ds.get("date") or "the previous session"
    # `pnl_baseline` is what an explicit /resume excludes from the day's
    # target and loss-limit arithmetic. A fresh day has nothing to exclude.
    ds = {"date": today, "start_balance": balance, "target_hit": False,
          "pnl_baseline": 0.0}
    settings["daily_state"] = ds
    _user_daily[chat_id] = ds

    # The in-memory risk manager has its own drawdown flag that previously only
    # expired inside ``is_in_strict_mode()``, i.e. only when a trade was already
    # being placed — so a long-running process could keep blocking every
    # evaluation with no persisted reason and no way to see it. Expire it with
    # the same trading-day boundary the persisted stops use.
    risk = _user_risks.get(chat_id)
    if risk is not None:
        try:
            risk.last_reset_date = today
            if risk.paused and not settings.get("paused"):
                log.warning(
                    f"[{chat_id}] New trading day — expiring the in-memory drawdown pause "
                    "(settings are not paused); monitoring and limits are unchanged"
                )
                risk.paused = False
                risk.peak_balance = balance
                risk._dd_breach_since = 0.0
        except Exception as risk_err:  # never let cleanup break the loop
            log.error(f"[{chat_id}] Risk daily expiry failed: {risk_err}", exc_info=True)

    reason = str(settings.get("paused_reason") or "")
    resumed = ""
    if settings.get("paused") and (reason in _SESSION_PAUSE_REASONS or old_target_hit):
        settings["paused"] = False
        settings.pop("paused_reason", None)
        settings.pop("daily_loss_stopped_at", None)
        risk = _user_risks.get(chat_id)
        if risk:
            risk.paused = False
            risk.peak_balance = balance
            risk._dd_breach_since = 0.0
            risk.daily_realized_pnl = 0.0
            risk.last_reset_date = today
        resumed = reason or "daily_target"
        log.warning(
            f"[{chat_id}] TRADING DAY ROLLOVER — cleared the '{resumed}' pause set on "
            f"{previous_date}; entries are open again"
        )
    asyncio.create_task(asyncio.to_thread(database.update_settings, chat_id, settings))
    return ds, resumed


def _daily(chat_id: str, balance: float, settings: dict) -> dict:
    ds, _ = _advance_trading_day(chat_id, balance, settings)
    return ds


def reset_session_restrictions(
    chat_id: str, reason: str = "manual_resume", *, current_equity: float | None = None,
) -> dict:
    """Apply an explicit operator resume using a verified equity baseline.

    The day-loss stop, target, and drawdown stop all derive from the session
    baseline. A zero/uninitialised in-memory balance used to be accepted here,
    creating a zero-sized daily-loss budget; the next cycle then paused again
    at ``₦+0`` even though the account had funds. Callers should pass a freshly
    fetched equity when possible. The in-memory balance remains a fallback for
    direct/internal callers, but a missing or invalid balance now fails closed
    instead of persisting a broken baseline.

    Already-realised PnL is captured separately in ``pnl_baseline`` so it does
    not re-trigger the stop being overridden. This grants a fresh, bounded
    risk budget, not an unlimited-loss day. Existing positions and process-wide
    systemic halts are deliberately left untouched.
    """
    today = _session_date()
    user = database.get_user(chat_id, force_fresh=True)
    if not user:
        raise ValueError(f"Cannot resume unknown user {chat_id}")
    settings = dict(user.get("settings") or {})
    risk = _user_risks.get(chat_id)

    if current_equity is None and risk is not None:
        try:
            current_equity = float(risk.current_free_cash) + float(risk.deployed())
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError("Current account equity is unavailable; refusing to resume") from err
    try:
        equity = float(current_equity)
    except (TypeError, ValueError, OverflowError) as err:
        raise ValueError("Current account equity is unavailable; refusing to resume") from err
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("Current account equity is unavailable or zero; refusing to resume")

    booked_pnl = 0.0
    try:
        booked_pnl = float(
            database.get_daily_resolved_pnl(chat_id, today, config.TRADING_TIMEZONE)
        )
    except Exception as err:
        log.warning(f"[{chat_id}] session reset could not read today's realised PnL: {err}")

    day = {
        "date": today,
        "start_balance": equity,
        "target_hit": False,
        "pnl_baseline": booked_pnl,
    }
    settings["daily_state"] = day
    settings["paused"] = False
    settings.pop("paused_reason", None)
    settings.pop("daily_loss_stopped_at", None)
    settings["session_reset_at"] = datetime.now(timezone.utc).isoformat()
    settings["session_reset_reason"] = reason

    # A persisted strategy suspension removes strategies from the account's
    # scope before any market is evaluated (`no_enabled_strategies` when it
    # removes the last one), so an explicit resume must lift it. Nothing in
    # this codebase writes the key today; it is read from whatever an earlier
    # release stored in the user's settings, where it would otherwise block
    # trading forever with no Telegram command that clears it.
    cleared_suspensions: list[str] = []
    try:
        raw_learned = settings.get("learned")
        if isinstance(raw_learned, dict) and raw_learned.get("suspended_strategies"):
            learned = dict(raw_learned)
            cleared_suspensions = list(learned.pop("suspended_strategies") or [])
            settings["learned"] = learned
    except Exception as suspension_err:
        log.debug(f"[{chat_id}] suspension clear skipped: {suspension_err}")

    # Persist first. Do not tell Telegram that trading resumed (or clear the
    # in-memory pause) if the baseline failed to reach the database.
    try:
        database.update_settings(chat_id, settings)
        database.invalidate_user_cache(chat_id)
    except Exception as err:
        log.error(f"[{chat_id}] session reset could not be persisted: {err}", exc_info=True)
        raise RuntimeError("Could not persist the resume baseline") from err

    _user_daily[chat_id] = day
    if risk is not None:
        risk.paused = False
        risk.peak_balance = equity
        risk._dd_breach_since = 0.0
        risk.daily_realized_pnl = 0.0
        risk.daily_target = 0.0
        risk.last_reset_date = today

    cleared_cooldowns = 0
    try:
        stale_keys = [key for key in list(executor._trade_cooldown) if key and key[0] == chat_id]
        for key in stale_keys:
            executor._trade_cooldown.pop(key, None)
        cleared_cooldowns = len(stale_keys)
    except Exception as cooldown_err:
        log.debug(f"[{chat_id}] cooldown clear skipped: {cooldown_err}")

    stall.note_state(chat_id, paused=False, paused_reason="")
    global _active_users_cache_time
    _active_users_cache_time = 0.0

    log.warning(
        f"[{chat_id}] SESSION RESET ({reason}) — equity baseline ₦{equity:,.2f}, "
        f"today's PnL before the override ₦{booked_pnl:+,.2f} excluded, "
        f"{cleared_cooldowns} cooldown(s) cleared, "
        f"{len(cleared_suspensions)} suspension(s) cleared; stops now measure from now"
    )
    return {
        "equity": equity,
        "booked_pnl": booked_pnl,
        "cleared_cooldowns": cleared_cooldowns,
        "cleared_suspensions": cleared_suspensions,
    }


async def _roll_trading_day(chat_id: str, equity: float, settings: dict) -> None:
    """Advance the trading day and tell the operator if entries just re-opened."""
    _, resumed = _advance_trading_day(chat_id, equity, settings)
    if not resumed:
        return
    stall.reject(chat_id, "cycle", "auto_resumed_new_trading_day", f"cleared '{resumed}'")
    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    if app_to_use:
        await telegram_bot.send_message(
            app_to_use, chat_id,
            "🌅 *New trading day — entries re-opened*\n\n"
            f"The previous session was stopped by the *{resumed.replace('_', ' ')}* safety limit.\n"
            "Risk limits, scope and monitoring are unchanged; this is only the daily stop expiring.",
            parse_mode="Markdown",
        )


def _session_pnl_for_day(profit: float, day: dict) -> float:
    """Today's realised PnL measured from the session's own baseline.

    ``pnl_baseline`` is what an explicit /resume records (see
    ``reset_session_restrictions``): the result already booked when the
    operator overrode the stop. Subtracting it is what makes the override
    hold. Without a resume the baseline is ``0.0``, so this is the plain daily
    PnL the stops have always used.
    """
    try:
        baseline = float((day or {}).get("pnl_baseline") or 0.0)
    except (TypeError, ValueError):
        baseline = 0.0
    return float(profit) - baseline


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
    # Pre-warm TCP/TLS connection for sub-10ms order dispatch
    asyncio.create_task(client.prewarm())

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
            holdings = float(t.get("filled_quantity") or 0.0)
            restored_at = time.time()
            created = t.get("created_at")
            if created is not None:
                if getattr(created, "tzinfo", None) is None:
                    created = created.replace(tzinfo=timezone.utc)
                restored_at = created.timestamp()
            risk.add_position(key, {
                "market_id": mid,
                "trade_id": t["trade_id"], "event_id": t["event_id"],
                "order_id": t.get("order_id"),
                "outcome": t["outcome"], "outcome_id": t["outcome_id"],
                "entry_price": t["entry_price"], "amount_ngn": t["amount_ngn"],
                "filled_quantity": holdings,
                "strategy": t["strategy"], "asset": t["asset"],
                "timeframe": t["timeframe"],
                "confirmed_filled": holdings > 0,
                # `placed_at` drives every staleness/requote decision. Without
                # it, a restored position was stamped "now" on each restart, so
                # a dormant unfilled MAKER order kept a market locked forever:
                # /manage saw a fresh order, never cancelled it, and the risk
                # book held the market permanently. Age it from the ledger row.
                "placed_at": restored_at,
                "restored": True,
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
                            risk._dd_breach_since = 0.0
                            _last_balance[chat_id] = equity
                            # Auto-resume if the account was paused by drawdown.
                            # A fresh deposit resets the baseline, so the drawdown
                            # condition is no longer valid. The user shouldn't have
                            # to manually /resume after every deposit.
                            if settings.get("paused") and settings.get("paused_reason") == "drawdown":
                                risk.paused = False
                                settings["paused"] = False
                                settings["paused_reason"] = ""
                                await asyncio.to_thread(database.update_settings, chat_id, settings)
                                log.info(f"[{chat_id}] Auto-resumed after deposit (drawdown pause cleared)")
                            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                            if app_to_use:
                                resumed_note = "\n✅ *Trading auto-resumed* — drawdown pause cleared." if not settings.get("paused") else "\nSend /resume if trading was paused."
                                await telegram_bot.send_message(
                                    app_to_use, chat_id,
                                    f"💸 *Deposit detected* +₦{delta:,.0f}\n"
                                    f"New balance: ₦{equity:,.0f}\n"
                                    f"Drawdown baseline reset.{resumed_note}",
                                    parse_mode="Markdown",
                                )
                        else:
                            log.info(f"[{chat_id}] WITHDRAWAL detected ₦{delta:,.0f} | new balance ₦{equity:,.0f}")
                            # Adjust peak_balance to prevent false drawdown pauses on withdrawals.
                            # Again, do NOT touch start_balance — daily target stays fixed.
                            risk.peak_balance = max(0.0, risk.peak_balance + delta)
                            _last_balance[chat_id] = equity
                            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                            if app_to_use:
                                await telegram_bot.send_message(
                                    app_to_use, chat_id,
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
            await _manage_unfilled_maker_orders(chat_id, client, risk, settings)
        except Exception as maker_err:
            log.error(f"[{chat_id}] Maker order management error: {maker_err}", exc_info=True)

        try:
            await _evaluate_and_exit_positions(chat_id, client, risk, settings)
        except Exception as exit_err:
            log.error(f"[{chat_id}] Position exit eval error: {exit_err}", exc_info=True)

        # ── Trading-day rollover (BEFORE the paused gate, on purpose) ───────
        # A safety pause that is scoped to one trading day has to be released by
        # the day changing, and that release cannot hide behind the paused
        # check — otherwise a paused account never reaches the code that lifts
        # the pause and stays dark forever. Runs on every cycle; cheap and
        # idempotent inside a trading day.
        try:
            await _roll_trading_day(chat_id, equity, settings)
        except Exception as roll_err:
            log.error(f"[{chat_id}] Trading-day rollover failed: {roll_err}", exc_info=True)

        stall.note_state(
            chat_id,
            paused=bool(settings.get("paused")),
            paused_reason=str(settings.get("paused_reason") or ""),
            dry_run=not config.LIVE_TRADING,
        )

        # ── Drawdown auto-recovery guard ─────────────────────────────────────
        # If the account was paused due to drawdown, but equity has recovered
        # (e.g. resting maker orders cancelled, transient balance drop cleared,
        # or deposit made), automatically clear the pause so the bot isn't
        # permanently stuck dead.
        if settings.get("paused") and settings.get("paused_reason") == "drawdown":
            peak = risk.peak_balance
            if peak > 0:
                current_dd = (peak - equity) / peak
                # If drawdown is now safe (< 50% of MAX_DRAWDOWN_STOP, i.e. < 5%)
                if current_dd < config.MAX_DRAWDOWN_STOP * 0.5:
                    log.info(
                        f"[{chat_id}] DRAWDOWN RECOVERED: dd={current_dd:.1%} "
                        f"(equity=₦{equity:,.0f}, peak=₦{peak:,.0f}) — auto-resuming trading"
                    )
                    settings["paused"] = False
                    settings["paused_reason"] = ""
                    risk.paused = False
                    risk._dd_breach_since = 0.0
                    await asyncio.to_thread(database.update_settings, chat_id, settings)
                    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                    if app_to_use:
                        try:
                            await telegram_bot.send_message(
                                app_to_use, chat_id,
                                f"✅ *Trading Auto-Resumed*\n"
                                f"Equity recovered to ₦{equity:,.0f} (drawdown {current_dd:.1%}).\n"
                                f"Drawdown pause cleared.",
                                parse_mode="Markdown",
                            )
                        except Exception as ne:
                            log.debug(f"Auto-resume notification failed: {ne}")

        # ── Paused check ───────────────────────────────────────────────────
        if settings.get("paused"):
            if iter_count % 6 == 0:   # log every 3 minutes when paused
                log.info(
                    f"[{chat_id}] PAUSED ({settings.get('paused_reason') or 'manual'}) "
                    "— skipping evaluation"
                )
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
        # Everything already realised before an explicit /resume belongs to a
        # baseline the operator has overridden (see
        # `reset_session_restrictions`). Both the loss stop and the target are
        # therefore measured on PnL *since the session reset*, which is what
        # makes the override hold instead of being recomputed away one cycle
        # later. It is 0.0 on a normal day, so this is a no-op without a
        # manual resume.
        session_profit = _session_pnl_for_day(profit, day)
        target = _daily_target(settings, day["start_balance"])

        # Sync ground-truth values onto risk so is_in_strict_mode() actually
        # works. risk.daily_target was never assigned anywhere before this —
        # it stayed at its 0.0 default permanently, silently disabling the
        # "tighten up near daily target" safety check with no error at all.
        risk.daily_target       = target
        # The in-memory risk manager and the persisted stops must measure the
        # same quantity, or `risk.target_hit`/`is_in_strict_mode` disagree with
        # the messages the user receives.
        risk.daily_realized_pnl = session_profit
        risk.last_reset_date    = session_date

        daily_loss_pct = min(
            max(float(settings.get("daily_loss_limit_pct", config.DEFAULT_DAILY_LOSS_LIMIT_PCT)), 0.1),
            config.MAX_DAILY_LOSS_LIMIT_PCT,
        )
        daily_loss_limit = day["start_balance"] * daily_loss_pct / 100.0
        if session_profit <= -daily_loss_limit:
            settings["paused"] = True
            settings["paused_reason"] = "daily_loss_limit"
            risk.paused = True
            await asyncio.to_thread(database.update_settings, chat_id, settings)
            log.warning(
                f"[{chat_id}] DAILY LOSS STOP ₦{session_profit:+,.0f} <= "
                f"-₦{daily_loss_limit:,.0f} (baseline ₦{day['start_balance']:,.0f})"
            )
            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
            if app_to_use:
                await telegram_bot.send_message(
                    app_to_use, chat_id,
                    f"🛑 *Daily loss limit reached* — ₦{session_profit:+,.0f}. "
                    "New entries are paused; open positions remain monitored.\n"
                    "/resume restarts the session from the current balance.",
                    parse_mode="Markdown",
                )
            continue

        if target > 0 and session_profit >= target and not day["target_hit"]:
            day["target_hit"] = True
            settings["daily_state"] = day
            settings["paused"]       = True
            settings["paused_reason"] = "daily_target"
            await asyncio.to_thread(database.update_settings, chat_id, settings)
            log.info(f"[{chat_id}] DAILY TARGET HIT ₦{session_profit:+,.0f} — trading paused")
            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
            if app_to_use:
                await telegram_bot.send_message(
                    app_to_use, chat_id,
                    f"🎯 *Daily target reached!* ₦{session_profit:+,.0f}\n/resume to override.",
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
            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
            if app_to_use:
                await telegram_bot.notify_drawdown(app_to_use, chat_id, equity, risk.peak_balance, dd)
            continue

        # ── Systemic halt ──────────────────────────────────────────────────
        alert = strategy.check_systemic_risk()
        if alert:
            if not _systemic_alert.get(chat_id):
                _systemic_alert[chat_id] = True
                log.warning(f"[{chat_id}] SYSTEMIC HALT — {alert}")
                app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                if app_to_use:
                    await telegram_bot.send_message(
                        app_to_use, chat_id,
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


async def _resolve_unfilled_position(chat_id: str, risk, pos: dict, position_key: str,
                                     reason: str, *, notify: bool = True) -> None:
    """Close out an order that never filled: free the capital, settle the trade
    row at 0.0 PnL, and tell the user.

    An unfilled order is not a loss, but it must never be silent. Maker quotes
    resolving without a single Telegram message is the exact failure this path
    exists to prevent: the capital came back, the position left the risk book,
    and the operator had no way to know either happened.
    """
    trade_id = pos.get("trade_id")
    if trade_id:
        try:
            await asyncio.to_thread(database.resolve_trade, trade_id, None, 0.0)
        except Exception as db_err:
            log.error(f"[{chat_id}] Could not settle unfilled trade {trade_id} ({reason}): {db_err}")
    risk.remove_position(position_key)
    log.info(
        f"[{chat_id}] UNFILLED {pos.get('strategy', 'MAKER')} {pos.get('asset', '?')} "
        f"order={pos.get('order_id')} — {reason}; capital freed, trade settled at ₦0"
    )
    if not notify:
        return
    app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
    if app_to_use:
        try:
            await telegram_bot.notify_unfilled(
                app_to_use, chat_id, pos.get("strategy", "MAKER"),
                pos.get("asset", "?"), pos.get("timeframe", ""),
                pos.get("outcome", ""), pos.get("amount_ngn", 0),
            )
        except Exception as ne:
            log.warning(f"[{chat_id}] notify_unfilled ({reason}) failed: {ne}")


async def _manage_unfilled_maker_orders(chat_id: str, client, risk, settings: dict):
    """
    Lifecycle management for resting two-sided quotes.

    For every resting MAKER leg:
      * filled            -> update the DB and the risk book, and skew the
                             next quote toward completing the set;
      * both legs filled  -> burn the complete set and realise the lock;
      * cancelled/expired -> settle the row at zero and free the reservation;
      * sibling filled    -> keep resting while the pair still locks and the
                             bid is still worth owning (`_completion_leg_verdict`);
      * stale or the oracle moved -> withdraw BOTH legs and re-quote.

    The rule that runs through all of it: **legs are withdrawn together.**
    Cancelling one leg of a two-sided quote and leaving the other resting is
    how a market maker acquires an unintended position -- the survivor is now
    a one-sided bet nobody is hedging. The one order that is not a "survivor"
    in that sense is the other half of an already-filled pair: it is the
    completion of a set, and it is exempt from the quote clock for as long as
    it still locks and is still worth owning.

    A cancelled-but-unconfirmable order is reported as still resting rather
    than dropped, so an operator is never told the book is clean when it is
    not.
    """
    if not risk.open_positions:
        return

    from strategies import book as booklib
    from strategies.maker import maker_strategy

    for position_key, pos in list(risk.open_positions.items()):
        if str(pos.get("strategy") or "").upper() != "MAKER":
            continue
        if pos.get("confirmed_filled"):
            continue
        order_id = pos.get("order_id")
        if not order_id:
            continue

        market_id = pos.get("market_id", "")
        market = next((m for m in active_markets if m["market_id"] == market_id), None)
        secs = market.get("secs_to_close", 0) if market else 0

        try:
            order_data = await client.get_order(order_id)
            status = str(order_data.get("status") or "").lower()
            shares = client.parse_filled_shares(order_data)

            if status in ("filled", "completed") or shares > 0:
                fill_price = float(
                    order_data.get("avgFillPrice")
                    or order_data.get("price")
                    or pos.get("entry_price", 0.5)
                )
                # Makers pay no fee on Bayse CLOB. The old code added the
                # order's `fee` field to the cost of a maker fill, which
                # understated every maker profit by the taker fee -- the one
                # economic advantage this leg exists to capture.
                confirmed_cost = shares * fill_price * config.CURRENCY_BASE_MULTIPLIER

                pos["confirmed_filled"] = True
                pos["filled_quantity"] = shares
                pos["entry_price"] = fill_price
                pos["amount_ngn"] = confirmed_cost
                trade_id = pos.get("trade_id")
                if trade_id:
                    await asyncio.to_thread(
                        database.update_trade_fill,
                        trade_id, confirmed_cost, shares, fill_price,
                    )
                    stall.note_trade(chat_id, market_id=market_id)
                risk.current_free_cash -= confirmed_cost

                # Skew the next quote toward completing this set: a fill on one
                # leg makes the opposite leg more valuable, not less.
                maker_strategy.record_fill(market_id, pos.get("outcome", ""), shares)
                log.info(
                    f"[{chat_id}] MAKER FILL | {pos.get('asset')} {pos.get('outcome')} "
                    f"{shares:.2f}sh @ {fill_price:.3f} ₦{confirmed_cost:,.0f} "
                    f"(fee-free) | order={order_id}"
                )
                app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                if app_to_use:
                    try:
                        await telegram_bot.notify_fill(
                            app_to_use, chat_id, pos, shares, fill_price
                        )
                    except Exception:
                        pass

                # Both legs filled -> the set is complete. Burn it and take the
                # locked profit rather than carrying it to settlement.
                paired = _paired_leg(risk, position_key, market_id)
                if paired is not None:
                    await _burn_complete_set(
                        chat_id, client, risk, pos, position_key, market_id=market_id
                    )
                continue

            if status in ("cancelled", "canceled", "expired", "rejected", "killed"):
                await _resolve_unfilled_position(
                    chat_id, risk, pos, position_key,
                    f"exchange reported the order as {status}",
                )
                risk.remove_position(position_key)
                maker_strategy.open_quotes.pop(market_id, None)
                continue

            # ── Completion leg: the other half of this pair has filled ─────
            # The survivor is not a standing quote any more, it is the order
            # that completes a set -- and one fill away from a locked profit
            # is exactly what this strategy exists to buy. It is therefore not
            # subject to the standing-quote clock or to the oracle-move
            # requote: withdrawing it here is what turned half a pair into an
            # unhedged directional position that nobody asked for. It keeps
            # resting while it is still worth owning and the pair still locks.
            sibling = _filled_sibling(risk, pos, market_id)
            if sibling is not None:
                keep, why = _completion_leg_verdict(pos, sibling, market)
                if keep:
                    fillable, detail = await _completion_leg_can_fill(client, pos)
                    if fillable:
                        log.debug(
                            f"[{chat_id}] MAKER completion leg "
                            f"{pos.get('outcome')} on {market_id} stays: {why}"
                        )
                        continue
                    # Worth owning, but the book has left it behind: it cannot
                    # fill where it is. Withdraw it so the next pass re-quotes
                    # it at the skewed price a completion is priced at.
                    await _withdraw_resting_quote(
                        chat_id, client, risk, pos, position_key,
                        reason=f"completion leg buried, re-quoting: {detail}",
                    )
                    continue
                await _withdraw_resting_quote(
                    chat_id, client, risk, pos, position_key,
                    reason=f"completion leg no longer worth holding: {why}",
                )
                continue

            # Still resting. Withdraw both legs when the quote is stale, when
            # the oracle has moved through it, or when settlement is close.
            quote = maker_strategy.open_quotes.get(market_id) or {}
            spot = feeds.spot.get(pos.get("asset", "")) or 0.0
            oracle_moved = bool(
                spot and quote.get("spot")
                and abs(spot - quote["spot"]) / quote["spot"]
                > config.MAKER_REQUOTE_THRESHOLD
            )
            too_old = (
                time.time() - float(quote.get("placed_at", 0))
                > config.MAKER_ORDER_TIMEOUT
            )
            near_close = 0 < secs < config.MAKER_MIN_SECS_TO_CLOSE

            if oracle_moved or too_old or near_close:
                reason = (
                    "oracle moved through the quote" if oracle_moved
                    else "quote timed out" if too_old
                    else "too close to settlement"
                )
                await _withdraw_resting_quote(
                    chat_id, client, risk, pos, position_key, reason=reason
                )

        except Exception as e:
            log.error(f"[{chat_id}] Maker order management error on {order_id}: {e}")


def _filled_sibling(risk, pos: dict, market_id: str):
    """The opposite-outcome leg of this market that has already filled.

    Returns the sibling position dict or None. Used to recognise the half-built
    pair: a quote whose other leg is now a confirmed position is no longer a
    quote, and must be judged as a completion order instead of expiring on the
    standing-quote clock.
    """
    outcome = str(pos.get("outcome") or "").upper()
    for key, other in risk.open_positions.items():
        if other is pos or not isinstance(other, dict):
            continue
        if str(other.get("market_id") or key) != str(market_id):
            continue
        if str(other.get("strategy") or "").upper() not in config.MAKER_STRATEGIES:
            continue
        if str(other.get("outcome") or "").upper() == outcome:
            continue
        try:
            filled = float(other.get("filled_quantity") or 0.0) > 0.0
        except (TypeError, ValueError):
            filled = False
        if not (other.get("confirmed_filled") or filled):
            continue
        return other
    return None


async def _completion_leg_can_fill(client, pos: dict):
    """Is the completing bid still placed where it can fill?

    A completion bid that the book has left behind cannot do the one job it
    exists for: every seller hits the bids above it first. Holding it until the
    candle ends is not patience, it is the same cancelled pair with a later
    timestamp. It is withdrawn instead, so the next pass re-quotes the side at
    the skewed price the strategy prices a completion at.

    Fail *open* (keep the order) when the book cannot be read or is stale: a
    missing price is not evidence that the order is buried, and churn on bad
    data is worse than a bid that may still be first in line.
    """
    from strategies import book as booklib

    outcome_id = pos.get("outcome_id")
    try:
        bid = float(pos.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        return True, "unreadable bid"
    if not outcome_id or bid <= 0:
        return True, "nothing to check"
    try:
        book = await asyncio.wait_for(
            client.get_orderbook(outcome_id, depth=5), timeout=1.5
        )
    except Exception as exc:
        return True, f"book unavailable ({exc})"
    if booklib.book_is_stale(book):
        return True, "book stale"
    price, code, detail = booklib.passive_bid_price(book, bid)
    if price is None and code == "behind_book":
        # `would_cross_book` is deliberately NOT a requote: the ask has come to
        # our bid, which means it is about to fill rather than unable to.
        return False, detail
    return True, detail or "still competitive"


def _completion_leg_verdict(pos: dict, sibling: dict, market: dict | None):
    """Should the surviving leg of a half-filled pair keep resting?

    It should while both of its reasons to exist still hold:

      * the pair still locks against the price the sibling *actually* filled
        at -- ``bid + sibling_fill <= 1 - MAKER_PAIR_MIN_EDGE`` -- because a
        completion fill that costs more than the set pays is not a completion;
      * the bid is still worth owning on its own, by the same edge rule that
        priced it, against a fresh fair value.

    Fails *closed* on missing information: with no market, no fresh oracle or
    no fair value there is nothing to re-judge the bid with, and keeping a
    priced-with-edge bid is the smaller error than cancelling the one order
    that stands between an open position and a locked profit.
    """
    from strategies.model import fair_value_pair

    try:
        bid = float(pos.get("entry_price") or 0.0)
        sibling_price = float(sibling.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        return False, "unreadable bid prices"
    if bid <= 0.0 or sibling_price <= 0.0:
        return False, "no recorded price to judge the pair"

    lock = 1.0 - (bid + sibling_price)
    if lock < config.MAKER_PAIR_MIN_EDGE - 1e-9:
        return False, (
            f"pair no longer locks: bid {bid:.3f} + filled {sibling_price:.3f} "
            f"= {bid + sibling_price:.3f} (needs <= "
            f"{1.0 - config.MAKER_PAIR_MIN_EDGE:.3f})"
        )

    if not market:
        return True, "market rotated out; holding the completion bid"
    asset = str(pos.get("asset") or "")
    spot = None
    direct_price, direct_time = feeds_direct.get_direct_price(asset)
    if direct_price and time.time() - direct_time <= config.FEED_STALE_SEC:
        spot = direct_price
    elif time.time() - feeds.spot_updated_at.get(asset, 0.0) <= config.FEED_STALE_SEC:
        spot = feeds.spot.get(asset)
    if not spot:
        return True, "no fresh oracle to re-judge the bid; holding it"

    synthetic = dict(market)
    synthetic["asset"] = asset
    synthetic["threshold"] = market.get("threshold") or pos.get("threshold")
    fv_pair = fair_value_pair(asset, synthetic, strategy.global_state, spot)
    if fv_pair is None:
        return True, "no fair value available; holding the completion bid"
    fv = fv_pair[0] if str(pos.get("outcome") or "").upper() == "YES" else fv_pair[1]

    # The same skew that priced the quote has to be applied here, or the
    # verdict is stricter than the rule that admitted the order: a completing
    # leg is deliberately allowed a thinner edge than a fresh one, and
    # re-judging it without that allowance would cancel exactly the order the
    # skew was created to keep.
    try:
        held = float(sibling.get("filled_quantity") or 0.0)
    except (TypeError, ValueError):
        held = 0.0
    net = held if str(sibling.get("outcome") or "").upper() == "YES" else -held
    ticks = max(-config.MAKER_MAX_SKEW_TICKS,
                min(config.MAKER_MAX_SKEW_TICKS, net))
    leg_skew = ticks * config.MAKER_TICK
    if str(pos.get("outcome") or "").upper() != "YES":
        leg_skew = -leg_skew

    from strategies.maker import maker_strategy
    return maker_strategy.leg_still_worth_owning(
        fair_value=fv, bid=bid, skew=leg_skew
    )


def _paired_leg(risk, position_key: str, market_id: str):
    """The sibling leg of a complete set, if it has also filled.

    The property that matters is not the strategy name: two *opposite* outcomes
    of the same market, both held, together pay 1.00 whichever resolves. That
    is a MAKER pair resting under the book and it is equally a complete-set
    TAKER, which is two immediate FAK fills and never passes through the maker
    fill path. Requiring ``strategy == "MAKER"`` here is what left a taker set
    to be managed -- and sold -- as two independent directional bets.

    Quantities must match: ``_burn_complete_set`` burns ``min(shares)`` and
    resolves both rows, so a partial-fill imbalance would otherwise leave the
    excess shares untracked. Only exchange/float precision noise is tolerated;
    a real mismatch is left alone and both fills remain in the risk book.
    """
    this = risk.open_positions.get(position_key)
    if not this or not this.get("confirmed_filled"):
        return None
    this_qty = float(this.get("filled_quantity") or this.get("shares") or 0.0)
    if this_qty <= 0:
        return None
    family = str(this.get("strategy") or "").upper()
    for key, other in risk.open_positions.items():
        if key == position_key:
            continue
        if (other.get("market_id") or key) != market_id:
            continue
        if str(other.get("strategy") or "").upper() != family:
            continue
        if not other.get("confirmed_filled"):
            continue
        if str(other.get("outcome", "")).upper() == str(this.get("outcome", "")).upper():
            continue
        other_qty = float(other.get("filled_quantity") or other.get("shares") or 0.0)
        if other_qty <= 0:
            continue
        if not share_quantities_match(other_qty, this_qty):
            log.warning(
                f"[{position_key}] complete set on {market_id} is unbalanced "
                f"({this_qty:.2f} vs {other_qty:.2f} shares) — not burning it; "
                f"burns settle the overlap and would leave the excess untracked"
            )
            continue
        return key, other
    return None



def _exit_plan(market_id, position_key, pos, market, *, w_est, current_price,
               ev_hold, exit_reason):
    """One queued exit decision."""
    return {
        "market_id":     market_id,
        "position_key":  position_key,
        "pos":           pos,
        "market":        market,
        "w_est":         w_est,
        "current_price": current_price,
        "ev_hold":       ev_hold,
        "exit_reason":   exit_reason,
    }


async def _burn_complete_set(
    chat_id: str, client, risk, pos: dict, position_key: str, *, market_id: str
) -> bool:
    """Burn a complete set and book the lock.

    A complete set always settles to exactly 1.00, so holding it is a bond and
    selling it is a mistake: a sale pays the best bid and a taker fee to
    receive something less than the unit the set is worth. Burning pays the
    full unit with no fee. This is the moment the maker's edge becomes cash.

    Cost basis is the sum of what we paid for both legs -- the set is one
    asset assembled from two, and its profit is the difference.
    """
    paired = _paired_leg(risk, position_key, market_id)
    if not paired:
        # The sibling has not filled (or no longer exists). Half a set is not a
        # set; leave it for the next pass, when it either pairs or is managed
        # as a directional position.
        log.debug(
            f"[{chat_id}] complete set on {market_id} not yet paired — "
            f"no sibling leg has filled"
        )
        return False

    other_key, other = paired
    qty = min(
        float(pos.get("filled_quantity") or pos.get("shares") or 0.0),
        float(other.get("filled_quantity") or other.get("shares") or 0.0),
    )
    if qty <= 0:
        return False

    try:
        resp = await client.burn_shares(market_id, qty, config.CURRENCY)
    except Exception as exc:
        # Not burning is not losing: the set still settles to 1.00. Say so and
        # let the next pass try again rather than treating it as a position to
        # be stopped out of.
        log.error(
            f"[{chat_id}] burn failed on {market_id}: {exc} — "
            f"the set still settles to 1.00; leaving it to resolve"
        )
        return False

    proceeds = float(
        resp.get("amount")
        or resp.get("proceeds")
        or resp.get("payout")
        or (qty * 1.0 * config.CURRENCY_BASE_MULTIPLIER)
    )
    cost = sum(
        float(p.get("amount_ngn") or 0.0)
        for p in (pos, other)
    )
    pnl = proceeds - cost
    risk.current_free_cash += proceeds
    risk.add_pnl(pnl)

    trade_ids = [
        (risk.open_positions.get(k) or {}).get("trade_id")
        for k in (position_key, other_key)
    ]
    for k in (position_key, other_key):
        risk.remove_position(k)
    try:
        from strategies import maker as maker_mod
        maker_mod.maker_strategy.open_quotes.pop(market_id, None)
        # The set is closed: there is no longer an inventory to complete.
        maker_mod.maker_strategy.clear_inventory(market_id)
    except Exception:
        pass

    for tid in trade_ids:
        if tid:
            try:
                await asyncio.to_thread(
                    database.resolve_trade, tid, True, pnl / 2.0
                )
            except Exception as db_err:
                log.error(f"[{chat_id}] burn reconciliation failed: {db_err}")

    log.info(
        f"[{chat_id}] COMPLETE SET BURNED | {pos.get('asset', '?')} "
        f"{pos.get('timeframe', '?')} {qty:.2f} sets | "
        f"cost ₦{cost:,.0f} → ₦{proceeds:,.0f} | PnL ₦{pnl:+,.0f}"
    )
    app_to_use = _tg_app if "_tg_app" in globals() else None
    if app_to_use:
        try:
            await telegram_bot.notify_set_burned(
                app_to_use, chat_id, pos.get("asset", "?"),
                pos.get("timeframe", "?"), qty, cost, proceeds, pnl,
            )
        except Exception:
            pass
    return True


async def _withdraw_resting_quote(
    chat_id: str, client, risk, pos: dict, position_key: str, *, reason: str
) -> bool:
    """Cancel both legs of a resting two-sided quote and settle their rows.

    Cancelling one leg and leaving the other resting is how a market maker
    acquires a position it did not intend to hold: the surviving leg fills
    against whoever was happy to trade with it, and we are long a thesis we
    priced as a spread. Both legs go, or neither does -- if the second cancel
    fails, the first leg's removal is the smaller of two errors, since a lone
    resting bid is exactly the exposure this function exists to prevent.
    """
    market_id = pos.get("market_id") or position_key
    sibling = None
    for key, other in risk.open_positions.items():
        if key == position_key:
            continue
        if (other.get("market_id") or key) != market_id:
            continue
        if str(other.get("strategy") or "").upper() != "MAKER":
            continue
        if other.get("confirmed_filled"):
            continue
        sibling = (key, other)
        break

    victims = [(position_key, pos)] + ([sibling] if sibling else [])

    for key, p in victims:
        order_id = p.get("order_id")
        if order_id:
            try:
                await client.cancel_order(order_id)
            except Exception as exc:
                log.debug(
                    f"[{chat_id}] cancel of resting leg {order_id} "
                    f"({p.get('outcome')}) failed: {exc}"
                )
            # Confirm before forgetting, and require a terminal answer. Two
            # things can go wrong here and both are worse than a stale row:
            # a cancel and a fill can cross in flight (we would drop shares
            # the risk book never knew about), and our cancel may simply not
            # have taken effect yet (we would stop managing an order the
            # exchange still considers live).
            try:
                state = await client.get_order(order_id)
                status = str(state.get("status") or "").lower()
                filled = client.parse_filled_shares(state)
                if filled > 0 or status in ("filled", "completed"):
                    log.warning(
                        f"[{chat_id}] resting leg {order_id} ({p.get('outcome')}) "
                        f"filled in the cancel race — keeping it as a position"
                    )
                    p["confirmed_filled"] = True
                    p["filled_quantity"] = filled
                    p["entry_price"] = float(
                        state.get("avgFillPrice")
                        or state.get("price")
                        or p.get("entry_price") or 0.0
                    )
                    continue
                if status not in ("cancelled", "canceled", "expired",
                                  "rejected", "killed"):
                    log.warning(
                        f"[{chat_id}] cancel of resting leg {order_id} "
                        f"({p.get('outcome')}) not confirmed — the exchange still "
                        f"reports it as '{status or 'unknown'}'; keeping it tracked"
                    )
                    # Say so once. An order we asked to cancel and cannot
                    # confirm is the one state an operator most needs to see:
                    # it may still fill, and nothing downstream will mention
                    # it again.
                    if not p.get("unfilled_alerted"):
                        p["unfilled_alerted"] = True
                        p["pending_cancel"] = True
                        app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
                        if app_to_use:
                            try:
                                # Not notify_unfilled: that message says the
                                # money came back. Here it may not have — the
                                # order can still fill.
                                await telegram_bot.notify_order_resting(
                                    app_to_use, chat_id, p.get("strategy", "MAKER"),
                                    p.get("asset", "?"), p.get("timeframe", ""),
                                    p.get("outcome", ""), p.get("amount_ngn", 0),
                                    price=float(p.get("entry_price") or 0.0),
                                    reason="cancel not confirmed by the exchange",
                                )
                            except Exception as ne:
                                log.warning(f"[{chat_id}] notify_order_resting failed: {ne}")
                    continue
            except Exception as exc:
                log.debug(f"[{chat_id}] post-cancel check on {order_id}: {exc}")
                continue

        trade_id = p.get("trade_id")
        if trade_id:
            try:
                await asyncio.to_thread(database.resolve_trade, trade_id, None, 0.0)
            except Exception as db_err:
                log.error(f"[{chat_id}] resting-quote settle failed: {db_err}")
        risk.remove_position(key)

    try:
        from strategies import maker as maker_mod
        maker_mod.maker_strategy.open_quotes.pop(market_id, None)
    except Exception:
        pass

    log.info(
        f"[{chat_id}] MAKER QUOTE WITHDRAWN | {market_id} | {reason} | "
        f"legs cancelled: {[p.get('outcome') for _, p in victims]}"
    )
    return True



def _exit_decision(
    *,
    outcome: str,
    w_est: float,
    bid: float,
    peak_price: float,
    entry_price: float,
    secs: float,
    fee_rate: float,
    is_maker_pos: bool,
    confirmed_filled: bool,
    complete_set: bool = False,
    completion_leg: bool = False,
) -> dict | None:
    """Decide whether one position should be exited, and why.

    Pure: no exchange, no database, no risk book. Everything the policy needs
    is in the arguments, which is the point -- this is the single most
    money-relevant decision the bot makes, and it can now be tested on a grid
    of prices and probabilities instead of through a mock exchange client.

    The comparison underneath every rule:

      * held to resolution, a share is worth ``w_est`` (a win pays 1.00);
      * sold now, it is worth ``bid`` less the taker fee on the exit.

    Returns ``{"reason", "current_price", "ev_hold"}`` or None to hold.
    """
    from strategies import book as booklib

    # A complete set pays 1.00 whichever outcome resolves. There is no thesis
    # to invalidate and nothing for a stop to protect: the only correct action
    # is to burn the set and realise the lock. `complete_set` says the caller
    # found the opposite-outcome sibling already filled in the risk book --
    # that is how a two-leg set is recognised at exit time, since its legs are
    # stored as ordinary YES/NO positions.
    if str(outcome).upper() == "BOTH" or complete_set:
        return {"reason": "BURN_COMPLETE_SET", "current_price": 1.0, "ev_hold": 0.0}

    current_price = float(bid)

    # Cost basis per share. A taker paid the fee inside the fill (it came out
    # of the shares received); a maker paid none at all.
    entry_cost_per_share = (
        entry_price if is_maker_pos
        else booklib.effective_buy_price(entry_price, fee_rate, is_maker=False)
    )
    basis = max(entry_cost_per_share, 1e-6)

    # Value per share if we sell now, net of the taker fee on the exit.
    exit_value_per_share = booklib.effective_sell_proceeds(
        current_price, 1.0, fee_rate, is_maker=False
    )
    # Value per share if we hold to resolution: a win pays 1.00.
    hold_value_per_share = float(w_est)

    # A resting maker quote still unfilled near close is not a position we
    # want to acquire: withdraw it rather than let it fill into settlement.
    #
    # The completion leg of a half-filled pair is the exception, and it is a
    # real one: that order does not *acquire* a position, it finishes building
    # a set, and it only keeps resting while `bid + sibling_fill` is below one.
    # Cancelling it near the close takes the one fill that would have locked a
    # profit and leaves the sibling as an unhedged directional bet.
    if (is_maker_pos and not confirmed_filled and not completion_leg
            and secs < config.MAKER_LATE_CANCEL_SECS):
        return {"reason": "CANCEL_RESTING", "current_price": current_price,
                "ev_hold": 0.0}

    # ── 1. TAKE PROFIT: the market is paying more than it is worth ───────
    premium = config.EXIT_TAKE_PROFIT_PREMIUM * basis
    # Selling must also clear our cost basis. Without this the rule fires on
    # any position where the bid merely exceeds a depressed model estimate --
    # realising a loss while logging it as profit taking, which is the one
    # thing a profit rule must never do.
    if (exit_value_per_share > hold_value_per_share + premium
            and exit_value_per_share > entry_cost_per_share
            and secs >= config.EXIT_TAKE_PROFIT_MIN_SECS):
        return {"reason": "TAKE_PROFIT", "current_price": current_price,
                "ev_hold": (exit_value_per_share - entry_cost_per_share) / basis}

    # Trailing protection: a gain that has started to evaporate is still a
    # gain. This is the one price-based rule, and it only ever sells into
    # profit -- a reversal below entry is the stop's job, not this one's.
    dropped_from_peak = (
        (peak_price - current_price) / peak_price if peak_price > 0 else 0.0
    )
    peak_gain = (peak_price - entry_cost_per_share) / basis
    if (peak_gain >= config.EXIT_TAKE_PROFIT_PREMIUM
            and dropped_from_peak >= config.EXIT_TRAILING_DROP
            and current_price >= entry_cost_per_share):
        return {"reason": "REVERSAL_EXIT", "current_price": current_price,
                "ev_hold": (exit_value_per_share - entry_cost_per_share) / basis}

    # ── 2. STOP: the thesis is worth materially less than we paid ────────
    # Not a P&L stop. A stop that triggers on P&L alone sells precisely when a
    # binary is cheapest and its expected value is unchanged -- it converts
    # recoverable variance into a realised loss. Selling is correct when the
    # estimate moved against us, and that is the only time this fires.
    thesis_broken = hold_value_per_share < entry_cost_per_share * (
        1.0 - config.EXIT_STOP_DRAWDOWN
    )
    # Hard backstop. The model is not the only thing that can go wrong, and a
    # catastrophic move should not need the model's permission to exit. The
    # salvage floor stops us paying a fee to sell something for nothing.
    hard_stop = (
        current_price <= entry_cost_per_share * (1.0 - config.EXIT_HARD_STOP_LOSS_PCT)
        and current_price >= config.EXIT_MIN_SALVAGE_PRICE
    )
    if thesis_broken or hard_stop:
        return {"reason": "STOP_LOSS", "current_price": current_price,
                "ev_hold": hold_value_per_share - entry_cost_per_share}

    return None


async def _evaluate_and_exit_positions(chat_id: str, client, risk, settings: dict):
    """
    Priced exit policy.

    A binary held to resolution is worth ``fv`` per share -- the model's
    estimate of ``P(this outcome wins)``, since a winning share pays 1.00.
    A binary sold now is worth ``bid * (1 - taker fee)`` per share. Every exit
    decision is a comparison of those two numbers, and nothing else enters it:

      TAKE PROFIT  the market is offering more than the position is worth,
                   by enough to compensate for the option value of holding
                   and for the model being wrong.
      STOP         the model's estimate has fallen below what we paid.

    Neither rule is a fixed percentage stop. A stop that triggers on P&L alone
    sells precisely when a binary is cheapest and its expected value is
    unchanged -- it converts recoverable variance into a realised loss. A stop
    that triggers on the estimate sells when we were wrong, which is the only
    time selling is correct.

    A hard price backstop remains, because a model is not the only thing that
    can go wrong and a catastrophic move should not need the model's
    permission to exit.
    """
    if not risk.open_positions:
        return

    from strategies import book as booklib
    from strategies.model import fair_value

    # One book round-trip for every held outcome rather than one per position:
    # the per-position loop runs every 5s and a call per position would rival
    # the scan itself in request volume.
    outcome_ids = sorted({
        pos.get("outcome_id")
        for pos in risk.open_positions.values()
        if pos.get("outcome_id") and not pos.get("awaiting_settlement")
    })
    books: dict[str, dict] = {}
    if outcome_ids:
        try:
            books = await asyncio.wait_for(
                client.get_orderbooks(outcome_ids, depth=5), timeout=3.0
            )
        except Exception as exc:
            log.debug(f"[{chat_id}] exit-eval book fetch failed: {exc}")

    positions_to_exit = []
    stale_positions = []

    for position_key, pos in list(risk.open_positions.items()):
        # A confirmed position whose market has closed is still real exposure
        # until the resolution monitor reconciles the ledger and payout. Keep
        # it in deployed equity, but stop retrying exit work on every 5s pass.
        if pos.get("awaiting_settlement"):
            continue
        market_id = pos.get("market_id") or position_key
        market = next((m for m in active_markets if m["market_id"] == market_id), None)

        # ── CRITICAL: if the market rotated out of active_markets ─────────
        # (new candle started, scanner replaced the old market_id), we MUST
        # still evaluate. Without this a position rides all the way to
        # resolution at 0.00 or 1.00 with zero protection.
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

        threshold    = (market.get("threshold") if market else None) or pos.get("threshold")
        closing_date = ((market.get("closing_date") if market else "")
                        or pos.get("closing_date", ""))
        secs = (market.get("secs_to_close", 0) if market
                else (scanner._seconds_to_close(closing_date) if closing_date else 0))

        if not market:
            if secs <= 0:
                age_secs = time.time() - pos.get("placed_at", 0)
                if age_secs > 960:  # 16 minutes — well past any 15m candle
                    log.warning(
                        f"[{chat_id}] Cleaning stale resolved position on {market_id} "
                        f"(age={age_secs:.0f}s, asset={asset}, strategy={pos.get('strategy')})"
                    )
                    stale_positions.append((position_key, pos))
                continue

        # Inside the final seconds, settlement risk dominates any exit price
        # and the outcome is decided anyway. Let it resolve.
        if secs < config.EXIT_MIN_SECS_REMAINING:
            continue

        if not threshold or not spot_price or not entry_price:
            continue

        # ── Re-price the position ─────────────────────────────────────────
        synthetic_market = market or {
            "asset": asset, "threshold": threshold, "secs_to_close": secs,
            "timeframe": pos.get("timeframe", ""),
        }
        p_yes = fair_value(asset, synthetic_market, strategy.global_state, spot_price)
        if p_yes is None:
            continue
        # We hold one side; our win probability is that side's probability.
        w_est = p_yes if outcome == "YES" else (1.0 - p_yes)

        # Executable bid for the side we hold -- we are selling, so the bid is
        # the price we can actually hit. A mid is not executable.
        book = books.get(pos.get("outcome_id") or "")
        bid = booklib.best_bid(book) if booklib.is_usable(book) else None
        if bid is None:
            bid = (market.get("yes_price") if outcome == "YES"
                   else market.get("no_price")) if market else None
        if bid is None:
            bid = entry_price

        pos["peak_price"] = max(pos.get("peak_price", entry_price), float(bid))

        # Both legs of a complete set are held: the set pays 1.00 whatever
        # happens, so it must be burned rather than stopped or sold. A taker
        # complete set (two immediate FAK legs) is only ever seen here.
        paired = _paired_leg(risk, position_key, market_id)
        # Half a pair: this leg is unfilled but its sibling is a confirmed
        # position. That makes it a completion order rather than a standing
        # quote, which changes what the late-candle cancel should do with it.
        completion_leg = (
            not bool(pos.get("confirmed_filled"))
            and _filled_sibling(risk, pos, market_id) is not None
        )
        decision = _exit_decision(
            outcome=outcome,
            w_est=w_est,
            bid=float(bid),
            peak_price=float(pos["peak_price"]),
            entry_price=entry_price,
            secs=secs,
            fee_rate=float((market or {}).get("fee_rate") or config.DEFAULT_FEE_RATE),
            is_maker_pos=str(pos.get("strategy") or "").upper() in config.MAKER_STRATEGIES,
            confirmed_filled=bool(pos.get("confirmed_filled")),
            complete_set=paired is not None,
            completion_leg=completion_leg,
        )
        if decision is None:
            continue
        positions_to_exit.append(_exit_plan(
            market_id, position_key, pos, market, w_est=w_est,
            current_price=decision["current_price"],
            ev_hold=decision["ev_hold"],
            exit_reason=decision["reason"],
        ))


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

        # A complete set settles to 1.00 whichever outcome wins. Selling it is
        # strictly worse than burning it: a sale pays the bid and a taker fee,
        # while a burn pays the full unit. Realise the lock and stop managing
        # it as a position.
        if exit_reason == "BURN_COMPLETE_SET":
            await _burn_complete_set(
                chat_id, client, risk, pos, position_key, market_id=market_id
            )
            continue

        # A resting quote late in the candle: withdraw both legs rather than
        # let someone fill us into settlement.
        if exit_reason == "CANCEL_RESTING":
            await _withdraw_resting_quote(
                chat_id, client, risk, pos, position_key,
                reason="too close to settlement",
            )
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
                f"hold_vs_cost={ev_hold:+.4f}/share | "
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
                # Makers pay no CLOB fee on Bayse. Adding the order's `fee`
                # field here was charging a maker fill the taker fee and
                # understating every maker profit by it.
                confirmed_cost = (
                    confirmed_qty * confirmed_entry
                    * config.CURRENCY_BASE_MULTIPLIER
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
                # If remaining shares are dust (value < ₦30 or shares < 0.5), clear from tracker
                if sell_amount < 30.0 or available_shares < 0.5:
                    risk.remove_position(position_key)
                    log.info(f"[{chat_id}] Cleared dust position for {market_id} (value=₦{sell_amount:,.2f})")
                    continue
                log.warning(
                    f"[{chat_id}] EXIT deferred for {market_id}: position value "
                    f"₦{sell_amount:,.2f} is below sell minimum ₦{min_sell:,.0f}"
                )
                continue

            if (
                exit_reason == "TAKE_PROFIT"
                and sell_amount
                < amount_ngn * (1.0 + config.MIN_TAKE_PROFIT_NET_GAIN)
            ):
                log.info(
                    f"[{chat_id}] TAKE-PROFIT deferred for {market_id}: "
                    f"executable value ₦{sell_amount:,.2f} does not lock "
                    f"{config.MIN_TAKE_PROFIT_NET_GAIN:.0%} net"
                )
                continue
            if exit_reason == "REVERSAL_EXIT" and sell_amount < amount_ngn:
                # A trailing "profit lock" may not realize a net loss. The
                # ordinary stop-loss path remains available if thesis breaks.
                continue

            sell_quote = await client.get_quote(
                event_id, market_id, outcome_id, "SELL", sell_amount, CURRENCY
            )
            quote_qty = float(sell_quote.get("quantity") or 0.0)
            if quote_qty > available_shares * 1.001 and quote_qty > 0:
                # Fast price drop: quote requires more shares than held.
                # Scale sell_amount down to match available shares so exit can execute!
                scale = (available_shares / quote_qty) * 0.98
                scaled_sell = round(sell_amount * scale, 2)
                if scaled_sell >= min_sell:
                    log.info(
                        f"[{chat_id}] Scaling exit amount ₦{sell_amount:,.2f} → "
                        f"₦{scaled_sell:,.2f} to match {available_shares:.1f} shares"
                    )
                    sell_amount = scaled_sell
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

            # Step 2: Sell held shares with calibrated slippage.
            # - TAKE_PROFIT: 0.05 (5%) — refuse to give away locked gains to wide AMM spreads
            # - REVERSAL_EXIT: 0.08 (8%) — profit protection before candle dump
            # - STOP_LOSS: 0.20 (20%) — urgent but bounded
            if exit_reason == "TAKE_PROFIT":
                exit_slippage = 0.05
            elif exit_reason == "REVERSAL_EXIT":
                exit_slippage = 0.08
            else:
                exit_slippage = 0.20

            is_clob = str((market or {}).get("engine") or "").upper() == "CLOB" or (
                pos.get("asset") in {"BTC", "ETH", "SOL"} and pos.get("timeframe") in {"15min", "5min"}
            )
            if is_clob:
                # CLOB markets reject raw MARKET orders; use LIMIT FAK with slippage buffer
                limit_sell_price = round(max(0.01, current_price * (1.0 - exit_slippage)), 3)
                resp = await client.place_order(
                    event_id=event_id, market_id=market_id,
                    outcome_id=outcome_id, side="SELL",
                    amount=sell_amount, order_type="LIMIT",
                    price=limit_sell_price, time_in_force="FAK",
                    currency=CURRENCY,
                )
            else:
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
            app_to_use = _tg_app or getattr(telegram_bot, "_bot_app", None)
            if app_to_use:
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
                        app_to_use, chat_id, tg_msg, parse_mode="Markdown",
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

    # ── Stale positions that rotated out of active_markets ───────────────
    # Unfilled orders can be cancelled and released. A confirmed fill must stay
    # in the risk book until resolution_monitor records the actual payout: if we
    # remove it here, free cash is still lower by the stake, so the same winning
    # position can look like an immediate 10%+ drawdown until settlement syncs.
    for stale_key, stale_pos in stale_positions:
        order_id = stale_pos.get("order_id")
        filled = float(stale_pos.get("filled_quantity") or 0.0)
        confirmed = bool(stale_pos.get("confirmed_filled")) or filled > 0
        if order_id and not confirmed:
            try:
                await client.cancel_order(order_id)
                log.info(f"[{chat_id}] Cancelled stale unfilled order {order_id} on "
                         f"{stale_pos.get('market_id')}")
            except Exception as ce:
                log.warning(
                    f"[{chat_id}] Stale order {order_id} could not be cancelled ({ce}); "
                    "it may still be resting on the exchange"
                )
            await _resolve_unfilled_position(
                chat_id, risk, stale_pos, stale_key,
                "market rotated out and the order never filled",
            )
        elif confirmed:
            stale_pos["awaiting_settlement"] = True
            log.info(
                f"[{chat_id}] Retaining filled position {stale_pos.get('market_id')} "
                f"(filled={filled:.2f}) in risk equity until settlement is reconciled"
            )
        else:
            # A tracked entry without an order id or confirmed fill cannot be
            # reconciled as a position; remove only that empty reservation.
            log.info(
                f"[{chat_id}] Dropping stale empty reservation {stale_pos.get('market_id')}"
            )
            risk.remove_position(stale_key)


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
    stall.note_state(
        chat_id,
        paused=bool(settings.get("paused")),
        paused_reason=str(settings.get("paused_reason") or ""),
        dry_run=not config.LIVE_TRADING,
    )
    if settings.get("paused"):
        return

    free_cash = risk.current_free_cash
    if free_cash <= 0:
        try:
            free_cash = await client.get_balance_ngn()
            risk.current_free_cash = free_cash
        except Exception:
            stall.reject(chat_id, "cycle", "balance_unavailable",
                         "exchange balance request failed; no equity figure to size against")
            return

    equity = free_cash + risk.deployed()
    if risk.target_hit or risk.max_drawdown_hit:
        stall.reject(chat_id, "cycle", "risk_gate",
                     f"target_hit={risk.target_hit} drawdown_pause={risk.max_drawdown_hit}")
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
    # Non-custom modes follow the platform default scope: union the saved
    # choices with the current defaults so existing database users
    # automatically evaluate newly enabled strategies, assets, and
    # timeframes. Custom mode keeps exact user control (no expansion).
    if settings.get("mode", "balanced") != "custom":
        requested_strats = list(dict.fromkeys(
            [*requested_strats, *config.DEFAULT_STRATEGIES]
        ))
        user_assets = list(dict.fromkeys(
            [*user_assets, *config.DEFAULT_ASSETS]
        ))
        raw_tfs = list(dict.fromkeys(
            [*raw_tfs, *config.DEFAULT_TIMEFRAMES]
        ))
    user_strats = [s for s in requested_strats if s in config.PERMITTED_STRATEGIES]
    blocked = sorted(set(requested_strats) - set(user_strats))
    if blocked:
        log.warning(f"[{chat_id}] Strategies blocked by global safety policy: {blocked}")
        stall.reject(chat_id, "scope", "blocked_by_policy", ",".join(blocked))
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
        stall.reject(chat_id, "scope", "suspended_by_learner", ",".join(suspended))
    learned["strategies"] = [s for s in user_strats if s not in suspended]
    if not learned["strategies"]:
        # Nothing left to evaluate: this is a configuration state, not an
        # absence of edge, and the two need different operator actions.
        stall.reject(
            chat_id, "scope", "no_enabled_strategies",
            f"requested={requested_strats} permitted={config.PERMITTED_STRATEGIES} suspended={suspended}",
        )

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
        # Degraded relay/oracle agreement raises directional edge requirements.
        learned["oracle_penalty"] = max(0.0, float(penalty or 0.0))
        # Strategies attribute their gate rejections to this evaluation's user.
        learned["chat_id"] = chat_id
        in_scope = 0
        for market in active_markets:
            if market.get("status") != "open":
                skipped_status += 1
                continue
            if market["asset"] not in user_assets:
                skipped_asset += 1
                continue
            if market["timeframe"] not in user_tfs:
                skipped_tf += 1
                continue
            # In scope for this account (a feed-triggered partial pass filters
            # further below, but the account's scope itself is not empty).
            in_scope += 1
            if trigger_asset and market["asset"] != trigger_asset:
                skipped_trigger += 1
                continue
            if strategy.is_halted(market["asset"]):
                skipped_halted += 1
                continue
            evaluated += 1
            # Use a fresh independent oracle for crypto probability models.
            # Never trade on an indefinitely cached tick.
            asset = market["asset"]
            relay_price = feeds.spot.get(asset)
            relay_time = feeds.spot_updated_at.get(asset, 0.0)
            direct_price, direct_time = feeds_direct.get_direct_price(asset)
            is_crypto = asset in {"BTC", "ETH", "SOL"}

            if is_crypto and config.REQUIRE_DIRECT_ORACLE:
                if direct_price and (now - direct_time <= config.FEED_STALE_SEC):
                    spot_price = direct_price
                elif relay_price and (now - relay_time <= config.FEED_STALE_SEC):
                    # Graceful fallback: direct oracle has temporary lag, fall back to Bayse relay price
                    spot_price = relay_price
                else:
                    skipped_stale_feed += 1
                    continue
            else:
                spot_price = relay_price
                if not spot_price or now - relay_time > config.FEED_STALE_SEC:
                    skipped_stale_feed += 1
                    continue
            if not spot_price:
                skipped_no_spot += 1
                continue
            # Both legs must be priced off ONE book snapshot. Fetching it
            # inside each strategy let a tick between the two calls turn a
            # spread that looked locked into one that is not -- and a pair
            # priced off two different books is not a locked spread at all.
            books = {}
            if str(market.get("engine") or "").upper() == "CLOB":
                ids = [i for i in (market.get("yes_id"), market.get("no_id")) if i]
                if ids:
                    try:
                        books = await asyncio.wait_for(
                            client.get_orderbooks(ids, depth=5), timeout=2.5
                        )
                    except Exception as be:
                        log.debug(f"[{chat_id}] book fetch failed for {asset}: {be}")
                        books = {}

            sigs = await strategies.evaluate_all(
                market, learned, strategy.global_state,
                spot_price=spot_price, books=books,
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

        skips = {
            "status": skipped_status,
            "asset": skipped_asset,
            "timeframe": skipped_tf,
            "trigger": skipped_trigger,
            "halted": skipped_halted,
            "stale_feed": skipped_stale_feed,
            "no_spot": skipped_no_spot,
        }
        stall.note_evaluation(
            chat_id,
            markets_total=len(active_markets),
            # The scanner holds markets whose status is not "open" as well, so
            # len(active_markets) is a discovery count, not an open count.
            open_markets=len(active_markets) - skipped_status,
            in_scope=in_scope,
            evaluated=evaluated,
            signals=len(all_signals),
            skips=skips,
            detail=(
                f"strategies={learned.get('strategies', [])} assets={user_assets} "
                f"tfs={user_tfs} max_exposure={max_exp:.0%}"
            ),
        )
        for sig in all_signals:
            stall.note_signal(chat_id, sig.strategy, sig.asset)

        final = strategies.merge_signals(all_signals, strategy.global_state)
        for sig in final:
            await executor.execute_trade(
                chat_id, sig, client, risk, settings, equity, free_cash
            )
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
            stall.note_scan(len(active_markets))
            telegram_bot._active_markets = active_markets
            executor.init_executor(active_markets, _tg_app)
            log.info(f"Scan: {len(active_markets)} markets")
            feeds.restart_bayse_feed(active_markets, _on_market_update)
        except Exception as e:
            health.fail("scanner", e)
            # Keep the last known market count honest (the loop may still be
            # trading against a cached scan) while recording why it is stale.
            stall.note_scan(len(active_markets), error=str(e))
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
    # This history feeds the measured volatility, the 5-minute momentum, the
    # Kalman drift and the GARCH variance — so it must be the independent
    # oracle series. check_lag hands back the *relay* price once its oracle
    # sample is 2s old, because it is answering "which price is fresher right
    # now". That was harmless while the relay was Binance-derived; since
    # 2026-09-26 the relay is a Chainlink 60-second TWAP, so the substitution
    # injects a smoothed series that understates all four estimators. A
    # 5-second-old Binance print is still a Binance print: use the oracle until
    # it is stale by the same standard the rest of the bot applies, and fall
    # back to the relay only past that.
    direct_price, direct_time = feeds_direct.get_direct_price(asset)
    history_price = (
        direct_price
        if direct_price and (time.time() - direct_time) <= config.FEED_STALE_SEC
        else lag["price"]
    )
    strategy.update_price_history(asset, history_price)
    recorder.record_spot_tick(asset, history_price)
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

    # Commit live price updates to market state in real time
    new_yes = prices.get("yes")
    new_no  = prices.get("no")
    if new_yes is not None and new_no is not None:
        ny, nn = float(new_yes), float(new_no)
        if 0.01 <= ny <= 0.99 and 0.01 <= nn <= 0.99:
            market["yes_price"] = ny
            market["no_price"]  = nn

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
            # Dead-man switch: "is the engine still turning at all". A completed
            # pass where every account deliberately short-circuited (paused, dry
            # run) still counts as progress — that state gets its own, far more
            # useful alert from the trading-drought watchdog below. Conflating
            # the two is how alerts become wallpaper a real outage hides behind.
            if all(not isinstance(r, Exception) for r in results):
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
    global _last_feed_alert
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

# ── Trading-drought watchdog ─────────────────────────────────────────────────

_STALL_CHECK_SEC = 60.0


def _worst_feed_age_sec(now: float) -> float | None:
    """Oldest oracle/relay sample across the crypto assets we trade."""
    ages: list[float] = []
    for asset in ("BTC", "ETH", "SOL"):
        _, direct_time = feeds_direct.get_direct_price(asset)
        if direct_time:
            ages.append(now - direct_time)
            continue
        relay_time = feeds.spot_updated_at.get(asset, 0.0)
        ages.append(now - relay_time if relay_time else now - feeds_direct._startup_time)
    return max(ages) if ages else None


async def _seed_stall_clocks(users: list[dict]) -> None:
    """Backdate drought clocks from the ledger so a restart is not amnesia.

    Without this, deploying clears "minutes since last order" to zero and a
    multi-day stall would be invisible for the first two hours of every restart —
    which is precisely when an operator is busiest reading deploy output.
    """
    for user in users or []:
        chat_id = user.get("chat_id")
        if not chat_id:
            continue
        try:
            settings = user.get("settings", {}) or {}
            stall.note_state(
                chat_id,
                paused=bool(settings.get("paused")),
                paused_reason=str(settings.get("paused_reason") or ""),
                dry_run=not config.LIVE_TRADING,
            )
            rows = await asyncio.to_thread(database.recent_trades, chat_id, 1)
            created = rows[0].get("created_at") if rows else None
            try:
                filled_at = await asyncio.to_thread(database.last_filled_trade_at, chat_id)
            except Exception:
                filled_at = None
            # Prefer the last CONFIRMED fill. Falling back to the newest trade
            # row would let a resting quote that was later cancelled unfilled
            # reset the drought clock, hiding exactly the drought this seeds.
            if filled_at is not None:
                created = filled_at
            elif rows:
                log.info(
                    f"[{chat_id}] No confirmed fill in the ledger — drought clock "
                    "starts from process start, not from the last unfilled quote"
                )
                continue
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                stall.seed_last_trade(chat_id, created.timestamp())
                log.info(
                    f"[{chat_id}] Stall clock seeded from ledger "
                    f"(last confirmed fill {(time.time() - created.timestamp()) / 3600:.1f}h ago)"
                )
        except Exception as seed_err:
            log.debug(f"[{chat_id}] Stall clock seeding skipped: {seed_err}")


def _stall_context(chat_id: str, user: dict | None = None) -> dict:
    """Keyword arguments for :func:`stall.report` / :func:`stall.verdict`.

    Only keys those functions accept. Pause and dry-run state are *recorded*
    separately via ``stall.note_state`` when it changes, so the verdict can be
    built from any call site without every caller threading it through.
    """
    risk = _user_risks.get(chat_id)
    equity = 0.0
    if risk is not None:
        try:
            equity = float(getattr(risk, "current_free_cash", 0.0) or 0.0) + risk.deployed()
        except Exception:
            equity = 0.0
    return {
        "equity": equity,
        "min_viable": _MIN_VIABLE_BALANCE,
        "feed_age_sec": _worst_feed_age_sec(time.time()),
        "eval_max_age_sec": config.STALL_EVAL_MAX_AGE_SEC,
        "resting_now": _resting_order_count(risk),
    }


def _resting_order_count(risk) -> int | None:
    """Unfilled orders currently resting on the exchange, from the risk book.

    The stall counter of passive placements only ever grows; reporting it as
    "still resting" told the user five quotes were live when they had all
    expired. The risk book is the source of truth for what is open now.
    """
    if risk is None:
        return None
    try:
        return sum(
            1 for pos in risk.open_positions.values()
            if pos.get("order_id") and not position_is_filled(pos)
        )
    except Exception:
        return None


async def _check_trading_stalls() -> None:
    """Name the reason an account has not traded, and alert on it.

    This is deliberately a *reporting* loop. It never resizes, never lowers a
    gate, and never resumes a manual pause: silence caused by "no qualifying
    edge" is the correct outcome and stays a single informational line. Silence
    caused by a dead task, a stale feed, a config state, or a dry-run flag is a
    fault, and a fault that nobody can see is how an account sits dark for days.
    """
    users = _active_users_cache or []
    if not users:
        return
    limit = float(config.TRADE_STALL_ALERT_MIN)
    for user in users:
        chat_id = user.get("chat_id")
        if not chat_id:
            continue
        try:
            context = _stall_context(chat_id, user)
            # One instant for the whole alert. The header, the report body and
            # the NO_CONFIRMED_FILL detail all print "minutes since the last
            # confirmed fill"; they disagreed inside a single message (observed:
            # "stall — 1573 min" over "Last confirmed fill: 1572 min ago"). The
            # rounding itself is fixed in stall.format_gap_minutes — every
            # renderer goes through it — and reading one clock here keeps a
            # sub-second drift from straddling a minute boundary as well.
            now = time.time()
            data = stall.report(chat_id, now=now, **context)
            verdict = data["verdict"]
            gap = stall.trade_gap_minutes(chat_id, now=now)
            severe = verdict.get("severity") == "critical"
            if gap < limit and not severe:
                health.touch("trading_stall", chat_id=chat_id, verdict=verdict["code"],
                             gap_min=round(gap, 1))
                continue
            if not stall.note_alert(chat_id, verdict["code"], now=now):
                continue
            gap_text = stall.format_gap_minutes(gap)
            log.warning(
                f"[{chat_id}] TRADING STALL after {gap_text} min — {verdict['code']}: "
                f"{verdict['headline']} | {verdict['detail']} | action: {verdict['action']}"
            )
            if severe:
                health.fail("trading_stall", f"{chat_id}: {verdict['code']}", gap_min=round(gap))
            if _tg_app:
                text = stall.format_report(
                    chat_id, markdown=True, now=now,
                    **{k: v for k, v in context.items()
                       if k in ("equity", "min_viable", "feed_age_sec",
                                "eval_max_age_sec", "resting_now")},
                )
                # The gap is measured from the last exchange-confirmed FILL.
                # "without an order" contradicted the report whenever MAKER had
                # just placed quotes that never filled.
                await telegram_bot.send_message(
                    _tg_app, chat_id,
                    f"🩺 *Trading stall — {gap_text} min without a confirmed fill*\n\n"
                    f"{text[:3500]}",
                    parse_mode="Markdown",
                )
        except Exception as stall_err:
            # Diagnostics must never take down the loop that trades.
            log.debug(f"[{chat_id}] Stall check error: {stall_err}", exc_info=True)


async def _stall_watchdog():
    log.info(
        "Trading-drought watchdog started (alert after %.0f min without a confirmed fill)",
        config.TRADE_STALL_ALERT_MIN,
    )
    while True:
        await asyncio.sleep(_STALL_CHECK_SEC)
        await _check_trading_stalls()


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
            # "Why is this account quiet?" belongs in the same read-only view as
            # balances: a dashboard that shows money but not stalls invites the
            # wrong conclusion that quiet == broken money rather than quiet == gate.
            stall_reports = {}
            for cid in list(_user_clients.keys()):
                try:
                    stall_reports[cid] = stall.report(cid, **_stall_context(cid))
                except Exception as stall_err:
                    log.debug(f"[{cid}] Stall report failed: {stall_err}")
            server.stats_cache.update({
                "users": user_stats,
                "oracles": oracle_stats,
                "stalls": stall_reports,
                "live_trading": bool(config.LIVE_TRADING),
                "last_update": time.time(),
            })
        except Exception as e:
            log.error(f"Dashboard update error: {e}")
        await asyncio.sleep(30)


# ── Graceful shutdown ─────────────────────────────────────────────────────────
# Docker — and therefore Coolify — stops a container with SIGTERM and escalates
# to SIGKILL after a grace period. Python's default SIGTERM action kills the
# process without running `finally` blocks, so the singleton lease stayed held
# until it expired (LOCK_LEASE_SEC of dead air) on every deploy and restart.
# Turning the signal into an event lets main() unwind in a known order: stop the
# trading loops, stop Telegram polling, hand the lease over, close the clients.
_shutdown_event = asyncio.Event()


def _request_shutdown(signum=None) -> None:
    """Signal-handler body: wind down instead of dying in place."""
    if _shutdown_event.is_set():
        # A second signal means the first shutdown is slower than whatever sent
        # it is willing to wait. Leave now rather than be SIGKILLed mid-cleanup.
        log.warning("Repeated shutdown request (%s) — exiting immediately.", signum)
        os._exit(1)
    try:
        name = signal.Signals(signum).name if signum is not None else "shutdown request"
    except ValueError:
        name = str(signum)
    log.info("Received %s — stopping tasks and releasing the singleton lease.", name)
    # /ready must go 503 *before* the lease is handed over, never after.
    health.set_ready(False)
    server.instance_state["role"] = "stopping"
    _shutdown_event.set()


def _install_signal_handlers(loop) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig)
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            # Non-main thread or a platform without loop signal support. The
            # lease still expires on its own; the handover is just slower.
            log.warning("Cannot install a %s handler (%s).", sig, exc)


def _release_lease_if_owned() -> None:
    """Give the lease back so a standby instance can take over immediately."""
    global _owns_singleton
    if not _owns_singleton:
        return
    _owns_singleton = False
    if not hasattr(database, "release_singleton_lock"):
        return
    try:
        released = database.release_singleton_lock()
    except Exception as exc:
        log.warning("Could not release the singleton lease: %s", exc)
        return
    log.info(
        "Singleton lease released — a standby instance can take over now."
        if released
        else "Singleton lease was already gone (another owner)."
    )


async def _graceful_stop() -> None:
    """Unwind in the order that makes a handover cheap instead of conflicting.

    Trading loops stop first, then Telegram polling, then the lease is released.
    Releasing before polling stops would let the standby instance start its own
    poller against the same bot token and eat `409 Conflict` from Telegram.

    Every step is time-bounded and the bounds add up to less than Docker's
    default 10s stop grace period *up to the release*, because the release is
    the only step the next deployment is waiting on: if SIGKILL lands first, the
    handover degrades to "wait for the lease to expire" instead of "immediate".
    """
    pending = [task for task in _background_tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        done, not_done = await asyncio.wait(pending, timeout=3)
        _background_tasks.difference_update(done)
        if not_done:
            log.warning(
                "%d background task(s) ignored cancellation: %s",
                len(not_done), [t.get_name() for t in not_done],
            )

    app = _tg_app
    if app is not None:
        try:
            updater = getattr(app, "updater", None)
            if updater is not None and updater.running:
                await asyncio.wait_for(updater.stop(), timeout=4)
        except Exception as exc:
            log.warning("Telegram polling did not stop cleanly: %s", exc)

    _release_lease_if_owned()

    if app is not None:
        for label, step in (("stop", app.stop), ("shutdown", app.shutdown)):
            try:
                await asyncio.wait_for(step(), timeout=3)
            except Exception as exc:
                log.warning("Telegram app %s did not complete: %s", label, exc)


# ── Database startup ──────────────────────────────────────────────────────────
# `init_db` opens the connection pool and runs migrations, and it is the first
# blocking thing `main()` does after binding the health port. On an unreachable
# Postgres it raises `psycopg2.OperationalError`, and that exception used to
# propagate out of `main()`: the process died in under a second, `/live` never
# answered a single probe, and the platform rolled the release back. A Supabase
# pause, a pool-exhaustion blip or a DNS hiccup during a deploy therefore looked
# exactly like a broken image (2026-09-25).
#
# Binding the port early is only half the fix — a container that exits keeps the
# port closed no matter when it was opened. Waiting a bounded time instead of
# dying on the first error is what makes a transient outage survivable, while
# still failing loudly and non-zero when the database never comes back.
DB_INIT_RETRY_SEC = max(1.0, float(os.getenv("DB_INIT_RETRY_SEC", "5")))
# 0 means "retry forever". The default covers the platform's probe grace window
# (Coolify: 5 attempts, and the Dockerfile HEALTHCHECK has start-period=90s), so
# a database that is briefly unreachable is outlasted rather than fatal.
DB_INIT_TIMEOUT_SEC = max(0.0, float(os.getenv("DB_INIT_TIMEOUT_SEC", "120")))


async def _init_database_with_retry(
    init_fn,
    *,
    retry_sec: float = DB_INIT_RETRY_SEC,
    timeout_sec: float = DB_INIT_TIMEOUT_SEC,
    sleep=asyncio.sleep,
    clock=time.monotonic,
) -> bool:
    """Run ``init_fn`` until it succeeds, a shutdown arrives, or the cap hits.

    True  — the database is up and migrated.
    False — gave up at the timeout, or a shutdown was requested while waiting.
            The caller distinguishes the two via ``_shutdown_event``.

    ``init_fn`` is a blocking psycopg2 call, so it runs in a worker thread: on
    the event loop it would freeze the health server, which is the exact symptom
    a platform reads as a dead container.
    """
    started = clock()
    attempt = 0
    while True:
        attempt += 1
        try:
            await asyncio.to_thread(init_fn)
        except Exception as exc:
            # Recorded so /ready explains *why* this container is not serving.
            health.fail("database", exc, state="starting", attempts=attempt)
            waited = clock() - started
            log.error(
                "Database startup failed (attempt %d, %.0fs elapsed): %s",
                attempt, waited, exc,
            )
            if _shutdown_event.is_set():
                log.info("Shutdown requested while waiting for the database.")
                return False
            if timeout_sec > 0 and waited >= timeout_sec:
                log.critical(
                    "Database unreachable for %.0fs. Exiting non-zero so the "
                    "platform reports a failed start rather than restarting "
                    "silently. Check DATABASE_URL and that Postgres/Supabase "
                    "accepts connections from this host.",
                    waited,
                )
                return False
            await sleep(retry_sec)
            continue
        health.touch("database", attempts=attempt)
        if attempt > 1:
            log.info(
                "Database up after %d attempt(s) (%.0fs).", attempt, clock() - started,
            )
        return True


# ── Singleton lease standby ───────────────────────────────────────────────────
# A rolling update starts the NEW container before stopping the OLD one, and the
# old one keeps renewing its lease until it is told to stop. "Lease held by
# another live instance" is therefore the normal state of a fresh container for
# the first seconds of every deploy — not an error.
#
# The previous code waited 12 × 5s and exited. That made deploys unrecoverable:
# Coolify stops the old container only after the new one passes its health
# check, and the new one only passed after owning the lease, which only freed
# once the old container was stopped. Both sides waited on each other and the
# new container crash-looped until the deploy was rolled back (2026-09-25).
# Standing by instead — alive, answering /live, never /ready — lets the platform
# finish the swap and the lease hand over in seconds.
LOCK_RETRY_SEC = max(1.0, float(os.getenv("LOCK_ACQUIRE_RETRY_SEC", "5")))
# 0 means "stand by forever": a live lease holder is a real bot somewhere, and
# trading twice is worse than waiting. The default cap exists so a wedged
# two-deployments-at-once setup still surfaces as a restart instead of silence.
LOCK_STANDBY_LIMIT_SEC = max(0.0, float(os.getenv("LOCK_ACQUIRE_TIMEOUT_SEC", "900")))
_STANDBY_ESCALATE_SEC = 300.0


async def _acquire_singleton_lease(
    acquire,
    *,
    retry_sec: float = LOCK_RETRY_SEC,
    standby_limit_sec: float = LOCK_STANDBY_LIMIT_SEC,
    sleep=asyncio.sleep,
    clock=time.monotonic,
) -> bool:
    """Wait until this process owns the singleton lease.

    True  — the lease is ours.
    False — a shutdown was requested while standing by, or the standby limit was
            reached. The caller distinguishes the two via ``_shutdown_event``.

    ``acquire`` runs in a worker thread because it is a blocking database call,
    and must never be called from the event loop directly: while it blocks, the
    health server cannot answer and the platform thinks the container is dead.
    """
    started = clock()
    attempt = 0
    escalated = False
    server.instance_state["role"] = "standby"
    while True:
        attempt += 1
        if await asyncio.to_thread(acquire):
            server.instance_state["role"] = "active"
            health.touch("singleton_lock")
            if attempt > 1:
                log.info(
                    "Singleton lease acquired after %.0fs in standby (%d attempts).",
                    clock() - started, attempt,
                )
            return True

        if _shutdown_event.is_set():
            log.warning("Shutdown requested while standing by for the lease.")
            return False

        waited = clock() - started
        # Recorded as a failure so /ready explains *why* this container is not
        # serving, instead of just reporting "not ready".
        health.fail(
            "singleton_lock",
            f"lease held by another instance for {waited:.0f}s",
            state="standby",
        )
        # One line a minute rather than one per retry: a long standby should be
        # visible without burying the logs around it.
        if attempt == 1 or attempt % 12 == 0:
            log.warning(
                "Singleton lease held by another instance — standing by "
                "(attempt %d, %.0fs elapsed, retrying in %.0fs)",
                attempt, waited, retry_sec,
            )
        if not escalated and waited >= _STANDBY_ESCALATE_SEC:
            escalated = True
            log.error(
                "Still in standby after %.0fs. If no deploy is in progress, two "
                "live deployments share this database and this one will never "
                "trade — stop one of them.",
                waited,
            )
        if standby_limit_sec > 0 and waited >= standby_limit_sec:
            log.critical(
                "No singleton lease after %.0fs in standby. Exiting so the "
                "platform restarts this container and retries.",
                waited,
            )
            return False
        await sleep(retry_sec)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _tg_app, active_markets, _scan_client, _owns_singleton

    config.validate()
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set")

    _install_signal_handlers(asyncio.get_running_loop())

    # Bind the health port before anything that can block. Coolify probes the
    # new container while the old one still owns the lease and only stops the
    # old one once the new one is healthy; a container that starts its HTTP
    # server after winning the lease can never pass that probe, so the deploy
    # rolls back and both containers restart forever.
    #
    # Binding early is necessary but not sufficient: /live stays dark just as
    # surely if the process *exits*. Everything below that can fail for an
    # external reason (the database, the singleton lease) therefore retries
    # inside a bounded window instead of propagating out of main() — see the
    # database retry and the lease standby defined above.
    server_task = asyncio.create_task(
        server.start_server(port=int(os.getenv("PORT", "8080"))),
        name="http-server",
    )
    _background_tasks.add(server_task)
    server_task.add_done_callback(_background_tasks.discard)
    _start_supervised("self_ping", _self_ping_loop)

    init_db_fn = getattr(database, "init_db", None) or getattr(database, "_init_pool", None)
    if init_db_fn is not None:
        if not await _init_database_with_retry(init_db_fn):
            if _shutdown_event.is_set():
                log.info("Stood down before the database came up.")
                return
            # Non-zero so the platform (and the deploy log) shows a failure
            # instead of a clean stop that quietly restarts.
            raise SystemExit(1)

    # Data hygiene before the first decision is taken. A stale lease must be
    # cleared before we try to take it (otherwise the deploy cannot trade),
    # and a contradictory trade row must be corrected before the risk manager
    # reads exposure from it. Both run off the event loop and neither is
    # allowed to stop startup.
    try:
        hygiene = await asyncio.to_thread(maintenance.run, False)
        if hygiene.total:
            log.warning("Startup data hygiene found issues:\\n%s", hygiene.text())
            applied = await asyncio.to_thread(maintenance.apply_safe)
            if applied.total:
                log.info("Startup data hygiene applied:\\n%s", applied.text())
    except Exception as exc:
        log.error(f"Startup maintenance failed (non-fatal): {exc}")

    if hasattr(database, "force_acquire_singleton_lock"):
        owned = await _acquire_singleton_lease(database.force_acquire_singleton_lock)
        if not owned:
            if _shutdown_event.is_set():
                log.info("Stood down without the singleton lease.")
                return
            # Non-zero so the platform (and the deploy log) shows a failure
            # instead of a clean stop that quietly restarts.
            raise SystemExit(1)
        _owns_singleton = True
        log.info("Singleton lease acquired.")
        health.touch("singleton_lock")
    else:
        server.instance_state["role"] = "active"

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
    _start_supervised("stall_watchdog", _stall_watchdog)

    # ── Reconnect existing users with CORRECT status message ─────────────────
    existing = await asyncio.to_thread(_safe_get_all_active)
    log.info(f"Reconnecting {len(existing)} existing user(s)")
    await _seed_stall_clocks(existing)

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
                pause_reason = str(settings.get("paused_reason") or "manual")
                if pause_reason == "manual":
                    guidance = (
                        "It was a manual pause, so only /resume will clear it."
                    )
                else:
                    guidance = (
                        f"Reason: `{pause_reason.replace('_', ' ')}` — that kind of stop "
                        "expires by itself at the next trading-day rollover. "
                        "/resume overrides it now, /why explains the account state."
                    )
                await telegram_bot.send_message(
                    _tg_app, cid,
                    f"🔄 *Bot restarted* (update deployed)\n\n"
                    f"⏸ Your trading was *paused* before the restart and is still paused.\n"
                    f"{guidance}",
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
    if _shutdown_event.is_set():
        log.warning("Shutdown arrived during startup — skipping readiness.")
    else:
        health.set_ready(True)
        log.info("Bot startup complete; readiness enabled")
    while not _shutdown_event.is_set():
        try:
            # One wait serving two purposes: the 5s supervision tick and an
            # immediate response to SIGTERM. Sleeping blindly for 5s after a
            # stop request burns the platform's grace period doing nothing.
            await asyncio.wait_for(_shutdown_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        if _shutdown_event.is_set():
            break
        _refresh_timers()
        health.touch("bot")
        if server_task.done():
            error = server_task.exception()
            raise RuntimeError(f"HTTP server stopped unexpectedly: {error}")
        if not _tg_app.updater.running:
            raise RuntimeError("Telegram polling stopped unexpectedly")

    log.info("Shutdown requested — handing over to the next instance.")
    await _graceful_stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        health.set_ready(False)
        # Backstop. _graceful_stop() already released the lease on an orderly
        # shutdown; this covers the paths that raise instead — a dead HTTP
        # server, polling that stopped, an unrecoverable startup error — so the
        # lease is never left held by a process that is gone.
        _release_lease_if_owned()
