"""
Telegram bot — multi-user setup and control.
Fixes: engine label removed from notifications (always MARKET now),
       every command logged with chat_id for multi-user observability.
"""

import logging
import asyncio
from datetime import date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

import database
import learner
import config
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

_VALID_STRATEGIES = {"SNIPE", "ARB", "FRONTRUN", "CORRELATE"}
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


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [
        ("start",         cmd_start),
        ("status",        cmd_status),
        ("balance",       cmd_balance),
        ("trades",        cmd_trades),
        ("markets",       cmd_markets),
        ("analysis",      cmd_analysis),
        ("settings",      cmd_settings),
        ("set",           cmd_set),
        ("pause",         cmd_pause),
        ("resume",        cmd_resume),
        ("mode",          cmd_mode),
        ("learning",      cmd_learning),
        ("resetlearning", cmd_resetlearning),
        ("learnstats",    cmd_learnstats),
        ("debug",         cmd_debug),
        ("disconnect",    cmd_disconnect),
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
        log.warning("Telegram Conflict — ghost instance polling. Waiting.")
        return
    log.error(f"Telegram error: {err}", exc_info=err)


# ── Setup flow ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    log.info(f"[{cid}] /start")
    if await asyncio.to_thread(database.get_user, cid):
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
        msg = await update.message.reply_text("🔄 Connecting…")
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
            await update.message.reply_text(f"❌ *Connection failed*\n\n`{e}`\n\nCheck your keys and try /start again.", parse_mode="Markdown")
        return

    if not await asyncio.to_thread(database.get_user, cid):
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
    if not await asyncio.to_thread(database.get_user, cid):
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
        await _set_paused(cid, False)
        await _clear_daily(cid)
        log.info(f"[{cid}] RESUMED via button")
        await q.message.reply_text("▶️ Trading resumed.")
    elif d == "resetlearning":
        from datetime import datetime, timezone
        user = await asyncio.to_thread(database.get_user, cid)
        s    = user["settings"]
        s["learned"] = {}
        s["reset_learning_at"] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(database.update_settings, cid, s)
        log.info(f"[{cid}] /resetlearning via button")
        await q.message.reply_text("🔄 Learned settings cleared and trade history reset.")
    elif d in _MODES:
        mode_cfg = _MODES[d]
        user     = await asyncio.to_thread(database.get_user, cid)
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
        if not await asyncio.to_thread(database.get_user, cid):
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
async def cmd_trades(update: Update, _ctx):
    cid  = str(update.effective_chat.id)
    rows = await asyncio.to_thread(database.recent_trades, cid, limit=10)
    if not rows:
        await update.message.reply_text("No trades yet.")
        return
    lines = ["📋 *Last 10 Trades*\n"]
    for r in rows:
        icon = "✅" if r["won"] == 1 else ("❌" if r["won"] == 0 else "⏳")
        pnl  = f"₦{r['pnl_ngn']:+,.0f}" if r.get("pnl_ngn") is not None else "pending"
        lines.append(f"{icon} {r['strategy']} {r['asset']} {r['timeframe']} {r['outcome']} — {pnl}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@_guard
async def cmd_markets(update: Update, _ctx):
    await update.message.reply_text(await _markets_text(str(update.effective_chat.id)), parse_mode="Markdown")

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
async def cmd_set(update: Update, _ctx):
    cid  = str(update.effective_chat.id)
    args = update.message.text.split()[1:]
    if len(args) < 2:
        await update.message.reply_text(
            "*Usage:*\n"
            "`/set assets BTC ETH SOL`\n"
            "`/set timeframes 15min 1h`\n"
            "`/set strategies SNIPE ARB`\n"
            "`/set risk 2`\n"
            "`/set mintrade 100`\n"
            "`/set maxtrade 5000`\n"
            "`/set maxexposure 20`\n"
            "`/set dailymultiplier 10`\n"
            "`/set dailytarget 1000`",
            parse_mode="Markdown",
        )
        return

    user = await asyncio.to_thread(database.get_user, cid)
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
        s["timeframes"] = [v.lower() for v in vals]; msg = f"Timeframes: {s['timeframes']}"
    elif key == "strategies":
        bad = [v for v in vals if v.upper() not in _VALID_STRATEGIES]
        if bad:
            await update.message.reply_text(f"Unknown: {bad}\nValid: {', '.join(sorted(_VALID_STRATEGIES))}"); return
        s["strategies"] = [v.upper() for v in vals]; msg = f"Strategies: {s['strategies']}"
    elif key == "risk":
        try:
            pct = float(vals[0])
            if not 0.1 <= pct <= 10: raise ValueError
            s["risk_pct"] = pct; msg = f"Risk per trade: {pct}%"
        except ValueError:
            await update.message.reply_text("Risk must be 0.1–10."); return
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
            s["maxtrade"] = float(vals[0]); msg = f"Max trade: ₦{s['maxtrade']:,.0f}"
        except ValueError:
            await update.message.reply_text("Enter a number."); return
    elif key == "maxexposure":
        try:
            pct = float(vals[0])
            if not 5 <= pct <= 100: raise ValueError
            s["maxexposure"] = pct; msg = f"Max exposure: {pct}%"
        except ValueError:
            await update.message.reply_text("Exposure must be 5–100."); return
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
    await _set_paused(cid, False)
    await _clear_daily(cid)
    risk = _user_risks.get(cid)
    if risk:
        risk.paused = False
        risk.peak_balance = 0
    log.info(f"[{cid}] RESUMED via /resume")
    await update.message.reply_text("▶️ Trading resumed.")

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
    user = await asyncio.to_thread(database.get_user, cid)
    s    = user["settings"]
    s["learned"] = {}
    s["reset_learning_at"] = datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(database.update_settings, cid, s)
    log.info(f"[{cid}] /resetlearning")
    await update.message.reply_text("🔄 Learned settings cleared and trade history reset.")

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
        lines.append(f"{icon} {r['strategy']}/{r['asset']}/{r['timeframe']}: {r['win_rate']:.0%} WR ({r['total']} trades) ₦{pnl:,.0f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@_guard
async def cmd_debug(update: Update, _ctx):
    import feeds
    cid   = str(update.effective_chat.id)
    lines = ["🔍 *Debug*\n"]
    lines.append(f"*Spot prices:* {feeds.spot if feeds.spot else '⚠️ EMPTY'}")
    lines.append(f"*Active markets:* {len(_active_markets)}")
    user = await asyncio.to_thread(database.get_user, cid)
    s    = user.get("settings", {}) if user else {}
    ua, ut = s.get("assets", []), s.get("timeframes", [])
    rel  = [m for m in _active_markets if m.get("asset") in ua and m.get("timeframe") in ut]
    lines.append(f"*Matching your settings:* {len(rel)}")
    for m in rel[:5]:
        lines.append(
            f"  {m['asset']} {m['timeframe']} | "
            f"{int(m.get('secs_to_close',0))}s left | "
            f"YES={m.get('yes_price',0):.3f} NO={m.get('no_price',0):.3f} | "
            f"threshold={'✅' if m.get('threshold') else '❌ MISSING'}"
        )
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
        "/start — connect account\n"
        "/status — balance, PnL, positions\n"
        "/trades — last 10 trades\n"
        "/markets — active markets\n"
        "/analysis — full performance report\n"
        "/learning — run AI learning cycle now\n"
        "/resetlearning — clear learned overrides\n"
        "/learnstats — 7-day win rates\n"
        "/settings — current config\n"
        "/mode — switch risk mode\n"
        "/set — change a setting\n"
        "/pause — stop trading\n"
        "/resume — resume trading\n"
        "/debug — diagnose why trades aren't firing\n"
        "/disconnect — remove account",
        parse_mode="Markdown",
    )


# ── Mode presets ──────────────────────────────────────────────────────────────

_MODES = {
    "mode_safe": {
        "label": "🟢 *Safe mode applied.*",
        "settings": {
            "mode": "safe", "assets": ["BTC", "EURUSD", "GBPUSD"],
            # 1h kept only for FX (EURUSD/GBPUSD only exist at 1h granularity,
            # and ARB can still work there). 5min added for the BTC leg.
            "timeframes": ["5min", "15min", "1h"], "strategies": ["SNIPE", "ARB"],
            "risk_pct": 0.5, "mintrade": MIN_TRADE_NGN,
            "maxexposure": 15.0, "daily_multiplier": 5,
        },
    },
    "mode_balanced": {
        "label": "🔵 *Balanced mode applied.*",
        "settings": {
            "mode": "balanced", "assets": ["BTC", "ETH", "SOL"],
            # Pure fast-cycle focus — dropped 1h. SNIPE/FRONTRUN/CORRELATE are
            # all hard-restricted to 5min/15min in code now; this just keeps
            # the user-level filter consistent so ARB doesn't waste cycles
            # scanning 1h candles this account isn't otherwise using.
            "timeframes": ["5min", "15min"], "strategies": ["SNIPE", "ARB", "FRONTRUN"],
            "risk_pct": 1.5, "mintrade": MIN_TRADE_NGN,
            "maxexposure": 20.0, "daily_multiplier": 10,
        },
    },
    "mode_aggressive": {
        "label": "🟠 *Aggressive mode applied.*",
        "settings": {
            "mode": "aggressive", "assets": ["BTC", "ETH", "SOL"],
            "timeframes": ["5min", "15min"], "strategies": ["SNIPE", "ARB", "FRONTRUN", "CORRELATE"],
            "risk_pct": 3.0, "mintrade": MIN_TRADE_NGN,
            "maxexposure": 30.0, "daily_multiplier": 20,
        },
    },
    "mode_degen": {
        "label": "🔴 *Full Send mode applied.*",
        "settings": {
            "mode": "full_send", "assets": ["BTC", "ETH", "SOL"],
            "timeframes": ["5min", "15min"], "strategies": ["SNIPE", "ARB", "FRONTRUN", "CORRELATE"],
            "risk_pct": 5.0, "mintrade": MIN_TRADE_NGN,
            "maxexposure": 50.0, "daily_multiplier": 50,
        },
    },
}


@_guard
async def cmd_mode(update: Update, _ctx):
    kb = [
        [InlineKeyboardButton("🟢 Safe",       callback_data="mode_safe"),
         InlineKeyboardButton("🔵 Balanced",   callback_data="mode_balanced")],
        [InlineKeyboardButton("🟠 Aggressive", callback_data="mode_aggressive"),
         InlineKeyboardButton("🔴 Full Send",  callback_data="mode_degen")],
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
    user  = await asyncio.to_thread(database.get_user, cid)
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
    # which based on production logs is most of the time (0-5 open SNIPE
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
    except Exception as e:
        return f"Could not fetch balance: {e}"


async def _markets_text(cid: str) -> str:
    user = await asyncio.to_thread(database.get_user, cid)
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
    user = await asyncio.to_thread(database.get_user, cid)
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
    user = await asyncio.to_thread(database.get_user, cid)
    if user:
        s = user["settings"]
        s["paused"] = paused
        await asyncio.to_thread(database.update_settings, cid, s)


async def _clear_daily(cid: str):
    _user_daily.pop(cid, None)


# ── Notifications ─────────────────────────────────────────────────────────────

async def send_message(app: Application, chat_id: str, text: str, **kwargs):
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        log.warning(f"send_message → {chat_id}: {e}")


async def notify_trade(app, cid: str, sig, amount: float, engine: str = "AMM"):
    """
    Notification shows strategy, direction, amount and certainty.
    Engine label removed — we always use MARKET orders so it's noise.
    """
    icon = "⬆️" if sig.outcome.upper() in ("YES", "UP") else "⬇️"
    # Escape markdown special chars in reason
    safe_reason = (getattr(sig, "reason", "") or "").replace("_", "\\_").replace("*", "\\*")
    msg = (
        f"🔔 *Trade* ({sig.strategy})\n"
        f"{sig.asset} {sig.timeframe}\n"
        f"{icon} {sig.outcome} | ₦{amount:,.0f} @ {sig.certainty:.0%}\n"
        f"_{safe_reason}_"
    )
    try:
        await app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
    except Exception:
        # Fallback — plain text if markdown parsing fails
        try:
            plain = f"Trade ({sig.strategy}) {sig.asset} {sig.timeframe} {sig.outcome} ₦{amount:,.0f} @ {sig.certainty:.0%}"
            await app.bot.send_message(chat_id=cid, text=plain)
        except Exception as e:
            log.error(f"notify_trade failed: {e}")


async def notify_win(app, cid, _mid, asset, tf, strat, pnl):
    await send_message(app, cid, f"✅ *WIN* — {strat} {asset} {tf}\n+₦{pnl:,.2f}", parse_mode="Markdown")

async def notify_loss(app, cid, _mid, asset, tf, strat, pnl):
    await send_message(app, cid, f"❌ *LOSS* — {strat} {asset} {tf}\n₦{-abs(pnl):,.2f}", parse_mode="Markdown")

async def notify_drawdown(app, cid, balance, peak, dd):
    await send_message(app, cid,
        f"⚠️ *Drawdown — Trading Paused*\n\n"
        f"Peak: ₦{peak:,.0f} → Now: ₦{balance:,.0f}\n"
        f"Drawdown: {dd:.1%}\n\n/resume to override.",
        parse_mode="Markdown")

async def notify_arb(app, cid, sig, pairs, profit):
    await send_message(app, cid,
        f"⚖️ *ARB* | {sig.asset} {sig.timeframe}\n{pairs:.0f} pairs → ₦{profit:,.2f}",
        parse_mode="Markdown")

async def notify_deposit_detected(app, cid, amount, currency):
    await send_message(app, cid,
        f"💸 *Deposit detected* +{currency} {amount:,.0f}\n"
        f"Drawdown baseline reset. Send /resume if trading was paused.",
        parse_mode="Markdown")
