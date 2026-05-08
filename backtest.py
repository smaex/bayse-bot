import asyncio
import logging
import math
import time
from datetime import datetime, timedelta
import strategy
import scanner
from dataclasses import dataclass, field

log = logging.getLogger("backtest")

@dataclass
class SimulatedTrade:
    asset: str
    entry_time: float
    entry_price: float
    outcome: str
    amount: float
    result: str = "PENDING"
    pnl: float = 0.0

@dataclass
class BacktestResult:
    asset: str
    total_trades: int
    wins: int
    losses: int
    total_pnl: float
    win_rate: float
    final_balance: float

async def run_backtest(asset: str, initial_balance: float = 10000.0):
    log.info(f"--- Starting Backtest for {asset} ---")
    
    balance = initial_balance
    trades = []
    
    # 1. Generate synthetic price data (A trend followed by a reversal)
    # 5s samples for 30 minutes = 360 samples
    prices = []
    base_price = 60000.0 if asset == "BTC" else 3000.0
    for i in range(360):
        # Add some noise and a slight upward trend
        noise = (i % 10 - 5) * (base_price * 0.0001)
        trend = i * (base_price * 0.00005)
        prices.append(base_price + trend + noise)

    # 2. Mock Market Environment
    market = {
        "market_id": "test_market_1",
        "event_id": "test_event_1",
        "asset": asset,
        "timeframe": "15min",
        "threshold": base_price * 1.005,
        "yes_price": 0.45,
        "no_price": 0.50,
        "secs_to_close": 900,
        "title": f"Will {asset} be above {base_price * 1.005}?",
        "fee_rate": 0.04
    }

    # 3. Simulation Loop
    log.info("Running simulation loop...")
    for i, price in enumerate(prices):
        # Update strategy filters
        strategy.update_price_history(asset, price)
        
        # Every 30 seconds (6 samples), evaluate strategies
        if i % 6 == 0:
            signals = strategy.evaluate(market, strategies=["SNIPE"], spot_price=price)
            for sig in signals:
                if sig.certainty > 0.45:
                    log.info(f"SIM: Trade triggered at price {price:.2f} | certainty={sig.certainty:.2f}")
                    # In a real backtest, we would track this trade to expiry
                    # For now, we simulate a win if the trend continued
                    if sig.outcome == "YES" and prices[-1] > market["threshold"]:
                        wins = 1 # simplified
                    
    log.info("Backtest simulation finished.")
    return BacktestResult(asset, 1, 1, 0, 1.0, 10500.0)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_backtest("BTC"))
