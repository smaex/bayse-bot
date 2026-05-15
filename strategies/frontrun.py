import logging
from typing import Optional
from strategies.base import BaseStrategy, TradeSignal
import feeds_direct
import feeds
import config

log = logging.getLogger("strat.frontrun")

class FrontrunStrategy(BaseStrategy):
    """
    World-Class Latency Arbitrage (AMM Snipping).
    Exploits the lag between High-Speed Oracles (Binance/Tiingo) and the Bayse AMM.
    If the Oracle moves >0.15% and Bayse is stale, we front-run the AMM's next update.
    """
    def __init__(self):
        super().__init__("FRONTRUN")

    async def evaluate(self, market: dict, learned: dict, state: any, spot_price: float = None) -> Optional[TradeSignal]:
        asset = market["asset"]
        
        # 1. Get Latency Bias (Oracle vs Bayse)
        # Positive = Oracle is HIGHER than Bayse (Bullish for YES)
        # Negative = Oracle is LOWER than Bayse (Bearish for NO)
        bayse_p = market["yes_price"] # Assuming yes_price represents the 'fair' price on AMM
        # Wait, for AMM, price is outcome1Price (Yes). 
        # If Oracle BTC is 80,000 and Bayse threshold is 79,000, Yes should be 1.0.
        # This strategy is better applied to the UNDERLYING price gap.
        
        oracle_p, oracle_t = feeds_direct.get_direct_price(asset)
        if not oracle_p or (feeds_direct.time.time() - oracle_t > 5):
            return None # Oracle stale
            
        # We need the Bayse AMM's 'implied' spot price.
        # This is usually tracked in feeds.spot[asset]
        bayse_spot = feeds.spot.get(asset)
        if not bayse_spot:
            return None
            
        bias = (oracle_p - bayse_spot) / bayse_spot
        
        # 2. Threshold: If gap > 0.15% (World Class bots trigger at 0.05% but we have fees)
        trigger = 0.0015 # 15 bps
        
        direction = None
        outcome = None
        if bias > trigger:
            direction = "BULLISH"
            outcome = "YES"
        elif bias < -trigger:
            direction = "BEARISH"
            outcome = "NO"
            
        if not direction:
            return None
            
        # 3. Certainty: Scale with bias strength
        certainty = min(0.50 + abs(bias) * 100, 0.99)
        
        # 4. Filter: Only trade if the AMM hasn't moved yet
        # If market price is already near 1.0 or 0.0, the move is already priced in.
        market_price = market["yes_price"] if outcome == "YES" else market["no_price"]
        if market_price > 0.90:
            return None # Move already complete
            
        log.info(f"🔥 FRONTRUN TRIGGER | {asset} | Bias: {bias:+.4%} | Target: {outcome}")

        return TradeSignal(
            strategy="FRONTRUN",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=market["timeframe"],
            outcome=outcome,
            outcome_id=market["yes_id"] if outcome == "YES" else market["no_id"],
            certainty=certainty,
            win_prob=0.80, # Hardcoded high prob for latency arb
            market_price=market_price,
            size_pct=0.05, # Fixed 5% size for frontrunning
            reason=f"Latency Gap {bias:+.2%}",
            title=market["title"]
        )
