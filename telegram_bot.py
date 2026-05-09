"""
Telegram bot — multi-user with guided setup flow.

New users: /start → paste Public Key → paste Secret Key → connected.
Existing users: all commands work immediately.
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

# ── Setup states ──────────────────────────────────────────────────────────────
_NEED_PUBLIC = 1
_NEED_SECRET = 2
_setup_state: dict[str, int] = {}
_temp_pub:    dict[str, str] = {}

# ── Injected runtime refs ─────────────────────────────────────────────────────
_user_clients:  dict = {}
_user_risks:    dict = {}
_user_daily:    dict = {}
_active_markets: list = []
_start_user_fn = None


def inject(user_clients, user_risks, user_daily, active_markets, start_user_fn):
    global _user_clients, _user_risks, _user_daily, _active_markets, _start_user_fn
    _user_clients   = user_clients
    _user_risks     = user_risks
    _user_daily     = user_daily
    _active_markets = active_markets
    _start_user_fn  = start_user_fn


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("balance",    cmd_balance))
    app.add_handler(CommandHandler("trades",     cmd_trades))
    app.add_handler(CommandHandler("markets",    cmd_markets))
    app.add_handler(CommandHandler("analysis",   cmd_analysis))
    app.add_handler(CommandHandler("settings",   cmd_settings))
    app.add_handler(CommandHandler("set",        cmd_set))
    app.add_handler(CommandHandler("pause",      cmd_pause))
    app.add_handler(CommandHandler("resume",     cmd_resume))
    app.add_handler(CommandHandler("learning",      cmd_learning))
    app.add_handler(CommandHandler("resetlearning", cmd_resetlearning))
    app.add_handler(CommandHandler("learnstats", cmd_learnstats))
    app.add_handler(CommandHandler("mode",       cmd_mode))
    app.add_handler(CommandHandler("debug",      cmd_debug))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("wallet",     cmd_wallet))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


# ── Setup flow ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    if await asyncio.to_thread(database.get_user, cid):
        await _main_menu(update)
        return

    _setup_state[cid] = _NEED_PUBLIC
    await update.message.reply_text(
        "👋 *Welcome to Bayse Bot!*\n\n"
        "I trade BTC, ETH, SOL, FX, and Gold prediction markets automatically on your behalf.\n\n"
        "To connect your account:\n\n"
        "1. Open *app.bayse.markets*\n"
        "2. Go to *More → Account Settings → API Keys → Create*\n"
        "3. Copy and paste your *Public Key* here (starts with `pk_`):",
        parse_mode="Markdown",
    )


async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid   = str(update.effective_chat.id)
    text  = update.message.text.strip()
    state = _setup_state.get(cid)

    if state == _NEED_PUBLIC:
        if not text.startswith("pk_"):
            await update.message.reply_text(
                "❌ Public keys start with `pk_` — try again:", parse_mode="Markdown"
            )
            return
        _temp_pub[cid]    = text
        _setup_state[cid] = _NEED_SECRET
        await update.message.reply_text(
            "✅ Got it!\n\nNow paste your *Secret Key* (starts with `sk_`):",
            parse_mode="Markdown",
        )
        return

    if state == _NEED_SECRET:
        if not text.startswith("sk_"):
            await update.message.reply_text(
                "❌ Secret keys start with `sk_` — try again:", parse_mode="Markdown"
            )
            return

        pub = _temp_pub.pop(cid, "")
        _setup_state.pop(cid, None)

        msg = await update.message.reply_text("🔄 Connecting to Bayse…")
        try:
            from client import BayseClient
            client  = BayseClient(pub, text)
            balance = await client.get_balance_ngn()
            await asyncio.to_thread(database.add_user, cid, pub, text)
            _user_clients[cid] = client
            if _start_user_fn:
                await _start_user_fn(cid)
            await msg.delete()
            await update.message.reply_text(
                f"🎉 *Connected!*\n\n"
                f"Balance: ₦{balance:,.2f}\n\n"
                f"The bot is now trading for you. You'll get alerts here for every trade.\n\n"
                f"Default daily target: *10% of your starting balance*. "
                f"Change it with `/set dailymultiplier 20` (or any number 1–100).",
                parse_mode="Markdown",
            )
            await _main_menu(update)
        except Exception as e:
            _setup_state[cid] = _NEED_PUBLIC
            await msg.delete()
            await update.message.reply_text(
                f"❌ *Connection failed*\n\n`{e}`\n\nCheck your keys and try /start again.",
                parse_mode="Markdown",
            )
        return

    if not await asyncio.to_thread(database.get_user, cid):
        await update.message.reply_text("Use /start to connect your Bayse account.")


async def _main_menu(update: Update):
    keyboard = [
        [InlineKeyboardButton("📊 Status",   callback_data="status"),
         InlineKeyboardButton("💰 Balance",  callback_data="balance")],
        [InlineKeyboardButton("🏦 Markets",  callback_data="markets"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("⏸ Pause",    callback_data="pause"),
         InlineKeyboardButton("▶️ Resume",   callback_data="resume")],
    ]
    await update.message.reply_text(
        "🤖 *Bayse Bot — Active*\n\nWhat would you like to check?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_button(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = str(query.from_user.id)
    if not await asyncio.to_thread(database.get_user, cid):
        await query.message.reply_text("Use /start to connect your account.")
        return
    data = query.data
    if   data == "status":   await query.message.reply_text(await _status_text(cid), parse_mode="Markdown")
    elif data == "balance":  await query.message.reply_text(await _balance_text(cid), parse_mode="Markdown")
    elif data == "markets":  await query.message.reply_text(await _markets_text(cid), parse_mode="Markdown")
    elif data == "settings": await query.message.reply_text(await _settings_text(cid), parse_mode="Markdown")
    elif data == "pause":
        await _set_paused(cid, True)
        await query.message.reply_text("⏸ Trading paused.")
    elif data == "resume":
        await _set_paused(cid, False)
        await _clear_target_hit(cid)
        await query.message.reply_text("▶️ Trading resumed.")
    elif data in _MODES:
        mode   = _MODES[data]
        user   = await asyncio.to_thread(database.get_user, cid)
        s      = user["settings"]
        s.update(mode["settings"])
        # Store the mode name so /settings and /status can show it
        s["mode"] = data.replace("mode_", "")   # e.g. "mode_safe" → "safe"
        await asyncio.to_thread(database.update_settings, cid, s)
        log.info(f"[USER ACTION] {cid} switched to mode: {s['mode']}")
        await query.message.reply_text(
            f"{mode['description']}\n\n✅ *Mode applied. Trading resumes now.*\n"
            f"Use `/set` to fine-tune any individual setting.",
            parse_mode="Markdown",
        )


# ── Guard decorator ───────────────────────────────────────────────────────────

def _guard(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        cid = str(update.effective_chat.id)
        if not await asyncio.to_thread(database.get_user, cid):
            await update.message.reply_text("Use /start to connect your account.")
            return
        await fn(update, ctx)
    wrapper.__name__ = fn.__name__
    return wrapper


# ── Commands ──────────────────────────────────────────────────────────────────

@_guard
async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        await _status_text(str(update.effective_chat.id)), parse_mode="Markdown"
    )


@_guard
async def cmd_balance(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        await _balance_text(str(update.effective_chat.id)), parse_mode="Markdown"
    )


@_guard
async def cmd_wallet(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Show the raw wallet API response — useful for debugging balance issues."""
    cid    = str(update.effective_chat.id)
    client = _user_clients.get(cid)
    if not client:
        await update.message.reply_text("Bot still starting up. Try again in a moment.")
        return
    try:
        import json
        data = await client.get_wallet()
        text = json.dumps(data, indent=2)
        # Telegram message limit is 4096 chars
        if len(text) > 3800:
            text = text[:3800] + "\n… (truncated)"
        await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error fetching wallet: {e}")


