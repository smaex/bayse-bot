"""
Strategy engine — three independent signal generators:

1. SNIPE     Near-close certainty trading (primary edge)
2. CORRELATE Cross-asset signal from BTC→ETH/SOL lead-lag
3. ARB       Mint/Burn complete-set arbitrage (risk-free)

All strategies return a TradeSignal or None.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Literal
from config import (
    SNIPE_ENTRY_SECONDS, SNIPE_MIN_CERTAINTY, SNIPE_MAX_PRICE,
    CORRELATION_THRESHOLD, CORRELATION_WINDOW_SEC, ARB_TRIGGER,
)
import feeds
import news as news_mod

log = logging.getLogger(__name__)

StrategyType = Literal["SNIPE", "CORRELATE", "ARB"]


@dataclass
class TradeSignal:
    strategy: StrategyType
    event_id: str
    market_id: str
    asset: str
    timeframe: str
    outcome: str          # "YES" or "NO"
    outcome_id: str
    certainty: float      # 0–1, how confident we are
    market_price: float   # current AMM price of chosen outcome
    size_pct: float       # fraction of bankroll to trade
    reason: str
    title: str = ""
    arb_quantity: float = 0.0   # only for ARB strategy
    converged_with: list = field(default_factory=list)  # other strategies that agreed


# Track last BTC market move timestamp for correlation signals
_btc_signal_time: dict[str, float] = {}   # timeframe → unix timestamp
_btc_signal_direction: dict[str, str] = {}  # timeframe → "UP" or "DOWN"


def effective_fee(fee_rate: float, price: float) -> float:
    """Variance-based fee: fee_rate × P × max(1−P, 0.5)"""
    return fee_rate * price * max(1.0 - price, 0.5)


def breakeven_probability(market_price: float, fee_rate: float) -> float:
    """Minimum true probability needed to be profitable at this price."""
    fee = effective_fee(fee_rate, market_price)
    return market_price + fee


# ── Strategy 1: Near-close sniping ───────────────────────────────────────────

def snipe_signal(market: dict, min_certainty: float = SNIPE_MIN_CERTAINTY) -> Optional[TradeSignal]:
    """
    Compare live Binance spot vs market threshold in the final seconds.

    Certainty = how far spot has moved from threshold as a fraction.
    E.g. threshold=77714, spot=77900 → distance = 186 pts → 0.24% above.
    We scale this to a certainty score using time remaining.
    """
    secs = market["secs_to_close"]
    if secs > SNIPE_ENTRY_SECONDS or secs < 0:
        return None

    asset = market["asset"]
    threshold = market.get("threshold")
    if not threshold:
        return None

    live_price = feeds.spot.get(asset)
    if not live_price:
        log.warning(f"SNIPE [{asset} {market['timeframe']}] {secs:.0f}s left — no spot price in feeds.spot")
        return None

    distance_pct = (live_price - threshold) / threshold  # positive = above = YES wins
    time_weight = 1.0 - (secs / SNIPE_ENTRY_SECONDS)     # 0 at entry, 1 at close

    # Certainty: distance from threshold scaled by time remaining
    # More time left = need bigger distance to be confident
    raw_certainty = abs(distance_pct) / max(0.005 - time_weight * 0.004, 0.001)
    certainty = min(raw_certainty, 0.99)

    log.info(
        f"SNIPE [{asset} {market['timeframe']}] {secs:.0f}s left | "
        f"spot={live_price:,.2f} threshold={threshold:,.2f} ({distance_pct:+.3%}) | "
        f"certainty={certainty:.2%} min={min_certainty:.0%} "
        f"{'✓' if certainty >= min_certainty else '✗ LOW'}"
    )

    if certainty < min_certainty:
        return None

    if distance_pct > 0:
        outcome = "YES"
        outcome_id = market["yes_id"]
        market_price = market["yes_price"]
    else:
        outcome = "NO"
        outcome_id = market["no_id"]
        market_price = market["no_price"]

    if market_price > SNIPE_MAX_PRICE:
        log.info(f"SNIPE [{asset} {market['timeframe']}] REJECTED — price {market_price:.3f} > max {SNIPE_MAX_PRICE}")
        return None  # already priced in, not worth it

    # Check fee-adjusted profitability
    fee_rate = market.get("fee_rate", 0.04)
    be_prob = breakeven_probability(market_price, fee_rate)
    if certainty < be_prob:
        log.info(f"SNIPE [{asset} {market['timeframe']}] REJECTED — certainty {certainty:.2%} < breakeven {be_prob:.2%}")
        return None

    # Size: 3% of bankroll for high certainty, scale down for lower
    size_pct = 0.03 * certainty

    reason = (
        f"Spot {asset}={live_price:,.2f} vs threshold={threshold:,.2f} "
        f"({distance_pct:+.3%}), {secs:.0f}s to close, certainty={certainty:.2%}"
    )

    return TradeSignal(
        strategy="SNIPE",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=asset,
        timeframe=market["timeframe"],
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=certainty,
        market_price=market_price,
        size_pct=size_pct,
        reason=reason,
        title=market["title"],
    )


# ── Strategy 2: Cross-asset correlation ──────────────────────────────────────

def record_btc_move(market: dict, yes_price_new: float):
    """Call this when BTC market YES price moves significantly."""
    if market["asset"] != "BTC":
        return
    tf = market["timeframe"]
    old = feeds.prev_yes.get(market["market_id"], yes_price_new)
    move = yes_price_new - old
    if abs(move) >= CORRELATION_THRESHOLD:
        _btc_signal_time[tf] = time.time()
        _btc_signal_direction[tf] = "UP" if move > 0 else "DOWN"
        log.info(f"BTC {tf} market moved {move:+.3f} — correlation signal {_btc_signal_direction[tf]}")


def correlate_signal(market: dict) -> Optional[TradeSignal]:
    """
    If BTC market recently repriced significantly, trade same direction on ETH/SOL.
    """
    if market["asset"] == "BTC":
        return None  # only ETH and SOL benefit from this

    tf = market["timeframe"]
    signal_time = _btc_signal_time.get(tf)
    if not signal_time:
        return None

    age = time.time() - signal_time
    if age > CORRELATION_WINDOW_SEC:
        return None

    # Signal fades with age
    freshness = 1.0 - (age / CORRELATION_WINDOW_SEC)
    direction = _btc_signal_direction.get(tf)

    if direction == "UP":
        outcome = "YES"
        outcome_id = market["yes_id"]
        market_price = market["yes_price"]
    else:
        outcome = "NO"
        outcome_id = market["no_id"]
        market_price = market["no_price"]

    if market_price > 0.80:
        return None  # already priced in

    certainty = 0.60 * freshness  # lower confidence than snipe
    fee_rate = market.get("fee_rate", 0.04)
    if certainty < breakeven_probability(market_price, fee_rate):
        return None

    size_pct = 0.015 * freshness  # smaller size for correlation trades

    return TradeSignal(
        strategy="CORRELATE",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=market["asset"],
        timeframe=tf,
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=certainty,
        market_price=market_price,
        size_pct=size_pct,
        reason=f"BTC→{market['asset']} correlation {direction} signal, age={age:.0f}s, freshness={freshness:.2f}",
        title=market["title"],
    )


# ── Strategy 3: Mint/Burn arbitrage ──────────────────────────────────────────

def arb_signal(market: dict) -> Optional[TradeSignal]:
    """
    Risk-free arb: if YES + NO < 1.00, buy both sides then burn for 1.00.
    Only applicable when there's enough runway before close to execute both legs.
    """
    secs = market["secs_to_close"]
    if secs < 30:
        return None  # not enough time to safely execute both legs

    yes_p = market["yes_price"]
    no_p = market["no_price"]
    combined = yes_p + no_p

    if combined > ARB_TRIGGER:
        return None  # no arb

    profit_per_unit = 1.00 - combined  # e.g. 0.03 per ₦1 invested
    # Must exceed fee on both legs
    fee_yes = effective_fee(market.get("fee_rate", 0.04), yes_p)
    fee_no = effective_fee(market.get("fee_rate", 0.04), no_p)
    net_profit = profit_per_unit - fee_yes - fee_no

    if net_profit <= 0:
        return None

    return TradeSignal(
        strategy="ARB",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=market["asset"],
        timeframe=market["timeframe"],
        outcome="YES",  # we buy both, YES is first leg
        outcome_id=market["yes_id"],
        certainty=1.0,
        market_price=combined,
        size_pct=0.05,  # max 5% for arb (risk-free but capital tied up)
        reason=f"ARB: YES({yes_p:.3f})+NO({no_p:.3f})={combined:.3f} < 1.00, net_profit={net_profit:.4f}/unit",
        title=market["title"],
        arb_quantity=0,  # set by bot based on available balance
    )


# ── Strategy 4: News sentiment ────────────────────────────────────────────────

def news_signal(market: dict, sentiment_threshold: float = 0.35) -> Optional[TradeSignal]:
    """
    If a live news signal exists for this asset, trade in that direction.
    Only fires when:
      - Signal is within its decay window
      - Market hasn't already priced in the move (price still near 0.50)
      - Signal strength × edge exceeds fee drag
    """
    asset = market["asset"]
    sig = news_mod.best_signal_for(asset)
    if not sig:
        return None

    strength = sig.strength()
    if strength < sentiment_threshold:
        return None

    yes_p = market["yes_price"]
    no_p = market["no_price"]

    if sig.direction == "BULLISH":
        # Buy UP/YES — asset expected to rise
        if yes_p > 0.72:
            return None  # market already priced the bullish move
        outcome, outcome_id, market_price = "YES", market["yes_id"], yes_p
    else:
        # Buy DOWN/NO — asset expected to fall
        if no_p > 0.72:
            return None  # market already priced the bearish move
        outcome, outcome_id, market_price = "NO", market["no_id"], no_p

    fee_rate = market.get("fee_rate", 0.04)
    if strength < breakeven_probability(market_price, fee_rate):
        return None

    size_pct = 0.02 * strength  # news trades are smaller — more uncertainty

    return TradeSignal(
        strategy="NEWS",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=asset,
        timeframe=market["timeframe"],
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=strength,
        market_price=market_price,
        size_pct=size_pct,
        reason=f"News [{sig.direction}] src={sig.source} score={strength:.2f}: {sig.headline[:60]}",
        title=market["title"],
    )


def _apply_convergence(signals: list[TradeSignal]) -> list[TradeSignal]:
    """
    Boost the best directional signal when multiple independent strategies agree.

    Rules:
    - ARB is excluded (risk-free, certainty already 1.0, different category)
    - 2+ directional signals pointing the same way → certainty +7% each, size +25% each
    - Conflicting signals (some YES, some NO) → flag it, no boost, highest certainty wins
    """
    arb = [s for s in signals if s.strategy == "ARB"]
    directional = [s for s in signals if s.strategy != "ARB"]

    if len(directional) < 2:
        return signals  # nothing to converge

    yes_sigs = [s for s in directional if s.outcome == "YES"]
    no_sigs  = [s for s in directional if s.outcome == "NO"]

    if yes_sigs and no_sigs:
        # Conflicting strategies — flag it, let the strongest directional signal stand
        best = max(directional, key=lambda s: s.certainty)
        conflict_strats = " vs ".join(
            f"{s.strategy}({'YES' if s.outcome=='YES' else 'NO'})" for s in directional
        )
        best.reason = f"[⚡ CONFLICT: {conflict_strats}] " + best.reason
        return arb + sorted(directional, key=lambda s: s.certainty, reverse=True)

    # All directional signals agree — boost the best one
    dominant = yes_sigs or no_sigs
    top = max(dominant, key=lambda s: s.certainty)
    n_extra = len(dominant) - 1  # number of confirming signals beyond the primary

    top.certainty      = min(0.99, top.certainty + 0.07 * n_extra)
    top.size_pct       = top.size_pct * (1.0 + 0.25 * n_extra)
    top.converged_with = [s.strategy for s in dominant if s is not top]

    confirming = "+".join(s.strategy for s in dominant)
    top.reason = f"[🎯 CONVERGED: {confirming}] " + top.reason

    log.info(
        f"Signal convergence: {confirming} → {top.outcome} "
        f"certainty={top.certainty:.2%} size_pct={top.size_pct:.4f}"
    )

    return arb + [top]


def evaluate(market: dict, strategies: list | None = None, learned: dict | None = None) -> list[TradeSignal]:
    """Run the given strategies on a market, return signals sorted by certainty."""
    if strategies is None:
        strategies = ["SNIPE", "CORRELATE", "ARB", "NEWS"]
    learned = learned or {}

    min_cert    = learned.get("snipe_min_certainty", SNIPE_MIN_CERTAINTY)
    news_thresh = learned.get("news_sentiment_threshold", 0.35)

    dispatch = {
        "SNIPE":     lambda m: snipe_signal(m, min_certainty=min_cert),
        "CORRELATE": correlate_signal,
        "ARB":       arb_signal,
        "NEWS":      lambda m: news_signal(m, sentiment_threshold=news_thresh),
    }

    signals = []
    for name in strategies:
        fn = dispatch.get(name)
        if fn:
            sig = fn(market)
            if sig:
                signals.append(sig)

    if not signals:
        return []

    signals = _apply_convergence(signals)
    return sorted(signals, key=lambda s: s.certainty, reverse=True)
