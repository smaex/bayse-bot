import logging
from typing import List, Optional
from strategies.base import TradeSignal
from strategies.snipe import SnipeStrategy
from strategies.correlate import CorrelateStrategy
from strategies.news import NewsStrategy
from strategies.poly_edge import PolyEdgeStrategy
from strategies.frontrun import FrontrunStrategy
from strategies.regime import regime_controller

# Strategy instances
_strategies = {
    "SNIPE": SnipeStrategy(),
    "CORRELATE": CorrelateStrategy(),
    "NEWS": NewsStrategy(),
    "POLY_EDGE": PolyEdgeStrategy(),
    "FRONTRUN": FrontrunStrategy()
}

log = logging.getLogger("strategies")

async def evaluate_all(market: dict, learned: dict, state: any, spot_price: float = None) -> List[TradeSignal]:
    """
    Modular Orchestrator:
    1. Determines market regime.
    2. Evaluates all active strategy plugins.
    3. Applies regime-based multipliers.
    4. Returns sorted signals.
    """
    asset = market["asset"]
    learned = learned or {}
    active_strat_names = learned.get("strategies", ["SNIPE", "CORRELATE", "NEWS", "POLY_EDGE", "FRONTRUN"])
    
    # 1. Get Regime Multipliers
    regime_mults = regime_controller.get_multipliers(asset, state)
    cert_mults   = learned.get("certainty_multipliers", {})
    
    signals = []
    for name in active_strat_names:
        strat = _strategies.get(name)
        if not strat:
            continue
            
        try:
            sig = await strat.evaluate(market, learned, state, spot_price=spot_price)
            if sig:
                # 2. Apply Regime Multiplier
                cat = "TREND" if name == "CORRELATE" else "SNIPE"
                if name == "NEWS": cat = "NEWS"
                
                mult = regime_mults.get(cat, 1.0)
                
                # 3. Apply Bayesian Performance Multiplier
                meta_mult = cert_mults.get(name, 1.0)
                combo_key = f"{sig.strategy}:{sig.asset}:{sig.timeframe}"
                combo_mult = cert_mults.get(combo_key, 1.0)
                
                final_mult = mult * meta_mult * combo_mult
                
                if final_mult != 1.0:
                    sig.certainty = min(1.0, max(0.0, sig.certainty * final_mult))
                    sig.reason += f" | MULT(x{final_mult:.2f})"
                
                # 4. Apply Order Flow Imbalance (OFI) Boost
                from strategies.order_flow import get_ofi_boost
                import comparative_analysis
                quality = comparative_analysis.get_edge_quality(asset)
                depth = {"bids": quality.get("bids", []), "asks": quality.get("asks", [])}
                ofi_boost = get_ofi_boost(asset, sig.outcome, depth)
                if ofi_boost != 0:
                    sig.certainty = min(1.0, max(0.0, sig.certainty + ofi_boost))
                    sig.reason += f" | OFI({ofi_boost:+.2f})"

                # Re-verify certainty hurdle after multipliers
                mode = learned.get("mode", "balanced")
                mode_floor = {"safe": 0.65, "balanced": 0.55, "aggressive": 0.45, "full_send": 0.35}.get(mode, 0.55)
                
                if sig.certainty >= mode_floor:
                    signals.append(sig)
                    
        except Exception as e:
            log.error(f"Error evaluating strategy {name} on {asset}: {e}", exc_info=True)
            
    return sorted(signals, key=lambda s: s.certainty, reverse=True)
