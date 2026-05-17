"""
Intelligence loop — per-user daily self-improvement.
Uses the shared database module for trade records.
"""

import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta

import config
import database

def binomial_cdf(k: int, n: int, p: float) -> float:
    """Returns the cumulative probability of getting exactly or fewer than k wins in n trials with win prob p."""
    cdf = 0.0
    for i in range(k + 1):
        cdf += math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return cdf

log = logging.getLogger("learner")

DEFAULT_LEARNED: dict = {
    "snipe_min_certainty":      config.SNIPE_MIN_CERTAINTY,
    "correlation_threshold":    config.CORRELATION_THRESHOLD,
    "news_sentiment_threshold": config.NEWS_SENTIMENT_THRESHOLD,
    "size_multipliers": {
        "SNIPE": 1.0, "CORRELATE": 1.0, "ARB": 1.0, "NEWS": 1.0,
    },
    "certainty_multipliers": {
        "SNIPE": 1.0, "CORRELATE": 1.0, "ARB": 1.0, "NEWS": 1.0,
    },
}


def get_learned_overrides(chat_id: str) -> dict:
    """Helper for bot.py to fetch current AI-tuned settings for a user."""
    user = database.get_user(chat_id)
    if not user:
        return DEFAULT_LEARNED.copy()
    s = user.get("settings", {})
    learned = {**DEFAULT_LEARNED, **s.get("learned", {})}
    learned["mode"] = s.get("mode", "balanced")
    return learned


