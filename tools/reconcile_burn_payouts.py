"""Read-only audit of complete-set burns whose cash was booked on a guess.

Before the 2026-11-01 fix, a complete-set burn whose payout could not be read
was booked at the guaranteed amount (shares x base multiplier). Each such
booking is announced by a "Fill reconciliation required" alert whose reason
contains "used guaranteed set payout" (or the older "used guaranteed payout").

This tool:

1. Scans log files for those alerts and groups them by account and market.
   Post-fix pauses ("cash received is unverified") are reported separately:
   they never booked cash, so they are not phantom.
2. With --check-accounts, reads each affected account's NGN wallet balance and
   its portfolio. For each flagged market it reports the complete sets still
   held (min of YES and NO). Those shares were never burned, so they are value
   the account still owns and can burn correctly with the fixed code.

It never writes: no database writes, no orders, no burns. Run the account
check where the bot runs (DATABASE_URL and the encryption key must be set),
because account keys are read from the database.

Usage:
    python tools/reconcile_burn_payouts.py --logs '/var/log/bayse/*.log'
    python tools/reconcile_burn_payouts.py --logs bot.log --check-accounts
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import re
import sys
from collections import defaultdict

BOOKED_MARKERS = ("used guaranteed set payout", "used guaranteed payout")
UNVERIFIED_MARKER = "cash received is unverified"
BASE_UNIT_NGN = 100.0

_ALERT = re.compile(
    r"\[(?P<chat>[^\]\s]+)\][^\n]*?Fill reconciliation required for order (?P<order>\S+)"
)


def parse_alerts(lines):
    """Yield (chat_id, market_id_or_None, order, category, reason) per alert.

    category is "booked" for a phantom-cash booking, "unverified" for a
    post-fix pause that booked nothing.
    """
    for line in lines:
        match = _ALERT.search(line)
        if not match:
            continue
        reason = line.split("Reason:", 1)[1].strip() if "Reason:" in line else line
        if any(marker in reason for marker in BOOKED_MARKERS):
            category = "booked"
        elif UNVERIFIED_MARKER in reason:
            category = "unverified"
        else:
            continue
        order = match.group("order")
        market = order.split("burn:", 1)[1] if order.startswith("burn:") else None
        if market is not None and not market:
            market = None
        yield match.group("chat"), market, order, category, reason


def summarize(alerts):
    """Group alerts per account: booked phantom bookings and paused-only events."""
    accounts: dict[str, dict] = defaultdict(
        lambda: {"booked_events": 0, "unverified_events": 0, "markets": set(), "orders": []}
    )
    for chat, market, order, category, _reason in alerts:
        entry = accounts[chat]
        if category == "booked":
            entry["booked_events"] += 1
            entry["orders"].append(order)
            if market:
                entry["markets"].add(market)
        else:
            entry["unverified_events"] += 1
    return {chat: {**data, "markets": sorted(data["markets"])} for chat, data in accounts.items()}


def market_holdings(markets, portfolio_rows):
    """Complete sets still held per market, from the account's portfolio rows.

    A complete set is one YES plus one NO share, so the sets still held are
    min(YES, NO). Those shares were never burned.
    """
    held: dict[str, dict[str, float]] = defaultdict(lambda: {"YES": 0.0, "NO": 0.0})
    for row in portfolio_rows or []:
        market_id = ((row.get("market") or {}).get("id")) or row.get("marketId")
        outcome = str(row.get("outcome") or "").upper()
        if not market_id or outcome not in ("YES", "NO"):
            continue
        try:
            held[market_id][outcome] += float(row.get("balance") or 0.0)
        except (TypeError, ValueError):
            continue
    out = {}
    for market in markets:
        pair = held.get(market, {"YES": 0.0, "NO": 0.0})
        sets = min(pair["YES"], pair["NO"])
        out[market] = {
            "yes": round(pair["YES"], 4),
            "no": round(pair["NO"], 4),
            "complete_sets_held": round(sets, 4),
            "value_ngn_if_burned": round(sets * BASE_UNIT_NGN, 2),
        }
    return out


def _read_lines(paths):
    for pattern in paths:
        matches = sorted(glob.glob(pattern)) or [pattern]
        for path in matches:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    yield from handle
            except OSError as exc:
                print(f"skip {path}: {exc}", file=sys.stderr)


async def _check_account(chat_id: str):
    """Read-only: wallet NGN balance and portfolio for one account."""
    import database
    from client import BayseClient

    user = await asyncio.to_thread(database.get_user, chat_id)
    if not user or not user.get("public_key") or not user.get("secret_key"):
        return None, None, "no usable API credentials in the database"
    client = BayseClient(user["public_key"], user["secret_key"])
    try:
        wallet = await client.get_wallet()
        assets = wallet if isinstance(wallet, list) else wallet.get("assets", [])
        ngn = 0.0
        for asset in assets:
            if str(asset.get("currency") or asset.get("symbol") or "").upper() == "NGN":
                ngn = float(asset.get("availableBalance") or asset.get("balance") or 0.0)
                break
        portfolio = await client.get_portfolio()
        rows = portfolio.get("outcomeBalances", []) if isinstance(portfolio, dict) else []
        return ngn, rows, ""
    finally:
        await client.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--logs", nargs="+", required=True, help="log file paths or globs")
    parser.add_argument("--check-accounts", action="store_true",
                        help="query wallet and portfolio for each affected account (read-only)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)

    alerts = list(parse_alerts(_read_lines(args.logs)))
    report = summarize(alerts)

    if args.check_accounts:
        for chat, data in report.items():
            if not data["markets"]:
                continue
            ngn, rows, error = asyncio.run(_check_account(chat))
            if error:
                data["error"] = error
                continue
            data["wallet_ngn"] = ngn
            data["markets_held"] = market_holdings(data["markets"], rows)

    if args.json:
        print(json.dumps(report, indent=2, default=list))
        return 0

    if not report:
        print("No burn payout alerts found in the given logs.")
        return 0
    for chat, data in sorted(report.items()):
        print(f"account {chat}")
        print(f"  phantom-cash bookings: {data['booked_events']}  "
              f"(orders: {', '.join(data['orders']) or 'none'})")
        print(f"  paused, nothing booked: {data['unverified_events']}")
        if data.get("error"):
            print(f"  account check skipped: {data['error']}")
        if "wallet_ngn" in data:
            print(f"  wallet NGN available now: {data['wallet_ngn']:,.2f}")
        for market, held in data.get("markets_held", {}).items():
            print(f"  market {market}: YES {held['yes']}  NO {held['no']}  "
                  f"complete sets still held {held['complete_sets_held']} "
                  f"(≈ ₦{held['value_ngn_if_burned']:,.2f} once burned correctly)")
    print("\nCompare each account's wallet balance with the bot's /status free cash.")
    print("A gap of roughly the booked amounts confirms phantom cash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
