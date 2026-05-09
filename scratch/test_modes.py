import sys
import os
import asyncio
import logging

# Mock logging and feeds before importing strategy
import logging
logging.basicConfig(level=logging.INFO)

# Add parent dir to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy
import feeds
import config

# Mock feeds.spot
feeds.spot = {
    "BTC": 60000.0,
    "ETH": 3000.0,
    "EURUSD": 1.0800,
}

# Mock market data
def create_market(asset="BTC", tf="1h", secs=3600, threshold=59500):
    return {
        "market_id": f"m_{asset}_{tf}",
        "event_id": f"e_{asset}",
        "asset": asset,
        "timeframe": tf,
        "secs_to_close": secs,
        "threshold": threshold,
        "yes_id": "y",
        "no_id": "n",
        "yes_price": 0.50,
        "no_price": 0.50,
        "title": f"{asset} > {threshold}",
    }

async def run_test():
    print("=== Testing Engine Profiles ===\n")
    
    # 1. Safe Mode - BTC 1h (Low Noise)
    market = create_market(asset="BTC", tf="1h", secs=1200, threshold=59500) # distance ~0.84%
    # distance = (60000 - 59500) / 59500 = 0.0084
    # With BTC hourly vol 0.018, 1200s (0.33h)
    # z = 0.0084 / (0.018 * sqrt(0.33)) = 0.0084 / 0.0104 = 0.80
    # prob = norm_cdf(0.8) = 0.788
    # certainty = (0.788 - 0.5) / 0.45 = 0.64
    
    print("--- Test 1: Safe Mode | BTC 1h | Threshold 59500 (Base ~0.64) ---")
    sig = strategy.snipe_signal(market, learned={"mode": "safe"})
    if sig:
        print(f"✅ Safe Mode Signal: certainty={sig.certainty:.2f}")
    else:
        print("❌ Safe Mode: No signal (Expected due to certainty 0.64 < 0.65 or models not agreeing)")

    # 2. Safe Mode - ETH 1h (Rejected Asset)
    print("\n--- Test 2: Safe Mode | ETH 1h (Volatile Asset) ---")
    market_eth = create_market(asset="ETH", tf="1h", secs=1200, threshold=2950)
    sig = strategy.snipe_signal(market_eth, learned={"mode": "safe"})
    if sig:
        print(f"⚠️ Unexpected Signal: {sig.asset}")
    else:
        print("✅ Safe Mode: ETH rejected as expected")

    # 3. Full Send - Gamma Guard Bypass
    print("\n--- Test 3: Full Send | BTC 1h | secs=60 (Gamma Guard Bypass) ---")
    market_late = create_market(asset="BTC", tf="1h", secs=60, threshold=59900)
    sig = strategy.snipe_signal(market_late, learned={"mode": "full_send"})
    if sig:
        print(f"✅ Full Send Signal: certainty={sig.certainty:.2f} (Gamma Guard bypassed)")
    else:
        print("❌ Full Send: No signal")

    # 4. Aggressive Mode - Momentum Weighting
    print("\n--- Test 4: Aggressive vs Balanced Momentum ---")
    # Mock momentum score
    # I'll manually set momentum in the next iteration if needed, 
    # but here I'll just check if it returns a signal.
    # To really test weighting, I'd need to mock _momentum_score.
    
    market_mom = create_market(asset="BTC", tf="1h", secs=1200, threshold=59500)
    sig_bal = strategy.snipe_signal(market_mom, learned={"mode": "balanced"})
    sig_agg = strategy.snipe_signal(market_mom, learned={"mode": "aggressive"})
    print(f"Balanced certainty: {sig_bal.certainty if sig_bal else 'N/A'}")
    print(f"Aggressive certainty: {sig_agg.certainty if sig_agg else 'N/A'}")

if __name__ == "__main__":
    asyncio.run(run_test())
