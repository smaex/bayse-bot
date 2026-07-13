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
            # Trending market — SNIPE has the most statistical edge here.
            # FRONTRUN/CORRELATE can participate at full size.
            return {"TREND": 1.5, "SNIPE": 1.2, "FRONTRUN": 1.0, "CORRELATE": 1.1, "NEWS": 1.2}
        elif r == "CHOP":
            # Choppy market — reduce directional strategies; SNIPE still OK as
            # it trades near-close with tight time horizon.
            return {"TREND": 0.5, "SNIPE": 1.0, "FRONTRUN": 0.6, "CORRELATE": 0.7, "NEWS": 0.8}
        else:  # STRESS
            # High-volatility regime. GBM win-prob uncertainty is highest here.
            # Previously SNIPE was boosted 1.5× in STRESS — backwards logic.
            # Under high vol the model is LESS reliable, so we shrink positions.
            return {"TREND": 0.2, "SNIPE": 0.6, "FRONTRUN": 0.4, "CORRELATE": 0.4, "NEWS": 1.5}


regime_controller = RegimeController()
