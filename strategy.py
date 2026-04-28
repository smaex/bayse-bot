"""
Strategy engine — four independent signal generators.

Quantitative framework (applied throughout):

  Win probability via diffusion model
  ────────────────────────────────────
  Treats the underlying asset price as a Brownian motion process.
  P(win) = Φ( |d| / (σ_h × √T_h) )
  where d   = fractional distance of spot from threshold
        σ_h = asset hourly volatility
        T_h = hours remaining until market close
        Φ   = standard normal CDF

  This is the same integral that Black-Scholes uses for d2 — the probability
  that the asset ends above/below the strike given current distance and time.

  Dynamic max entry price (Kelly-derived)
  ────────────────────────────────────────
  From first principles:
    EV = W × (1/P × (1-fee) - 1) - (1-W) > 0
    → P < W × (1 - fee)
  So we only enter when: market_price < win_prob × (1 - fee_rate)
  This guarantees every trade has positive expected value before sizing.

  Quarter-Kelly position sizing
  ──────────────────────────────
  b  = net payoff ratio = (1-fee)/market_price - 1
  f* = (W×b - (1-W)) / b    ← full Kelly fraction
  We use f*/4 capped at 5% of bankroll for robustness against model error.

  Per-timeframe entry windows
  ────────────────────────────
  Each timeframe has its own optimal entry window. Short markets (5min) move
  fast — enter 4 minutes out when the signal is already strong. Long markets
  (1h, 6h) need earlier entry to catch prices before they fully converge.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional, Literal
from config import (
    SNIPE_MIN_CERTAINTY, SNIPE_ENTRY_WINDOWS, ASSET_HOURLY_VOL,
    CORRELATION_THRESHOLD, CORRELATION_WINDOW_SEC, ARB_TRIGGER,
)
import feeds
import news as news_mod

log = logging.getLogger(__name__)

StrategyType = Literal["SNIPE", "CORRELATE", "ARB", "NEWS"]


@dataclass
class TradeSignal:
    strategy: StrategyType
    event_id: str
    market_id: str
    asset: str
    timeframe: str
    outcome: str          # "YES" or "NO"
    outcome_id: str
    certainty: float      # 0–1 derived from win_probability
    market_price: float   # current AMM price of chosen outcome
    size_pct: float       # fraction of bankroll (quarter-Kelly)
    reason: str
    title: str = ""
    arb_quantity: float = 0.0
    converged_with: list = field(default_factory=list)


# ── BTC tracking for CORRELATE ────────────────────────────────────────────────
_btc_signal_time:      dict[str, float] = {}
_btc_signal_direction: dict[str, str]   = {}
_btc_signal_move:      dict[str, float] = {}


# ── Quantitative helpers ──────────────────────────────────────────────────────

def _norm_cdf(z: float) -> float:
    """Standard normal CDF — uses math.erf (stdlib, no scipy needed)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def win_probability(distance_pct: float, secs_remaining: float, asset: str) -> float:
    """
    P(our direction wins) via diffusion model (Brownian motion assumption).

    If spot is d% above threshold with T hours left and the asset has hourly
    volatility σ_h, the probability it stays above the threshold is:
        P = Φ( d / (σ_h × √T) )

    More time remaining → more chance of reversal → lower P for the same distance.
    Larger distance → less likely to reverse → higher P.
    """
    sigma_h = ASSET_HOURLY_VOL.get(asset, 0.022)
    t_hours = max(secs_remaining / 3600.0, 1.0 / 3600.0)  # floor at 1 second
    z = abs(distance_pct) / (sigma_h * math.sqrt(t_hours))
    return _norm_cdf(z)


def max_ev_price(win_prob: float, fee_rate: float = 0.04) -> float:
    """
    Max entry price where EV > 0.
    Derived from EV = W×(payout - 1) - (1-W) > 0, where payout = (1-fee)/price.
    Rearranges to: price < W × (1 - fee_rate)
    """
    return win_prob * (1.0 - fee_rate)


