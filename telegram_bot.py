"""
Telegram bot — multi-user setup and control.
Fixes: engine label removed from notifications (always MARKET now),
       every command logged with chat_id for multi-user observability.
"""

import logging
import asyncio
import time
from datetime import date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

import database

def _safe_get_user(cid: str):
    if hasattr(database, "get_user"):
        try:
            return database.get_user(cid)
        except Exception:
            return None
    return None
import learner
import config
import health
from config import TELEGRAM_TOKEN

log = logging.getLogger("telegram_bot")

_NEED_PUBLIC = 1
_NEED_SECRET = 2
_setup_state: dict[str, int] = {}
_temp_pub:    dict[str, str] = {}

_user_clients:   dict = {}
_user_risks:     dict = {}
_user_daily:     dict = {}
_active_markets: list = []
_start_user_fn       = None

_VALID_STRATEGIES = set(config.ACTIVE_STRATEGIES)
# Aliases kept for strategies that no longer exist resolve to nothing, which
# makes `/set strategies SNIPE` an error rather than a silent no-op that looks
# like a working configuration for a strategy that is not running.
_STRATEGY_ALIASES = {}

def _normalize_strat(s: str) -> str:
    cleaned = s.strip().upper().replace("-", "_")
    return _STRATEGY_ALIASES.get(cleaned, cleaned)

_STRAT_ICONS = {
    "TAKER": ("🎯", "TAKER"),
    "MAKER": ("📊", "MAKER"),
}

_VALID_ASSETS     = {"BTC", "ETH", "SOL", "EURUSD", "GBPUSD", "XAUUSD"}
_VALID_TIMEFRAMES = {"5min", "15min", "1h", "6h", "1d"}
MIN_TRADE_NGN     = 100


def inject(user_clients, user_risks, user_daily, active_markets, start_user_fn):
    global _user_clients, _user_risks, _user_daily, _active_markets, _start_user_fn
    _user_clients   = user_clients
    _user_risks     = user_risks
    _user_daily     = user_daily
    _active_markets = active_markets
    _start_user_fn  = start_user_fn


_bot_app = None


def build_app() -> Application:
    global _bot_app
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    _bot_app = app
    for cmd, fn in [
        ("start",         cmd_start),
        ("status",        cmd_status),
        ("balance",       cmd_balance),
        ("trades",        cmd_trades),
        ("markets",       cmd_markets),
        ("analysis",      cmd_analysis),
        ("settings",      cmd_settings),
        ("strategies",    cmd_strategies),
        ("strategy",      cmd_strategies),
        ("set",           cmd_set),
        ("pause",         cmd_pause),
        ("resume",        cmd_resume),
        ("mode",          cmd_mode),
        ("learning",      cmd_learning),
        ("resetlearning", cmd_resetlearning),
        ("learnstats",    cmd_learnstats),
        ("debug",         cmd_debug),
        ("why",           cmd_why),
        ("whytrading",    cmd_why),
        ("quotes",        cmd_quotes),
        ("disconnect",    cmd_disconnect),
        ("rekey",         cmd_rekey),
        ("wallet",        cmd_wallet),
        ("help",          cmd_help),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)
    return app


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if "Conflict" in str(err):
        # Repeated 409s look alive from the outside but make the bot silent.
        # Stop polling so the main supervisor exits and the platform restarts it.
        health.fail("telegram", "polling conflict")
        log.critical("Telegram polling conflict; stopping this instance")
        updater = context.application.updater
        if updater and updater.running:
            asyncio.create_task(updater.stop())
        return
    health.fail("telegram_updates", type(err).__name__)
    log.error("Telegram update failed", exc_info=err)


# ── Setup flow ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    log.info(f"[{cid}] /start")
    user = await asyncio.to_thread(_safe_get_user, cid)
    if user and user.get("is_active"):
        await _main_menu(update)
        return
    _setup_state[cid] = _NEED_PUBLIC
    await update.message.reply_text(
        "👋 *Welcome to Bayse Bot!*\n\n"
        "To connect your account:\n"
        "1. Open *app.bayse.markets*\n"
        "2. Go to *More → Account Settings → API Keys → Create*\n"
        "3. Paste your *Public Key* here (starts with `pk_`):",
        parse_mode="Markdown",
    )


async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid  = str(update.effective_chat.id)
    text = update.message.text.strip()
    st   = _setup_state.get(cid)

    if st == _NEED_PUBLIC:
        if not text.startswith("pk_"):
            await update.message.reply_text("❌ Public keys start with `pk_` — try again:", parse_mode="Markdown")
            return
        _temp_pub[cid]    = text
        _setup_state[cid] = _NEED_SECRET
        await update.message.reply_text("✅ Got it!\n\nNow paste your *Secret Key* (starts with `sk_`):", parse_mode="Markdown")
        return

    if st == _NEED_SECRET:
        if not text.startswith("sk_"):
            await update.message.reply_text("❌ Secret keys start with `sk_` — try again:", parse_mode="Markdown")
            return
        pub = _temp_pub.pop(cid, "")
        _setup_state.pop(cid, None)
        try:
            await update.message.delete()  # remove the secret from chat history
        except Exception:
            log.warning(f"[{cid}] Could not delete API secret message")
        msg = await update.effective_chat.send_message("🔄 Connecting…")
        try:
            from client import BayseClient
            client  = BayseClient(pub, text)
            balance = await client.get_balance_ngn()
            await asyncio.to_thread(database.add_user, cid, pub, text)
            _user_clients[cid] = client
            if _start_user_fn:
                await _start_user_fn(cid)
            await msg.delete()
            log.info(f"[{cid}] New user connected | balance=₦{balance:,.0f}")
            await update.message.reply_text(
                f"🎉 *Connected!*\n\nBalance: ₦{balance:,.2f}\n\n"
                f"Min trade: ₦{MIN_TRADE_NGN} | Default risk: 2% per trade.",
                parse_mode="Markdown",
            )
            await _main_menu(update)
        except Exception as e:
            _setup_state[cid] = _NEED_PUBLIC
            await msg.delete()
            log.warning(f"[{cid}] Connection failed: {e}")
            await update.message.reply_text(
                "❌ *Connection failed*\n\nCheck your keys and try /start again.",
                parse_mode="Markdown",
            )
        return

    if not await asyncio.to_thread(_safe_get_user, cid):
        await update.message.reply_text("Use /start to connect your Bayse account.")


