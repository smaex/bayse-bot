import logging
import time
from datetime import datetime, timezone
from typing import Optional
import feeds
import config
from strategies.base import BaseStrategy, TradeSignal, global_state
from strategies.manager import kelly_size

log = logging.getLogger("strat.market_bias")

class MarketBiasStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("MARKET_BIAS")

    async def evaluate(self, market: dict, learned: dict, state: any, spot_price: float = None) -> Optional[TradeSignal]:
        asset = market["asset"]
        mid = market["market_id"]
        secs_to_close = market["secs_to_close"]
        learned = learned or {}
        
        opening = global_state.market_opening_prices.get(mid)
        
        # 1. Opening Spread Bias
        # Active within the first 90 seconds of the candle
        if opening and (time.time() - opening["timestamp"] <= 90.0):
            opening_yes = opening["yes"]
            opening_no = opening["no"]
            opening_spread = abs(opening_yes - opening_no)
            
            if opening_spread >= 0.15:
                # Target the favorite (the one with the higher opening price)
                if opening_yes >= opening_no:
                    outcome = "YES"
                    outcome_id = market["yes_id"]
                    market_price = market["yes_price"]
                else:
                    outcome = "NO"
                    outcome_id = market["no_id"]
                    market_price = market["no_price"]
                    
                if opening_spread >= 0.20:
                    win_prob = 0.673
                    certainty = 0.75
                else:
                    win_prob = 0.653
                    certainty = 0.70
                    
                size_pct = kelly_size(win_prob, market_price, fee_rate=0.02, asset=asset, learned=learned, strategy_name="MARKET_BIAS")
                
                log.info(f"Market Bias: Opening Spread Bias triggered on {asset} ({mid}). Outcome: {outcome}, win_prob={win_prob}")
                return TradeSignal(
                    strategy="MARKET_BIAS",
                    event_id=market["event_id"],
                    market_id=mid,
                    asset=asset,
                    timeframe=market["timeframe"],
                    outcome=outcome,
                    outcome_id=outcome_id,
                    certainty=certainty,
                    win_prob=win_prob,
                    market_price=market_price,
                    size_pct=size_pct,
                    reason=f"Opening Spread Bias (spread={opening_spread:.2f}, open_yes={opening_yes:.2f})",
                    title=market["title"]
                )
                
        # 2. Hour Bias (Coin flips)
        # Active if opening price is near 50/50 coin flip (0.47 to 0.53)
        if opening:
            opening_yes = opening["yes"]
            if 0.47 <= opening_yes <= 0.53:
                utc_hour = datetime.now(timezone.utc).hour
                if utc_hour in [12, 14, 15, 19]:
                    # BUG-FIX: Pick the current favorite, not always YES
                    if opening_yes >= 0.50:
                        outcome = "YES"
                        outcome_id = market["yes_id"]
                        market_price = market["yes_price"]
                    else:
                        outcome = "NO"
                        outcome_id = market["no_id"]
                        market_price = market["no_price"]
                    
                    if utc_hour == 15:
                        win_prob = 0.82
                        certainty = 0.85
                    else:
                        win_prob = 0.72
                        certainty = 0.75
                        
                    size_pct = kelly_size(win_prob, market_price, fee_rate=0.02, asset=asset, learned=learned, strategy_name="MARKET_BIAS")
                    
                    log.info(f"Market Bias: Hour Bias triggered on {asset} ({mid}) at hour {utc_hour} UTC. Outcome: {outcome}, win_prob={win_prob}")
                    return TradeSignal(
                        strategy="MARKET_BIAS",
                        event_id=market["event_id"],
                        market_id=mid,
                        asset=asset,
                        timeframe=market["timeframe"],
                        outcome=outcome,
                        outcome_id=outcome_id,
                        certainty=certainty,
                        win_prob=win_prob,
                        market_price=market_price,
                        size_pct=size_pct,
                        reason=f"Hour Bias ({utc_hour} UTC Coin Flip open={opening_yes:.2f})",
                        title=market["title"]
                    )
                    

        # 3. Tie-Breaker Bias
        # Active in final 30 seconds if distance from threshold is extremely small (< 0.01%)
        if secs_to_close <= 30:
            threshold = market.get("threshold")
            live_spot = spot_price if spot_price is not None else feeds.spot.get(asset)
            
            if threshold and live_spot:
                distance_pct = (live_spot - threshold) / threshold
                if abs(distance_pct) < 0.0001:  # < 0.01%
                    outcome = "YES"
                    outcome_id = market["yes_id"]
                    market_price = market["yes_price"]
                    win_prob = 0.683
                    certainty = 0.70
                    
                    size_pct = kelly_size(win_prob, market_price, fee_rate=0.02, asset=asset, learned=learned, strategy_name="MARKET_BIAS")
                    
                    log.info(f"Market Bias: Tie-Breaker Bias triggered on {asset} ({mid}). Distance: {distance_pct:.5%}. Outcome: YES, win_prob={win_prob}")
                    return TradeSignal(
                        strategy="MARKET_BIAS",
                        event_id=market["event_id"],
                        market_id=mid,
                        asset=asset,
                        timeframe=market["timeframe"],
                        outcome=outcome,
                        outcome_id=outcome_id,
                        certainty=certainty,
                        win_prob=win_prob,
                        market_price=market_price,
                        size_pct=size_pct,
                        reason=f"Tie-Breaker Bias (distance={distance_pct:.4%})",
                        title=market["title"]
                    )
                    
        return None
