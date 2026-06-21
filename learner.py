"""
Intelligence loop — trade resolution + daily self-improvement.

Resolution fix: tries 4 different field patterns because Bayse docs
don't explicitly document the resolvedOutcome field name.
"""

import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta

import config
import database

log = logging.getLogger("learner")

DEFAULT_LEARNED: dict = {
    "snipe_min_certainty":      config.SNIPE_MIN_CERTAINTY,
    "correlation_threshold":    config.CORRELATION_THRESHOLD,
    "size_multipliers":         {s: 1.0 for s in config.ACTIVE_STRATEGIES},
    "certainty_multipliers":    {s: 1.0 for s in config.ACTIVE_STRATEGIES},
    "trade_counts":             {},
}


def get_learned_overrides(chat_id: str) -> dict:
    user = database.get_user(chat_id)
    if not user:
        return DEFAULT_LEARNED.copy()
    s = user.get("settings", {})
    learned = {**DEFAULT_LEARNED, **s.get("learned", {})}
    learned["mode"] = s.get("mode", "balanced")
    return learned


def binomial_cdf(k: int, n: int, p: float) -> float:
    cdf = 0.0
    for i in range(k + 1):
        cdf += math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return cdf


# ── Resolution ────────────────────────────────────────────────────────────────

def _resolved_won(resolved_label: str, trade: dict, market: dict) -> bool:
    """Determine win/loss from the resolved outcome label."""
    yes_label = (market.get("outcome1Label") or "YES").upper()
    no_label  = (market.get("outcome2Label") or "NO").upper()
    r         = resolved_label.upper().strip()
    yes_set   = {"YES", "UP", "1", yes_label}
    no_set    = {"NO",  "DOWN", "2", no_label}

    if r in yes_set:
        return trade["outcome"].upper() == "YES"
    if r in no_set:
        return trade["outcome"].upper() == "NO"
    # Fallback: direct outcome_id comparison
    return resolved_label == trade.get("outcome_id", "")


def _detect_resolution(event: dict, trade: dict) -> tuple:
    """
    Returns (resolved_label, market_dict) or (None, None) if not resolved.
    Tries 4 field patterns because the Bayse API docs are incomplete here.
    """
    markets = event.get("markets", [{}])
    market  = markets[0] if markets else {}

    # Method 1: direct resolvedOutcome on market
    r = market.get("resolvedOutcome") or event.get("resolvedOutcome")
    if r and str(r).upper() not in ("", "NONE", "NULL", "PENDING"):
        return r, market

    # Method 2: resolved outcome ID → map to label
    rid = market.get("resolvedOutcomeId") or event.get("resolvedOutcomeId")
    if rid:
        if rid == market.get("outcome1Id"):
            return market.get("outcome1Label", "YES"), market
        if rid == market.get("outcome2Id"):
            return market.get("outcome2Label", "NO"), market

    # Method 3: one outcome price settled at 1.0
    p1 = float(market.get("outcome1Price") or 0)
    p2 = float(market.get("outcome2Price") or 0)
    if p1 >= 0.99:
        return market.get("outcome1Label", "YES"), market
    if p2 >= 0.99:
        return market.get("outcome2Label", "NO"), market

    # Method 4: event status says resolved but no specific field — assume from price
    status = event.get("status", "").lower()
    if status in ("resolved", "settled"):
        # Both prices collapsed — can't determine winner without more info
        log.warning(f"Event {event.get('id')} is resolved but no outcome field found. Skipping.")
        return None, None

    return None, None


