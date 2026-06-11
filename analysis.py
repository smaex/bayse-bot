"""
Financial & investment analysis module.

Produces a report on:
  - Win rate and PnL per strategy type
  - Edge per timeframe (how often we're right)
  - Fee drag analysis
  - Capital efficiency (returns per NGN deployed)
  - Recommendation on where to focus capital
"""

import logging
import asyncio
import database
from client import BayseClient

log = logging.getLogger(__name__)


async def full_report(client: BayseClient, chat_id: str | None = None) -> str:
    """Fetch trade history and produce a full analysis report."""
    try:
        wallet   = await client.get_wallet()
        pnl_data = await client.get_pnl()
    except Exception as e:
        return f"Error fetching data: {e}"

    balance = 0.0
    assets = wallet if isinstance(wallet, list) else wallet.get("assets", [])
    for asset in assets:
        currency = (asset.get("currency") or asset.get("symbol") or "").upper()
        if currency == "NGN":
            for field in ("availableBalance", "available", "balance", "total"):
                v = asset.get(field)
                if v is not None and float(v) > 0:
                    balance = float(v)
                    break

    realized_pnl = float(pnl_data.get("realizedPnl", 0) or pnl_data.get("pnl", 0))

    stats_rows = await asyncio.to_thread(database.recent_stats, chat_id, days=30) if chat_id else []
    all_time   = await asyncio.to_thread(database.all_time_stats, chat_id) if chat_id else {}
    recent     = await asyncio.to_thread(database.recent_trades, chat_id, limit=5) if chat_id else []

    total = all_time.get("total", 0)

    lines = [
        "📈 *Trading Analysis Report*",
        "",
        f"💰 *Balance:* ₦{balance:,.2f}",
        f"📊 *Realized PnL (all-time):* ₦{realized_pnl:,.2f}",
        "",
    ]

    if total == 0:
        lines += [
            "No resolved trades yet.",
            "This report will fill in once trades start resolving.",
        ]
        return "\n".join(lines)

    wins      = all_time.get("wins", 0)
    losses    = all_time.get("losses", 0)
    win_rate  = all_time.get("win_rate", 0)
    total_pnl = all_time.get("total_pnl", 0)

    lines += [
        "🎯 *All-Time Summary*",
        f"   Trades: {total}  |  Wins: {wins}  |  Losses: {losses}",
        f"   Win rate: {win_rate:.1%}",
        f"   Total PnL: ₦{total_pnl:+,.2f}",
        "",
    ]

    # ── Risk-Reward Analysis ──────────────────────────────────────────────────
    recent_resolved = [t for t in recent if t.get("won") is not None and t.get("pnl_ngn") is not None]
    if recent_resolved:
        wins_only = [t["pnl_ngn"] for t in recent_resolved if t["won"] == 1]
        loss_only = [abs(t["pnl_ngn"]) for t in recent_resolved if t["won"] == 0]
        avg_win   = sum(wins_only) / len(wins_only) if wins_only else 0
        avg_loss  = sum(loss_only) / len(loss_only) if loss_only else 0
        rr_ratio  = avg_win / avg_loss if avg_loss > 0 else 0

        lines += [
            "⚖️ *Risk-Reward (last 5 resolved)*",
            f"   Avg Win: ₦{avg_win:,.0f}  |  Avg Loss: ₦{avg_loss:,.0f}",
            f"   RR Ratio: 1:{rr_ratio:.2f} {'(Healthy ✅)' if rr_ratio > 0.15 else '(Skewed ⚠️)'}",
            "",
        ]

    # ── Per-strategy breakdown ────────────────────────────────────────────────
    by_strategy: dict[str, dict] = {}
    by_asset:    dict[str, dict] = {}
    by_tf:       dict[str, dict] = {}

    for r in stats_rows:
        for key, bucket in [
            (r["strategy"] or "UNKNOWN", by_strategy),
            (r["asset"]    or "?",       by_asset),
            (r["timeframe"]or "?",       by_tf),
        ]:
            if key not in bucket:
                bucket[key] = {"total": 0, "wins": 0, "pnl": 0.0}
            bucket[key]["total"] += r["total"]
            bucket[key]["wins"]  += r["wins"] or 0
            bucket[key]["pnl"]   += r["total_pnl"] or 0.0

    if by_strategy:
        lines.append("📋 *Strategy Breakdown (last 30 days)*")
        for strat, d in sorted(by_strategy.items()):
            wr = d["wins"] / d["total"] if d["total"] else 0
            lines.append(f"   *{strat}*: {d['total']} trades, {wr:.0%} WR, ₦{d['pnl']:+,.0f}")
        lines.append("")

    if by_asset:
        lines.append("🪙 *Asset Breakdown (last 30 days)*")
        for asset, d in sorted(by_asset.items()):
            wr = d["wins"] / d["total"] if d["total"] else 0
            lines.append(f"   *{asset}*: {d['total']} trades, {wr:.0%} WR, ₦{d['pnl']:+,.0f}")
        lines.append("")

    if by_tf:
        lines.append("⏱ *Timeframe Breakdown (last 30 days)*")
        for tf, d in sorted(by_tf.items()):
            wr = d["wins"] / d["total"] if d["total"] else 0
            lines.append(f"   *{tf}*: {d['total']} trades, {wr:.0%} WR, ₦{d['pnl']:+,.0f}")
        lines.append("")

    # ── Fee drag ─────────────────────────────────────────────────────────────
    avg_fee_drag = 0.04 * 0.50 * 0.50  # ~1% at mid-market
    lines += [
        "💸 *Fee Analysis*",
        f"   Estimated fee drag: {avg_fee_drag:.2%} per trade",
        f"   Break-even accuracy needed: {50 + avg_fee_drag * 100:.1f}%+",
        "",
    ]

    # ── Recent trades ─────────────────────────────────────────────────────────
    if recent:
        lines.append("🕐 *Last 5 Trades*")
        for t in recent:
            result  = "✅" if t.get("won") == 1 else ("❌" if t.get("won") == 0 else "⏳")
            pnl_str = f"₦{t['pnl_ngn']:+,.0f}" if t.get("pnl_ngn") is not None else "pending"
            lines.append(
                f"   {result} {t.get('strategy','?')} {t.get('asset','?')} "
                f"{t.get('timeframe','?')} {t.get('outcome','?')} — {pnl_str}"
            )
        lines.append("")

    lines += _recommendations(win_rate, total, total_pnl, by_strategy, by_asset)
    return "\n".join(lines)


