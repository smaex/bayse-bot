import math
import logging
import time
from typing import Literal

log = logging.getLogger("strategies.regime")

RegimeState = Literal["TREND", "CHOP", "STRESS"]

class RegimeController:
    """
    World Class Quant Brain:
    Determines market regime and provides strategy multipliers to bias signal generation.
    """
    def __init__(self):
        self.last_update = 0
        
    def get_regime(self, asset: str, state: any) -> RegimeState:
        """
        Classifies market based on Volatility (GARCH) and Momentum.
        """
        if not hasattr(state, 'garch_state') or asset not in state.garch_state:
            return "CHOP"
            
        garch_var = state.garch_state[asset]["var"]
        current_vol = math.sqrt(garch_var * 720.0)
        
        # 1. Stress Detection (Volatility Spike)
        if current_vol > 0.05: # Extreme volatility
            return "STRESS"
            
        # 2. Trend vs Chop (using a simple ADX-like logic or Momentum persistence)
        # We'll use price history from state to check for trendiness
        history = getattr(state, 'price_history', {}).get(asset, [])
        if len(history) < 20:
            return "CHOP"
            
        # Efficiency Ratio (ER): Net Move / Total Move
        # ER near 1.0 = strong trend. ER near 0 = chop.
        net_move = abs(history[-1] - history[0])
        total_move = sum(abs(history[i] - history[i-1]) for i in range(1, len(history)))
        er = net_move / total_move if total_move > 0 else 0
        
        if er > 0.40:
            return "TREND"
        return "CHOP"

    def get_multipliers(self, asset: str, state: any) -> dict:
        """
        Returns multipliers for strategy types based on the regime.
        """
        regime = self.get_regime(asset, state)
        
        if regime == "TREND":
            return {"TREND": 1.5, "MEAN_REV": 0.5, "NEWS": 1.2, "SNIPE": 1.0}
        elif regime == "CHOP":
            return {"TREND": 0.5, "MEAN_REV": 1.5, "NEWS": 0.8, "SNIPE": 1.2}
        else: # STRESS
            # In stress, we favor news and snipers, kill trends (whipsaws)
            return {"TREND": 0.2, "MEAN_REV": 0.8, "NEWS": 2.0, "SNIPE": 1.5}

regime_controller = RegimeController()