async def resolution_monitor(user_clients: dict, user_risks: dict = None, tg_app=None):
    """Check unresolved trades every 30 seconds (was 2 minutes).

    Previously only checked trades 6+ minutes old, which created a long
    window where Bayse's real balance already reflected a trade's
    resolution while our own risk.deployed() tracking hadn't caught up yet
    — directly contributing to false deposit/withdrawal detection on 15-min
    markets where SNIPE often enters in the final seconds before close.
    """
    import telegram_bot as tgb

    while True:
        await asyncio.sleep(30)
        for chat_id, client in list(user_clients.items()):
            pending = await asyncio.to_thread(database.get_unresolved, chat_id, older_than_minutes=1)
            for trade in pending:
                try:
                    event   = await client.get_event(trade["event_id"])
                    status  = event.get("status", "").lower()

                    # Not resolved yet
                    if status not in ("resolved", "settled", "closed"):
                        continue

                    # Cancelled / voided markets — free the position without recording a loss
                    if status in ("cancelled", "voided", "invalid"):
                        log.info(f"[{chat_id}] Trade {trade['trade_id']} voided — skipping")
                        if user_risks and chat_id in user_risks:
                            user_risks[chat_id].remove_position(trade["market_id"])
                        continue

                    resolved_label, market = _detect_resolution(event, trade)
                    if resolved_label is None:
                        continue

                    won = _resolved_won(resolved_label, trade, market)

                    # Try to get real PnL from the order API
                    pnl = None
                    if trade.get("order_id"):
                        try:
                            order_data  = await client.get_order(trade["order_id"])
                            shares      = float(order_data.get("quantity") or
                                                order_data.get("filledSize") or
                                                order_data.get("shares") or 0)
                            if shares <= 0:
                                # Maker order never filled
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

                    # Fallback PnL estimate
                    if pnl is None:
                        fr     = float((market or {}).get("feePercentage", 2)) / 100
                        entry  = trade["entry_price"]
                        amount = trade["amount_ngn"]
                        if won:
                            shares  = amount / entry
                            fee_amt = fr * shares * entry * max(1 - entry, config.FEE_FLOOR)
                            pnl     = shares * (1.0 - entry) - fee_amt
                        else:
                            pnl = -amount

                    await asyncio.to_thread(database.resolve_trade, trade["trade_id"], won, pnl)

                    import strategy as strat_mod
                    if won:
                        strat_mod.record_success(trade["strategy"], trade["asset"])
                    else:
                        strat_mod.record_failure(trade["strategy"], trade["asset"])

                    if user_risks and chat_id in user_risks:
                        rm = user_risks[chat_id]
                        rm.add_pnl(pnl)
                        rm.remove_position(trade["market_id"])

                    result = "WIN" if won else "LOSS"
                    log.info(
                        f"[{chat_id}] RESOLVED {result} | {trade['strategy']} "
                        f"{trade['asset']} {trade['timeframe']} | pnl=₦{pnl:+,.2f}"
                    )

                    if tg_app:
                        try:
                            fn = tgb.notify_win if won else tgb.notify_loss
                            await fn(tg_app, chat_id, trade["market_id"],
                                     trade["asset"], trade["timeframe"],
                                     trade["strategy"], pnl)
                        except Exception as ne:
                            log.warning(f"[{chat_id}] Notify failed: {ne}")

                except Exception as e:
                    log.warning(f"[{chat_id}] Resolution check failed {trade['trade_id']}: {e}")


# ── Daily learning ────────────────────────────────────────────────────────────