def _recommendations(win_rate, total, total_pnl, by_strategy, by_asset) -> list[str]:
    lines = ["💡 *Recommendations*"]

    snipe = by_strategy.get("SNIPE", {})
    if snipe.get("total", 0) >= 10:
        wr = snipe["wins"] / snipe["total"]
        if wr < 0.88:
            lines.append(f"   ⚠️ SNIPE WR too low ({wr:.0%}). Needs >87% to be profitable.")

    if total < 10:
        lines.append("   Need 30+ trades for statistical significance.")
    elif win_rate >= 0.85 and total_pnl > 0:
        lines.append("   Strong performance. Risk-Reward is healthy.")
    elif total_pnl < 0:
        lines.append("   In drawdown. Bot is in Selective mode to protect capital.")

    for strat, d in by_strategy.items():
        if d["total"] >= 5 and d["pnl"] < 0:
            wr = d["wins"] / d["total"]
            lines.append(f"   ⚠️ {strat} losing (₦{d['pnl']:+,.0f}, {wr:.0%} WR) — consider /set strategies")

    for asset, d in by_asset.items():
        if d["total"] >= 5 and d["pnl"] < 0:
            wr = d["wins"] / d["total"]
            lines.append(f"   ⚠️ {asset} losing (₦{d['pnl']:+,.0f}, {wr:.0%} WR) — consider /set assets")

    return lines
