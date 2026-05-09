import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import List, Dict, Optional
import strategy
import config
from dataclasses import dataclass, field

log = logging.getLogger("backtester")

@dataclass
class SimulatedTrade:
    market_id: str
    asset: str
    timeframe: str
    direction: str        # "YES" or "NO"
    entry_time: float
    entry_price: float
    limit_price: float
    size_ngn: float
    certainty: float
    expiry_time: float
    threshold: float
    status: str = "OPEN"  # OPEN, FILLED, EXPIRED, WON, LOST
    fill_time: Optional[float] = None
    exit_time: Optional[float] = None
    pnl: float = 0.0

class BacktestEngine:
    def __init__(self, initial_balance: float = 100000.0):
        self.balance = initial_balance
        self.equity_curve = []
        self.trades: List[SimulatedTrade] = []
        self.state = strategy.MarketState()
        self.current_time = 0.0
        
    def log_trade(self, trade: SimulatedTrade):
        self.trades.append(trade)
        
    async def run(self, ticker_data: List[dict], market_definitions: List[dict]):
        """
        ticker_data: List of {'asset': 'BTC', 'price': 60000.0, 'time': 1715230000}
        market_definitions: List of market dicts as seen by strategy.evaluate
        """
        log.info(f"Starting High-Fidelity Backtest with {len(ticker_data)} ticks...")
        
        # Sort data by time
        ticker_data.sort(key=lambda x: x['time'])
        
        for i, tick in enumerate(ticker_data):
            self.current_time = tick['time']
            asset = tick['asset']
            price = tick['price']
            
            # 1. Update Strategy Engine State
            strategy.update_price_history(asset, price, state=self.state)
            
            # 2. Check for LIMIT fills on open trades
            self._process_fills(asset, price)
            
            # 3. Process Expirations / Resolutions
            self._process_resolutions(asset, price)
            
            # 4. Evaluate Markets (throttled to every 30s)
            if i % 6 == 0: # Assuming 5s samples
                for market in market_definitions:
                    if market['asset'] == asset:
                        # Update market time-to-close
                        market['secs_to_close'] = max(0, market['expiry_time'] - self.current_time)
                        
                        if market['secs_to_close'] > 0:
                            signals = strategy.evaluate(market, ["SNIPE", "CORRELATE"], spot_price=price, state=self.state)
                            for sig in signals:
                                self._attempt_entry(sig, market, price)

        self._print_results()

    def _attempt_entry(self, sig: strategy.TradeSignal, market: dict, current_spot: float):
        # Avoid duplicate entries for same market
        if any(t.market_id == market['market_id'] for t in self.trades if t.status in ["OPEN", "FILLED"]):
            return

        # Calculate position size in NGN
        size_ngn = self.balance * sig.size_pct
        if size_ngn < 100: return # min trade

        trade = SimulatedTrade(
            market_id=market['market_id'],
            asset=market['asset'],
            timeframe=market['timeframe'],
            direction=sig.outcome,
            entry_time=self.current_time,
            entry_price=sig.market_price,
            limit_price=sig.market_price, # In this sim, we use market price as limit
            size_ngn=size_ngn,
            certainty=sig.certainty,
            expiry_time=market['expiry_time'],
            threshold=market['threshold']
        )
        
        # Check for immediate fill (simulated)
        # In reality, we might have slippage.
        # slip_map = {"safe": 0.002, "balanced": 0.005, "aggressive": 0.01, "full_send": 0.025}
        # For backtest, we just assume we get filled if price doesn't immediately move away.
        trade.status = "FILLED"
        trade.fill_time = self.current_time
        self.balance -= size_ngn
        self.trades.append(trade)
        log.info(f"[{datetime.fromtimestamp(self.current_time)}] ENTER {trade.direction} {trade.asset} @ {trade.entry_price:.3f} | Size: {size_ngn:.0f}")

    def _process_fills(self, asset: str, price: float):
        # (Simplified: already assuming immediate fill in _attempt_entry for now)
        pass

    def _process_resolutions(self, asset: str, price: float):
        for trade in self.trades:
            if trade.status == "FILLED" and self.current_time >= trade.expiry_time:
                # Resolve
                is_win = False
                if trade.direction == "YES":
                    is_win = price > trade.threshold
                else:
                    is_win = price < trade.threshold
                
                if is_win:
                    trade.status = "WON"
                    # Payout is approx 1/market_price (minus fees handled by market_price at entry)
                    # Simplified payout calculation:
                    payout = trade.size_ngn / trade.entry_price
                    trade.pnl = payout - trade.size_ngn
                    self.balance += payout
                else:
                    trade.status = "LOST"
                    trade.pnl = -trade.size_ngn
                
                trade.exit_time = self.current_time
                log.info(f"[{datetime.fromtimestamp(self.current_time)}] RESOLVE {trade.asset} {trade.status} | PnL: {trade.pnl:+.0f}")

    def _print_results(self):
        wins = [t for t in self.trades if t.status == "WON"]
        losses = [t for t in self.trades if t.status == "LOST"]
        total = len(wins) + len(losses)
        
        if total == 0:
            print("No trades executed.")
            return

        wr = len(wins) / total
        total_pnl = sum(t.pnl for t in self.trades)
        
        print("\n" + "="*40)
        print("BACKTEST RESULTS")
        print("="*40)
        print(f"Total Trades: {total}")
        print(f"Win Rate:     {wr:.1%}")
        print(f"Total PnL:    {total_pnl:+.2f} NGN")
        print(f"Final Balance: {self.balance:,.2f} NGN")
        print("="*40)

if __name__ == "__main__":
    # Example usage (synthetic)
    logging.basicConfig(level=logging.INFO)
    engine = BacktestEngine()
    
    # Generate 1 hour of BTC data
    start = 1715230000
    ticks = []
    for i in range(720): # 1 hour of 5s samples
        ticks.append({
            'asset': 'BTC',
            'price': 60000 + math.sin(i/10) * 500 + i * 2, # sinusoid + trend
            'time': start + i * 5
        })
        
    markets = [{
        'market_id': 'm1',
        'asset': 'BTC',
        'timeframe': '1h',
        'threshold': 60500,
        'expiry_time': start + 3600,
        'yes_id': 'y',
        'no_id': 'n',
        'yes_price': 0.45,
        'no_price': 0.50,
        'fee_rate': 0.04
    }]
    
    asyncio.run(engine.run(ticks, markets))
