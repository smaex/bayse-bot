#!/usr/bin/env python3
"""Read-only Bayse API contract probe; never loads credentials or places orders."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import BayseClient

SERIES = ("crypto-btc-15min", "crypto-sol-15min")


async def main() -> int:
    client = BayseClient("", "")
    checks = []
    try:
        for slug in SERIES:
            events = await client.get_series_events(slug)
            checks.append({"series": slug, "events": len(events)})
            if not events:
                continue
            event_id = events[0].get("id")
            event = await client.get_event(event_id, currency="NGN")
            market = (event.get("markets") or [None])[0]
            if not market:
                raise RuntimeError(f"{slug}: event has no market")
            required = {"id", "outcome1Id", "outcome2Id"}
            missing = sorted(required - set(market))
            if missing:
                raise RuntimeError(f"{slug}: missing market fields {missing}")

            market_id = market["id"]
            outcome_id = market["outcome1Id"]
            quote = await client.get_quote(
                event_id, market_id, outcome_id, "BUY", 100, "NGN"
            )
            quote_required = {
                "price", "quantity", "amount", "currencyBaseMultiplier",
                "completeFill",
            }
            quote_missing = sorted(quote_required - set(quote))
            if quote_missing:
                raise RuntimeError(f"{slug}: missing quote fields {quote_missing}")

            engine = str(event.get("engine") or market.get("engine") or "").upper()
            check = {
                "series": slug,
                "engine": engine,
                "quote_complete": quote.get("completeFill"),
                "quote_price": quote.get("price"),
                "quote_quantity": quote.get("quantity"),
            }
            if engine == "CLOB":
                book = await client.get_orderbook(outcome_id, depth=2)
                check["book_bids"] = len(book.get("bids") or [])
                check["book_asks"] = len(book.get("asks") or [])
                check["book_has_timestamp"] = bool(book.get("timestamp"))
            checks.append(check)
    except Exception as exc:
        print(json.dumps({
            "read_only": True,
            "writes_attempted": 0,
            "reachable": False,
            "error": str(exc),
            "checks": checks,
        }, indent=2))
        return 2
    finally:
        await client.close()

    print(json.dumps({
        "read_only": True,
        "writes_attempted": 0,
        "reachable": True,
        "checks": checks,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
