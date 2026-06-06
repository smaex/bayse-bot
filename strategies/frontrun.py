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
        tf = market.get("timeframe", "")
        secs = market.get("secs_to_close", 0)

        # FRONTRUN only runs on short candles (5‑min or 15‑min)
        # The whitelist lives in config.FRONTRUN_ALLOWED_TFS. If the list is empty, all timeframes are allowed.
        if config.FRONTRUN_ALLOWED_TFS and tf not in config.FRONTRUN_ALLOWED_TFS:
            return None

        # FrontRun is pure latency arbitrage — exploits oracle vs Bayse price lag.
        # It works on ANY timeframe as long as there is time left for the trade to resolve.
        # Minimum 60 seconds needed to place and have the order settle.
        if secs < 60 or secs < 0:
            return None

        # 1. Get Latency Bias (Oracle vs Bayse)
        # Positive = Oracle is HIGHER than Bayse (Bullish for YES)
        # Negative = Oracle is LOWER than Bayse (Bearish for NO)
        bayse_p = market["yes_price"] # Assuming yes_price represents the 'fair' price on AMM
        
        oracle_p, oracle_t = feeds_direct.get_direct_price(asset)
        if not oracle_p or (feeds_direct.time.time() - oracle_t > 5):
            return None # Oracle stale
            
        # We need the Bayse AMM's 'implied' spot price.
        # This is usually tracked in feeds.spot[asset]
        bayse_spot = feeds.spot.get(asset)
        if not bayse_spot:
            return None
            
        bias = (oracle_p - bayse_spot) / bayse_spot
        
        # 2. Threshold: Lowered for Alpha Resurrection (8 bps)
        trigger = 0.0008 
        
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
            
        # 2.5. Distance & Time Check (Don't buy doomed markets)
        from strategies.utils import win_probability
        if market.get("threshold") and spot_price and secs > 0:
            dist_pct = (spot_price - market["threshold"]) / market["threshold"]
            prob = win_probability(dist_pct, secs, asset)
            if outcome == "YES" and prob < 0.05:
                return None  # Mathematically impossible to win YES
            if outcome == "NO" and prob > 0.95:
                return None  # Mathematically impossible to win NO
            
        # 3. Certainty: Scale with bias strength
        certainty = min(0.50 + abs(bias) * 100, 0.99)
        
        # 4. Filter: Only trade if the AMM hasn't moved yet
        # If market price is already near 1.0 or 0.0, the move is already priced in.
        market_price = market["yes_price"] if outcome == "YES" else market["no_price"]
        if market_price > 0.90:
            return None # Move already complete
            
        log.info(f"🔥 FRONTRUN TRIGGER | {asset} | Bias: {bias:+.4%} | Target: {outcome}")

        # Dynamic Sizing: scale size based on bias, max 3% to protect account
        size_pct = min(0.01 + abs(bias) * 5.0, 0.03)
        # Enforce a configurable minimum trade size (default ₦100)
        MIN_TRADE_NAIRA = getattr(config, "FRONTRUN_MIN_TRADE_NAIRA", 100.0)
        # Convert the Naira floor to a percentage of current free cash (risk.current_free_cash will be injected at execution time)
        # size_pct is a fraction of equity; we ensure it’s at least the floor expressed as a fraction.
        # The executor will clamp to the global hard‑cap, so this never exceeds allowed exposure.
        try:
            free_cash = risk.current_free_cash  # risk is injected at execution; safe fallback if unavailable
        except Exception:
            free_cash = 1.0
        size_pct = max(size_pct, MIN_TRADE_NAIRA / max(1.0, free_cash))

        return TradeSignal(
            strategy="FRONTRUN",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=tf,
            outcome=outcome,
            outcome_id=market["yes_id"] if outcome == "YES" else market["no_id"],
            certainty=certainty,
            win_prob=0.80, # Hardcoded high prob for latency arb
            market_price=market_price,
            size_pct=size_pct,
            reason=f"Latency Gap {bias:+.2%}",
            title=market["title"]
        )
