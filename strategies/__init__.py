import logging
import time
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
                
                # ── Alpha Resurrection: Lowered Floors ──
                # We lower floors by ~0.10 to capture more signals.
                mode_floor = {"safe": 0.60, "balanced": 0.48, "aggressive": 0.40, "full_send": 0.32}.get(mode, 0.48)
                
                # ── Market Stagnation Relief (Pantry Raid) ──
                # If the bot hasn't traded in 12 hours, we lower hurdles further.
                if learned.get("pantry_raid_active"):
                    mode_floor -= 0.10
                
                # ── Discovery Filter (Alpha Resurrection) ──
                # We allow signals as low as 0.35 to pass through so the executor 
                # can fire 'Discovery Probes' (₦100) to collect data.
                discovery_floor = 0.35
                
                if sig.certainty >= mode_floor or sig.certainty >= discovery_floor:
                    # ── Macro Compass Boost ──
                    from feeds_direct import macro_bias
                    now = time.time()
                    
                    # USD Spike is BEARISH for Crypto
                    if macro_bias.get("USD_SPIKE", {}).get("active") and macro_bias["USD_SPIKE"]["expires"] > now:
                        if sig.outcome == "YES": sig.certainty -= 0.05
                        else: sig.certainty += 0.05
                    
                    # Gold Breakout is BULLISH for Crypto
                    if macro_bias.get("GOLD_BREAKOUT", {}).get("active") and macro_bias["GOLD_BREAKOUT"]["expires"] > now:
                        if sig.outcome == "YES": sig.certainty += 0.05
                    
                    # ── Liquidity Wall Boost ──
                    from feeds_direct import direct_spot
                    ds = direct_spot.get(sig.asset, {})
                    bid_sz, ask_sz = ds.get("bid_sz", 0), ds.get("ask_sz", 0)
                    if bid_sz > 0 and ask_sz > 0:
                        if bid_sz > ask_sz * 5: # Support Wall
                            if sig.outcome == "YES": sig.certainty += 0.08; sig.reason += " | BUY_WALL"
                        elif ask_sz > bid_sz * 5: # Resistance Wall
                            if sig.outcome == "NO": sig.certainty += 0.08; sig.reason += " | SELL_WALL"

                    sig.mode_floor = mode_floor
                    signals.append(sig)
                    
        except Exception as e:
            log.error(f"Error evaluating strategy {name} on {asset}: {e}", exc_info=True)
            
    return sorted(signals, key=lambda s: s.certainty, reverse=True)

def merge_signals(all_signals: List[TradeSignal], state: any = None) -> List[TradeSignal]:
    """
    World-Class Convergence Engine:
    Detects when multiple strategies or timeframes align on the same asset/outcome.
    Boosts certainty by +15% for 'Stacked' conviction.
    """
    merged = {}
    for sig in all_signals:
        key = f"{sig.asset}:{sig.outcome}"
        if key not in merged:
            merged[key] = sig
        else:
            # Convergence Detected!
            existing = merged[key]
            if existing.timeframe != sig.timeframe:
                existing.certainty = min(1.0, existing.certainty + 0.15)
                existing.reason += f" | CONVERGENCE({sig.timeframe})"
                existing.converged_with.append(sig.timeframe)
            
            # If multiple strategies agree, take the one with highest certainty
            if sig.certainty > existing.certainty:
                sig.certainty = existing.certainty # Keep the boost
                merged[key] = sig
                
    final_signals = list(merged.values())
    
    # ── Dynamic Risk Parity (Cross-Asset Correlation) ──
    if state and len(final_signals) > 1:
        from strategies.utils import realized_correlation
        by_outcome = {"YES": [], "NO": []}
        for sig in final_signals:
            if sig.outcome in by_outcome:
                by_outcome[sig.outcome].append(sig)
                
        for outcome, sigs in by_outcome.items():
            if len(sigs) < 2:
                continue
                
            clusters = []
            for sig in sigs:
                assigned = False
                for cluster in clusters:
                    leader = cluster[0]
                    corr = realized_correlation(sig.asset, leader.asset, state)
                    if corr > 0.85:
                        cluster.append(sig)
                        assigned = True
                        break
                if not assigned:
                    clusters.append([sig])
                    
            for cluster in clusters:
                c_size = len(cluster)
                if c_size > 1:
                    cluster_assets = [s.asset for s in cluster]
                    for sig in cluster:
                        sig.size_pct /= c_size
                        others = [a for a in cluster_assets if a != sig.asset]
                        sig.reason += f" | RISK_PARITY({','.join(others)})"
                        
    return final_signals