async def run_learning(chat_id: str) -> tuple[dict, str]:
    """
    Analyse 30-day trades for one user, save updated learned params, return report.
    
    v2: Self-Correction Engine
    - Detects losing (strategy, asset, timeframe) combos and suspends them
    - Detects losing streaks (3+ consecutive losses) and applies cooldowns
    - Auto-reactivates combos when performance recovers
    """
    user = await asyncio.to_thread(database.get_user, chat_id)
    if not user:
        return DEFAULT_LEARNED.copy(), "User not found."

    s       = user["settings"]
    learned = {**DEFAULT_LEARNED, **s.get("learned", {})}
    mults   = dict(learned.get("size_multipliers", {k: 1.0 for k in ["SNIPE", "CORRELATE", "ARB", "NEWS"]}))
    cert_mults = dict(learned.get("certainty_multipliers", {k: 1.0 for k in ["SNIPE", "CORRELATE", "ARB", "NEWS"]}))
    counts = {}

    stats = await asyncio.to_thread(database.recent_stats, chat_id, days=30)
    changes, warnings = [], []

    by_strategy: dict[str, list] = {}
    for row in stats:
        by_strategy.setdefault(row["strategy"], []).append(row)

    for strat, rows in by_strategy.items():
        total    = int(sum(r["total"] for r in rows))
        wins     = int(sum(r.get("wins") or 0 for r in rows))
        win_rate = wins / total if total > 0 else None
        
        counts[strat] = total

        if total < 5:
            continue

        expected_wr = 0.90 if strat == "SNIPE" else 0.60
        
        if total >= 5:
            # p-value: probability of observing 'wins' or fewer if the true win rate was 'expected_wr'
            p_value = binomial_cdf(wins, total, expected_wr)
            
            c_mult = cert_mults.get(strat, 1.0)
            if p_value < 0.05:
                # Bayesian certainty decay
                c_mult = max(0.1, c_mult - 0.25)
                warnings.append(f"⚠️ {strat} certainty penalized (-25%) — edge broken (p={p_value:.3f})")
            elif win_rate >= expected_wr:
                # Recover
                c_mult = min(1.5, c_mult + 0.10)
                if c_mult > cert_mults.get(strat, 1.0):
                    changes.append(f"✅ {strat} certainty boosted (+10%) — expected WR maintained")
            
            cert_mults[strat] = round(c_mult, 2)

        # Size multiplier (range 0.25×–2.0×)
        m = mults.get(strat, 1.0)
        if win_rate is not None:
            if win_rate >= 0.75:   m = min(3.0, m + 0.25)
            elif win_rate >= 0.60: m = min(1.5, m + 0.10)
            elif win_rate < 0.55:  m = max(0.10, m - 0.25)
        mults[strat] = round(m, 2)

        # Strategy-specific threshold tuning
        if strat == "SNIPE" and win_rate is not None:
            cur = learned.get("snipe_min_certainty", config.SNIPE_MIN_CERTAINTY)
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

    # ══════════════════════════════════════════════════════════════════════════
    # SELF-CORRECTION ENGINE (Granular Combo Analysis)
    # Examines each (strategy, asset, timeframe) independently.
    # A losing combo gets suspended WITHOUT affecting the same strategy on
    # other assets/timeframes.
    # ══════════════════════════════════════════════════════════════════════════
    combo_stats = await asyncio.to_thread(database.get_combo_stats, chat_id, days=14)
    
    for combo in combo_stats:
        key = f"{combo['strategy']}:{combo['asset']}:{combo['timeframe']}"
        wr = combo["win_rate"]
        total = int(combo["total"])
        pnl = combo.get("total_pnl") or 0
        
        # We will adjust the cert_mults for this specific combo
        expected_wr = 0.90 if combo['strategy'] == "SNIPE" else 0.60
        wins = int(wr * total)
        p_value = binomial_cdf(wins, total, expected_wr)
        
        c_mult = cert_mults.get(key, 1.0)
        if total >= 5 and p_value < 0.05 and pnl < 0:
            c_mult = max(0.1, c_mult - 0.50) # Massive penalty for broken combo
            warnings.append(
                f"🔴 SELF-CORRECT: {key} certainty penalized (-50%) — "
                f"Statistically broken edge p={p_value:.3f} ({total} trades, ₦{pnl:+,.0f})"
            )
        elif p_value > 0.20 and wr >= expected_wr:
            c_mult = min(1.5, c_mult + 0.20)
            if c_mult > cert_mults.get(key, 1.0):
                changes.append(f"🟢 SELF-CORRECT: {key} certainty recovered")
                
        cert_mults[key] = round(c_mult, 2)
    
    # Rule 3: Losing streak detection (3+ consecutive losses on same combo)
    for combo in combo_stats:
        key = f"{combo['strategy']}:{combo['asset']}:{combo['timeframe']}"
        
        streak = await asyncio.to_thread(
            database.get_recent_streak, chat_id,
            combo["strategy"], combo["asset"], combo["timeframe"], 5
        )
        
        # Count consecutive losses from the most recent trade
        consec_losses = 0
        for won in streak:
            if not won:
                consec_losses += 1
            else:
                break
        
        if consec_losses >= 3:
            cert_mults[key] = max(0.1, cert_mults.get(key, 1.0) - 0.3)
            warnings.append(
                f"🔥 STREAK HALT: {key} — {consec_losses} consecutive losses! "
                f"Certainty penalized (-30%)."
            )

    learned["size_multipliers"] = mults
    learned["certainty_multipliers"] = cert_mults
    learned["trade_counts"] = counts

    # Save learned params back into settings
    s["learned"] = learned
    await asyncio.to_thread(database.update_settings, chat_id, s)

    # Build report
    overall = await asyncio.to_thread(database.all_time_stats, chat_id)
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

    suspended = learned.get("suspended_strategies", [])
    suspended_combos = learned.get("suspended_combos", [])
    active = [s for s in ["SNIPE", "CORRELATE", "ARB", "NEWS", "POLY_EDGE"] if s not in suspended]
    lines += [
        "",
        f"Active Strategies: {active}",
        f"Suspended Strategies: {list(suspended) or 'None'}",
    ]
    
    if suspended_combos:
        lines += [
            "",
            "🔒 *Suspended Combos (Self-Correction)*",
        ]
        for combo_key in sorted(suspended_combos):
            lines.append(f"  ❌ {combo_key}")
    
    # Add temporal analysis
    temporal_stats = await asyncio.to_thread(database.get_hourly_stats, chat_id)
    if temporal_stats:
        lines.append("\n🕒 *Time-of-Day Performance (UTC)*")
        sorted_hours = sorted(temporal_stats, key=lambda x: x["win_rate"], reverse=True)
        for h in sorted_hours[:3]:
            lines.append(f"  🌟 {h['hour']:02d}:00 — {h['win_rate']:.0%} WR ({h['total']} trades)")
        for h in sorted_hours[-3:]:
            if h["win_rate"] < 0.45 and h["total"] >= 5:
                lines.append(f"  🚫 {h['hour']:02d}:00 — {h['win_rate']:.0%} WR (DANGER ZONE)")

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
            pending = await asyncio.to_thread(database.get_unresolved, chat_id, older_than_minutes=6)
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
                            
                            # Skip if order was completely unfilled (e.g., Maker order that never hit)
                            shares_filled = float(order_data.get("shares", order_data.get("quantity", 0)) or 0)
                            if shares_filled <= 0:
                                log.info(f"[{chat_id}] MAKER UNFILLED: {trade['strategy']} on {trade['asset']} resolved with zero fills.")
                                await asyncio.to_thread(database.resolve_trade, trade["trade_id"], False, 0.0)
                                if user_risks and chat_id in user_risks:
                                    user_risks[chat_id].remove_position(trade["market_id"])
                                continue

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

                    await asyncio.to_thread(database.resolve_trade, trade["trade_id"], won, pnl)
                    
                    import strategy
                    if won:
                        strategy.record_success(trade["strategy"], trade["asset"])
                    else:
                        strategy.record_failure(trade["strategy"], trade["asset"])

                    # Update risk manager with the result
                    if user_risks and chat_id in user_risks:
                        risk_mgr = user_risks[chat_id]
                        risk_mgr.add_pnl(pnl)
                        risk_mgr.remove_position(trade["market_id"])

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

        for user in await asyncio.to_thread(database.get_all_active):
            cid = user["chat_id"]
            try:
                _, report = await run_learning(cid)
                log.info(f"Learning complete for {cid}")
                if tg_app:
                    import telegram_bot as tgb
                    await tgb.send_message(
                        tg_app, cid,
                        f"🧠 *Daily Intelligence Report*\n\n{report}",
                        parse_mode="Markdown",
                    )
            except Exception as e:
                log.error(f"Learning failed for {cid}: {e}")

