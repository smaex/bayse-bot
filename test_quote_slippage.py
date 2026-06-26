"""
Test script: Pre-trade quote & slippage verification.

Queries Bayse AMM quotes at different trade sizes for active markets
to verify:
  1. get_quote endpoint works correctly
  2. AMM price impact / slippage at different sizes
  3. New size-scaling-down logic in executor.py is properly calibrated

Usage:
  python test_quote_slippage.py

Requires BAYSE_PUBLIC_KEY and BAYSE_SECRET_KEY env vars (or .env file).
"""

import asyncio
import os
import sys
import json

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from client import BayseClient
from config import CURRENCY
import scanner


async def test_quotes():
    pub = os.getenv("BAYSE_PUBLIC_KEY") or os.getenv("PUBLIC_KEY", "")
    sec = os.getenv("BAYSE_SECRET_KEY") or os.getenv("SECRET_KEY", "")

    if not pub or not sec:
        print("❌ Set BAYSE_PUBLIC_KEY & BAYSE_SECRET_KEY env vars")
        return

    client = BayseClient(pub, sec)
    print(f"💰 Currency: {CURRENCY}")
    print(f"🔑 Key: {pub[:8]}...")

    # Fetch balance
    try:
        balance = await client.get_balance_ngn()
        print(f"💵 Balance: ₦{balance:,.2f}")
    except Exception as e:
        print(f"⚠️  Balance fetch failed: {e}")

    # Scan markets
    print("\n📡 Scanning active markets...")
    markets = await scanner.scan_all(client)
    print(f"   Found {len(markets)} active markets\n")

    if not markets:
        print("No active markets found. Exiting.")
        await client.close()
        return

    # Test quotes at different sizes
    test_sizes = [100, 500, 1000, 2000]
    results = []

    for market in markets[:6]:  # Test up to 6 markets
        asset = market["asset"]
        tf = market["timeframe"]
        yes_p = market["yes_price"]
        no_p = market["no_price"]
        mid = market["market_id"]
        eid = market["event_id"]
        yes_id = market.get("yes_id", "")
        secs = market.get("secs_to_close", 0)

        print(f"━━━ {asset} {tf} ━━━")
        print(f"   Market price: YES={yes_p:.3f} NO={no_p:.3f} | {secs:.0f}s remaining")
        print(f"   Fee rate: {market.get('fee_rate', 0.02):.2%}")

        if not yes_id:
            print("   ⚠️  No yes_id found, skipping\n")
            continue

        for size in test_sizes:
            try:
                quote = await client.get_quote(
                    event_id=eid, market_id=mid,
                    outcome_id=yes_id, side="BUY",
                    amount=size, currency=CURRENCY
                )
                q_price = float(quote.get("price", 0))
                q_qty = float(quote.get("quantity", 0))
                q_fee = float(quote.get("fee", 0))
                q_cost = float(quote.get("costOfShares", 0))
                impact = q_price - yes_p if yes_p > 0 else 0
                impact_pct = (impact / yes_p * 100) if yes_p > 0 else 0

                print(
                    f"   ₦{size:>5,} → price={q_price:.4f} "
                    f"(impact={impact_pct:+.2f}%) "
                    f"qty={q_qty:.2f} fee=₦{q_fee:.2f}"
                )

                results.append({
                    "asset": asset, "tf": tf, "size": size,
                    "market_price": yes_p, "quote_price": q_price,
                    "impact_pct": impact_pct, "qty": q_qty, "fee": q_fee,
                })

            except Exception as e:
                print(f"   ₦{size:>5,} → ❌ {e}")

        print()

    # Summary
    if results:
        print("═══ SLIPPAGE SUMMARY ═══")
        print(f"{'Asset':<8} {'TF':<6} {'Size':>6} {'Impact%':>8} {'Fee':>8}")
        print("─" * 40)
        for r in results:
            print(
                f"{r['asset']:<8} {r['tf']:<6} "
                f"₦{r['size']:>5,} "
                f"{r['impact_pct']:>+7.2f}% "
                f"₦{r['fee']:>7.2f}"
            )

        # Average impact by size
        print("\n═══ AVG IMPACT BY SIZE ═══")
        for size in test_sizes:
            size_results = [r for r in results if r["size"] == size]
            if size_results:
                avg_impact = sum(r["impact_pct"] for r in size_results) / len(size_results)
                avg_fee = sum(r["fee"] for r in size_results) / len(size_results)
                print(f"   ₦{size:>5,} → avg impact: {avg_impact:+.3f}% | avg fee: ₦{avg_fee:.2f}")

    # Test orderbook endpoint
    print("\n═══ ORDERBOOK TEST ═══")
    for market in markets[:3]:
        yes_id = market.get("yes_id", "")
        if not yes_id:
            continue
        try:
            ob = await client.get_orderbook(yes_id)
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            engine = "CLOB" if (bids or asks) else "AMM"
            print(f"   {market['asset']} {market['timeframe']}: engine={engine} bids={len(bids)} asks={len(asks)}")
        except Exception as e:
            print(f"   {market['asset']} {market['timeframe']}: ❌ {e}")

    await client.close()
    print("\n✅ Done")


if __name__ == "__main__":
    asyncio.run(test_quotes())