@_guard
async def cmd_debug(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Show internal bot state to diagnose why trades aren't firing."""
    import feeds
    cid = str(update.effective_chat.id)

    lines = ["🔍 *Bot Debug Info*\n"]

    # Spot prices
    if feeds.spot:
        lines.append("*Spot prices (Bayse Realtime):*")
        for asset, price in feeds.spot.items():
            lines.append(f"  {asset}: ${price:,.2f}")
    else:
        lines.append("⚠️ *Spot prices: EMPTY* — Realtime feed not loaded yet. SNIPE is disabled.")

    # Active markets
    lines.append(f"\n*Active markets: {len(_active_markets)}*")
    user = database.get_user(cid)
    s = user["settings"] if user else {}
    ua = s.get("assets", [])
    ut = s.get("timeframes", [])
    relevant = [m for m in _active_markets if m.get("asset") in ua and m.get("timeframe") in ut]
    lines.append(f"Matching your settings ({ua} / {ut}): {len(relevant)}")

    for m in relevant[:6]:
        secs = int(m.get("secs_to_close", -1))
        threshold = m.get("threshold")
        yes_p = m.get("yes_price", 0)
        no_p  = m.get("no_price", 0)
        lines.append(
            f"\n  *{m['asset']} {m['timeframe']}*"
            f"\n    Closes in: {secs}s"
            f"\n    Threshold: {threshold if threshold else '⚠️ MISSING'}"
            f"\n    YES: {yes_p:.3f}  NO: {no_p:.3f}  Sum: {yes_p+no_p:.3f}"
            f"\n    ARB possible: {'✅' if yes_p+no_p < 0.97 else '❌'}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@_guard
async def cmd_trades(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid  = str(update.effective_chat.id)
    rows = await asyncio.to_thread(database.recent_trades, cid, limit=10)
    if not rows:
        await update.message.reply_text("No trades yet.")
        return
    lines = ["📋 *Last 10 Trades*\n"]
    for r in rows:
        icon = "✅" if r["won"] == 1 else ("❌" if r["won"] == 0 else "⏳")
        pnl  = f"₦{r['pnl_ngn']:+,.0f}" if r["pnl_ngn"] is not None else "pending"
        lines.append(f"{icon} {r['strategy']} {r['asset']} {r['timeframe']} {r['outcome']} — {pnl}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@_guard
async def cmd_markets(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        await _markets_text(str(update.effective_chat.id)), parse_mode="Markdown"
    )


@_guard
async def cmd_analysis(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    import analysis as anal
    cid    = str(update.effective_chat.id)
    client = _user_clients.get(cid)
    if not client:
        await update.message.reply_text("Bot still starting up. Try again in a moment.")
        return
    report = await anal.full_report(client, cid)
    await update.message.reply_text(report, parse_mode="Markdown")


@_guard
async def cmd_settings(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        await _settings_text(str(update.effective_chat.id)), parse_mode="Markdown"
    )


@_guard
async def cmd_set(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid  = str(update.effective_chat.id)
    args = update.message.text.split()[1:]
    if len(args) < 2:
        await update.message.reply_text(
            "*Usage:*\n"
            "`/set assets BTC ETH SOL`\n"
            "`/set timeframes 5min 15min 1h`\n"
            "`/set strategies SNIPE ARB`\n"
            "`/set risk 3`\n"
            "`/set mintrade 100`\n"
            "`/set maxtrade 50000`\n"
            "`/set maxexposure 25`\n"
            "`/set dailymultiplier 50`\n"
            "`/set dailytarget 5000`",
            parse_mode="Markdown",
        )
        return

    user = await asyncio.to_thread(database.get_user, cid)
    s    = user["settings"]
    key, vals = args[0].lower(), args[1:]

    ASSETS     = {"BTC", "ETH", "SOL", "EURUSD", "GBPUSD", "EURGBP", "XAUUSD"}
    TIMEFRAMES = {"5min", "15min", "1h", "6h", "1d"}
    STRATEGIES = {"SNIPE", "CORRELATE", "ARB", "NEWS"}

    if key == "assets":
        bad = [v for v in vals if v.upper() not in ASSETS]
        if bad:
            await update.message.reply_text(f"Unknown: {bad}. Valid: BTC ETH SOL EURUSD GBPUSD EURGBP XAUUSD"); return
        s["assets"] = [v.upper() for v in vals]
        msg = f"Assets: {s['assets']}"

    elif key == "timeframes":
        bad = [v for v in vals if v.lower() not in TIMEFRAMES]
        if bad:
            await update.message.reply_text(f"Unknown: {bad}. Valid: 5min 15min 1h 6h 1d"); return
        s["timeframes"] = [v.lower() for v in vals]
        msg = f"Timeframes: {s['timeframes']}"

    elif key == "strategies":
        bad = [v for v in vals if v.upper() not in STRATEGIES]
        if bad:
            await update.message.reply_text(f"Unknown: {bad}. Valid: SNIPE CORRELATE ARB NEWS"); return
        s["strategies"] = [v.upper() for v in vals]
        msg = f"Strategies: {s['strategies']}"

    elif key == "risk":
        try:
            pct = float(vals[0])
            if not 0.1 <= pct <= 10: raise ValueError
            s["risk_pct"] = pct;  msg = f"Risk per trade: {pct}%"
        except ValueError:
            await update.message.reply_text("Risk must be a number 0.1–10."); return

    elif key == "mintrade":
        try:
            amt = float(vals[0])
            if amt < 100:
                await update.message.reply_text("Minimum is ₦100 (Bayse platform limit)."); return
            s["mintrade"] = amt;  msg = f"Min trade: ₦{amt:,.0f}"
        except ValueError:
            await update.message.reply_text("Enter a number."); return

    elif key == "maxtrade":
        try:
            s["maxtrade"] = float(vals[0]);  msg = f"Max trade: ₦{s['maxtrade']:,.0f}"
        except ValueError:
            await update.message.reply_text("Enter a number."); return

    elif key == "maxexposure":
        try:
            pct = float(vals[0])
            if not 5 <= pct <= 100: raise ValueError
            s["maxexposure"] = pct;  msg = f"Max exposure: {pct}% at once"
        except ValueError:
            await update.message.reply_text("Exposure must be 5–100."); return

    elif key == "dailymultiplier":
        try:
            mult = float(vals[0])
            if mult <= 0 or mult > 100: raise ValueError
            s["daily_multiplier"] = mult
            s["daily_target_ngn"] = 0
            msg = f"Daily target: {mult}% of starting balance"
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100."); return

    elif key == "dailytarget":
        try:
            amt = float(vals[0])
            s["daily_target_ngn"] = amt
            s["daily_multiplier"] = 0
            msg = f"Daily target: ₦{amt:,.0f} absolute"
        except ValueError:
            await update.message.reply_text("Enter a number."); return

    else:
        await update.message.reply_text(f"Unknown setting `{key}`.", parse_mode="Markdown"); return

    s["mode"] = "custom"   # individual /set changes override the preset mode
    await asyncio.to_thread(database.update_settings, cid, s)
    await update.message.reply_text(f"✅ {msg}\n_(Mode → Custom)_", parse_mode="Markdown")


@_guard
async def cmd_pause(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    await _set_paused(cid, True)
    await update.message.reply_text("⏸ Trading paused. Use /resume to restart.")


@_guard
async def cmd_resume(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    await _set_paused(cid, False)
    await _clear_target_hit(cid)
    # Reset in-memory drawdown state so the risk manager doesn't immediately
    # re-pause on the next loop tick.  peak_balance=0 causes update_peak() to
    # re-anchor to the current live balance on the next balance fetch.
    risk = _user_risks.get(cid)
    if risk:
        risk.paused = False
        risk.peak_balance = 0
    await update.message.reply_text("▶️ Trading resumed.")


@_guard
async def cmd_learning(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    await update.message.reply_text("🧠 Running intelligence cycle…")
    _, report = await learner.run_learning(cid)
    await update.message.reply_text(report, parse_mode="Markdown")


@_guard
async def cmd_resetlearning(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    user = await asyncio.to_thread(database.get_user, cid)
    if not user:
        return
    s = user["settings"]
    s["learned"] = {}
    await asyncio.to_thread(database.update_settings, cid, s)
    await update.message.reply_text(
        "🔄 *Learned settings cleared.*\n\n"
        "The bot will now use the base config thresholds.\n"
        "Intelligence will rebuild from scratch as new trades complete.",
        parse_mode="Markdown",
    )


@_guard
async def cmd_learnstats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid  = str(update.effective_chat.id)
    rows = await asyncio.to_thread(database.recent_stats, cid, days=7)
    if not rows:
        await update.message.reply_text("No resolved trades in the last 7 days.")
        return
    lines = ["📈 *7-Day Performance*\n"]
    for r in sorted(rows, key=lambda x: -(x.get("total_pnl") or 0)):
        wr   = r["win_rate"]
        icon = "✅" if wr >= 0.55 else ("⚠️" if wr >= 0.48 else "❌")
        pnl  = r.get("total_pnl") or 0
        lines.append(
            f"{icon} {r['strategy']}/{r['asset']}/{r['timeframe']}: "
            f"{wr:.0%} WR ({r['total']} trades) ₦{pnl:,.0f}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Risk modes ────────────────────────────────────────────────────────────────

_MODES = {
    "mode_safe": {
        "label": "🟢 Safe",
        "description": (
            "*🟢 Safe Mode (Quant Conservative)*\n\n"
            "Prioritizes capital preservation. High-conviction engine filters noise.\n\n"
            "• *Entry Guard*: 0.65 (Ultra-high conviction)\n"
            "• *Engine Logic*: Requires 2+ models to agree\n"
            "• *Slippage Guard*: 0.2% limit\n"
            "• *Risk per Trade*: 0.5%\n"
            "• *Markets*: 1h, 6h, 1d (no noise)\n"
            "• *Assets*: Low-vol (FX & BTC only)"
        ),
        "settings": {
            "mode":             "safe",
            "assets":           ["BTC", "EURUSD", "GBPUSD", "USDJPY", "EURJPY", "GBPJPY", "EURGBP", "XAUUSD"],
            "timeframes":       ["1h", "6h", "1d"],
            "strategies":       ["SNIPE", "ARB"],
            "risk_pct":         0.5,
            "mintrade":         100,
            "maxexposure":      15.0,
            "daily_multiplier": 5,
        },
    },
    "mode_balanced": {
        "label": "🔵 Balanced",
        "description": (
            "*🔵 Balanced Mode (Default)*\n\n"
            "Mathematically optimized blend of frequency and safety.\n\n"
            "• *Entry Guard*: 0.55 (Standard)\n"
            "• *Engine Logic*: Standard 5-model blend\n"
            "• *Slippage Guard*: 0.5% limit\n"
            "• *Risk per Trade*: 1.5%\n"
            "• *Markets*: All timeframes\n"
            "• *Assets*: Full Universe"
        ),
        "settings": {
            "mode":             "balanced",
            "assets":           config.ALL_ASSETS,
            "timeframes":       ["15min", "1h", "6h"],
            "strategies":       ["SNIPE", "ARB", "CORRELATE"],
            "risk_pct":         1.5,
            "mintrade":         100,
            "maxexposure":      25.0,
            "daily_multiplier": 10,
        },
    },
    "mode_aggressive": {
        "label": "🟠 Aggressive",
        "description": (
            "*🟠 Aggressive Mode (Growth Focus)*\n\n"
            "Chases momentum. Lower certainty requirements, higher frequency.\n\n"
            "• *Entry Guard*: 0.45 (Frequency-bias)\n"
            "• *Engine Logic*: Momentum-weighted\n"
            "• *Slippage Guard*: 1.0% limit\n"
            "• *Risk per Trade*: 3.0%\n"
            "• *Markets*: All timeframes\n"
            "• *Assets*: Full Universe"
        ),
        "settings": {
            "mode":             "aggressive",
            "assets":           config.ALL_ASSETS,
            "timeframes":       ["5min", "15min", "1h"],
            "strategies":       ["SNIPE", "ARB", "CORRELATE", "NEWS"],
            "risk_pct":         3.0,
            "mintrade":         200,
            "maxexposure":      35.0,
            "daily_multiplier": 20,
        },
    },
    "mode_degen": {
        "label": "🔴 Full Send",
        "description": (
            "*🔴 Full Send Mode (Raw Alpha)*\n\n"
            "Maximum aggression. No mathematical safety guards, raw EV only.\n\n"
            "• *Entry Guard*: 0.35 (Gambler's Edge)\n"
            "• *Engine Logic*: Raw EV (no guards)\n"
            "• *Slippage Guard*: 2.5% limit\n"
            "• *Risk per Trade*: 5.0%\n"
            "• *Markets*: All timeframes\n"
            "• *Assets*: Full Universe"
        ),
        "settings": {
            "mode":             "full_send",
            "assets":           config.ALL_ASSETS,
            "timeframes":       config.ALL_TIMEFRAMES,
            "strategies":       ["SNIPE", "ARB", "CORRELATE", "NEWS"],
            "risk_pct":         5.0,
            "mintrade":         500,
            "maxexposure":      50.0,
            "daily_multiplier": 50,
        },
    },
}


@_guard
async def cmd_mode(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🟢 Safe",      callback_data="mode_safe"),
            InlineKeyboardButton("🔵 Balanced",  callback_data="mode_balanced"),
        ],
        [
            InlineKeyboardButton("🟠 Aggressive", callback_data="mode_aggressive"),
            InlineKeyboardButton("🔴 Full Send",  callback_data="mode_degen"),
        ],
    ]
    await update.message.reply_text(
        "⚙️ *Choose a Risk Mode*\n\n"
        "Each mode applies a full set of recommended settings instantly.\n"
        "You can fine-tune individual settings with `/set` afterwards.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@_guard
async def cmd_disconnect(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    await asyncio.to_thread(database.deactivate, cid)
    _user_clients.pop(cid, None)
    _user_risks.pop(cid, None)
    _user_daily.pop(cid, None)
    await update.message.reply_text(
        "🔌 Account disconnected. Trade history is preserved. Use /start to reconnect."
    )


async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Bayse Bot Commands*\n\n"
        "/start — connect your Bayse account\n"
        "/status — balance, PnL, drawdown, positions\n"
        "/balance — wallet balance\n"
        "/trades — last 10 trades\n"
        "/markets — active markets you're watching\n"
        "/analysis — full performance report\n"
        "/learning — run intelligence cycle now\n"
        "/resetlearning — clear learned overrides, reset to base config\n"
        "/learnstats — 7-day win rates by strategy\n"
        "/settings — current configuration\n"
        "/mode — switch risk mode (Safe / Balanced / Aggressive / FX+Crypto / Full Send)\n"
        "/set — change a single setting (type /set for options)\n"
        "/pause — stop all trading\n"
        "/resume — restart trading\n"
        "/disconnect — remove your account",
        parse_mode="Markdown",
    )


# ── Text builders ──────────────────────────────────────────────────────────────

async def _status_text(cid: str) -> str:
    client = _user_clients.get(cid)
    if not client:
        return "Bot still starting up. Try again in a moment."
    try:
        balance = await client.get_balance_ngn()
    except Exception:
        return "Could not fetch balance right now."

    risk  = _user_risks.get(cid)
    user  = await asyncio.to_thread(database.get_user, cid)
    s     = user["settings"] if user else {}

    # Prefer in-memory cache; fall back to DB-persisted daily_state on cold restart
    day = _user_daily.get(cid) or {}
    if not day and user:
        saved = s.get("daily_state", {})
        if saved.get("date") == date.today().isoformat():
            day = saved

    profit_today = balance - day.get("start_balance", balance)
    target       = _calc_target(s, day.get("start_balance", balance))

    dd = deployed = 0.0
    n_pos = 0
    if risk:
        dd       = max(0, (risk.peak_balance - balance) / risk.peak_balance) if risk.peak_balance else 0
        n_pos    = len(risk.open_positions)
        deployed = sum(p.get("amount_ngn", 0) for p in risk.open_positions.values())

    stats = await asyncio.to_thread(database.all_time_stats, cid)

    lines = [
        "📊 *Bot Status*\n",
        f"Balance: ₦{balance:,.2f}",
        f"Today's profit: ₦{profit_today:+,.2f}",
    ]
    if target > 0:
        pct = min(profit_today / target * 100, 100) if target else 0
        lines.append(f"Daily target: ₦{target:,.0f} ({pct:.0f}% done)")
    lines += [
        f"Drawdown from peak: {dd:.1%}",
        f"Open positions: {n_pos} (₦{deployed:,.0f} deployed)",
        "",
        f"All-time: {stats['wins']}/{stats['total']} wins ({stats['win_rate']:.0%} WR) ₦{stats['total_pnl']:+,.0f}",
    ]

    lines += [
        f"\nStatus: {'⏸ Paused' if s.get('paused') else '🟢 Active'}",
        f"Mode: *{_mode_label(s.get('mode', 'balanced'))}*",
    ]

    # Mode Advisor Tip
    if balance < 3000 and s.get("mode", "balanced") not in ("aggressive", "full_send"):
        lines.append("\n💡 *Mode Advisor*: Your balance is under ₦3,000. Switch to *Aggressive* or *Full Send* to beat the platform's ₦100 fee floor effectively.")
    elif balance >= 10000 and s.get("mode") == "full_send":
        lines.append("\n💡 *Mode Advisor*: Great balance! Consider *Balanced* mode to preserve your gains with tighter conviction guards.")

    return "\n".join(lines)


async def _balance_text(cid: str) -> str:
    client = _user_clients.get(cid)
    if not client:
        return "Bot still starting up."
    try:
        bal = await client.get_balance_ngn()
        return f"💰 Balance: ₦{bal:,.2f}"
    except Exception as e:
        return f"Could not fetch balance: {e}"


async def _markets_text(cid: str) -> str:
    user = await asyncio.to_thread(database.get_user, cid)
    if not user:
        return "Not connected."
    s  = user["settings"]
    ua = s.get("assets",     ["BTC", "ETH", "SOL"])
    ut = s.get("timeframes", ["5min", "15min", "1h"])
    relevant = [m for m in _active_markets if m.get("asset") in ua and m.get("timeframe") in ut]
    if not relevant:
        return "No active markets matching your settings."
    lines = ["🏦 *Active Markets*\n"]
    for m in relevant[:15]:
        mins = (m.get("secs_to_close") or 0) // 60
        lines.append(
            f"{'🟢' if m.get('status')=='open' else '🔴'} "
            f"{m['asset']} {m['timeframe']} | "
            f"UP:{m.get('yes_price',0):.3f} DN:{m.get('no_price',0):.3f} | {mins}m left"
        )
    return "\n".join(lines)


async def _settings_text(cid: str) -> str:
    user = await asyncio.to_thread(database.get_user, cid)
    if not user:
        return "Not connected."
    s    = user["settings"]
    mult = s.get("daily_multiplier", 10)
    abs_ = s.get("daily_target_ngn", 0)
    tgt  = f"₦{abs_:,.0f} (fixed)" if abs_ > 0 else f"{mult}% of starting balance"
    mode_name = _mode_label(s.get("mode", "balanced"))
    return (
        "⚙️ *Settings*\n\n"
        f"Mode:         {mode_name}\n"
        f"Assets:       {s.get('assets')}\n"
        f"Timeframes:   {s.get('timeframes')}\n"
        f"Strategies:   {s.get('strategies')}\n"
        f"Risk/trade:   {s.get('risk_pct', 3)}%\n"
        f"Min trade:    ₦{s.get('mintrade', 100):,.0f}\n"
        f"Max trade:    ₦{s.get('maxtrade', 500000):,.0f}\n"
        f"Max exposure: {s.get('maxexposure', 30)}%\n"
        f"Daily target: {tgt}\n"
        f"Status:       {'⏸ Paused' if s.get('paused') else '🟢 Active'}"
    )


_MODE_LABELS = {
    "safe":       "🟢 Safe",
    "balanced":   "🔵 Balanced",
    "aggressive": "🟠 Aggressive",
    "degen":      "🔴 Full Send",
    "fx":         "💱 FX + Crypto",
    "custom":     "🔧 Custom",
}

def _mode_label(mode_key: str) -> str:
    return _MODE_LABELS.get(mode_key, f"🔧 {mode_key.title()}")

def _calc_target(settings: dict, start_balance: float) -> float:
    abs_ = settings.get("daily_target_ngn", 0)
    if abs_ > 0:
        return float(abs_)
    return start_balance * settings.get("daily_multiplier", 10) / 100


async def _set_paused(cid: str, paused: bool):
    user = await asyncio.to_thread(database.get_user, cid)
    if user:
        s = user["settings"]
        s["paused"] = paused
        await asyncio.to_thread(database.update_settings, cid, s)
        action = "PAUSED" if paused else "RESUMED"
        log.info(f"[USER ACTION] {cid} {action} trading.")


async def _clear_target_hit(cid: str):
    # Drop the entry entirely so the next loop tick resets start_balance
    # to the actual current balance — prevents deposits looking like profit
    _user_daily.pop(cid, None)


# ── Notifications ──────────────────────────────────────────────────────────────

async def send_message(app: Application, chat_id: str, text: str, **kwargs):
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        log.warning(f"Telegram send failed → {chat_id}: {e}")


async def notify_trade(app, cid: str, sig, amount: float):
    icon = "⬆️" if "YES" in sig.outcome.upper() or "UP" in sig.outcome.upper() else "⬇️"
    converged = getattr(sig, "converged_with", [])
    if converged:
        header = f"🎯 *Converged Trade* ({sig.strategy} + {' + '.join(converged)})"
    else:
        header = f"🔔 *Trade* ({sig.strategy})"
    await send_message(
        app, cid,
        f"{header}\n{sig.asset} {sig.timeframe}\n"
        f"{icon} {sig.outcome} | ₦{amount:,.0f} @ {sig.certainty:.0%}\n_{sig.reason}_",
        parse_mode="Markdown",
    )


async def notify_win(app, cid: str, _mid: str, asset: str, tf: str, strat: str, pnl: float):
    await send_message(app, cid, f"✅ *WIN* — {strat} {asset} {tf}\n+₦{pnl:,.2f}", parse_mode="Markdown")


async def notify_loss(app, cid: str, _mid: str, asset: str, tf: str, strat: str, pnl: float):
    loss_amt = -abs(pnl)  # ensure negative regardless of how the API returned it
    await send_message(app, cid, f"❌ *LOSS* — {strat} {asset} {tf}\n₦{loss_amt:,.2f}", parse_mode="Markdown")


async def notify_drawdown(app, cid: str, balance: float, peak: float, dd: float):
    await send_message(
        app, cid,
        f"⚠️ *Drawdown Alert — Trading Paused*\n\n"
        f"Peak: ₦{peak:,.0f}  →  Now: ₦{balance:,.0f}\n"
        f"Drawdown: {dd:.1%}\n\n/resume to override.",
        parse_mode="Markdown",
    )


async def notify_arb(app, cid: str, sig, pairs: float, profit: float):
    await send_message(
        app, cid,
        f"⚖️ *ARB* | {sig.asset} {sig.timeframe}\n{pairs:.0f} pairs → est ₦{profit:,.2f}",
        parse_mode="Markdown",
    )


async def notify_news(app, cid: str, headline: str, direction: str, assets: list, strength: float):
    bull = direction == "BULLISH"
    await send_message(
        app, cid,
        f"📰 *News Signal*\n{headline}\n"
        f"{'🐂 Bullish' if bull else '🐻 Bearish'} ({strength:.0%}) — {', '.join(assets)}",
        parse_mode="Markdown",
    )


async def notify_deposit_detected(app, cid: str, amount: float, currency: str):
    await send_message(
        app, cid, 
        f"💸 *Deposit Detected* +{currency} {amount:,.0f}\n\n"
        "Your drawdown baseline has been reset. If your bot was previously paused, send /resume to start trading with your new balance.",
        parse_mode="Markdown"
    )