async def stagnation_monitor(tg_app=None):
    """
    Checks for trading droughts. If no trades in 12h, activates 'Pantry Raid' mode.
    """
    while True:
        await asyncio.sleep(3600) # Check every hour
        users = await asyncio.to_thread(database.get_all_active)
        for user in users:
            cid = user["chat_id"]
            try:
                # Check last trade entry time
                with database._cx() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT created_at FROM trades WHERE chat_id=%s ORDER BY created_at DESC LIMIT 1", (cid,))
                        row = cur.fetchone()
                        if not row: continue
                        
                        val = row[0]
                        if isinstance(val, str):
                            last_trade = datetime.fromisoformat(val.replace("Z", "+00:00"))
                        else:
                            last_trade = val
                        
                        if last_trade.tzinfo is None:
                            last_trade = last_trade.replace(tzinfo=timezone.utc)
                            
                        # If more than 12 hours ago
                        if datetime.now(timezone.utc) - last_trade > timedelta(hours=12):
                            s = user["settings"]
                            learned = s.get("learned", {})
                            if not learned.get("pantry_raid_active"):
                                learned["pantry_raid_active"] = True
                                s["learned"] = learned
                                await asyncio.to_thread(database.update_settings, cid, s)
                                log.info(f"[{cid}] 🚨 PANTRY RAID ACTIVATED: Trading drought detected (>12h).")
                                if tg_app:
                                    import telegram_bot as tgb
                                    await tgb.send_message(tg_app, cid, "🚨 *Pantry Raid Activated* — No trades for 12h. Lowering hurdles to find alpha.")
                        else:
                            # Deactivate if we traded recently
                            s = user["settings"]
                            learned = s.get("learned", {})
                            if learned.get("pantry_raid_active"):
                                learned["pantry_raid_active"] = False
                                s["learned"] = learned
                                await asyncio.to_thread(database.update_settings, cid, s)
                                log.info(f"[{cid}] ✅ Pantry Raid deactivated.")
            except Exception as e:
                log.error(f"Stagnation monitor error for {cid}: {e}")