def kelly_size(win_prob: float, market_price: float, fee_rate: float = 0.04,
               fraction: float = 0.25, cap: float = 0.05) -> float:
    """
    Quarter-Kelly position size fraction, capped at `cap`.

    Full Kelly: f* = (W×b - (1-W)) / b  where b = net payoff if win = (1-fee)/price - 1
    We use fraction=0.25 (quarter-Kelly) to account for model error and
    the fat-tailed reality of crypto price distributions.
    """
    b = (1.0 - fee_rate) / market_price - 1.0
    if b <= 0:
        return 0.0
    raw_kelly = (win_prob * b - (1.0 - win_prob)) / b
    return min(max(raw_kelly * fraction, 0.0), cap)


def _certainty_from_prob(win_prob: float) -> float:
    """Map win_prob [0.50–0.999] → certainty [0–1] for display and thresholds."""
    return max(0.0, min((win_prob - 0.50) / 0.45, 0.99))


def certainty_to_prob(certainty: float) -> float:
    """Legacy helper used by CORRELATE / NEWS — maps certainty [0–1] → prob [0.50–0.95]."""
    return 0.50 + 0.45 * min(certainty, 1.0)


# ── Strategy 1: SNIPE — per-timeframe diffusion model ────────────────────────

def snipe_signal(market: dict, min_certainty: float = SNIPE_MIN_CERTAINTY) -> Optional[TradeSignal]:
    """
    Enter when the live spot price has moved convincingly past the threshold,
    using a diffusion model to estimate true win probability.

    Each timeframe gets its own entry window:
      5min  → last 4 min  (market price still exploitable when certainty is high)
      15min → last 10 min
      1h    → last 30 min (catch before price fully converges)
      6h/1d → even earlier

    Max entry price is dynamic: only enter if market_price < win_prob × (1-fee),
    guaranteeing positive EV on every trade we accept.
    """
    tf   = market["timeframe"]
    secs = market["secs_to_close"]

    entry_window = SNIPE_ENTRY_WINDOWS.get(tf)
    if entry_window is None or secs > entry_window or secs < 0:
        return None

    asset     = market["asset"]
    threshold = market.get("threshold")
    live_spot = feeds.spot.get(asset)
    if not threshold or not live_spot:
        if not live_spot:
            log.warning(f"SNIPE [{asset} {tf}] no spot price in feeds.spot")
        return None

    distance_pct = (live_spot - threshold) / threshold  # +ve = YES wins

    # ── Diffusion-model win probability ──────────────────────────────────────
    w_est     = win_probability(distance_pct, secs, asset)
    certainty = _certainty_from_prob(w_est)

    log.info(
        f"SNIPE [{asset} {tf}] {secs:.0f}s | "
        f"spot={live_spot:,.2f} threshold={threshold:,.2f} ({distance_pct:+.3%}) | "
        f"w_est={w_est:.1%} certainty={certainty:.2%} "
        f"{'✓' if certainty >= min_certainty else '✗ LOW'}"
    )

    if certainty < min_certainty:
        return None

    # ── Direction ─────────────────────────────────────────────────────────────
    if distance_pct > 0:
        outcome, outcome_id, market_price = "YES", market["yes_id"], market["yes_price"]
    else:
        outcome, outcome_id, market_price = "NO",  market["no_id"],  market["no_price"]

    # ── Dynamic EV gate: only enter if market hasn't priced out our edge ──────
    fee_rate  = market.get("fee_rate", 0.04)
    ev_ceiling = max_ev_price(w_est, fee_rate)

    if market_price >= ev_ceiling:
        log.info(
            f"SNIPE [{asset} {tf}] REJECTED — market {market_price:.3f} >= "
            f"EV ceiling {ev_ceiling:.3f} (w_est={w_est:.1%})"
        )
        return None

    # ── Quarter-Kelly sizing ──────────────────────────────────────────────────
    size = kelly_size(w_est, market_price, fee_rate)

    log.info(
        f"SNIPE [{asset} {tf}] ✅ SIGNAL | "
        f"market={market_price:.3f} < ceiling={ev_ceiling:.3f} | "
        f"size={size:.2%}"
    )

    return TradeSignal(
        strategy="SNIPE",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=asset,
        timeframe=tf,
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=certainty,
        market_price=market_price,
        size_pct=size,
        reason=(
            f"Spot {asset}={live_spot:,.2f} vs threshold={threshold:,.2f} "
            f"({distance_pct:+.3%}), {secs:.0f}s to close | "
            f"w_est={w_est:.1%} market={market_price:.3f} ceiling={ev_ceiling:.3f} [{tf}]"
        ),
        title=market["title"],
    )


