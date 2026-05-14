import logging
from typing import Optional
from strategies.base import BaseStrategy, TradeSignal
from strategies.manager import kelly_size

log = logging.getLogger("strat.poly_edge")

class PolyEdgeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("POLY_EDGE")

    async def evaluate(self, market: dict, learned: dict, state: any, spot_price: float = None) -> Optional[TradeSignal]:
        asset = market["asset"]
        import comparative_analysis
        
        quality = comparative_analysis.get_edge_quality(asset)
        if not quality.get("price"): return None
        if not quality["is_real"]: return None
            
        poly_p = quality["price"]
        yes_p  = market["yes_price"]
        no_p   = market["no_price"]
        
        yes_edge = poly_p - yes_p
        no_edge  = (1.0 - poly_p) - no_p
        
        edge = 0.0
        outcome = ""
        outcome_id = ""
        market_price = 0.0
        
        THRESHOLD = 0.15
        if yes_edge > THRESHOLD:
            edge = yes_edge
            outcome, outcome_id, market_price = "YES", market["yes_id"], yes_p
        elif no_edge > THRESHOLD:
            edge = no_edge
            outcome, outcome_id, market_price = "NO", market["no_id"], no_p
            
        if not outcome: return None

        w_est = 0.50 + (edge * 2.0)
        size_pct = kelly_size(w_est, market_price, fee_rate=0.02, asset=asset, learned=learned, strategy_name="POLY_EDGE")
        
        depth = quality.get("depth_usd", 0)
        if 0 < depth < 200: size_pct *= 0.5
        
        return TradeSignal(
            strategy="POLY_EDGE",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=market["timeframe"],
            outcome=outcome,
            outcome_id=outcome_id,
            certainty=0.90,
            win_prob=w_est,
            market_price=market_price,
            size_pct=size_pct,
            reason=f"Poly Discrepancy ({edge:+.1%} edge) [{quality['reason']}]",
            title=market["title"]
        )
