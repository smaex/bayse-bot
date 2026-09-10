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

def _settlement_pnl(
    *, won: bool, amount_ngn: float, entry_price: float,
    filled_quantity: float = 0.0,
) -> float:
    """Compute settlement PnL in wallet currency from normalized shares."""
    amount = float(amount_ngn)
    if not won:
        return -amount
    shares = float(filled_quantity) or (
        amount / (float(entry_price) * config.CURRENCY_BASE_MULTIPLIER)
        if entry_price > 0 else 0.0
    )
    return shares * config.CURRENCY_BASE_MULTIPLIER - amount


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
    target_mid = trade.get("market_id")
    market = next((m for m in markets if m.get("id") == target_mid or m.get("marketId") == target_mid), None)
    if not market:
        market = markets[0] if markets else {}

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

                    # Early check: if the order itself is already cancelled/expired with 0 fill,
                    # resolve as unfilled immediately without waiting 15m/1h for the entire event to close.
                    if trade.get("order_id"):
                        try:
                            order_data   = await client.get_order(trade["order_id"])
                            order_status = str(order_data.get("status") or "").lower()
                            shares       = client.parse_filled_shares(order_data)
                            if shares <= 0 and order_status in ("cancelled", "expired", "rejected", "killed"):
                                log.info(f"[{chat_id}] Order {trade['order_id']} was cancelled on exchange ({order_status}) — resolving with 0.0 PnL")
                                await asyncio.to_thread(database.resolve_trade, trade["trade_id"], None, 0.0)
                                if user_risks and chat_id in user_risks:
                                    user_risks[chat_id].remove_position(
                                        trade["market_id"], order_id=trade.get("order_id", "")
                                    )
                                if tg_app:
                                    try:
                                        await tgb.notify_unfilled(
                                            tg_app, chat_id,
                                            trade.get("strategy", "MAKER"),
                                            trade.get("asset", "?"),
                                            trade.get("timeframe", ""),
                                            trade.get("outcome", ""),
                                            trade.get("amount_ngn", 0),
                                        )
                                    except Exception as ne:
                                        log.warning(f"[{chat_id}] notify_unfilled failed: {ne}")
                                continue
                        except Exception as oe:
                            log.debug(f"early get_order check: {oe}")

                    # Not resolved yet
                    if status not in ("resolved", "settled", "closed"):
                        continue

                    # Cancelled / voided markets — free the position without recording a loss
                    if status in ("cancelled", "voided", "invalid"):
                        log.info(f"[{chat_id}] Trade {trade['trade_id']} voided — skipping")
                        await asyncio.to_thread(database.resolve_trade, trade["trade_id"], None, 0.0)
                        if user_risks and chat_id in user_risks:
                            user_risks[chat_id].remove_position(
                                trade["market_id"], order_id=trade.get("order_id", "")
                            )
                        if tg_app:
                            try:
                                await tgb.notify_unfilled(
                                    tg_app, chat_id,
                                    trade.get("strategy", "MAKER"),
                                    trade.get("asset", "?"),
                                    trade.get("timeframe", ""),
                                    trade.get("outcome", ""),
                                    trade.get("amount_ngn", 0),
                                )
                            except Exception as ne:
                                log.warning(f"[{chat_id}] notify_unfilled (voided) failed: {ne}")
                        continue

                    resolved_label, market = _detect_resolution(event, trade)
                    if resolved_label is None:
                        continue

                    won = _resolved_won(resolved_label, trade, market)

                    # Try to get real PnL from the order API
                    pnl = None
                    actual_shares = None
                    actual_fill_price = None
                    if trade.get("order_id"):
                        try:
                            order_data   = await client.get_order(trade["order_id"])
                            order_status = str(order_data.get("status") or "").lower()
                            shares       = client.parse_filled_shares(order_data)
                            if shares <= 0 or order_status in ("cancelled", "expired", "open", "rejected", "killed"):
                                # Maker/Taker order was never filled — mark as 0.0 PnL without counting as a loss
                                log.info(f"[{chat_id}] Order {trade['order_id']} was unfilled (status={order_status}, shares={shares}) — resolving with 0.0 PnL")
                                await asyncio.to_thread(database.resolve_trade, trade["trade_id"], None, 0.0)
                                if user_risks and chat_id in user_risks:
                                    user_risks[chat_id].remove_position(
                                        trade["market_id"], order_id=trade.get("order_id", "")
                                    )
                                if tg_app:
                                    try:
                                        await tgb.notify_unfilled(
                                            tg_app, chat_id,
                                            trade.get("strategy", "MAKER"),
                                            trade.get("asset", "?"),
                                            trade.get("timeframe", ""),
                                            trade.get("outcome", ""),
                                            trade.get("amount_ngn", 0),
                                        )
                                    except Exception as ne:
                                        log.warning(f"[{chat_id}] notify_unfilled failed: {ne}")
                                continue

                            # Save fill data for precise PnL calculation below
                            actual_shares = shares
                            actual_fill_price = float(order_data.get("avgFillPrice") or order_data.get("price") or 0)

                            # If Bayse directly provides realized PnL, use it
                            raw = (order_data.get("profit") or order_data.get("pnl")
                                   or order_data.get("realizedPnl"))
                            if raw is not None:
                                pnl = float(raw)
                            elif actual_shares > 0 and actual_fill_price > 0:
                                # NGN has a base multiplier of 100: one winning
                                # share pays ₦100 and costs price×₦100. The old
                                # path omitted this multiplier and understated
                                # both wins and losses by roughly 100x.
                                fee = float(order_data.get("fee") or 0)
                                cost = (
                                    actual_shares * actual_fill_price
                                    * config.CURRENCY_BASE_MULTIPLIER
                                    + fee
                                )
                                pnl = (
                                    actual_shares * config.CURRENCY_BASE_MULTIPLIER - cost
                                    if won else -cost
                                )
                        except Exception as oe:
                            log.debug(f"get_order fallback: {oe}")

                    # Fallback PnL estimate (when order API unavailable)
                    if pnl is None:
                        entry  = trade["entry_price"]
                        amount = trade["amount_ngn"]
                        filled_quantity = float(trade.get("filled_quantity") or 0.0)
                        pnl = _settlement_pnl(
                            won=won,
                            amount_ngn=amount,
                            entry_price=entry,
                            filled_quantity=filled_quantity,
                        )

                    await asyncio.to_thread(database.resolve_trade, trade["trade_id"], won, pnl)

                    import strategy as strat_mod
                    if won:
                        strat_mod.record_success(trade["strategy"], trade["asset"])
                    else:
                        strat_mod.record_failure(trade["strategy"], trade["asset"])

                    if user_risks and chat_id in user_risks:
                        rm = user_risks[chat_id]
                        rm.add_pnl(pnl)
                        rm.remove_position(
                            trade["market_id"], order_id=trade.get("order_id", "")
                        )

                    # Stamp the resolution time so bot.py's quiet-state guard
                    # suppresses deposit/withdrawal detection for the next 60 s
                    # while the exchange balance and risk.deployed() re-sync.
                    import bot as _bot_mod
                    _bot_mod._last_resolution_time[chat_id] = __import__("time").time()

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

    # ── Mean-reversion: blend all multipliers 10% back toward default (1.0) ──
    # Without this, a suppressed strategy generates fewer trades (because it's
    # suppressed), which means less data to prove recovery, which means the
    # penalty persists → data starvation death spiral.  A 10% daily blend
    # gives a half-life of ~7 days: old adjustments naturally lose influence
    # over 2-3 weeks even without new trades.
    for k in list(mults.keys()):
        mults[k] = round(mults[k] * 0.90 + 1.0 * 0.10, 2)
    for k in list(cmults.keys()):
        cmults[k] = round(cmults[k] * 0.90 + 1.0 * 0.10, 2)

    reset_str = s.get("reset_learning_at")
    reset_dt = datetime.fromisoformat(reset_str) if reset_str else None

    stats    = await asyncio.to_thread(database.recent_stats, chat_id, days=30, after_dt=reset_dt)
    changes  = []
    warnings = []

    by_strategy: dict[str, list] = {}
    for row in stats:
        by_strategy.setdefault(row["strategy"], []).append(row)

    for strat, rows in by_strategy.items():
        total    = int(sum(r["total"] for r in rows))
        wins     = int(sum(r.get("wins") or 0 for r in rows))
        win_rate = wins / total if total > 0 else None
        strat_pnl = float(sum(r.get("total_pnl") or 0.0 for r in rows))
        counts[strat] = total

        if total < 10:
            continue

        expected_wr = 0.65 if strat == "SNIPE" else 0.55
        p_value     = binomial_cdf(wins, total, expected_wr)
        c           = cmults.get(strat, 1.0)

        if p_value < 0.05:
            # Certainty multiplier floor at 0.85 — this is a gentle nudge,
            # not a gate.  Size multipliers handle real throttling.
            # Symmetric rate: penalty and recovery both ±0.15 so strategies
            # recover at the same pace they're penalised.
            c = max(0.85, c - 0.15)
            warnings.append(f"⚠️ {strat} certainty penalised (p={p_value:.3f})")
        elif win_rate is not None and win_rate >= expected_wr and strat_pnl > 0:
            c = min(1.20, c + 0.05)
        cmults[strat] = round(c, 2)

        m = mults.get(strat, 1.0)
        total_pnl = strat_pnl
        total_deployed = float(sum(r.get("total_deployed") or 0.0 for r in rows))
        roi = total_pnl / total_deployed if total_deployed > 0 else 0.0
        if win_rate is not None:
            # Win rate alone is not profitability in a binary market: buying
            # at 0.85 can lose money with a high hit rate. Only positive net
            # PnL/ROI can increase size; negative PnL always decreases it.
            if total_pnl < 0 or roi < 0:
                m = max(0.25, m - 0.20)
            elif total >= 30 and roi >= 0.02 and win_rate >= expected_wr:
                m = min(1.25, m + 0.10)
        mults[strat] = round(m, 2)

        # SNIPE threshold tuning
        if strat == "SNIPE" and win_rate is not None:
            cur = learned.get("snipe_min_certainty", config.SNIPE_MIN_CERTAINTY)
            if win_rate < 0.50:
                new = min(round(cur + 0.02, 2), 0.70)
                learned["snipe_min_certainty"] = new
                changes.append(f"🎯 SNIPE certainty raised {cur} → {new}")
            elif win_rate > 0.70 and cur > 0.20:
                new = max(round(cur - 0.02, 2), 0.20)
                learned["snipe_min_certainty"] = new
                changes.append(f"🎯 SNIPE certainty eased {cur} → {new}")

    # Combo-level self-correction
    combos = await asyncio.to_thread(database.get_combo_stats, chat_id, days=14, after_dt=reset_dt)
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
            cv = max(0.85, cv - 0.15)
            warnings.append(f"🔴 SELF-CORRECT: {key} penalised (-15%) — p={pv:.3f}")
        elif total >= 20 and pv > 0.20 and wr >= exp_wr and pnl > 0:
            cv = min(1.20, cv + 0.05)
        cmults[key] = round(cv, 2)

    learned["size_multipliers"]     = mults
    learned["certainty_multipliers"] = cmults
    learned["trade_counts"]         = counts

    # Never lower standards merely because no trade appeared. A drought is not
    # evidence of alpha; the previous "pantry raid" introduced selection bias
    # precisely when the model found no qualifying edge.
    learned["pantry_raid_active"] = False

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
