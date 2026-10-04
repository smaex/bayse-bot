"""
Strategy orchestrator.

Two strategies exist, because there are exactly two ways to be on the right
side of a price:

* **TAKER** — cross the spread, pay the fee, be right about the settling
  quantity.
* **MAKER** — post the price, pay no fee, and get paid for being the
  liquidity by the exchange's own reward and rebate programmes.

Everything else that used to live here (ARB, FRONTRUN, CORRELATE,
ORACLE_ARB, PAIRED_SNIPER, MIDMARKET_MAKER, SNIPE) is gone. Several were
quarantined for non-atomic multi-leg risk, the rest never produced
out-of-sample evidence that their edge survived fees. Keeping them meant
keeping their gate counters, their tuning knobs and their failure modes in a
system whose job is to be predictable.

What this module still owns
---------------------------
* Which strategies run for this account (never auto-enabling anything).
* Shrinking a model probability after poor settled results -- and never
  inflating one.
* Resolving a TAKER/MAKER collision on the same market.
* Ranking, so the risk budget sees the best use of capital first.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import config
from strategies.base import TradeSignal
from strategies.maker import MakerStrategy
from strategies.taker import TakerStrategy
from strategies.utils import note_reject

log = logging.getLogger("strategies")

MAX_REPORTED_CERTAINTY = 0.99

_strategies = {
    "TAKER": TakerStrategy(),
    "MAKER": MakerStrategy(),
}

# A completed pair is direction-independent, so neither of these carries
# forecast risk on its own. They are still subject to *execution* learning
# (fill rate, adverse selection) in the learner; what they are exempt from is
# the directional shrinkage below, which would be meaningless on a locked
# spread.
_LOCKED_STRATEGIES = {"MAKER"}


def _perf_note(name: str, learned: dict, code: str, detail: str = "") -> None:
    """Attribute an orchestrator-level rejection to the evaluating account."""
    try:
        note_reject(learned, name, code, detail)
    except Exception:
        pass


def _enabled_names(learned: dict) -> set[str]:
    requested = set(learned.get("strategies") or list(_strategies))
    permitted = {n for n in requested if n in _strategies}
    unknown = requested - set(_strategies)
    if unknown:
        # A saved preference naming a strategy that no longer exists must not
        # fail silently: it would look like "no signals" forever.
        _perf_note("ORCHESTRATOR", learned, "unknown_strategy_in_scope",
                   ",".join(sorted(unknown)))
        log.warning(f"Ignoring unknown strategies in account scope: {sorted(unknown)}")
    return permitted


def _performance_adjusted_probability(
    win_prob: float, performance_multiplier: float
) -> float:
    """Shrink a directional probability toward 50% after poor settled results.

    Performance evidence may make the model *less* trustworthy. It never makes
    a fresh estimate more confident: the multiplier is clamped at 1.0, so a
    good track record cannot inflate a probability the model did not claim.
    """
    p = min(1.0, max(0.0, float(win_prob)))
    multiplier = min(1.0, max(0.0, float(performance_multiplier)))
    return 0.5 + (p - 0.5) * multiplier


def _performance_multiplier(learned: dict, name: str, sig) -> float:
    cert_mults = learned.get("certainty_multipliers", {}) or {}
    meta = float(cert_mults.get(name, 1.0) or 1.0)
    combo = float(cert_mults.get(f"{sig.strategy}:{sig.asset}:{sig.timeframe}", 1.0) or 1.0)
    return min(1.0, max(0.0, meta * combo))


def _score(sig: TradeSignal) -> float:
    """Expected profit per unit of capital committed, for ranking.

    Both legs are put on the same footing on purpose. A MAKER pair's profit is
    ``1 - (bid_yes + bid_no)`` on capital of ``bid_yes + bid_no``; a TAKER's is
    ``fv/effective_price - 1`` on capital of ``effective_price``. Ranking on
    anything coarser (certainty, raw edge) favours whichever strategy happens
    to use the bigger numbers, which is not a property of the opportunity.
    """
    if sig.strategy == "MAKER" and sig.is_multi_leg() and len(sig.legs) == 2:
        capital = sum(float(leg.price) for leg in sig.legs)
        if capital <= 0:
            return 0.0
        return (1.0 - capital) / capital
    try:
        return float(sig.edge_at_entry or 0.0) / max(float(sig.market_price or 0.0), 1e-9)
    except (TypeError, ValueError):
        return 0.0


async def evaluate_all(
    market: dict,
    learned: dict,
    state,
    spot_price: float = None,
    books: dict | None = None,
) -> List[TradeSignal]:
    """Evaluate the account's enabled strategies against one market.

    ``books`` is a snapshot fetched once per market per pass and shared by both
    strategies. Fetching it inside each strategy meant the two legs could be
    priced off different books, and a pair priced off two different books is
    not a locked spread -- it is a guess that happens to look like one.
    """
    asset = market.get("asset", "?")
    learned = learned or {}
    names = _enabled_names(learned)

    if not names:
        _perf_note("ORCHESTRATOR", learned, "no_enabled_strategies",
                   f"requested={learned.get('strategies')}")
        return []

    signals: List[TradeSignal] = []
    # Stable order makes signal selection reproducible across restarts.
    for name in (n for n in ("TAKER", "MAKER") if n in names):
        strategy = _strategies.get(name)
        if strategy is None:
            continue
        try:
            sig = await strategy.evaluate(
                market, learned, state, spot_price=spot_price, books=books
            )
        except Exception as exc:
            _perf_note(name, learned, "strategy_error", str(exc)[:150])
            log.error(f"Strategy {name} error on {asset}: {exc}", exc_info=True)
            continue
        if sig is None:
            continue

        # Directional shrinkage. Applied to the win probability only: a locked
        # spread has no forecast to be overconfident about.
        if name not in _LOCKED_STRATEGIES:
            mult = _performance_multiplier(learned, name, sig)
            if mult < 1.0:
                original = sig.win_prob
                sig.win_prob = _performance_adjusted_probability(original, mult)
                # The edge has to survive the shrinkage, or the trade does not
                # happen. Re-derive rather than assume: the gate that admitted
                # this signal was evaluated on the un-shrunk number.
                effective = sig.market_price / max(1.0 - 0.0, 1e-9)
                if sig.win_prob - effective < 0:
                    _perf_note(name, learned, "shrunk_below_price",
                               f"{original:.3f}->{sig.win_prob:.3f} vs {effective:.3f}")
                    log.info(
                        f"PERFORMANCE SKIP {name} {asset}: shrunk probability "
                        f"{sig.win_prob:.3f} no longer beats price {effective:.3f}"
                    )
                    continue
                sig.edge_at_entry = sig.win_prob - effective
                sig.reason += f" | PERF_P({original:.3f}->{sig.win_prob:.3f})"

        sig.certainty = min(MAX_REPORTED_CERTAINTY, max(0.0, float(sig.certainty)))
        signals.append(sig)
        log.debug(f"eval {name} on {asset}/{market.get('timeframe', '?')} -> SIGNAL")

    return _resolve_collision(signals, learned)


def _resolve_collision(signals: List[TradeSignal], learned: dict) -> List[TradeSignal]:
    """When both legs want the same market, keep the one that needs no forecast.

    A MAKER pair buys a guaranteed payoff; a TAKER buys a probability. On the
    same market, at the same moment, the guaranteed payoff wins unless the
    taker is also a locked complete set and locks more.
    """
    if len(signals) < 2:
        return sorted(signals, key=_score, reverse=True)

    maker = next((s for s in signals if s.strategy == "MAKER"), None)
    taker = next((s for s in signals if s.strategy == "TAKER"), None)
    if maker is None or taker is None:
        return sorted(signals, key=_score, reverse=True)

    taker_locked = str(taker.outcome).upper() == "BOTH"
    if not taker_locked:
        _perf_note("TAKER", learned, "maker_preferred_on_market",
                   "MAKER pair is direction-independent; TAKER defers")
        return [maker]

    # Both locked: keep whichever locks more per unit of capital.
    if _score(maker) >= _score(taker):
        _perf_note("TAKER", learned, "maker_locks_more",
                   f"maker={_score(maker):.4f} taker={_score(taker):.4f}")
        return [maker]
    _perf_note("MAKER", learned, "taker_locks_more",
               f"taker={_score(taker):.4f} maker={_score(maker):.4f}")
    return [taker]


def merge_signals(all_signals: List[TradeSignal], state=None) -> List[TradeSignal]:
    """De-duplicate across markets and thin out correlated directional bets.

    With two strategies there is no "convergence boost" to compute -- two
    methods agreeing was only ever meaningful when there were five. What is
    left is the part that was always the real job: not stacking the same bet
    three times because it appears on three correlated assets.
    """
    merged: dict[str, TradeSignal] = {}
    for sig in all_signals or []:
        key = f"{sig.market_id}:{sig.strategy}"
        if key not in merged:
            merged[key] = sig
            continue
        if _score(sig) > _score(merged[key]):
            merged[key] = sig

    final = list(merged.values())

    # Cross-asset risk parity: BTC/ETH/SOL moving together is one bet, not
    # three. Size both down rather than treating them as independent.
    if state and len(final) > 1:
        from strategies.utils import realized_correlation
        adjusted: set[tuple[str, str, str]] = set()
        directional = [s for s in final if s.strategy == "TAKER"]
        for i, sig_a in enumerate(directional):
            for sig_b in directional[i + 1:]:
                if sig_a.outcome != sig_b.outcome:
                    continue
                pair = tuple(sorted((sig_a.asset, sig_b.asset))) + (sig_a.outcome,)
                if pair in adjusted:
                    continue
                if realized_correlation(sig_a.asset, sig_b.asset, state) > 0.85:
                    for s in (sig_a, sig_b):
                        if "RISK_PARITY" not in s.reason:
                            s.size_pct = max(0.01, s.size_pct * 0.70)
                            s.reason += f" | RISK_PARITY"
                    adjusted.add(pair)

    return sorted(final, key=_score, reverse=True)
