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

import config
from strategies.base import TradeSignal
from strategies.snipe      import SnipeStrategy
from strategies.arb        import ArbStrategy
from strategies.frontrun   import FrontrunStrategy
from strategies.correlate  import CorrelateStrategy
from strategies.regime     import regime_controller
from strategies.maker          import MakerStrategy
from strategies.oracle_arb     import OracleArbStrategy
from strategies.paired_sniper  import PairedSniperStrategy
from strategies.midmarket_maker import MidmarketMakerStrategy
from strategies.liquidity_regime import classify_regime

log = logging.getLogger("strategies")

_strategies = {
    "SNIPE":           SnipeStrategy(),
    "ARB":             ArbStrategy(),
    "FRONTRUN":        FrontrunStrategy(),
    "CORRELATE":       CorrelateStrategy(),
    "MAKER":           MakerStrategy(),
    "ORACLE_ARB":      OracleArbStrategy(),
    "PAIRED_SNIPER":   PairedSniperStrategy(),
    "MIDMARKET_MAKER": MidmarketMakerStrategy(),
}

# Only genuinely structural/latency strategies bypass directional performance
# learning. Single-leg MAKER is a directional binary bet placed passively; it
# must remain subject to per-strategy and per-asset learning.
_STRUCTURAL_STRATEGIES = {"ORACLE_ARB", "MIDMARKET_MAKER"}
_TAKER_STRATEGIES = {"SNIPE", "FRONTRUN", "CORRELATE", "ARB"}


def _route_strategy_names(active_names, liquidity_regime: str) -> set[str]:
    """Apply liquidity routing without ever enabling an unrequested strategy."""
    routed = set(active_names)
    if liquidity_regime in {"DISLOCATED_WIDE", "THIN_ONE_SIDED"}:
        routed = {name for name in routed if name not in _TAKER_STRATEGIES}
    return routed


def _performance_adjusted_probability(
    win_prob: float, performance_multiplier: float
) -> float:
    """Shrink directional probability toward 50% after poor settled results.

    Performance evidence may make the model less trustworthy, but historical
    results never inflate a fresh model estimate above its original value.
    """
    p = min(1.0, max(0.0, float(win_prob)))
    multiplier = min(1.0, max(0.0, float(performance_multiplier)))
    return 0.5 + (p - 0.5) * multiplier


def _perf_note(name: str, learned: dict, code: str, detail: str = "") -> None:
    """Attribute an orchestrator-level rejection to the evaluating account."""
    try:
        from strategies.utils import note_reject

        note_reject(learned, name, code, detail)
    except Exception:
        pass


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

    # ── Liquidity Regime Switching Orchestrator ──────────────────────────────
    ob_yes = market.get("ob_yes")
    ob_no  = market.get("ob_no")
    yes_p  = float(market.get("yes_price") or 0.5)
    no_p   = float(market.get("no_price")  or 0.5)

    liq_regime = "TIGHT_LIQUID"
    if ob_yes and ob_no:
        liq_regime, _ = classify_regime(ob_yes, ob_no)
    elif (yes_p + no_p > 1.15) or (yes_p + no_p < 0.85):
        # Sum dislocation only: a confident-but-valid binary market
        # (e.g. YES=0.82, NO=0.18, sum=1.00) must NOT be flagged wide.
        liq_regime = "DISLOCATED_WIDE"

    # A liquidity regime may suppress unsafe takers, but may never promote a
    # quarantined or user-disabled strategy. Previously DISLOCATED_WIDE silently
    # added MIDMARKET_MAKER even when global policy had blocked it.
    all_names = _route_strategy_names(active_names, liq_regime)
    # A liquidity regime can suppress takers; say so, because "no signals" from
    # routing looks identical to "no signals" from absent edge in the logs.
    if not all_names:
        _perf_note("ORCHESTRATOR", learned, "no_enabled_strategies",
                   f"requested={list(active_names)} regime={liq_regime}")
    elif all_names != set(active_names):
        _perf_note("ORCHESTRATOR", learned, f"takers_suppressed_{liq_regime}",
                   ",".join(sorted(set(active_names) - set(all_names))))
    if liq_regime == "DISLOCATED_WIDE":
        log.debug(
            f"Market {asset}/{market.get('timeframe')} classified "
            "DISLOCATED_WIDE: suppressing takers"
        )
    elif liq_regime == "THIN_ONE_SIDED":
        log.debug(
            f"Market {asset}/{market.get('timeframe')} classified "
            "THIN_ONE_SIDED: suppressing takers"
        )

    signals = []
    # Stable order makes signal selection reproducible across process restarts.
    for name in (n for n in _strategies if n in all_names):
        strat = _strategies.get(name)
        if not strat:
            continue
        try:
            sig = await strat.evaluate(market, learned, state, spot_price=spot_price)
            log.debug(
                f"eval {name} on {asset}/{market.get('timeframe','?')} → {'SIGNAL' if sig else 'none'}"
            )
            if not sig:
                continue

            # Genuinely structural strategies and matched-pair hedges bypass
            # directional multipliers. Single-leg MAKER deliberately does not.
            if name in _STRUCTURAL_STRATEGIES or "PAIR_HEDGE" in getattr(sig, "reason", ""):
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

            performance_mult = min(1.0, max(0.0, meta_mult * combo_mult))
            if performance_mult < 1.0:
                original_prob = sig.win_prob
                sig.win_prob = _performance_adjusted_probability(
                    original_prob, performance_mult
                )
                min_edge = (
                    config.SNIPE_MIN_BLENDED_EDGE
                    if name == "SNIPE" else 0.01
                )
                if sig.win_prob - sig.market_price < min_edge:
                    _perf_note(name, learned,
                              f"adjusted {sig.win_prob:.3f} price {sig.market_price:.3f} "
                              f"needs {min_edge:.1%}")
                    log.info(
                        f"PERFORMANCE SKIP {name} {asset}: adjusted probability "
                        f"{sig.win_prob:.3f} no longer clears price "
                        f"{sig.market_price:.3f} by {min_edge:.1%}"
                    )
                    continue
                sig.edge_at_entry = sig.win_prob - sig.market_price
                sig.reason += (
                    f" | PERF_P({original_prob:.3f}->{sig.win_prob:.3f})"
                )

            final_mult = mult * meta_mult * combo_mult
            # Certainty is presentation/admission confidence; unlike the EV
            # probability above, it also reflects the current market regime.
            final_mult = max(0.80, final_mult)
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
                "safe":       0.20,   # 59.0% WR minimum
                "balanced":   0.12,   # 55.4% WR minimum — matches SNIPE_MIN_CERTAINTY
                "aggressive": 0.08,   # 53.6% WR minimum
                "full_send":  0.05,   # 52.3% WR minimum
                "custom":     0.12,   # same as balanced by default
            }.get(mode, 0.12)

            # Pantry raid (trading drought)
            if learned.get("pantry_raid_active"):
                mode_floor -= 0.03

            # Discovery probes: allow thin edges through as probe trades.
            discovery_floor = 0.10

            if sig.certainty >= mode_floor or sig.certainty >= discovery_floor:
                sig.mode_floor = mode_floor
                signals.append(sig)
            else:
                _perf_note(name, learned, "below_mode_floor",
                           f"certainty {sig.certainty:.1%} < floor {mode_floor:.1%}")

        except Exception as e:
            _perf_note(name, learned, "strategy_error", str(e)[:150])
            log.error(f"Strategy {name} error on {asset}: {e}", exc_info=True)

    return sorted(signals, key=lambda s: s.certainty, reverse=True)