async def run_learning(chat_id: str) -> tuple[dict, str]:
    user = await asyncio.to_thread(database.get_user, chat_id)
    if not user:
        return DEFAULT_LEARNED.copy(), "User not found."

    s       = user["settings"]
    learned = {**DEFAULT_LEARNED, **s.get("learned", {})}
    mults   = dict(learned.get("size_multipliers",    {k: 1.0 for k in config.ACTIVE_STRATEGIES}))
    cmults  = dict(learned.get("certainty_multipliers", {k: 1.0 for k in config.ACTIVE_STRATEGIES}))
    counts  = {}

    stats    = await asyncio.to_thread(database.recent_stats, chat_id, days=30)
    changes  = []
    warnings = []

    by_strategy: dict[str, list] = {}
    for row in stats:
        by_strategy.setdefault(row["strategy"], []).append(row)

    for strat, rows in by_strategy.items():
        total    = int(sum(r["total"] for r in rows))
        wins     = int(sum(r.get("wins") or 0 for r in rows))
        win_rate = wins / total if total > 0 else None
        counts[strat] = total

        if total < 10:
            continue

        expected_wr = 0.65 if strat == "SNIPE" else 0.55
        p_value     = binomial_cdf(wins, total, expected_wr)
        c           = cmults.get(strat, 1.0)

        if p_value < 0.05:
            # FLOOR RAISED 0.40 → 0.85: certainty_multipliers should never be
            # able to gate a strategy out of existence — that job belongs to
            # size_multipliers (floored at 0.25 below), which throttles
            # position SIZE, not whether a signal is allowed to fire at all.
            # The old 0.40 floor created a structural asymmetry: ARB's
            # certainty starts at a hardcoded 1.0, so 1.0*0.40=0.40 still
            # clears the 0.35 discovery floor — ARB was immune. SNIPE's raw
            # certainty typically runs 0.50-0.85, so the same 0.40 multiplier
            # could push it to 0.20-0.34 — below the floor, silencing SNIPE
            # for days while ARB kept trading. At 0.85 the multiplier is a
            # gentle nudge, not a gate.
            c = max(0.85, c - 0.20)
            warnings.append(f"⚠️ {strat} certainty penalised (p={p_value:.3f})")
        elif win_rate is not None and win_rate >= expected_wr:
            c = min(1.5, c + 0.10)
        cmults[strat] = round(c, 2)

        m = mults.get(strat, 1.0)
        if win_rate is not None:
            if win_rate >= 0.75:   m = min(3.0, m + 0.25)
            elif win_rate >= 0.60: m = min(1.5, m + 0.10)
            elif win_rate < 0.55:  m = max(0.25, m - 0.20)  # was max(0.10, m-0.25)
        mults[strat] = round(m, 2)

        # SNIPE threshold tuning
        if strat == "SNIPE" and win_rate is not None:
            cur = learned.get("snipe_min_certainty", config.SNIPE_MIN_CERTAINTY)
            if win_rate < 0.50:
                new = min(round(cur + 0.03, 2), 0.70)
                learned["snipe_min_certainty"] = new
                changes.append(f"🎯 SNIPE certainty raised {cur} → {new}")
            elif win_rate > 0.70 and cur > 0.45:
                new = max(round(cur - 0.02, 2), 0.45)
                learned["snipe_min_certainty"] = new
                changes.append(f"🎯 SNIPE certainty eased {cur} → {new}")

    # Combo-level self-correction
    combos = await asyncio.to_thread(database.get_combo_stats, chat_id, days=14)
    for c in combos:
        key    = f"{c['strategy']}:{c['asset']}:{c['timeframe']}"
        wr     = c["win_rate"]
        total  = int(c["total"])
        pnl    = c.get("total_pnl") or 0
        exp_wr = 0.65 if c["strategy"] == "SNIPE" else 0.55
        wins_n = int(wr * total)
        pv     = binomial_cdf(wins_n, total, exp_wr)
        cv     = cmults.get(key, 1.0)

        if total >= 10 and pv < 0.05 and pnl < 0:
            cv = max(0.85, cv - 0.35)  # same architectural fix as the per-strategy floor above
            warnings.append(f"🔴 SELF-CORRECT: {key} penalised (-35%) — p={pv:.3f}")
        elif pv > 0.20 and wr >= exp_wr:
            cv = min(1.5, cv + 0.20)
        cmults[key] = round(cv, 2)

    learned["size_multipliers"]     = mults
    learned["certainty_multipliers"] = cmults
    learned["trade_counts"]         = counts

    s["learned"] = learned
    await asyncio.to_thread(database.update_settings, chat_id, s)

    overall = await asyncio.to_thread(database.all_time_stats, chat_id)
    lines   = [
        "🧠 *Daily Learning Report*",
        f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        f"📊 All-time: {overall['wins']}/{overall['total']} | "
        f"{overall['win_rate']:.0%} WR | ₦{overall['total_pnl']:+,.0f}",
        "",
        "📈 30-day breakdown:",
    ]
    for row in stats:
        pnl = row.get("total_pnl") or 0
        lines.append(
            f"  {row['strategy']} {row['asset']} {row['timeframe']}: "
            f"{row['total']} trades | {row['win_rate']:.0%} WR | ₦{pnl:,.0f}"
        )
    if changes:
        lines += ["", "⚙️ Changes:"] + [f"  {c}" for c in changes]
    if warnings:
        lines += ["", "🚨 Warnings:"] + [f"  {w}" for w in warnings]
    if not changes and not warnings and stats:
        lines.append("\n✅ All strategies performing well.")

    # Temporal performance
    temporal = await asyncio.to_thread(database.get_hourly_stats, chat_id)
    if temporal:
        lines.append("\n🕒 Best trading hours (UTC):")
        for h in sorted(temporal, key=lambda x: x["win_rate"], reverse=True)[:3]:
            lines.append(f"  🌟 {h['hour']:02d}:00 — {h['win_rate']:.0%} WR ({h['total']} trades)")

    return learned, "\n".join(lines)


async def daily_learning_loop(tg_app=None):
    import telegram_bot as tgb
    while True:
        now      = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait     = (midnight - now).total_seconds()
        log.info(f"Next learning cycle in {wait/3600:.1f}h")
        await asyncio.sleep(wait)

        for user in await asyncio.to_thread(database.get_all_active):
            cid = user["chat_id"]
            try:
                _, report = await run_learning(cid)
                if tg_app:
                    await tgb.send_message(
                        tg_app, cid,
                        f"🧠 *Daily Intelligence Report*\n\n{report}",
                        parse_mode="Markdown",
                    )
            except Exception as e:
                log.error(f"Learning failed for {cid}: {e}")
