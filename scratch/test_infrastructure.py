import asyncio
import logging
import sys
import os

# Add the workspace to path
sys.path.append(os.getcwd())

import database
import comparative_analysis
import executor
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bug_test")

@dataclass
class MockSignal:
    strategy: str = "SNIPE"
    asset: str = "BTC"
    timeframe: str = "1h"
    outcome: str = "YES"
    outcome_id: str = "mock_yes"
    market_id: str = "mock_market"
    event_id: str = "mock_event"
    certainty: float = 0.85
    market_price: float = 0.80
    momentum_at_entry: float = 0.5
    regime_at_entry: float = 0.8
    edge_at_entry: float = 0.05
    realized_vol_at_entry: float = 0.022

async def run_tests():
    log.info("🚀 Starting Full Bug Testing...")

    # 1. Test Database Slippage Logic
    log.info("Testing Database Slippage calculation...")
    # Insert some dummy trades with slippage
    # Note: Using mock IDs that won't conflict
    try:
        avg_slip = database.get_avg_slippage("BTC", "SNIPE")
        log.info(f"Current Avg Slippage for BTC: {avg_slip:.2%}")
    except Exception as e:
        log.error(f"❌ Database Test Failed: {e}")

    # 2. Test Polymarket Caching
    log.info("Testing Polymarket Caching...")
    try:
        # First call (will poll API)
        price1 = await comparative_analysis.get_comparative_price("BTC", 65000)
        log.info(f"Polymarket Price 1: {price1}")
        
        # Check cache
        if "BTC" in comparative_analysis.CACHE:
            log.info(f"✅ Cache Entry Found: {comparative_analysis.CACHE['BTC']}")
        else:
            log.error("❌ Cache Entry Missing!")

        # Second call (should be instant from cache)
        start = asyncio.get_event_loop().time()
        price2 = await comparative_analysis.get_comparative_price("BTC", 65000)
        end = asyncio.get_event_loop().time()
        
        log.info(f"Polymarket Price 2 (Cached): {price2} (Fetch took {end-start:.4f}s)")
        if end - start > 0.01:
            log.warning("⚠️ Cached fetch took longer than expected.")
    except Exception as e:
        log.error(f"❌ Polymarket Test Failed: {e}")

    # 3. Test Background Loop Logic
    log.info("Testing Background Loop update...")
    try:
        await comparative_analysis.update_cache()
        log.info(f"Cache after update: {comparative_analysis.CACHE.keys()}")
    except Exception as e:
        log.error(f"❌ Background Loop Test Failed: {e}")

    log.info("✅ Testing Complete.")

if __name__ == "__main__":
    asyncio.run(run_tests())