# ── Strategy 2: CORRELATE — BTC → ETH/SOL lead-lag ───────────────────────────

def record_btc_move(market: dict, yes_price_new: float):
    """Record any BTC market move ≥1%. Per-user threshold applied later in correlate_signal."""
    if market["asset"] != "BTC":
        return
    tf  = market["timeframe"]
    old = feeds.prev_yes.get(market["market_id"], yes_price_new)
    move = yes_price_new - old
    if abs(move) >= 0.01:
        _btc_signal_time[tf]      = time.time()
        _btc_signal_direction[tf] = "UP" if move > 0 else "DOWN"
        _btc_signal_move[tf]      = abs(move)
        log.info(f"BTC {tf} market moved {move:+.3f} — stored for CORRELATE")


def correlate_signal(market: dict, threshold: float = CORRELATION_THRESHOLD) -> Optional[TradeSignal]:
    """
    BTC market reprices → trade same direction on ETH/SOL before it catches up.
    Uses dynamic EV ceiling (same framework as SNIPE) instead of hardcoded price cap.
    """
    if market["asset"] == "BTC":
        return None

    tf          = market["timeframe"]
    signal_time = _btc_signal_time.get(tf)
    if not signal_time:
        return None

    age = time.time() - signal_time
    if age > CORRELATION_WINDOW_SEC:
        return None

    actual_move = _btc_signal_move.get(tf, 0.0)
    if actual_move < threshold:
        return None

    freshness = 1.0 - (age / CORRELATION_WINDOW_SEC)
    direction = _btc_signal_direction.get(tf)

    if direction == "UP":
        outcome, outcome_id, market_price = "YES", market["yes_id"], market["yes_price"]
    else:
        outcome, outcome_id, market_price = "NO",  market["no_id"],  market["no_price"]

    certainty = 0.60 * freshness
    w_est     = certainty_to_prob(certainty)
    fee_rate  = market.get("fee_rate", 0.04)
    ev_ceiling = max_ev_price(w_est, fee_rate)

    if market_price >= ev_ceiling:
        return None

    size = kelly_size(w_est, market_price, fee_rate)

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
        size_pct=size,
        reason=(
            f"BTC→{market['asset']} {direction} correlation, "
            f"age={age:.0f}s freshness={freshness:.2f} "
            f"market={market_price:.3f} ceiling={ev_ceiling:.3f}"
        ),
        title=market["title"],
    )


# ── Strategy 3: ARB — Mint/Burn arbitrage (risk-free) ────────────────────────

def arb_signal(market: dict) -> Optional[TradeSignal]:
    """
    If YES + NO < 1.00, buy both sides then burn for 1.00 — risk-free profit.
    Net profit must exceed fee drag on both legs.
    """
    secs = market["secs_to_close"]
    if secs < 30:
        return None

    yes_p    = market["yes_price"]
    no_p     = market["no_price"]
    combined = yes_p + no_p

    if combined > ARB_TRIGGER:
        return None

    fee_rate   = market.get("fee_rate", 0.04)
    net_profit = (1.0 - combined) - (
        fee_rate * yes_p * max(1 - yes_p, 0.5) +
        fee_rate * no_p  * max(1 - no_p,  0.5)
    )

    if net_profit < 0.005:
        return None

    return TradeSignal(
        strategy="ARB",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=market["asset"],
        timeframe=market["timeframe"],
        outcome="YES",
        outcome_id=market["yes_id"],
        certainty=1.0,
        market_price=combined,
        size_pct=0.05,
        reason=(
            f"ARB: YES({yes_p:.3f})+NO({no_p:.3f})={combined:.3f} < 1.00 | "
            f"net_profit={net_profit:.4f}/unit"
        ),
        title=market["title"],
        arb_quantity=0,
    )


# ── Strategy 4: NEWS — sentiment-driven directional trade ────────────────────