def merge_signals(all_signals: List[TradeSignal], state=None) -> List[TradeSignal]:
    """Merge genuinely agreeing directional signals without dropping hedges.

    Structural/multi-leg signals keep their own execution slot. Directional
    strategies converge only when they select the *same* outcome; disagreement
    never receives a confidence boost.
    """
    merged: dict[str, TradeSignal] = {}

    for sig in all_signals:
        is_structural = (
            sig.strategy in _STRUCTURAL_STRATEGIES
            or sig.strategy == "ARB"
            or "PAIR_HEDGE" in getattr(sig, "reason", "")
        )
        key = f"{sig.market_id}:{sig.strategy}" if is_structural else sig.market_id

        if key not in merged:
            merged[key] = sig
            continue

        existing = merged[key]
        if existing.outcome == sig.outcome and existing.strategy != sig.strategy:
            # Convergence means independent methods agree on direction.
            stronger = existing if existing.certainty >= sig.certainty else sig
            other = sig if stronger is existing else existing
            stronger.certainty = min(1.0, max(existing.certainty, sig.certainty) + 0.10)
            stronger.reason += f" | CONVERGENCE({other.strategy})"
            stronger.converged_with.append(other.strategy)
            merged[key] = stronger
            continue

        # Opposite directions are not convergence. Keep only the signal with
        # larger model edge; certainty is the tie-breaker.
        existing_edge = existing.win_prob - existing.market_price
        incoming_edge = sig.win_prob - sig.market_price
        if (incoming_edge, sig.certainty) > (existing_edge, existing.certainty):
            merged[key] = sig

    final = list(merged.values())

    # Cross-asset risk parity: if highly-correlated assets carry the same
    # direction, reduce both sizes rather than treating them as independent.
    if state and len(final) > 1:
        from strategies.utils import realized_correlation
        adjusted_pairs: set[tuple[str, str, str]] = set()
        for outcome in ("YES", "NO"):
            group = [
                signal for signal in final
                if signal.outcome == outcome and signal.strategy not in _STRUCTURAL_STRATEGIES | {"ARB"}
            ]
            for i, sig_a in enumerate(group):
                for sig_b in group[i + 1:]:
                    pair = tuple(sorted((sig_a.asset, sig_b.asset))) + (outcome,)
                    if pair in adjusted_pairs:
                        continue
                    if realized_correlation(sig_a.asset, sig_b.asset, state) > 0.85:
                        sig_a.size_pct /= 2
                        sig_b.size_pct /= 2
                        sig_a.reason += f" | RISK_PARITY({sig_b.asset})"
                        sig_b.reason += f" | RISK_PARITY({sig_a.asset})"
                        adjusted_pairs.add(pair)

    return final
