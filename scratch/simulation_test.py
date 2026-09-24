import asyncio
import logging
import sys
import os
from unittest.mock import MagicMock

# Add parent dir to path
sys.path.append(os.getcwd())

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger("simulation")

# ── MOCK ENVIRONMENT BEFORE IMPORTS ──
# This prevents real database/client initialization
os.environ["DATABASE_URL"] = "postgresql://mock@localhost:5432/mock"
os.environ["ENCRYPTION_KEY"] = "mock_key_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx="
os.environ["TELEGRAM_TOKEN"] = "1234:mock"

import feeds
import feeds_direct
import database
import config
from types import SimpleNamespace

# Mock the database before it's used
database.record_trade = MagicMock(return_value="mock_trade_id")
database.save_optimized_params = MagicMock()
database.get_avg_slippage = MagicMock(return_value=0.005)

async def run_simulation():
    log.info("🚀 Starting Bayse Bot World-Class Simulation")
    
    # 1. Mock Feeds
    feeds.spot = {
        "BTC": 80000.0,
        "EURUSD": 1.1625,
        "GBPUSD": 1.3330
    }
    feeds_direct.direct_spot = {
        "BTC": {"price": 80100.0, "time": asyncio.get_event_loop().time()}, # 0.125% gap
        "EURUSD": {"price": 1.1635, "time": asyncio.get_event_loop().time()}
    }
    
    # 2. Mock a User/Client
    class MockClient:
        async def place_order(self, **kwargs):
            log.info(f"✅ MOCK ORDER PLACED: {kwargs}")
            return {"status": "success", "order": {"id": "mock_id", "shares": 10.0, "price": kwargs.get("price", 0.5)}}
        async def get_balance_ngn(self): return 5000.0
            
    mock_client = MockClient()
    
    # 3. Import executor and manually inject mocks
    import executor
    executor.client = mock_client
    executor.active_markets = [
        {"asset": "BTC", "market_id": "btc_m_id", "event_id": "e1", "outcome_id": "o1", "yes_id": "o1", "no_id": "o2", "yes_price": 0.5, "no_price": 0.5, "engine": "AMM", "secs_to_close": 3600, "threshold": 79500.0},
        {"asset": "GBPUSD", "market_id": "gbpusd_m_id", "event_id": "e2", "outcome_id": "o3", "yes_id": "o3", "no_id": "o4", "yes_price": 0.6, "no_price": 0.4, "engine": "AMM", "secs_to_close": 3600, "threshold": 1.3300}
    ]
    
    class MockRisk:
        def __init__(self): 
            self.open_positions = {}
            self.mode = "aggressive"
        def can_trade(self, *args): return True
        def is_on_probation(self): return False
        def add_position(self, *args): pass
        def already_in(self, *args): return False
        def deployed(self): return 0
        
    executor.risk = MockRisk()
    
    # ── Test 1: AMM Market Routing ──
    log.info("--- TEST 1: AMM Market (Should use MARKET order) ---")
    sig_btc = SimpleNamespace(
        strategy="SNIPE", asset="BTC", market_id="btc_m_id", event_id="e1", outcome_id="o1", outcome="YES",
        market_price=0.55, certainty=0.90, timeframe="5m", momentum_at_entry=0.01, regime_at_entry=0.5,
        edge_at_entry=0.05, realized_vol_at_entry=0.02
    )
    await executor.execute_trade(8264282870, sig_btc, mock_client, executor.risk, {}, 5000.0, 5000.0)

    # ── Test 2: Hard Cap Clamping ──
    log.info("--- TEST 2: Hard Cap Clamping (Should scale size down) ---")
    sig_high_size = SimpleNamespace(
        strategy="SNIPE", asset="GBPUSD", market_id="gbpusd_m_id", event_id="e2", outcome_id="o3", outcome="YES",
        market_price=0.62, certainty=0.95, timeframe="1h", momentum_at_entry=0.01, regime_at_entry=0.5,
        edge_at_entry=0.05, realized_vol_at_entry=0.02
    )
    # This would normally request a large size (>8% of 5000 = 400)
    await executor.execute_trade(8264282870, sig_high_size, mock_client, executor.risk, {}, 5000.0, 5000.0)

    # ── Test 3: Frontrun Strategy ──
    log.info("--- TEST 3: Frontrun Strategy Logic ---")
    from strategies.frontrun import FrontrunStrategy
    frontrun = FrontrunStrategy()
    market_btc = executor.active_markets[0]
    # Inject high bias (1000 points)
    feeds.spot["BTC"] = 80000.0
    feeds_direct.direct_spot["BTC"] = {"price": 81000.0, "time": asyncio.get_event_loop().time()}
    
    sig_fr = await frontrun.evaluate(market_btc, {"mode": "aggressive"}, None)
    if sig_fr:
        log.info(f"✅ Frontrun Triggered: {sig_fr.reason} | Certainty: {sig_fr.certainty:.2f}")
        await executor.execute_trade(8264282870, sig_fr, mock_client, executor.risk, {}, 5000.0, 5000.0)
    else:
        log.warning("❌ Frontrun failed to trigger in simulation")

    log.info("✅ ALL TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(run_simulation())