async def _main_menu(update: Update):
    kb = [
        [InlineKeyboardButton("📊 Status",   callback_data="status"),
         InlineKeyboardButton("💰 Balance",  callback_data="balance")],
        [InlineKeyboardButton("🏦 Markets",  callback_data="markets"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("⏸ Pause",    callback_data="pause"),
         InlineKeyboardButton("▶️ Resume",   callback_data="resume")],
        [InlineKeyboardButton("🔄 Reset Learning", callback_data="resetlearning")],
    ]
    await update.message.reply_text(
        "🤖 *Bayse Bot — Active*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def on_button(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    cid = str(q.from_user.id)
    if not await asyncio.to_thread(_safe_get_user, cid):
        await q.message.reply_text("Use /start to connect.")
        return
    d = q.data
    log.info(f"[{cid}] button:{d}")
    if   d == "status":   await q.message.reply_text(await _status_text(cid),   parse_mode="Markdown")
    elif d == "balance":  await q.message.reply_text(await _balance_text(cid),  parse_mode="Markdown")
    elif d == "markets":  await q.message.reply_text(await _markets_text(cid),  parse_mode="Markdown")
    elif d == "settings": await q.message.reply_text(await _settings_text(cid), parse_mode="Markdown")
    elif d == "pause":
        await _set_paused(cid, True)
        log.info(f"[{cid}] PAUSED via button")
        await q.message.reply_text("⏸ Trading paused.")
    elif d == "resume":
        await q.message.reply_text(
            await _apply_resume(cid, via="button"), parse_mode="Markdown"
        )
    elif d == "resetlearning":
        from datetime import datetime, timezone
        user = await asyncio.to_thread(_safe_get_user, cid)
        s    = user["settings"]
        s["learned"] = {}
        s["reset_learning_at"] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(database.update_settings, cid, s)
        log.info(f"[{cid}] /resetlearning via button")
        await q.message.reply_text("🔄 Learned settings cleared and trade history reset.")
    elif d in _MODES:
        mode_cfg = _MODES[d]
        user     = await asyncio.to_thread(_safe_get_user, cid)
        s        = user["settings"]
        s.update(mode_cfg["settings"])
        s["mode"] = d.replace("mode_", "")
        await asyncio.to_thread(database.update_settings, cid, s)
        log.info(f"[{cid}] MODE changed to {s['mode']} via button")
        await q.message.reply_text(f"{mode_cfg['label']} applied. ✅", parse_mode="Markdown")


def _guard(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        cid = str(update.effective_chat.id)
        log.info(f"[{cid}] /{fn.__name__.replace('cmd_','')}")
        if not await asyncio.to_thread(_safe_get_user, cid):
            await update.message.reply_text("Use /start to connect.")
            return
        await fn(update, ctx)
    wrapper.__name__ = fn.__name__
    return wrapper


# ── Commands ──────────────────────────────────────────────────────────────────

@_guard
async def cmd_status(update: Update, _ctx):
    await update.message.reply_text(await _status_text(str(update.effective_chat.id)), parse_mode="Markdown")

@_guard
async def cmd_balance(update: Update, _ctx):
    await update.message.reply_text(await _balance_text(str(update.effective_chat.id)), parse_mode="Markdown")

@_guard
async def cmd_wallet(update: Update, _ctx):
    cid    = str(update.effective_chat.id)
    client = _user_clients.get(cid)
    if not client:
        await update.message.reply_text("Still starting up.")
        return
    import json
    data = await client.get_wallet()
    text = json.dumps(data, indent=2)
    if len(text) > 3800:
        text = text[:3800] + "\n…(truncated)"
    await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")

@_guard
async def cmd_rekey(update: Update, _ctx):
    cid = str(update.effective_chat.id)
    _setup_state[cid] = _NEED_PUBLIC
    await update.message.reply_text(
        "🔑 *Update API Keys*\n\n"
        "Let's update your Bayse connection.\n"
        "Please paste your new *Public Key* (starts with `pk_`):",
        parse_mode="Markdown",
    )

@_guard
async def cmd_trades(update: Update, _ctx):
    cid  = str(update.effective_chat.id)
    rows = await asyncio.to_thread(database.recent_trades, cid, limit=10)
    if not rows:
        await update.message.reply_text("No trades yet.")
        return
    lines = ["📋 *Last 10 Trades*\n"]
    for r in rows:
        # won=null + pnl=0.0 → unfilled FAK order (returned), not a loss
        if r["won"] is None and (r.get("pnl_ngn") or 0) == 0.0:
            icon = "⚪"
            pnl  = "UNFILLED (returned)"
        elif r["won"] == 1:
            icon = "✅"
            pnl  = f"₦{r['pnl_ngn']:+,.0f}" if r.get("pnl_ngn") is not None else "pending"
        elif r["won"] == 0:
            icon = "❌"
            pnl  = f"₦{r['pnl_ngn']:+,.0f}" if r.get("pnl_ngn") is not None else "pending"
        else:
            icon = "⏳"
            pnl  = "pending"
        lines.append(f"{icon} {r['strategy']} {r['asset']} {r['timeframe']} {r['outcome']} — {pnl}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@_guard
async def cmd_markets(update: Update, _ctx):
    await update.message.reply_text(await _markets_text(str(update.effective_chat.id)), parse_mode="Markdown")

@_guard
async def cmd_quotes(update: Update, _ctx):
    """Resting maker quotes: what we are offering, at what price, for how long.

    Half the bot is a market maker and its quotes have a lifecycle -- they are
    placed, they age, they are withdrawn when the oracle moves through them.
    Without this the only way to know a quote exists is to wait for a fill
    message that may never come, which is how 19 hours went by with two
    resting bids and no fills before anyone noticed.
    """
    import config as _cfg
    from strategies.maker import maker_strategy

    cid = str(update.effective_chat.id)
    quotes = getattr(maker_strategy, "open_quotes", {}) or {}
    if not quotes:
        await update.message.reply_text(
            "📭 *No resting maker quotes.*\n\n"
            "MAKER re-quotes every cycle when a market is inside its window "
            f"({_cfg.MAKER_MIN_SECS_TO_CLOSE:.0f}s–{_cfg.MAKER_MAX_SECS_TO_CLOSE:.0f}s "
            "to close) and both books are readable. /why names the gate that "
            "is stopping it.",
            parse_mode="Markdown",
        )
        return

    now = time.time()
    lines = [f"📊 *Resting maker quotes* ({len(quotes)})\n"]
    for market_id, info in sorted(quotes.items()):
        age = now - float(info.get("placed_at") or now)
        spot_then = float(info.get("spot") or 0.0)
        legs = info.get("legs") or {}
        inv = float(info.get("inventory") or 0.0)
        expiry = max(0.0, _cfg.MAKER_ORDER_TIMEOUT - age)
        flag = "🔴" if expiry <= 0 else ("🟡" if age > _cfg.MAKER_QUOTE_MAX_AGE_SEC else "🟢")
        lines.append(
            f"\n{flag} `{market_id[:18]}` — {age:.0f}s old, withdraws in {expiry:.0f}s"
        )
        if spot_then:
            lines.append(f"   Oracle at placement: {spot_then:,.2f}")
        for outcome in ("YES", "NO"):
            leg = legs.get(outcome)
            if leg is None:
                continue
            price = float(getattr(leg, "price", 0.0) or 0.0)
            fv = float(getattr(leg, "fair_value", 0.0) or 0.0)
            lines.append(f"   {outcome}: bid *{price:.3f}* (fv {fv:.3f})")
        if inv:
            side = "YES" if inv > 0 else "NO"
            lines.append(
                f"   ⚖️ Long {abs(inv):.2f} {side} — next quote skews to complete the set"
            )
    text = "\n".join(lines)
    try:
        await update.message.reply_text(text[:3900], parse_mode="Markdown")
    except Exception as exc:
        if not _is_markdown_parse_error(exc):
            raise
        await update.message.reply_text(_markdown_to_plain(text)[:3900])


@_guard
async def cmd_analysis(update: Update, _ctx):
    import analysis as anal
    cid    = str(update.effective_chat.id)
    client = _user_clients.get(cid)
    if not client:
        await update.message.reply_text("Still starting up.")
        return
    report = await anal.full_report(client, cid)
    await update.message.reply_text(report, parse_mode="Markdown")

@_guard
async def cmd_settings(update: Update, _ctx):
    await update.message.reply_text(await _settings_text(str(update.effective_chat.id)), parse_mode="Markdown")

@_guard
async def cmd_strategies(update: Update, _ctx):
    cid = str(update.effective_chat.id)
    user = await asyncio.to_thread(_safe_get_user, cid)
    if not user:
        await update.message.reply_text("Use /start to connect.")
        return
    s = user.get("settings", {})
    active = set(s.get("strategies", []))
    lines = ["⚙️ *Bot Strategy Status*\n"]
    for strat in sorted(_VALID_STRATEGIES):
        icon, name = _STRAT_ICONS.get(strat, ("🔔", strat))
        if strat in active and strat not in config.PERMITTED_STRATEGIES:
            status = "🛑 SAVED BUT OPERATOR-BLOCKED"
        else:
            status = "✅ ACTIVE" if strat in active else "⚪ OFF"
        lines.append(f"{icon} *{strat}*: {status}")
    lines.append(
        "\n*To enable or set strategies:*\n"
        f"`/set strategies {' '.join(config.ACTIVE_STRATEGIES)}`"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@_guard
async def cmd_set(update: Update, _ctx):
    cid  = str(update.effective_chat.id)
    args = update.message.text.split()[1:]
    if len(args) < 2:
        await update.message.reply_text(
            "*Usage:*\n"
            "`/set assets BTC ETH SOL`\n"
            "`/set timeframes 15min 1h`\n"
            "`/set strategies TAKER MAKER`\n"
            "`/set risk 2`\n"
            "`/set mintrade 100`\n"
            "`/set maxtrade 5000`\n"
            "`/set maxexposure 20`\n"
            "`/set dailymultiplier 10`\n"
            "`/set dailytarget 1000`",
            parse_mode="Markdown",
        )
        return

    user = await asyncio.to_thread(_safe_get_user, cid)
    s    = user["settings"]
    key, vals = args[0].lower(), args[1:]
    msg = ""

    if key == "assets":
        bad = [v for v in vals if v.upper() not in _VALID_ASSETS]
        if bad:
            await update.message.reply_text(f"Unknown: {bad}\nValid: {', '.join(sorted(_VALID_ASSETS))}"); return
        s["assets"] = [v.upper() for v in vals]; msg = f"Assets: {s['assets']}"
    elif key == "timeframes":
        bad = [v for v in vals if v.lower() not in _VALID_TIMEFRAMES]
        if bad:
            await update.message.reply_text(f"Unknown: {bad}\nValid: {', '.join(sorted(_VALID_TIMEFRAMES))}"); return
    elif key in ("strategies", "strategy", "strat"):
        norm_strats = [_normalize_strat(v) for v in vals]
        bad = [v for v in norm_strats if v not in _VALID_STRATEGIES]
        if bad:
            await update.message.reply_text(f"Unknown: {bad}\nValid: {', '.join(sorted(_VALID_STRATEGIES))}"); return
        blocked = [v for v in norm_strats if v not in config.PERMITTED_STRATEGIES]
        if blocked:
            await update.message.reply_text(
                "Blocked by the operator safety policy: " + ", ".join(blocked)
            )
            return
        s["strategies"] = norm_strats; msg = f"Strategies: {s['strategies']}"
    elif key == "risk":
        try:
            pct = float(vals[0])
            max_risk_pct = config.MAX_TRADE_RISK * 100
            if not 0.1 <= pct <= max_risk_pct: raise ValueError
            s["risk_pct"] = pct; msg = f"Risk per trade: {pct}%"
        except ValueError:
            await update.message.reply_text(
                f"Risk must be 0.1–{config.MAX_TRADE_RISK * 100:g}% under the operator policy."
            ); return
    elif key == "mintrade":
        try:
            amt = float(vals[0])
            if amt < MIN_TRADE_NGN:
                await update.message.reply_text(f"Minimum is ₦{MIN_TRADE_NGN} (Bayse platform limit)."); return
            s["mintrade"] = amt; msg = f"Min trade: ₦{amt:,.0f}"
        except ValueError:
            await update.message.reply_text("Enter a number."); return
    elif key == "maxtrade":
        try:
            amount = float(vals[0])
            if amount < MIN_TRADE_NGN:
                raise ValueError
            s["maxtrade"] = amount; msg = f"Max trade: ₦{amount:,.0f}"
        except ValueError:
            await update.message.reply_text(f"Maximum trade must be at least ₦{MIN_TRADE_NGN}."); return
    elif key == "maxexposure":
        try:
            pct = float(vals[0])
            max_exposure_pct = config.MAX_PORTFOLIO_EXPOSURE * 100
            if not 1 <= pct <= max_exposure_pct: raise ValueError
            s["maxexposure"] = pct; msg = f"Max exposure: {pct}%"
        except ValueError:
            await update.message.reply_text(
                f"Exposure must be 1–{config.MAX_PORTFOLIO_EXPOSURE * 100:g}% under the operator policy."
            ); return
    elif key == "dailymultiplier":
        try:
            m = float(vals[0])
            if not 0 < m <= 100: raise ValueError
            s["daily_multiplier"] = m; s["daily_target_ngn"] = 0
            msg = f"Daily target: {m}% of starting balance"
        except ValueError:
            await update.message.reply_text("Enter 1–100."); return
    elif key == "dailytarget":
        try:
            s["daily_target_ngn"] = float(vals[0]); s["daily_multiplier"] = 0
            msg = f"Daily target: ₦{s['daily_target_ngn']:,.0f} fixed"
        except ValueError:
            await update.message.reply_text("Enter a number."); return
    else:
        await update.message.reply_text(f"Unknown setting `{key}`."); return

    s["mode"] = "custom"
    await asyncio.to_thread(database.update_settings, cid, s)
    log.info(f"[{cid}] /set {key} → {msg}")
    await update.message.reply_text(f"✅ {msg}", parse_mode="Markdown")

@_guard
async def cmd_pause(update: Update, _ctx):
    cid = str(update.effective_chat.id)
    await _set_paused(cid, True)
    log.info(f"[{cid}] PAUSED via /pause")
    await update.message.reply_text("⏸ Trading paused. /resume to restart.")

@_guard
async def cmd_resume(update: Update, _ctx):
    cid = str(update.effective_chat.id)
    await update.message.reply_text(await _apply_resume(cid, via="/resume"), parse_mode="Markdown")

@_guard
async def cmd_learning(update: Update, _ctx):
    cid = str(update.effective_chat.id)
    await update.message.reply_text("🧠 Running intelligence cycle…")
    _, report = await learner.run_learning(cid)
    await update.message.reply_text(report, parse_mode="Markdown")

@_guard
async def cmd_resetlearning(update: Update, _ctx):
    from datetime import datetime, timezone
    cid  = str(update.effective_chat.id)
    user = await asyncio.to_thread(_safe_get_user, cid)
    s    = user["settings"]
    s["learned"] = {}
    s["reset_learning_at"] = datetime.now(timezone.utc).isoformat()
    # Resetting model memory must not silently resume trading or broaden the
    # account's market universe.
    s["strategies"] = list(config.DEFAULT_STRATEGIES)
    s["timeframes"] = list(config.DEFAULT_TIMEFRAMES)
    s["assets"]     = list(config.DEFAULT_ASSETS)
    await asyncio.to_thread(database.update_settings, cid, s)
    await asyncio.to_thread(database.invalidate_user_cache, cid)
    risk = _user_risks.get(cid)
    if risk:
        risk.paused = bool(s.get("paused", True))
        risk.peak_balance = 0
    strat_list = ', '.join(name.replace('_', '\\_') for name in config.DEFAULT_STRATEGIES)
    log.info(f"[{cid}] /resetlearning — cleared learned; safe strategies={config.DEFAULT_STRATEGIES}")
    await update.message.reply_text(
        "🔄 *Learning Reset Complete*\n\n"
        "✅ All certainty/size multipliers reset\n"
        "✅ All strategy suspensions cleared\n"
        "✅ Trade history horizon reset to now\n"
        f"✅ Safe strategy set: {strat_list}\n\n"
        f"Trading remains {'paused' if s.get('paused', True) else 'active'}.",
        parse_mode="Markdown",
    )

@_guard
async def cmd_learnstats(update: Update, _ctx):
    cid  = str(update.effective_chat.id)
    rows = await asyncio.to_thread(database.recent_stats, cid, days=7)
    if not rows:
        await update.message.reply_text("No resolved trades in the last 7 days.")
        return
    lines = ["📈 *7-Day Performance*\n"]
    for r in sorted(rows, key=lambda x: -(x.get("total_pnl") or 0)):
        icon = "✅" if r["win_rate"] >= 0.55 else ("⚠️" if r["win_rate"] >= 0.48 else "❌")
        pnl  = r.get("total_pnl") or 0
        strat = r['strategy'].replace('_', '\\_')
        lines.append(f"{icon} {strat}/{r['asset']}/{r['timeframe']}: {r['win_rate']:.0%} WR ({r['total']} trades) ₦{pnl:,.0f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@_guard
async def cmd_why(update: Update, _ctx):
    """Answer one question with evidence: why has this account not traded?"""
    import time
    import stall
    import config

    cid = str(update.effective_chat.id)
    equity = 0.0
    min_viable = 0.0
    feed_age = None
    risk = _user_risks.get(cid)
    if risk is not None:
        try:
            equity = float(risk.current_free_cash or 0.0) + risk.deployed()
        except Exception:
            equity = 0.0
    resting_now = None
    try:
        import bot as _bot
        feed_age = _bot._worst_feed_age_sec(time.time())
        min_viable = float(getattr(_bot, "_MIN_VIABLE_BALANCE", 0.0))
        resting_now = _bot._resting_order_count(risk)
    except Exception:
        pass
    text = stall.format_report(
        cid,
        markdown=True,
        equity=equity,
        min_viable=min_viable,
        feed_age_sec=feed_age,
        eval_max_age_sec=float(config.STALL_EVAL_MAX_AGE_SEC),
        resting_now=resting_now,
    )
    try:
        await update.message.reply_text(text[:3900], parse_mode="Markdown")
    except Exception as exc:
        # Never leave /why unanswered because of a formatting slip.
        if not _is_markdown_parse_error(exc):
            raise
        await update.message.reply_text(_markdown_to_plain(text)[:3900])


@_guard
async def cmd_debug(update: Update, _ctx):
    import feeds
    import feeds_direct
    import config
    import learner
    import time
    cid   = str(update.effective_chat.id)
    _esc = lambda s: s.replace('_', '\\_')  # escape underscores for Telegram Markdown
    lines = ["🔍 *Strategy Debug*\n"]

    # Show current learned state
    learned = learner.get_learned_overrides(cid)
    suspended = learned.get("suspended_strategies", [])
    active = learned.get("strategies", config.ACTIVE_STRATEGIES)

    lines.append(f"*Active strategies:* {', '.join(_esc(s) for s in active)}")
    if suspended:
        lines.append(f"⛔ *SUSPENDED:* {', '.join(_esc(s) for s in suspended)}")
    else:
        lines.append("✅ No strategies suspended")

    # Show multipliers
    cmults = learned.get("certainty_multipliers", {})
    smults = learned.get("size_multipliers", {})
    lines.append("\n📊 *Multipliers:*")
    for strat in config.ACTIVE_STRATEGIES:
        cm = cmults.get(strat, 1.0)
        sm = smults.get(strat, 1.0)
        flag = "⚠️" if cm < 0.90 or sm < 0.70 else "✅"
        lines.append(f"  {flag} {_esc(strat)}: cert×{cm:.2f} size×{sm:.2f}")

    # Show feed status
    lines.append(f"\n📡 *Feed Status:*")
    for asset in ["BTC", "ETH", "SOL"]:
        p, t = feeds_direct.get_direct_price(asset)
        age = time.time() - t if t else 999
        status = "🟢" if age < 10 else ("🟡" if age < 60 else "🔴")
        if p:
            lines.append(f"  {status} {asset}: ${p:,.2f} ({age:.0f}s ago)")
        else:
            lines.append(f"  🔴 {asset}: NO DATA")

    # Show risk state
    risk = _user_risks.get(cid)
    if risk:
        lines.append(f"\n⚖️ *Risk:*")
        lines.append(f"  Paused: {'YES ⛔' if risk.paused else 'NO ✅'}")
        lines.append(f"  Probation: {risk.probation_trades_left} trades left")
        lines.append(f"  Deployed: ₦{risk.deployed():,.0f}")
        lines.append(f"  Open positions: {len(risk.open_positions)}")
        lines.append(f"  Daily PnL: ₦{risk.daily_realized_pnl:+,.0f}")

    # Show market count
    user = await asyncio.to_thread(_safe_get_user, cid)
    s    = user.get("settings", {}) if user else {}
    ua, ut = s.get("assets", []), s.get("timeframes", [])
    rel  = [m for m in _active_markets if m.get("asset") in ua and m.get("timeframe") in ut]
    lines.append(f"\n📊 *Markets:* {len(rel)} matching / {len(_active_markets)} total")
    for m in rel[:5]:
        lines.append(
            f"  {m['asset']} {m['timeframe']} | "
            f"{int(m.get('secs_to_close',0))}s | "
            f"Y={m.get('yes_price',0):.3f} N={m.get('no_price',0):.3f}"
        )

    # Show test mode
    if hasattr(config, 'TEST_MODE') and config.TEST_MODE:
        lines.append(f"\n🧪 *TEST MODE ON:* trades capped at ₦{config.TEST_MAX_TRADE_NGN}")

    # The verdict belongs here too: /debug used to describe the parts of the
    # system that are fine, and stay silent on the part that is not trading.
    try:
        import stall
        verdict_data = stall.verdict(
            cid, eval_max_age_sec=float(config.STALL_EVAL_MAX_AGE_SEC)
        )
        lines.append(f"\n🎯 *Current blocker:* {verdict_data['headline']}")
        lines.append(f"`{verdict_data['code']}`")
        if verdict_data.get("action"):
            lines.append(f"→ {verdict_data['action']}")
        lines.append("\n_/why gives the full breakdown (gate counters, skips, recent events)_")
    except Exception as why_err:
        log.debug(f"[{cid}] /debug verdict unavailable: {why_err}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@_guard
async def cmd_disconnect(update: Update, _ctx):
    cid = str(update.effective_chat.id)
    await asyncio.to_thread(database.deactivate, cid)
    _user_clients.pop(cid, None)
    _user_risks.pop(cid, None)
    _user_daily.pop(cid, None)
    log.info(f"[{cid}] DISCONNECTED")
    await update.message.reply_text("🔌 Disconnected. Trade history preserved. /start to reconnect.")

async def cmd_help(update: Update, _ctx):
    await update.message.reply_text(
        "*Commands*\n\n"
        "*Trading*\n"
        "/start — connect account\n"
        "/status — balance, PnL, positions\n"
        "/trades — last 10 trades\n"
        "/quotes — resting maker quotes and their age\n"
        "/markets — active markets\n\n"
        "*Controls*\n"
        "/pause — stop trading\n"
        "/resume — resume trading\n"
        "/mode — switch risk mode\n"
        "/set — change a setting\n"
        "/strategies — which strategies are on\n\n"
        "*Diagnostics*\n"
        "/why — the single reason nothing has traded, with evidence\n"
        "/debug — strategy, feed and risk state\n"
        "/analysis — full performance report\n"
        "/learning — run AI learning cycle now\n"
        "/resetlearning — clear learned overrides\n"
        "/learnstats — 7-day win rates\n\n"
        "*Account*\n"
        "/settings — current config\n"
        "/balance — wallet balance\n"
        "/wallet — raw wallet payload\n"
        "/rekey — update API keys\n"
        "/disconnect — remove account",
        parse_mode="Markdown",
    )


# ── Mode presets ──────────────────────────────────────────────────────────────

_MODES = {
    "mode_safe": {
        "label": "🟢 *Safe mode applied.*",
        "settings": {
            "mode": "safe", "assets": list(config.DEFAULT_ASSETS),
            "timeframes": list(config.DEFAULT_TIMEFRAMES),
            "strategies": list(config.DEFAULT_STRATEGIES),
            "risk_pct": min(0.5, config.MAX_TRADE_RISK * 100),
            "mintrade": MIN_TRADE_NGN,
            "maxexposure": min(5.0, config.MAX_PORTFOLIO_EXPOSURE * 100),
            "daily_multiplier": 3,
        },
    },
    "mode_balanced": {
        "label": "🔵 *Balanced mode applied.*",
        "settings": {
            "mode": "balanced", "assets": list(config.DEFAULT_ASSETS),
            "timeframes": list(config.DEFAULT_TIMEFRAMES),
            "strategies": list(config.DEFAULT_STRATEGIES),
            "risk_pct": min(1.0, config.MAX_TRADE_RISK * 100),
            "mintrade": MIN_TRADE_NGN,
            "maxexposure": min(10.0, config.MAX_PORTFOLIO_EXPOSURE * 100),
            "daily_multiplier": 3,
        },
    },
    "mode_aggressive": {
        "label": "🟠 *Aggressive mode applied within operator limits.*",
        "settings": {
            "mode": "aggressive", "assets": list(config.DEFAULT_ASSETS),
            "timeframes": list(config.DEFAULT_TIMEFRAMES),
            "strategies": list(config.ACTIVE_STRATEGIES),
            "risk_pct": min(2.0, config.MAX_TRADE_RISK * 100),
            "mintrade": MIN_TRADE_NGN,
            "maxexposure": config.MAX_PORTFOLIO_EXPOSURE * 100,
            "daily_multiplier": 3,
        },
    },
}


@_guard
async def cmd_mode(update: Update, _ctx):
    kb = [
        [InlineKeyboardButton("🟢 Safe",       callback_data="mode_safe"),
         InlineKeyboardButton("🔵 Balanced",   callback_data="mode_balanced")],
        [InlineKeyboardButton("🟠 Aggressive", callback_data="mode_aggressive")],
    ]
    await update.message.reply_text(
        "⚙️ *Choose a Risk Mode*\n\nEach mode sets a full recommended config.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── Text builders ─────────────────────────────────────────────────────────────

async def _status_text(cid: str) -> str:
    client = _user_clients.get(cid)
    if not client:
        return "Still starting up."
    try:
        free_cash = await client.get_balance_ngn()
    except Exception:
        return "Could not fetch balance."

    risk  = _user_risks.get(cid)
    user  = await asyncio.to_thread(_safe_get_user, cid)
    s     = user["settings"] if user else {}

    dd = deployed = 0.0; n_pos = 0
    if risk:
        n_pos    = len(risk.open_positions)
        deployed = sum(p.get("amount_ngn", 0) for p in risk.open_positions.values())

    # CRITICAL: get_balance_ngn() returns free/uncommitted cash only — it
    # does NOT include capital currently locked in open positions. But
    # day["start_balance"] (set in bot.py's _daily()) is always recorded as
    # full EQUITY (free_cash + deployed). Comparing free cash directly
    # against an equity baseline understated "today's profit" and
    # overstated "drawdown from peak" by exactly the deployed amount —
    # every single time the user checked /status while holding a position,
    # which based on production logs is most of the time (0-5 open
    # positions is the normal state, not the exception).
    equity = free_cash + deployed

    day    = _user_daily.get(cid) or s.get("daily_state", {})
    profit = equity - day.get("start_balance", equity)
    target = _calc_target(s, day.get("start_balance", equity))

    if risk and risk.peak_balance:
        dd = max(0, (risk.peak_balance - equity) / risk.peak_balance)

    stats = await asyncio.to_thread(database.all_time_stats, cid)
    lines = [
        "📊 *Bot Status*\n",
        f"Total equity: ₦{equity:,.2f}",
        f"Free cash: ₦{free_cash:,.2f}",
        f"Today's profit: ₦{profit:+,.2f}",
    ]
    if target > 0:
        lines.append(f"Daily target: ₦{target:,.0f} ({min(profit/target*100,100) if target else 0:.0f}% done)")
    lines += [
        f"Drawdown from peak: {dd:.1%}",
        f"Open positions: {n_pos} (₦{deployed:,.0f} deployed)",
        "",
        f"All-time: {stats['wins']}/{stats['total']} wins "
        f"({stats['win_rate']:.0%} WR) ₦{stats['total_pnl']:+,.0f}",
        "",
        f"Status: {'⏸ Paused' if s.get('paused') else '🟢 Active'}",
        f"Mode: *{s.get('mode','balanced').title()}*",
    ]
    return "\n".join(lines)


async def _balance_text(cid: str) -> str:
    client = _user_clients.get(cid)
    if not client:
        return "Still starting up."
    try:
        return f"💰 Balance: ₦{(await client.get_balance_ngn()):,.2f}"
    except Exception:
        return "Could not fetch balance right now. Please try again shortly."


async def _markets_text(cid: str) -> str:
    user = await asyncio.to_thread(_safe_get_user, cid)
    if not user:
        return "Not connected."
    s   = user["settings"]
    rel = [m for m in _active_markets
           if m.get("asset") in s.get("assets", [])
           and m.get("timeframe") in s.get("timeframes", [])]
    if not rel:
        return "No active markets matching your settings."
    lines = ["🏦 *Active Markets*\n"]
    for m in rel[:15]:
        mins = (m.get("secs_to_close") or 0) // 60
        lines.append(
            f"{'🟢' if m.get('status')=='open' else '🔴'} "
            f"{m['asset']} {m['timeframe']} | "
            f"YES:{m.get('yes_price',0):.3f} NO:{m.get('no_price',0):.3f} | {mins}m left"
        )
    return "\n".join(lines)


async def _settings_text(cid: str) -> str:
    user = await asyncio.to_thread(_safe_get_user, cid)
    if not user:
        return "Not connected."
    s   = user["settings"]
    tgt = f"₦{s['daily_target_ngn']:,.0f}" if s.get("daily_target_ngn", 0) > 0 else f"{s.get('daily_multiplier',10)}% of balance"
    return (
        "⚙️ *Settings*\n\n"
        f"Mode:         {s.get('mode','balanced')}\n"
        f"Assets:       {s.get('assets')}\n"
        f"Timeframes:   {s.get('timeframes')}\n"
        f"Strategies:   {s.get('strategies')}\n"
        f"Risk/trade:   {s.get('risk_pct',2)}%\n"
        f"Min trade:    ₦{s.get('mintrade',MIN_TRADE_NGN):,.0f}\n"
        f"Max trade:    ₦{s.get('maxtrade',5000):,.0f}\n"
        f"Max exposure: {s.get('maxexposure',20)}%\n"
        f"Daily target: {tgt}\n"
        f"Status:       {'⏸ Paused' if s.get('paused') else '🟢 Active'}"
    )


def _calc_target(s: dict, start: float) -> float:
    if s.get("daily_target_ngn", 0) > 0:
        return float(s["daily_target_ngn"])
    return start * s.get("daily_multiplier", 10) / 100


async def _set_paused(cid: str, paused: bool):
    """Persist the manual pause switch.

    A manual pause carries an explicit reason so the trading-day rollover can
    tell it apart from an expiring daily stop. Session pauses expire on their
    own; an operator's /pause never does.
    """
    user = await asyncio.to_thread(_safe_get_user, cid)
    if user:
        s = user["settings"]
        s["paused"] = paused
        if paused:
            s["paused_reason"] = "manual"
        else:
            s.pop("paused_reason", None)
        risk = _user_risks.get(cid)
        if risk is not None:
            risk.paused = bool(paused)
        try:
            import stall

            stall.note_state(cid, paused=bool(paused),
                             paused_reason="manual" if paused else "",
                             manual_pause=True if paused else None)
        except Exception as telemetry_err:
            # The pause switch must work even if diagnostics cannot.
            log.warning(f"[{cid}] pause-state telemetry failed: {telemetry_err}")
        await asyncio.to_thread(database.update_settings, cid, s)
        await asyncio.to_thread(database.invalidate_user_cache, cid)
        # Also bust bot.py's own user cache so _evaluate_single_user
        # picks up the new paused state on the very next heartbeat tick
        import bot as _bot
        _bot._active_users_cache_time = 0.0


async def _clear_daily(cid: str):
    _user_daily.pop(cid, None)


async def _apply_resume(cid: str, *, via: str = "/resume") -> str:
    """Lift every restriction one explicit resume is supposed to lift.

    Clearing the pause flag alone was not enough: the daily loss stop, the
    daily target and the drawdown stop are all recomputed from a baseline
    captured at the start of the trading day, so the account was re-paused on
    the very next cycle and the operator's override was silently discarded --
    while the stall report told them "/resume overrides it explicitly".

    The baseline itself is moved (see ``bot.reset_session_restrictions``), so
    the account restarts with a full, bounded risk budget from its current
    balance, and the reply says exactly that instead of a bare "resumed".
    """
    import bot as _bot

    await _set_paused(cid, False)
    await _clear_daily(cid)
    try:
        summary = await asyncio.to_thread(_bot.reset_session_restrictions, cid, "manual_resume")
    except Exception as err:
        log.error(f"[{cid}] session reset failed: {err}", exc_info=True)
        summary = {"equity": 0.0, "booked_pnl": 0.0, "cleared_cooldowns": 0}
    log.info(f"[{cid}] RESUMED via {via}")

    booked = float(summary.get("booked_pnl") or 0.0)
    lines = ["▶️ *Trading resumed*", ""]
    if booked:
        lines.append(
            f"Today's result so far ({booked:+,.0f}) is now the baseline: the daily "
            "loss limit and the daily target both measure from this point, so the "
            "stop you just overrode cannot re-trigger on it."
        )
    else:
        lines.append("The session baseline and the daily stops were reset from the current balance.")
    if summary.get("cleared_cooldowns"):
        lines.append(f"Cleared {summary['cleared_cooldowns']} trade cooldown(s).")
    if summary.get("cleared_suspensions"):
        # Strategy names are single uppercase words; no Markdown escaping is
        # needed here and the debug helper that owns `_esc` is out of scope.
        names = ", ".join(str(s) for s in summary["cleared_suspensions"])
        lines.append(f"Lifted learner suspension(s) on: {names}.")
    lines.append("")
    lines.append(
        "Open positions were never affected — they are monitored regardless of the pause."
    )
    return "\n".join(lines)


# ── Notifications ─────────────────────────────────────────────────────────────

def _code_span(text: str) -> str:
    """Render free text (gate codes, exchange reasons) as a Markdown code span.

    Legacy Markdown forbids nested entities, so an escaped ``\\_`` inside an
    ``_italic_`` entity is a parse error: every MAKER notification carried
    ``spread_capture`` in its reason and was therefore always downgraded to the
    one-line fallback. Inside a code span underscores are literal.
    """
    return "`" + str(text or "").replace("`", "'") + "`"


def _markdown_to_plain(text: str) -> str:
    """Best-effort plain rendering of a legacy-Markdown message."""
    for escaped in ("\\_", "\\*", "\\`", "\\["):
        text = text.replace(escaped, escaped[1])
    return text.replace("*", "").replace("`", "")


def _is_markdown_parse_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "parse" in message and "entit" in message


async def send_message(app: Application, chat_id: str, text: str, **kwargs):
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        if kwargs.get("parse_mode") and _is_markdown_parse_error(e):
            # A formatting slip must never drop an alert. The trading-stall
            # report quotes raw gate codes; one unbalanced underscore made
            # Telegram reject the whole message and it was only logged.
            plain_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
            try:
                await app.bot.send_message(
                    chat_id=chat_id, text=_markdown_to_plain(text), **plain_kwargs
                )
                log.warning(f"send_message → {chat_id}: Markdown rejected ({e}); sent as plain text")
                return
            except Exception as e2:
                log.warning(f"send_message → {chat_id}: plain-text retry failed: {e2}")
                return
        log.warning(f"send_message → {chat_id}: {e}")


async def notify_trade(app, cid: str, sig, amount: float, engine: str = "AMM"):
    """
    Differentiated Telegram notification per strategy.
    Custom icons, titles, price info, and order-type badges.
    """
    strat = sig.strategy.upper()
    strat_meta = {
        "TAKER": ("🎯", "*TAKER* (Crossing the spread)"),
        "MAKER": (
            "📊",
            "*MAKER* (Two-sided quote)"
            if getattr(sig, "is_multi_leg", lambda: False)() else "*MAKER* (Single leg)",
        ),
    }
    icon_strat, title_strat = strat_meta.get(strat, ("🔔", f"*{strat} Trade*"))
    
    dir_icon = "🎯" if sig.outcome.upper() in ("DUAL_LIMIT", "ARB") else ("⬆️" if sig.outcome.upper() in ("YES", "UP") else "⬇️")
    reason = (getattr(sig, "reason", "") or "")[:300]
    market_price = getattr(sig, "market_price", 0.0)
    win_prob = getattr(sig, "win_prob", sig.certainty)
    maker_limit = strat == "MAKER" and engine == "CLOB_LIMIT"
    # A resting LIMIT is an order, not a fill — say so, so a quote that later
    # expires unfilled does not read as a trade that happened.
    status_line = (
        "Status: resting post-only bid — *not filled yet*\n"
        if engine == "CLOB_LIMIT" else ""
    )
    probability_line = (
        f"Signal score (heuristic): *{sig.certainty:.0%}*\n"
        f"Model win estimate: *{win_prob:.1%}* (not an observed win rate)\n"
        if maker_limit else
        f"Certainty: *{sig.certainty:.0%}* (Prob: {win_prob:.1%})\n"
    )

    msg = (
        f"{icon_strat} {title_strat}\n"
        f"Asset: *{sig.asset} {sig.timeframe}*\n"
        f"Direction: {dir_icon} *{sig.outcome}*\n"
        f"Size: *₦{amount:,.0f}* @ price *{market_price:.3f}*\n"
        f"{status_line}"
        f"{probability_line}"
        + (_code_span(reason) if reason else "")
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception:
        # Fallback — plain text if markdown parsing fails
        try:
            fallback_probability = (
                f"signal score {sig.certainty:.0%} (heuristic); "
                f"model win estimate {win_prob:.1%} (not an observed win rate)"
                if maker_limit else f"Cert: {sig.certainty:.0%}"
            )
            plain = (
                f"{icon_strat} [{strat}] {sig.asset} {sig.timeframe} {dir_icon} {sig.outcome} "
                f"₦{amount:,.0f} @ {market_price:.3f} ({fallback_probability})"
                + (" — resting bid, not filled yet" if engine == "CLOB_LIMIT" else "")
            )
            await app.bot.send_message(chat_id=cid, text=plain)
        except Exception as e:
            log.error(f"notify_trade failed: {e}")


async def notify_win(app, cid, _mid, asset, tf, strat, pnl):
    app = app or _bot_app
    if not app:
        log.warning(f"notify_win dropped for {cid}: no Telegram app available")
        return
    try:
        pnl_val = float(pnl or 0.0)
    except (ValueError, TypeError):
        pnl_val = 0.0
    icon, name = _STRAT_ICONS.get((strat or "").upper(), ("🔔", strat or "Trade"))
    _esc = lambda s: (s or "").replace("_", "\\_").replace("*", "\\*")
    msg = (
        f"🟢 *WIN* {icon} ({_esc(name)})\n"
        f"Market: *{_esc(asset)} {_esc(tf)}*\n"
        f"Profit: *+₦{pnl_val:,.2f}*"
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.debug(f"notify_win markdown failed, falling back to plain text: {e}")
        try:
            await app.bot.send_message(chat_id=cid, text=f"🟢 WIN | {name} | {asset} {tf} | +₦{pnl_val:,.2f}")
        except Exception as e2:
            log.error(f"notify_win failed completely for {cid}: {e2}")

async def notify_loss(app, cid, _mid, asset, tf, strat, pnl):
    app = app or _bot_app
    if not app:
        log.warning(f"notify_loss dropped for {cid}: no Telegram app available")
        return
    try:
        pnl_val = float(pnl or 0.0)
    except (ValueError, TypeError):
        pnl_val = 0.0
    icon, name = _STRAT_ICONS.get((strat or "").upper(), ("🔔", strat or "Trade"))
    _esc = lambda s: (s or "").replace("_", "\\_").replace("*", "\\*")
    msg = (
        f"🔴 *LOSS* {icon} ({_esc(name)})\n"
        f"Market: *{_esc(asset)} {_esc(tf)}*\n"
        f"PnL: *-₦{abs(pnl_val):,.2f}*"
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.debug(f"notify_loss markdown failed, falling back to plain text: {e}")
        try:
            await app.bot.send_message(chat_id=cid, text=f"🔴 LOSS | {name} | {asset} {tf} | -₦{abs(pnl_val):,.2f}")
        except Exception as e2:
            log.error(f"notify_loss failed completely for {cid}: {e2}")

async def notify_fill(app, cid, pos, shares, price):
    """Notify user when a resting limit order is filled on the exchange."""
    app = app or _bot_app
    strat   = (pos or {}).get("strategy", "MAKER")
    asset   = (pos or {}).get("asset", "?")
    tf      = (pos or {}).get("timeframe", "")
    outcome = (pos or {}).get("outcome", "?")
    amount_ngn = float((pos or {}).get("amount_ngn") or 0.0)
    if not app:
        log.warning(f"notify_fill dropped for {cid}: no Telegram app available")
        return
    try:
        price_val = float(price or 0.0)
        amt_val = float(amount_ngn or 0.0)
    except (ValueError, TypeError):
        price_val = 0.0
        amt_val = 0.0
    icon, name = _STRAT_ICONS.get((strat or "").upper(), ("📊", strat or "MAKER"))
    _esc = lambda s: (s or "").replace("_", "\\_").replace("*", "\\*")
    msg = (
        f"⚡ *Limit Order Filled* {icon}\n"
        f"Strategy: *{_esc(name)}*\n"
        f"Market: *{_esc(asset)} {_esc(tf)}* (*{_esc(outcome)}*)\n"
        f"Fill Price: *{price_val:.3f}*\n"
        f"Shares: *{float(shares or 0.0):.2f}*\n"
        f"Amount: *₦{amt_val:,.0f}*\n"
        f"_Matched by a taker on the CLOB — no maker fee. The opposite leg is "
        f"now more valuable: the next quote skews to complete the set._"
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.debug(f"notify_fill markdown failed, falling back to plain text: {e}")
        try:
            await app.bot.send_message(chat_id=cid, text=f"⚡ FILLED | {name} | {asset} {tf} {outcome} | ₦{amt_val:,.0f} @ {price_val:.3f}")
        except Exception as e2:
            log.error(f"notify_fill failed completely for {cid}: {e2}")


async def notify_unfilled(app, cid, strat, asset, tf, outcome, amount_ngn):
    """Notify user when a FAK/limit order was cancelled with zero fill.
    This is NOT a loss — no money was deducted. The position was never opened."""
    app = app or _bot_app
    if not app:
        log.warning(f"notify_unfilled dropped for {cid}: no Telegram app available")
        return
    try:
        amt_val = float(amount_ngn or 0.0)
    except (ValueError, TypeError):
        amt_val = 0.0
    icon, name = _STRAT_ICONS.get((strat or "").upper(), ("🔔", strat or "Trade"))
    _esc = lambda s: (s or "").replace("_", "\\_").replace("*", "\\*")
    msg = (
        f"⚪ *Unfilled Order — No Loss*\n"
        f"{icon} {_esc(name)} | {_esc(asset)} {_esc(tf)} {_esc(outcome)}\n"
        f"₦{amt_val:,.0f} was *not* deducted — order cancelled before fill.\n"
        f"_The market moved before execution. Capital preserved._"
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.debug(f"notify_unfilled markdown failed, falling back to plain text: {e}")
        try:
            await app.bot.send_message(chat_id=cid, text=f"⚪ UNFILLED | {name} | {asset} {tf} {outcome} | ₦{amt_val:,.0f} returned, no loss")
        except Exception as e2:
            log.error(f"notify_unfilled failed completely for {cid}: {e2}")

async def notify_set_burned(app, cid, asset, tf, sets: float, cost_ngn: float,
                            proceeds_ngn: float, pnl_ngn: float, *,
                            structural: bool = False):
    """A complete set was burned and the lock was realised.

    A set settles to 1.00 whichever outcome wins, so paying less than 1.00
    for both legs is profit that does not depend on the forecast. That makes
    it the only trade this bot makes whose result is known at entry, and it
    is worth telling the operator about in those terms.
    """
    app = app or _bot_app
    if not app:
        log.warning(f"notify_set_burned dropped for {cid}: no Telegram app available")
        return
    _num = lambda v, d=0.0: v if isinstance(v, (int, float)) else d
    sets, cost = _num(sets), _num(cost_ngn)
    proceeds, pnl = _num(proceeds_ngn), _num(pnl_ngn)
    _esc = lambda s: (s or "").replace("_", "\\_").replace("*", "\\*")
    kind = "Structural take" if structural else "Maker pair completed"
    edge = (proceeds / cost - 1.0) if cost > 0 else 0.0
    msg = (
        f"🔥 *Complete set burned*\n"
        f"Asset: *{_esc(asset)} {_esc(tf)}*\n"
        f"Source: {_esc(kind)}\n"
        f"Sets: *{sets:.2f}* — cost ₦{cost:,.0f} → payout ₦{proceeds:,.0f}\n"
        f"Locked edge: *{edge:+.1%}*\n"
        f"PnL: *₦{pnl:+,.0f}*\n"
        f"_Direction-independent: a set pays 1.00 whichever outcome resolves._"
    )
    plain = (
        f"🔥 COMPLETE SET BURNED | {asset} {tf} | {kind} | "
        f"{sets:.2f} sets ₦{cost:,.0f} -> ₦{proceeds:,.0f} | PnL ₦{pnl:+,.0f}"
    )
    await send_message(app, cid, msg, parse_mode="Markdown")
    if not structural:
        return
    # send_message already falls back to plain text on a Markdown rejection,
    # so this only guards the case where the fallback itself logged a failure.
    log.debug(f"[{cid}] set burn plain summary: {plain}")


async def notify_order_resting(app, cid, strat, asset, tf, outcome, amount_ngn,
                               price: float = 0.0, reason: str = ""):
    """A passive order is still resting unfilled.

    Sent when a cancel could not be confirmed (the order may still be live and
    matchable), so the user is not left believing the position was closed.
    """
    app = app or _bot_app
    if not app:
        log.warning(f"notify_order_resting dropped for {cid}: no Telegram app available")
        return
    try:
        amt_val = float(amount_ngn or 0.0)
        price_val = float(price or 0.0)
    except (ValueError, TypeError):
        amt_val = 0.0
        price_val = 0.0
    icon, name = _STRAT_ICONS.get((strat or "").upper(), ("📊", strat or "MAKER"))
    _esc = lambda s: (s or "").replace("_", "\\_").replace("*", "\\*")
    detail = f"\nWhy: {_esc(reason)}" if reason else ""
    msg = (
        f"⏳ *Order Resting — Not Filled*\n"
        f"{icon} {_esc(name)} | {_esc(asset)} {_esc(tf)} {_esc(outcome)}\n"
        f"Limit: *{price_val:.3f}* | ₦{amt_val:,.0f}\n"
        f"_Still open on the exchange and cannot be cancelled for certain — it may "
        f"still fill, or expire unfilled. You will be told which.{detail}_"
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.debug(f"notify_order_resting markdown failed, falling back to plain text: {e}")
        try:
            await app.bot.send_message(
                chat_id=cid,
                text=f"⏳ RESTING (unfilled) | {name} | {asset} {tf} {outcome} | "
                     f"₦{amt_val:,.0f} @ {price_val:.3f}",
            )
        except Exception as e2:
            log.error(f"notify_order_resting failed completely for {cid}: {e2}")


async def notify_order_unconfirmed(app, cid, strat, asset, tf, outcome, amount_ngn,
                                   order_id: str = ""):
    """Bayse accepted an order but returned no fill confirmation.

    Fail-closed and deliberately loud: the wallet may or may not have been
    debited, so the user is asked to check the exchange portfolio.
    """
    app = app or _bot_app
    if not app:
        log.warning(f"notify_order_unconfirmed dropped for {cid}: no Telegram app available")
        return
    try:
        amt_val = float(amount_ngn or 0.0)
    except (ValueError, TypeError):
        amt_val = 0.0
    icon, name = _STRAT_ICONS.get((strat or "").upper(), ("🔔", strat or "Trade"))
    _esc = lambda s: (s or "").replace("_", "\\_").replace("*", "\\*")
    msg = (
        f"❓ *Order Unconfirmed — Please Verify*\n"
        f"{icon} {_esc(name)} | {_esc(asset)} {_esc(tf)} {_esc(outcome)} | ₦{amt_val:,.0f}\n"
        f"Order `{_esc(str(order_id))}` was accepted but no fill was confirmed.\n"
        f"_No position was recorded and this market is paused for new entries. "
        f"Check your Bayse portfolio before assuming the money is untouched._"
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.debug(f"notify_order_unconfirmed markdown failed, falling back to plain text: {e}")
        try:
            await app.bot.send_message(
                chat_id=cid,
                text=f"❓ UNCONFIRMED ORDER {order_id} | {name} | {asset} {tf} {outcome} | "
                     f"₦{amt_val:,.0f} — check the exchange portfolio",
            )
        except Exception as e2:
            log.error(f"notify_order_unconfirmed failed completely for {cid}: {e2}")


async def notify_order_rejected(app, cid, strat, asset, tf, outcome, amount_ngn,
                                reason: str = ""):
    """The exchange refused the order. Nothing was filled; say so explicitly."""
    app = app or _bot_app
    if not app:
        log.warning(f"notify_order_rejected dropped for {cid}: no Telegram app available")
        return
    try:
        amt_val = float(amount_ngn or 0.0)
    except (ValueError, TypeError):
        amt_val = 0.0
    icon, name = _STRAT_ICONS.get((strat or "").upper(), ("🔔", strat or "Trade"))
    _esc = lambda s: (s or "").replace("_", "\\_").replace("*", "\\*")
    msg = (
        f"🚫 *Order Rejected by Exchange*\n"
        f"{icon} {_esc(name)} | {_esc(asset)} {_esc(tf)} {_esc(outcome)} | ₦{amt_val:,.0f}\n"
        f"{_code_span((reason or 'no reason returned')[:300])}\n"
        f"_No fill occurred. Nothing was deducted for this order._"
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.debug(f"notify_order_rejected markdown failed, falling back to plain text: {e}")
        try:
            await app.bot.send_message(
                chat_id=cid,
                text=f"🚫 REJECTED | {name} | {asset} {tf} {outcome} | ₦{amt_val:,.0f} — "
                     f"{(reason or '')[:200]}",
            )
        except Exception as e2:
            log.error(f"notify_order_rejected failed completely for {cid}: {e2}")


async def notify_drawdown(app, cid, balance, peak, dd):
    await send_message(app, cid,
        f"⚠️ *Drawdown — Trading Paused*\n\n"
        f"Peak: ₦{peak:,.0f} → Now: ₦{balance:,.0f}\n"
        f"Drawdown: {dd:.1%}\n\n/resume to override.",
        parse_mode="Markdown")
