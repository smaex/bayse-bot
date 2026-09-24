import math
import logging
from typing import Literal

log = logging.getLogger("strategies.regime")

RegimeState = Literal["TREND", "CHOP", "STRESS"]


class RegimeController:
    """Classifies market regime and returns strategy multipliers."""

    def get_regime(self, asset: str, state) -> RegimeState:
        if not hasattr(state, "garch_state") or asset not in state.garch_state:
            return "CHOP"
        vol = math.sqrt(state.garch_state[asset]["var"] * 720.0)
        if vol > 0.05:
            return "STRESS"
        from strategies.utils import regime_score
        return "TREND" if regime_score(asset, state) > 0.60 else "CHOP"

    def get_multipliers(self, asset: str, state) -> dict:
        r = self.get_regime(asset, state)
        if r == "TREND":
            return {"TREND": 1.5, "SNIPE": 1.0, "NEWS": 1.2}
        elif r == "CHOP":
            return {"TREND": 0.5, "SNIPE": 1.2, "NEWS": 0.8}
        else:  # STRESS
            return {"TREND": 0.2, "SNIPE": 1.5, "NEWS": 2.0}


regime_controller = RegimeController()
