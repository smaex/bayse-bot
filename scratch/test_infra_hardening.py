import asyncio
import logging
import time
import sys
import os

# Add root to path
sys.path.append(os.getcwd())

import database
import feeds_direct
import config
from client import BayseClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("TEST")

async def test_database_optimization():
    log.info("--- Testing Database Optimization ---")
    try:
        database.init_db()
        # Verify pool is alive
        status = database.check_connection()
        log.info(f"DB Connection Check: {'✅' if status else '❌'}")
        
        # Test concurrent fetches
        log.info("Testing concurrent fetches...")
        results = await asyncio.gather(
            asyncio.to_thread(database.get_all_active),
            asyncio.to_thread(database.get_all_active),
            asyncio.to_thread(database.get_all_active)
        )
        log.info(f"Concurrent fetch success: {len(results)} calls returned.")
    except Exception as e:
        log.error(f"Database optimization test failed: {e}")

async def test_infra_guard():
    log.info("--- Testing Infra Guard Logic ---")
    
    # Mock some data
    asset = "BTC"
    relay_price = 60000.0
    
    # Scenario 1: Startup Grace Period
    log.info("Scenario 1: Startup Grace Period (should be 'ok')")
    res = feeds_direct.check_lag(asset, relay_price)
    log.info(f"Result: {res['status']} (Reason: {res.get('reason')})")
    
    # Scenario 2: Fresh Data (Manually update direct_spot)
    log.info("Scenario 2: Fresh Data Alignment")
    feeds_direct.direct_spot[asset] = {"price": 60010.0, "time": time.time()}
    # Wait for grace period to end or just bypass it for testing?
    # I'll manually set startup_time to long ago for testing
    feeds_direct.startup_time = time.time() - 300
    
    res = feeds_direct.check_lag(asset, relay_price)
    log.info(f"Result: {res['status']} (Lag: {res.get('lag_sec', 0):.1f}s, Diff: {res.get('diff_pct', 0):.4%})")

    # Scenario 3: Stale Data
    log.info("Scenario 3: Stale Data (>45s)")
    feeds_direct.direct_spot[asset] = {"price": 60000.0, "time": time.time() - 50}
    res = feeds_direct.check_lag(asset, relay_price)
    log.info(f"Result: {res['status']} (Expected: stale)")

    # Scenario 4: Mispriced Data
    log.info("Scenario 4: Mispriced Data (>0.2%)")
    feeds_direct.direct_spot[asset] = {"price": 61000.0, "time": time.time()}
    res = feeds_direct.check_lag(asset, relay_price)
    log.info(f"Result: {res['status']} (Expected: stale, Diff: {res.get('diff_pct'):.4%})")

async def test_panic_button_dry_run():
    log.info("--- Testing Panic Button (Simulation) ---")
    # Instead of running the real script which hits the API, 
    # we just check if it can load the logic
    try:
        import panic_button
        log.info("Panic Button module loaded successfully.")
    except Exception as e:
        log.error(f"Panic Button loading failed: {e}")

async def run_all():
    await test_database_optimization()
    await test_infra_guard()
    await test_panic_button_dry_run()
    log.info("--- All Infrastructure Tests Complete ---")

if __name__ == "__main__":
    asyncio.run(run_all())
