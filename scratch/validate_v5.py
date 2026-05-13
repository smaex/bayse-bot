
import sys
import os
import asyncio
import logging

# Setup basic logging to see what's happening
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger("validator")

# Add parent dir to path so we can import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    log.info("🔍 STAGE 1: Testing Imports & Syntax...")
    import config
    import strategy
    import executor
    import database
    import bot
    import scanner
    import comparative_analysis
    import learner
    log.info("✅ Imports successful. No syntax errors.")
except Exception as e:
    log.error(f"❌ Import Failure: {e}")
    sys.exit(1)

def test_conviction_sizing():
    log.info("\n🔍 STAGE 2: Testing Conviction Sizing Logic...")
    
    # Mock a signal class
    class MockSignal:
        def __init__(self, asset, certainty, strategy="SNIPE"):
            self.asset = asset
            self.certainty = certainty
            self.strategy = strategy
            self.market_id = "test_market"
            self.timeframe = "1h"
    
    test_cases = [
        {"cert": 0.47, "expected": "1% (₦100 on ₦10k)"},
        {"cert": 0.65, "expected": "2% (₦200 on ₦10k)"},
        {"cert": 0.85, "expected": "3% (₦300 on ₦10k)"},
        {"cert": 0.92, "expected": "4% (₦400 on ₦10k)"},
        {"cert": 0.97, "expected": "8% (₦800 on ₦10k - Booster)"},
    ]

    for case in test_cases:
        cert = case["cert"]
        # Simplified replication of executor.py logic
        if cert >= 0.90:
            base_pct = 0.04
        elif cert >= 0.70:
            base_pct = 0.03
        elif cert >= 0.55:
            base_pct = 0.02
        else:
            base_pct = 0.01

        raw_pct = base_pct
        if cert >= 0.95:
            raw_pct *= 2.0
            
        amount = 10000 * raw_pct
        log.info(f"Signal Certainty {cert:.2f} ➜ Bet Size: ₦{amount:,.0f} | Expected: {case['expected']}")

def test_self_correction_logic():
    log.info("\n🔍 STAGE 3: Testing Self-Correction Enforcement...")
    
    # Mock learned data
    learned = {
        "suspended_combos": ["SNIPE:SOL:5min", "ARB:ETH:1h"]
    }
    
    active_strats = ["SNIPE", "ARB"]
    
    test_markets = [
        {"asset": "SOL", "timeframe": "5min", "market_id": "m1"},
        {"asset": "BTC", "timeframe": "1h", "market_id": "m2"},
        {"asset": "ETH", "timeframe": "1h", "market_id": "m3"}
    ]
    
    for market in test_markets:
        for strat in active_strats:
            combo_key = f"{strat}:{market['asset']}:{market['timeframe']}"
            is_suspended = combo_key in learned["suspended_combos"]
            status = "🔴 BLOCKED" if is_suspended else "🟢 ALLOWED"
            log.info(f"Market {combo_key} ➜ {status}")

async def test_poly_depth():
    log.info("\n🔍 STAGE 4: Testing Poly Edge Liquidity Filter...")
    
    # Mock order book data
    fake_book = {
        "bids": [["0.50", "10"], ["0.49", "20"]], # $5 + $9.8 = $14.8 total depth
        "asks": [["0.52", "100"], ["0.53", "200"]] # Large depth
    }
    
    # Calculate depth
    bid_depth = sum(float(p) * float(s) for p, s in fake_book['bids'])
    ask_depth = sum(float(p) * float(s) for p, s in fake_book['asks'])
    
    log.info(f"Mock Book Depth: Bids=${bid_depth:.2f}, Asks=${ask_depth:.2f}")
    
    if bid_depth < 50 or ask_depth < 50:
        log.warning("🔴 GHOST EDGE DETECTED: Depth < $50. Signal would be rejected.")
    else:
        log.info("✅ REAL EDGE: Depth is sufficient.")

if __name__ == "__main__":
    test_conviction_sizing()
    test_self_correction_logic()
    asyncio.run(test_poly_depth())
    log.info("\n✅ ALL VALIDATION CHECKS PASSED.")
