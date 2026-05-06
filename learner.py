"""
Intelligence loop — per-user daily self-improvement.
Uses the shared database module for trade records.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import config
import database

log = logging.getLogger("learner")

DEFAULT_LEARNED: dict = {
    "snipe_min_certainty":      config.SNIPE_MIN_CERTAINTY,
    "correlation_threshold":    config.CORRELATION_THRESHOLD,
    "news_sentiment_threshold": config.NEWS_SENTIMENT_THRESHOLD,
    "size_multipliers": {
        "SNIPE": 1.0, "CORRELATE": 1.0, "ARB": 1.0, "NEWS": 1.0,
    },
    "suspended_strategies": [],
}


def run_learning(chat_id: str) -> tuple[dict, str]:
    """Analyse 30-day trades for one user, save updated learned params, return report."""
    user = database.get_user(chat_id)
    if not user:
        return DEFAULT_LEARNED.copy(), "User not found."

    s       = user["settings"]
    learned = {**DEFAULT_LEARNED, **s.get("learned", {})}
    mults   = dict(learned.get("size_multipliers", {k: 1.0 for k in ["SNIPE", "CORRELATE", "ARB", "NEWS"]}))
    suspended = set(learned.get("suspended_strategies", []))

    stats = database.recent_stats(chat_id, days=30)
    changes, warnings = [], []

    by_strategy: dict[str, list] = {}
    for row in stats:
        by_strategy.setdefault(row["strategy"], []).append(row)

    for strat, rows in by_strategy.items():
        total    = sum(r["total"] for r in rows)
        wins     = sum(r.get("wins") or 0 for r in rows)
        win_rate = wins / total if total > 0 else None

        if total < 5:
            continue

        # Suspend / reactivate — strategy-specific thresholds
        # SNIPE needs ~87% WR to break even with 15% profit floor.
        # CORRELATE/NEWS need ~57% WR with their 0.55 price ceiling.
        wr_suspend = 0.85 if strat == "SNIPE" else 0.55
        wr_recover = 0.90 if strat == "SNIPE" else 0.65

        if win_rate is not None and win_rate < wr_suspend and total >= 15:
            if strat not in suspended:
                suspended.add(strat)
                warnings.append(f"⚠️ {strat} suspended — WR {win_rate:.0%} < {wr_suspend:.0%} threshold")
        elif strat in suspended and win_rate and win_rate >= wr_recover:
            suspended.discard(strat)
            changes.append(f"✅ {strat} reactivated — WR {win_rate:.0%} recovered above {wr_recover:.0%}")

        # Size multiplier (range 0.25×–2.0×)
        m = mults.get(strat, 1.0)
        if win_rate is not None:
            if win_rate >= 0.75:   m = min(2.0, m + 0.10)
            elif win_rate >= 0.60: m = min(1.5, m + 0.05)
            elif win_rate < 0.55:  m = max(0.25, m - 0.10)
        mults[strat] = round(m, 2)

        # Strategy-specific threshold tuning
        if strat == "SNIPE" and win_rate is not None:
            cur = learned.get("snipe_min_certainty", config.SNIPE_MIN_CERTAINTY)
            # Be much more aggressive in raising certainty if WR is below break-even (87%)
            if win_rate < 0.88:
                new = min(round(cur + 0.05, 2), 0.98)
                learned["snipe_min_certainty"] = new
                changes.append(f"🎯 SNIPE certainty raised AGGRESSIVELY {cur} → {new}")
            elif win_rate < 0.92:
                new = min(round(cur + 0.02, 2), 0.98)
                learned["snipe_min_certainty"] = new
                changes.append(f"🎯 SNIPE certainty raised {cur} → {new}")
            elif win_rate > 0.96 and cur > 0.75:
                new = max(round(cur - 0.01, 2), 0.75)
                learned["snipe_min_certainty"] = new
                changes.append(f"🎯 SNIPE certainty lowered {cur} → {new}")

        elif strat == "CORRELATE" and win_rate is not None:
            cur = learned.get("correlation_threshold", config.CORRELATION_THRESHOLD)
            if win_rate < 0.55:
                new = min(round(cur + 0.01, 2), 0.20)
                learned["correlation_threshold"] = new
                changes.append(f"🔗 CORRELATE threshold raised {cur} → {new}")
            elif win_rate > 0.70 and cur > 0.05:
                new = max(round(cur - 0.01, 2), 0.05)
                learned["correlation_threshold"] = new
                changes.append(f"🔗 CORRELATE threshold lowered {cur} → {new}")

        elif strat == "NEWS" and win_rate is not None:
            cur = learned.get("news_sentiment_threshold", config.NEWS_SENTIMENT_THRESHOLD)
            if win_rate < 0.52:
                new = min(round(cur + 0.05, 2), 0.70)
                learned["news_sentiment_threshold"] = new
                changes.append(f"📰 NEWS threshold raised {cur} → {new}")
            elif win_rate > 0.65 and cur > 0.25:
                new = max(round(cur - 0.02, 2), 0.25)
                learned["news_sentiment_threshold"] = new
                changes.append(f"📰 NEWS threshold lowered {cur} → {new}")

    learned["size_multipliers"]     = mults
    learned["suspended_strategies"] = list(suspended)

    # Save learned params back into settings
    s["learned"] = learned
    database.update_settings(chat_id, s)

    # Build report
    overall = database.all_time_stats(chat_id)
    lines   = [
        "🧠 *Daily Learning Report*",
        f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "📊 *All-time*",
        f"  {overall['wins']}/{overall['total']} wins | "
        f"{overall['win_rate']:.0%} WR | ₦{overall['total_pnl']:+,.0f}",
        "",
        "📈 *30-Day Breakdown*",
    ]
    for row in stats:
        wr  = row["win_rate"]
        pnl = row.get("total_pnl") or 0
        lines.append(
            f"  {row['strategy']} {row['asset']} {row['timeframe']}: "
            f"{row['total']} trades | {wr:.0%} WR | ₦{pnl:,.0f}"
        )

    if changes:
        lines += ["", "⚙️ *Changes*"] + [f"  {c}" for c in changes]
    if warnings:
        lines += ["", "🚨 *Warnings*"] + [f"  {w}" for w in warnings]
    if not changes and not warnings and stats:
        lines.append("\n✅ All strategies performing well — no changes needed.")

    active = [s for s in ["SNIPE", "CORRELATE", "ARB", "NEWS"] if s not in suspended]
    lines += [
        "",
        f"Active: {active}",
        f"Suspended: {list(suspended) or 'None'}",
    ]

    return learned, "\n".join(lines)


_YES_OUTCOMES = {"yes", "up", "outcome1", "1"}
_NO_OUTCOMES  = {"no", "down", "outcome2", "2"}


def _resolved_won(resolved: str, trade: dict, market: dict) -> bool:
    """Determine if we won, handling Yes/Up/No/Down label variants from the API."""
    yes_label = (market.get("outcome1Label") or "Up").lower()
    no_label  = (market.get("outcome2Label") or "Down").lower()
    r = resolved.lower()
    yes_signals = _YES_OUTCOMES | {yes_label}
    no_signals  = _NO_OUTCOMES  | {no_label}
    if r in yes_signals:
        return trade["outcome"].upper() == "YES"
    if r in no_signals:
        return trade["outcome"].upper() == "NO"
    # Fallback: maybe resolvedOutcome is the outcome ID itself
    return resolved == trade.get("outcome_id", "")


async def resolution_monitor(user_clients: dict, user_risks: dict = None, tg_app=None):
    """Check all users' unresolved trades against the Bayse API every 2 minutes."""
    import telegram_bot as tgb

    while True:
        await asyncio.sleep(120)
        for chat_id, client in list(user_clients.items()):
            pending = database.get_unresolved(chat_id, older_than_minutes=6)
            for trade in pending:
                try:
                    event    = await client.get_event(trade["event_id"])
                    market   = (event.get("markets") or [{}])[0]
                    resolved = market.get("resolvedOutcome", "")
                    if not resolved:
                        continue

                    # Skip cancelled or invalid markets — not a real win or loss
                    if resolved.upper() in ("CANCEL", "CANCELLED", "INVALID", "VOID"):
                        log.info(f"[{chat_id}] Trade {trade['trade_id']} voided ({resolved}) — skipping")
                        # Still free up the position so exposure cap doesn't stall trading
                        if user_risks and chat_id in user_risks:
                            user_risks[chat_id].remove_position(trade["market_id"])
                        continue

                    won = _resolved_won(resolved, trade, market)

                    # Try to get the real PnL from Bayse — avoids fee estimation errors
                    pnl = None
                    order_id = trade.get("order_id")
                    if order_id:
                        try:
                            order_data = await client.get_order(order_id)
                            raw = (order_data.get("profit") or order_data.get("pnl")
                                   or order_data.get("realizedPnl"))
                            if raw is not None:
                                pnl = float(raw)
                        except Exception as oe:
                            log.debug(f"get_order fallback: {oe}")

                    # Fallback: estimate PnL from our own formula
                    if pnl is None:
                        fr     = float(market.get("feePercentage", 4)) / 100
                        entry  = trade["entry_price"]
                        amount = trade["amount_ngn"]
                        if won:
                            shares  = amount / entry
                            fee_amt = fr * shares * entry * max(1 - entry, 0.5)
                            pnl     = shares * (1.0 - entry) - fee_amt
                        else:
                            pnl = -amount

                    database.resolve_trade(trade["trade_id"], won, pnl)

                    # Free up position in risk manager so exposure cap doesn't block new trades
                    if user_risks and chat_id in user_risks:
                        user_risks[chat_id].remove_position(trade["market_id"])

                    result = "WIN" if won else "LOSS"
                    log.info(
                        f"[{chat_id}] RESOLVED {result} | {trade['strategy']} "
                        f"{trade['asset']} {trade['timeframe']} {trade['outcome']} | "
                        f"entry={trade['entry_price']:.3f} amount=₦{trade['amount_ngn']:,.0f} "
                        f"pnl=₦{pnl:+,.2f}"
                    )
                except Exception as e:
                    log.warning(f"[{chat_id}] Resolution check failed {trade['trade_id']}: {e}")
                    continue

                # Notify outside the main try/except so a Telegram error never silences the resolution
                if tg_app:
                    try:
                        fn = tgb.notify_win if won else tgb.notify_loss
                        await fn(
                            tg_app, chat_id, trade["market_id"],
                            trade["asset"], trade["timeframe"], trade["strategy"], pnl,
                        )
                    except Exception as ne:
                        log.warning(f"[{chat_id}] Notify failed {trade['trade_id']}: {ne}")


async def daily_learning_loop(tg_app=None):
    """Run at midnight UTC for every active user."""
    import telegram_bot as tgb

    while True:
        now      = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait     = (midnight - now).total_seconds()
        log.info(f"Next learning cycle in {wait / 3600:.1f}h")
        await asyncio.sleep(wait)

        for user in database.get_all_active():
            cid = user["chat_id"]
            try:
                _, report = run_learning(cid)
                log.info(f"Learning complete for {cid}")
                if tg_app:
                    await tgb.send_message(
                        tg_app, cid,
                        f"🧠 *Daily Intelligence Report*\n\n{report}",
                        parse_mode="Markdown",
                    )
            except Exception as e:
                log.error(f"Learning failed for {cid}: {e}")
