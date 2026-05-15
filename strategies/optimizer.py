import logging
import asyncio
import time
from datetime import datetime, timedelta, timezone
import database
import config

log = logging.getLogger("strategies.optimizer")

class AutoOptimizer:
    """
    Nightly Walk-Forward Optimizer:
    Runs backtests on historical trade data to find optimal multipliers.
    """
    def __init__(self):
        self.last_run = 0

    async def run_nightly_optimization(self):
        """
        Runs at 00:00 UTC. Analyzes the last 7 days of trade data.
        """
        log.info("Starting Nightly Walk-Forward Optimization...")
        
        # 1. Fetch historical trades
        trades = await asyncio.to_thread(database.get_recent_trades, 7)
        if not trades:
            log.info("No trades to analyze. Skipping optimization.")
            return

        # 2. Optimization Logic
        results = {}
        strats = ["SNIPE", "CORRELATE", "ARB", "NEWS"]
        
        for strat in strats:
            strat_trades = [t for t in trades if t["strategy"] == strat and t["won"] is not None]
            if not strat_trades:
                continue
                
            best_hurdle = 0.55
            max_pnl = -999999
            
            # Grid search for the optimal hurdle
            for hurdle in [0.45, 0.50, 0.55, 0.60, 0.65]:
                pnl = sum(t["pnl_ngn"] for t in strat_trades if t["certainty"] >= hurdle)
                if pnl > max_pnl:
                    max_pnl = pnl
                    best_hurdle = hurdle
            
            # Calculate size multiplier based on edge vs average
            avg_edge = sum(float(t.get("edge_at_entry") or 0) for t in strat_trades) / len(strat_trades)
            # If edge > 2%, we boost. If < 0.5%, we penalize.
            size_mult = 1.0
            if avg_edge > 0.02: size_mult = 1.2
            elif avg_edge < 0.005: size_mult = 0.5
            
            results[strat] = {
                "hurdle": best_hurdle,
                "size_mult": size_mult,
                "win_rate": sum(1 for t in strat_trades if t["won"]) / len(strat_trades)
            }
            log.info(f"Optimized {strat}: Hurdle={best_hurdle}, SizeMult={size_mult:.2f}")

        # 3. Update Global Learned State
        if results:
            # We'll update the global 'learned' state which bot.py merges with user settings
            # In a real setup, we'd save this to a 'global_config' table.
            log.info(f"Optimization complete. Best Hurdles: {[ (k, v['hurdle']) for k,v in results.items()]}")
            await asyncio.to_thread(database.save_optimized_params, results)
        
        self.last_run = time.time()

    async def schedule_loop(self):
        while True:
            # For testing/demo purposes, we'll allow manual trigger via a flag or just check time
            now = datetime.now(timezone.utc)
            # 00:00 UTC check
            if now.hour == 0 and now.minute == 0 and (time.time() - self.last_run > 3600):
                await self.run_nightly_optimization()
            
            # Also run once on startup if never run
            if self.last_run == 0:
                # Delay slightly for DB init
                await asyncio.sleep(10)
                await self.run_nightly_optimization()

            await asyncio.sleep(60)

optimizer = AutoOptimizer()