def news_signal(market: dict, sentiment_threshold: float = 0.35) -> Optional[TradeSignal]:
    """
    Trade in the direction of a live high-confidence news signal.
    Uses dynamic EV ceiling — avoids entering markets already priced for the news.
    """
    asset = market["asset"]
    sig   = news_mod.best_signal_for(asset)
    if not sig:
        return None

    strength = sig.strength()
    if strength < sentiment_threshold:
        return None

    yes_p = market["yes_price"]
    no_p  = market["no_price"]

    if sig.direction == "BULLISH":
        outcome, outcome_id, market_price = "YES", market["yes_id"], yes_p
    else:
        outcome, outcome_id, market_price = "NO",  market["no_id"],  no_p

    w_est      = certainty_to_prob(strength)
    fee_rate   = market.get("fee_rate", 0.04)
    ev_ceiling = max_ev_price(w_est, fee_rate)

    if market_price >= ev_ceiling:
        return None

    size = kelly_size(w_est, market_price, fee_rate, fraction=0.20)  # smaller fraction for news

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
        size_pct=size,
        reason=(
            f"News [{sig.direction}] src={sig.source} score={strength:.2f} "
            f"market={market_price:.3f} ceiling={ev_ceiling:.3f}: {sig.headline[:60]}"
        ),
        title=market["title"],
    )


# ── Signal convergence boost ──────────────────────────────────────────────────

def _apply_convergence(signals: list[TradeSignal]) -> list[TradeSignal]:
    """
    Boost the best signal when multiple independent strategies agree on direction.
    ARB excluded (risk-free, different category).
    2+ agreeing → certainty +7%, size +25% per confirming signal.
    Conflicting signals → no boost, strongest wins.
    """
    arb        = [s for s in signals if s.strategy == "ARB"]
    directional = [s for s in signals if s.strategy != "ARB"]

    if len(directional) < 2:
        return signals

    yes_sigs = [s for s in directional if s.outcome == "YES"]
    no_sigs  = [s for s in directional if s.outcome == "NO"]

    if yes_sigs and no_sigs:
        best = max(directional, key=lambda s: s.certainty)
        conflict = " vs ".join(
            f"{s.strategy}({'YES' if s.outcome=='YES' else 'NO'})" for s in directional
        )
        best.reason = f"[⚡ CONFLICT: {conflict}] " + best.reason
        return arb + sorted(directional, key=lambda s: s.certainty, reverse=True)

    dominant = yes_sigs or no_sigs
    top      = max(dominant, key=lambda s: s.certainty)
    n_extra  = len(dominant) - 1

    top.certainty      = min(0.99, top.certainty + 0.07 * n_extra)
    top.size_pct       = min(top.size_pct * (1.0 + 0.25 * n_extra), 0.05)
    top.converged_with = [s.strategy for s in dominant if s is not top]
    top.reason         = f"[🎯 CONVERGED: {'+'.join(s.strategy for s in dominant)}] " + top.reason

    log.info(
        f"Convergence: {top.converged_with} → {top.outcome} "
        f"certainty={top.certainty:.2%} size={top.size_pct:.2%}"
    )

    return arb + [top]


# ── Main evaluate entrypoint ──────────────────────────────────────────────────

def evaluate(market: dict, strategies: list | None = None, learned: dict | None = None) -> list[TradeSignal]:
    """Run the given strategies on one market. Returns signals sorted by certainty."""
    if strategies is None:
        strategies = ["SNIPE", "CORRELATE", "ARB", "NEWS"]
    learned = learned or {}

    raw_cert    = learned.get("snipe_min_certainty", SNIPE_MIN_CERTAINTY)
    min_cert    = max(SNIPE_MIN_CERTAINTY, min(raw_cert, 0.75))
    raw_corr    = learned.get("correlation_threshold", CORRELATION_THRESHOLD)
    corr_thresh = max(CORRELATION_THRESHOLD, min(raw_corr, 0.20))
    news_thresh = learned.get("news_sentiment_threshold", 0.35)

    dispatch = {
        "SNIPE":     lambda m: snipe_signal(m, min_certainty=min_cert),
        "CORRELATE": lambda m: correlate_signal(m, threshold=corr_thresh),
        "ARB":       arb_signal,
        "NEWS":      lambda m: news_signal(m, sentiment_threshold=news_thresh),
    }

    signals = []
    for name in strategies:
        fn = dispatch.get(name)
        if fn:
            try:
                sig = fn(market)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.error(f"Strategy {name} error on {market.get('market_id')}: {e}", exc_info=True)

    if not signals:
        return []

    signals = _apply_convergence(signals)
    return sorted(signals, key=lambda s: s.certainty, reverse=True)
