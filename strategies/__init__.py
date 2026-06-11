"""
Strategy orchestrator — evaluates all active strategies and merges signals.
Dead strategies removed: POLY_EDGE, NEWS, MARKET_BIAS, POLY_COPY.
Active: SNIPE, ARB, FRONTRUN, CORRELATE.
"""

import logging
import time
from typing import List
from strategies.base import TradeSignal
from strategies.snipe    import SnipeStrategy
from strategies.arb      import ArbStrategy
from strategies.frontrun import FrontrunStrategy
from strategies.correlate import CorrelateStrategy
from strategies.regime   import regime_controller

log = logging.getLogger("strategies")

_strategies = {
    "SNIPE":     SnipeStrategy(),
    "ARB":       ArbStrategy(),
    "FRONTRUN":  FrontrunStrategy(),
    "CORRELATE": CorrelateStrategy(),
}


async def evaluate_all(
    market: dict, learned: dict, state, spot_price: float = None
) -> List[TradeSignal]:
    """
    Evaluate all enabled strategies on a single market.
    Applies regime multipliers and per-strategy certainty multipliers from the learner.
    """
    asset        = market["asset"]
    learned      = learned or {}
    active_names = learned.get("strategies", list(_strategies.keys()))

    regime_mults = regime_controller.get_multipliers(asset, state)
    cert_mults   = learned.get("certainty_multipliers", {})

    signals = []
    for name in active_names:
        strat = _strategies.get(name)
        if not strat:
            continue
        try:
            sig = await strat.evaluate(market, learned, state, spot_price=spot_price)
            if not sig:
                continue

            # Regime multiplier
            cat  = "TREND" if name == "CORRELATE" else ("SNIPE" if name in ("SNIPE", "ARB", "FRONTRUN") else "SNIPE")
            mult = regime_mults.get(cat, 1.0)

            # Bayesian performance multiplier (per-strategy + per-combo)
            meta_mult  = cert_mults.get(name, 1.0)
            combo_key  = f"{sig.strategy}:{sig.asset}:{sig.timeframe}"
            combo_mult = cert_mults.get(combo_key, 1.0)

            final_mult = mult * meta_mult * combo_mult
            if final_mult != 1.0:
                sig.certainty = min(1.0, max(0.0, sig.certainty * final_mult))
                sig.reason   += f" | MULT(x{final_mult:.2f})"

            # Mode floor
            mode       = learned.get("mode", "balanced")
            mode_floor = {"safe": 0.60, "balanced": 0.48, "aggressive": 0.40, "full_send": 0.32}.get(mode, 0.48)

            # Pantry raid (trading drought)
            if learned.get("pantry_raid_active"):
                mode_floor -= 0.10

            # Discovery probes: allow low-certainty signals through as tiny ₦100 trades
            discovery_floor = 0.35

            if sig.certainty >= mode_floor or sig.certainty >= discovery_floor:
                sig.mode_floor = mode_floor
                signals.append(sig)

        except Exception as e:
            log.error(f"Strategy {name} error on {asset}: {e}", exc_info=True)

    return sorted(signals, key=lambda s: s.certainty, reverse=True)


def merge_signals(all_signals: List[TradeSignal], state=None) -> List[TradeSignal]:
    """
    Convergence engine: if multiple strategies agree on the same asset+outcome,
    boost certainty by +15%.  Also applies cross-asset risk parity.
    """
    merged: dict[str, TradeSignal] = {}

    for sig in all_signals:
        key = f"{sig.asset}:{sig.outcome}"
        if key not in merged:
            merged[key] = sig
        else:
            existing = merged[key]
            if existing.timeframe != sig.timeframe or existing.strategy != sig.strategy:
                # Convergence — boost the stronger signal
                existing.certainty = min(1.0, existing.certainty + 0.15)
                existing.reason   += f" | CONVERGENCE({sig.strategy}/{sig.timeframe})"
                existing.converged_with.append(sig.timeframe)
            if sig.certainty > existing.certainty:
                sig.certainty      = existing.certainty  # keep the boost
                merged[key]        = sig

    final = list(merged.values())

    # Cross-asset risk parity: if two highly-correlated assets both have YES signals,
    # halve each position to avoid doubling up on the same market move.
    if state and len(final) > 1:
        from strategies.utils import realized_correlation
        for outcome in ("YES", "NO"):
            group = [s for s in final if s.outcome == outcome and s.strategy != "ARB"]
            if len(group) < 2:
                continue
            for i, sig_a in enumerate(group):
                for sig_b in group[i + 1:]:
                    corr = realized_correlation(sig_a.asset, sig_b.asset, state)
                    if corr > 0.85:
                        sig_a.size_pct /= 2
                        sig_b.size_pct /= 2
                        sig_a.reason  += f" | RISK_PARITY({sig_b.asset})"
                        sig_b.reason  += f" | RISK_PARITY({sig_a.asset})"

    return final
