"""
Strategy orchestrator — evaluates all active strategies and merges signals.
Dead strategies removed: POLY_EDGE, NEWS, MARKET_BIAS, POLY_COPY.
Active: SNIPE, ARB, FRONTRUN, CORRELATE, MAKER, ORACLE_ARB.

New in this version:
  MAKER      — Passive CLOB market making (spread capture), pbot-6 style.
  ORACLE_ARB — Final-seconds latency arbitrage on the Binance oracle.
"""

import logging
import time
from typing import List
from strategies.base import TradeSignal
from strategies.snipe      import SnipeStrategy
from strategies.arb        import ArbStrategy
from strategies.frontrun   import FrontrunStrategy
from strategies.correlate  import CorrelateStrategy
from strategies.regime     import regime_controller
from strategies.maker      import MakerStrategy
from strategies.oracle_arb import OracleArbStrategy

log = logging.getLogger("strategies")

_strategies = {
    "SNIPE":      SnipeStrategy(),
    "ARB":        ArbStrategy(),
    "FRONTRUN":   FrontrunStrategy(),
    "CORRELATE":  CorrelateStrategy(),
    "MAKER":      MakerStrategy(),
    "ORACLE_ARB": OracleArbStrategy(),
}

# Structural strategies that bypass the regime/certainty multiplier system.
# They fire based on market structure (spread, oracle lag), not directional bets.
_STRUCTURAL_STRATEGIES = {"MAKER", "ORACLE_ARB"}


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

    # Respect the active strategies set configured by the user
    all_names = set(active_names)

    signals = []
    for name in all_names:
        strat = _strategies.get(name)
        if not strat:
            continue
        try:
            sig = await strat.evaluate(market, learned, state, spot_price=spot_price)
            if not sig:
                continue

            # Structural strategies (MAKER, ORACLE_ARB) bypass regime and
            # certainty multipliers — they exploit market structure, not direction.
            if name in _STRUCTURAL_STRATEGIES:
                sig.mode_floor = 0.0   # always allowed through
                signals.append(sig)
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

            # Mode floor — minimum certainty to actually execute a trade.
            # IMPORTANT: these must be consistent with SNIPE_MIN_CERTAINTY in config.py.
            # SNIPE_MIN_CERTAINTY=0.27 requires win_prob>=62%. If mode_floor is 0.48,
            # all SNIPE signals get killed here AFTER passing snipe.py's internal check.
            # Aligned to allow 62%+ WR signals through in all modes.
            mode       = learned.get("mode", "balanced")
            mode_floor = {
                "safe":       0.35,   # 65.8% WR minimum — cautious but not paralyzed
                "balanced":   0.27,   # 62.1% WR minimum — our primary trading mode
                "aggressive": 0.20,   # 59.0% WR minimum — more volume, higher variance
                "full_send":  0.15,   # 56.8% WR minimum — maximum volume
                "custom":     0.27,   # same as balanced by default
            }.get(mode, 0.27)

            # Pantry raid (trading drought)
            if learned.get("pantry_raid_active"):
                mode_floor -= 0.05

            # Discovery probes: allow genuinely thin edges through as ₦100 probe trades.
            # Strategy math has already confirmed +EV before this point.
            discovery_floor = 0.15

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
