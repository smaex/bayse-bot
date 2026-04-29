"""
Strategy engine — four independent signal generators.

Quantitative framework (5-model composite for SNIPE):

  1. Diffusion model (Brownian motion) — primary signal
  ──────────────────────────────────────────────────────
  P(win) = Φ( |d| / (σ_h × √T_h) )   ← same math as Black-Scholes d2
  σ_h is computed from LIVE realized volatility, not a fixed constant.

  2. Realized volatility (dynamic σ)
  ────────────────────────────────────
  Instead of a fixed hourly-vol config, the bot measures actual price
  movement from the last 10 minutes of ticks and blends that with the
  long-run config value. A calmer-than-usual market → lower σ → higher
  win probability for the same distance.

  3. Momentum confirmation
  ─────────────────────────
  Compares the early vs late thirds of the last 90-second price window.
  +1 = price strongly moving away from threshold (confirms our bet).
  −1 = price moving toward threshold (undermines our bet).
  Adds ±0.12 to composite certainty.

  4. Regime detection (efficiency ratio)
  ───────────────────────────────────────
  Net displacement ÷ total path length over the last 5 minutes.
  Clean trend → ratio near 1 → regime_factor up to 1.25× certainty.
  Random chop → ratio near 0 → regime_factor as low as 0.75×.

  5. Market mispricing / edge score
  ───────────────────────────────────
  Edge = our_model_win_prob − market_price.
  Positive edge (market underpricing our side) → up to +0.12 bonus.
  Negative edge (market already priced our move) → up to −0.08 penalty.

  Composite certainty
  ────────────────────
  composite = (base + mom_bonus + edge_bonus) × regime_factor
  Trade fires only when composite ≥ SNIPE_MIN_CERTAINTY (0.40).
  Hard veto: adverse momentum (< −0.7) on a weak base (< 0.55).

  Dynamic EV ceiling (Kelly-derived)
  ────────────────────────────────────
  Only enter when market_price < win_prob × (1 − fee).  Guarantees
  every accepted trade has positive expected value.

  Quarter-Kelly position sizing
  ──────────────────────────────
  f* = (W×b − (1−W)) / b   capped at 5% of bankroll.
  Position size is then scaled by composite certainty so high-conviction
  signals earn proportionally larger positions.
"""

import logging
import math
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Literal
from config import (
    SNIPE_MIN_CERTAINTY, SNIPE_ENTRY_WINDOWS, ASSET_HOURLY_VOL,
    CORRELATION_THRESHOLD, CORRELATION_WINDOW_SEC, ARB_TRIGGER,
    FX_SESSION_UTC, FX_MIN_DISTANCE, FX_MIN_REGIME,
    FX_ENTRY_WINDOW_1H, FX_TREND_VETO_MULT,
)
import feeds
import news as news_mod

log = logging.getLogger(__name__)

# Per-user logging context — set once per asyncio task in _user_loop.
# Every signal log line is automatically prefixed with [chat_id].
_user_ctx: ContextVar[str] = ContextVar("user_ctx", default="")

def set_user_context(chat_id: str) -> None:
    _user_ctx.set(chat_id)

def _u() -> str:
    uid = _user_ctx.get()
    return f"[{uid}] " if uid else ""

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
    certainty: float      # composite 0–1
    market_price: float   # current AMM price of chosen outcome
    size_pct: float       # fraction of bankroll (quarter-Kelly)
    reason: str
    title: str = ""
    arb_quantity: float = 0.0
    converged_with: list = field(default_factory=list)
    # ML feature snapshot at entry time — stored in DB for future model training
    momentum_at_entry:     float = 0.0
    regime_at_entry:       float = 0.0
    edge_at_entry:         float = 0.0
    realized_vol_at_entry: float = 0.0


# ── BTC tracking for CORRELATE ────────────────────────────────────────────────
_btc_signal_time:      dict[str, float] = {}
_btc_signal_direction: dict[str, str]   = {}
_btc_signal_move:      dict[str, float] = {}


# ── Price history (feeds all 5 quant signals) ─────────────────────────────────
_price_history:        dict[str, deque] = {}   # asset → deque[(timestamp, price)]
_last_history_update:  dict[str, float] = {}   # throttle to 1 sample per 5s

def update_price_history(asset: str, price: float) -> None:
    """Record spot tick for quant signals (throttled to 1 sample per 5 s)."""
    now = time.time()
    if now - _last_history_update.get(asset, 0) < 5:
        return
    _last_history_update[asset] = now
    if asset not in _price_history:
        _price_history[asset] = deque(maxlen=120)  # 10 min at 5s cadence
    _price_history[asset].append((now, price))


# ── Quantitative helpers ──────────────────────────────────────────────────────

def _norm_cdf(z: float) -> float:
    """Standard normal CDF via math.erf (stdlib — no scipy needed)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def win_probability(distance_pct: float, secs_remaining: float, asset: str,
                    sigma_override: float = None) -> float:
    """
    P(our direction wins) via diffusion model.

    P = Φ( |d| / (σ_h × √T_h) )

    sigma_override lets callers pass realized vol instead of config vol.
    """
    sigma_h = sigma_override if sigma_override is not None else ASSET_HOURLY_VOL.get(asset, 0.022)
    t_hours = max(secs_remaining / 3600.0, 1.0 / 3600.0)
    z = abs(distance_pct) / (sigma_h * math.sqrt(t_hours))
    return _norm_cdf(z)


def max_ev_price(win_prob: float, fee_rate: float = 0.04) -> float:
    """Max entry price guaranteeing EV > 0.  price < W × (1 − fee)."""
    return win_prob * (1.0 - fee_rate)


def kelly_size(win_prob: float, market_price: float, fee_rate: float = 0.04,
               fraction: float = 0.25, cap: float = 0.05) -> float:
    """Quarter-Kelly position size, capped at `cap`."""
    b = (1.0 - fee_rate) / market_price - 1.0
    if b <= 0:
        return 0.0
    raw_kelly = (win_prob * b - (1.0 - win_prob)) / b
    return min(max(raw_kelly * fraction, 0.0), cap)


def _certainty_from_prob(win_prob: float) -> float:
    """Map win_prob [0.50–0.999] → certainty [0–1]."""
    return max(0.0, min((win_prob - 0.50) / 0.45, 0.99))


def certainty_to_prob(certainty: float) -> float:
    """Legacy helper for CORRELATE/NEWS: certainty [0–1] → prob [0.50–0.95]."""
    return 0.50 + 0.45 * min(certainty, 1.0)


# ── Quant signal 2: realized volatility ──────────────────────────────────────

def realized_vol_hourly(asset: str) -> float:
    """
    Actual hourly vol computed from recent price ticks.
    Blends with config value — full weight after ~5 minutes of data.
    """
    hist = list(_price_history.get(asset, []))
    config_vol = ASSET_HOURLY_VOL.get(asset, 0.022)
    if len(hist) < 10:
        return config_vol
    prices = [p for _, p in hist]
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if len(log_returns) < 5:
        return config_vol
    mean_r   = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_r) ** 2 for r in log_returns) / len(log_returns)
    # 5-second samples → 720 ticks per hour
    hourly_rv = math.sqrt(variance) * math.sqrt(720)
    # Ramp blend weight from 0 → 1 over first 60 samples (~5 minutes)
    weight = min(len(log_returns) / 60.0, 1.0)
    return config_vol * (1.0 - weight) + hourly_rv * weight


# ── Quant signal 3: momentum ──────────────────────────────────────────────────

def _momentum_score(asset: str, direction: str) -> float:
    """
    +1 = price moving strongly in our favour (away from threshold).
    −1 = price moving strongly against us (toward threshold).
    direction: 'YES' (we want higher price) or 'NO' (we want lower price).
    Measured over last 90 seconds of ticks.
    """
    hist = list(_price_history.get(asset, []))
    n = min(len(hist), 18)   # 18 × 5s = 90s window
    if n < 6:
        return 0.0
    prices = [p for _, p in hist[-n:]]
    third = max(1, n // 3)
    early = sum(prices[:third]) / third
    late  = sum(prices[-third:]) / third
    change = (late - early) / early          # fractional price change
    signed = change if direction == "YES" else -change
    # ±0.1% over the window maps to ±1.0
    return min(max(signed / 0.001, -1.0), 1.0)


# ── Quant signal 4: regime (efficiency ratio) ─────────────────────────────────

def _regime_score(asset: str) -> float:
    """
    0 = pure choppy noise, 1 = clean directional trend.
    Uses efficiency ratio: net displacement / total path length over 5 min.
    Efficiency ≥ 0.50 scores 1.0 (already a decent trend).
    """
    hist = list(_price_history.get(asset, []))
    n = min(len(hist), 60)   # 60 × 5s = 5-minute window
    if n < 10:
        return 0.5            # neutral when insufficient data
    prices = [p for _, p in hist[-n:]]
    net  = abs(prices[-1] - prices[0])
    path = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
    if path < 1e-10:
        return 0.5
    return min(net / path / 0.5, 1.0)


# ── Quant signal 5b: FX distance trend ───────────────────────────────────────

def _fx_distance_trend(asset: str, threshold: float, direction: str) -> float:
    """
    How the price-to-threshold distance has changed over the last 10 minutes.
      Positive = distance growing in our direction (move has conviction).
      Negative = distance shrinking (price converging back — reversal risk).
    Returns 0.0 when insufficient history (treated as neutral).
    """
    hist = list(_price_history.get(asset, []))
    if len(hist) < 12:       # need at least ~60 s of ticks
        return 0.0
    now_price  = hist[-1][1]
    past_price = hist[max(0, len(hist) - 120)][1]   # ~10 min ago (120 × 5 s)
    if direction == "YES":
        return (now_price - past_price) / threshold  # +ve = moved further above threshold
    else:
        return (past_price - now_price) / threshold  # +ve = moved further below threshold


# ── Strategy 1: SNIPE — 5-model composite ────────────────────────────────────

def snipe_signal(market: dict, min_certainty: float = SNIPE_MIN_CERTAINTY) -> Optional[TradeSignal]:
    """
    Enter when the diffusion model AND supporting signals agree.

    Five-model composite certainty:
      base     = _certainty_from_prob(win_prob using realized vol)
      mom      = ±0.12 from 90-second momentum
      edge     = ±0.08–0.12 from model vs market-implied price
      regime   = ×0.75 (choppy) to ×1.25 (trending)
      composite = (base + mom_bonus + edge_bonus) × regime_factor

    Hard veto: composite below threshold, or strong adverse momentum
    on a weak base signal.  EV ceiling still applied after all filters.
    """
    tf    = market["timeframe"]
    secs  = market["secs_to_close"]
    asset = market["asset"]

    entry_window = SNIPE_ENTRY_WINDOWS.get(tf)
    # Gate 1 (FX): tighter entry window — 20 min instead of 30 min for crypto.
    # More time elapsed = move is more confirmed before we commit.
    if asset in FX_SESSION_UTC and tf == "1h":
        entry_window = FX_ENTRY_WINDOW_1H
    if entry_window is None or secs > entry_window or secs < 0:
        return None

    threshold = market.get("threshold")
    live_spot = feeds.spot.get(asset)
    if not threshold or not live_spot:
        if not live_spot:
            log.warning(f"{_u()}SNIPE [{asset} {tf}] no spot price in feeds.spot")
        return None

    distance_pct = (live_spot - threshold) / threshold   # +ve → YES wins

    # ── FX gate cascade (gates 1–3) ───────────────────────────────────────────
    if asset in FX_SESSION_UTC:
        # Gate 1: active session only
        hour_utc = datetime.now(timezone.utc).hour
        session_start, session_end = FX_SESSION_UTC[asset]
        if not (session_start <= hour_utc < session_end):
            log.debug(f"{_u()}SNIPE [{asset} {tf}] outside active session (UTC {hour_utc:02d}h)")
            return None

        # Gate 2: minimum distance — need a genuine move, not noise
        min_dist = FX_MIN_DISTANCE[asset]
        if abs(distance_pct) < min_dist:
            log.info(
                f"{_u()}SNIPE [{asset} {tf}] FX G2 DIST — {distance_pct:+.4%} < "
                f"min {min_dist:.4%} (too close to threshold)"
            )
            return None

        # Gate 3: distance trend — move must be holding or growing, not reversing
        direction_early = "YES" if distance_pct > 0 else "NO"
        trend = _fx_distance_trend(asset, threshold, direction_early)
        veto_level = -min_dist * FX_TREND_VETO_MULT
        if trend < veto_level:
            log.info(
                f"{_u()}SNIPE [{asset} {tf}] FX G3 TREND — converging {trend:+.4%}/10min "
                f"(veto < {veto_level:.4%})"
            )
            return None
        log.debug(
            f"{_u()}SNIPE [{asset} {tf}] FX gates 1-3 passed | "
            f"dist={distance_pct:+.4%} trend={trend:+.4%}/10min session=UTC{hour_utc:02d}h"
        )

    # ── Signal 1: diffusion win probability (realized vol) ────────────────────
    rv    = realized_vol_hourly(asset)
    w_est = win_probability(distance_pct, secs, asset, sigma_override=rv)
    base  = _certainty_from_prob(w_est)

    # ── Direction + market price ───────────────────────────────────────────────
    direction = "YES" if distance_pct > 0 else "NO"
    if direction == "YES":
        outcome, outcome_id, market_price = "YES", market["yes_id"], market["yes_price"]
    else:
        outcome, outcome_id, market_price = "NO",  market["no_id"],  market["no_price"]

    # ── Signal 3: momentum ────────────────────────────────────────────────────
    mom       = _momentum_score(asset, direction)
    mom_bonus = 0.12 * mom                     # ±0.12 range

    # ── Signal 4: regime ──────────────────────────────────────────────────────
    regime        = _regime_score(asset)
    regime_factor = 0.75 + 0.50 * regime       # 0.75 (choppy) → 1.25 (trending)

    # Gate 4 (FX): regime veto — choppy FX markets mean-revert and kill the edge
    if asset in FX_SESSION_UTC and regime < FX_MIN_REGIME:
        log.info(
            f"{_u()}SNIPE [{asset} {tf}] FX G4 REGIME — regime={regime:.2f} < {FX_MIN_REGIME} "
            f"(choppy, likely to revert)"
        )
        return None

    # ── Signal 5: edge vs market-implied probability ───────────────────────────
    raw_edge   = w_est - market_price           # +ve = market underpricing our side
    edge_bonus = min(max(raw_edge * 0.40, -0.08), 0.12)

    # ── Composite certainty ───────────────────────────────────────────────────
    composite = min((base + mom_bonus + edge_bonus) * regime_factor, 0.99)

    log.info(
        f"{_u()}SNIPE [{asset} {tf}] {secs:.0f}s | "
        f"spot={live_spot:,.2f} threshold={threshold:,.2f} ({distance_pct:+.3%}) | "
        f"w={w_est:.1%} rv={rv:.3f} base={base:.2f} mom={mom:+.2f} "
        f"regime={regime:.2f} edge={raw_edge:+.3f} ➜ composite={composite:.2f} "
        f"{'✓' if composite >= min_certainty else '✗ LOW'}"
    )

    # Hard veto: price racing toward threshold on an already-weak signal
    if mom < -0.7 and base < 0.55:
        log.info(
            f"{_u()}SNIPE [{asset} {tf}] VETOED — adverse momentum ({mom:+.2f}) "
            f"with weak base ({base:.2f})"
        )
        return None

    if composite < min_certainty:
        return None

    # ── Dynamic EV gate ───────────────────────────────────────────────────────
    fee_rate  = market.get("fee_rate", 0.04)
    ev_ceiling = max_ev_price(w_est, fee_rate)

    if market_price >= ev_ceiling:
        log.info(
            f"{_u()}SNIPE [{asset} {tf}] REJECTED — market {market_price:.3f} >= "
            f"EV ceiling {ev_ceiling:.3f} (w_est={w_est:.1%})"
        )
        return None

    # ── Quarter-Kelly sizing ──────────────────────────────────────────────────
    size = kelly_size(w_est, market_price, fee_rate)

    log.info(
        f"{_u()}SNIPE [{asset} {tf}] ✅ SIGNAL | "
        f"market={market_price:.3f} < ceiling={ev_ceiling:.3f} | "
        f"composite={composite:.2f} size={size:.2%}"
    )

    return TradeSignal(
        strategy="SNIPE",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=asset,
        timeframe=tf,
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=composite,
        market_price=market_price,
        size_pct=size,
        reason=(
            f"Spot {asset}={live_spot:,.2f} threshold={threshold:,.2f} "
            f"({distance_pct:+.3%}), {secs:.0f}s | "
            f"w={w_est:.1%} rv={rv:.3f} mom={mom:+.2f} "
            f"regime={regime:.2f} edge={raw_edge:+.3f} composite={composite:.2f} [{tf}]"
        ),
        title=market["title"],
        momentum_at_entry=round(mom, 4),
        regime_at_entry=round(regime, 4),
        edge_at_entry=round(raw_edge, 4),
        realized_vol_at_entry=round(rv, 6),
    )


# ── Strategy 2: CORRELATE — BTC → ETH/SOL lead-lag ───────────────────────────

def record_btc_move(market: dict, yes_price_new: float):
    """Record any BTC market move ≥1%. Per-user threshold applied later."""
    if market["asset"] != "BTC":
        return
    tf  = market["timeframe"]
    old = feeds.prev_yes.get(market["market_id"], yes_price_new)
    move = yes_price_new - old
    if abs(move) >= 0.01:
        _btc_signal_time[tf]      = time.time()
        _btc_signal_direction[tf] = "UP" if move > 0 else "DOWN"
        _btc_signal_move[tf]      = abs(move)
        log.info(f"{_u()}BTC {tf} market moved {move:+.3f} — stored for CORRELATE")


_CRYPTO_ASSETS = {"BTC", "ETH", "SOL"}

def correlate_signal(market: dict, threshold: float = CORRELATION_THRESHOLD) -> Optional[TradeSignal]:
    """
    BTC market reprices → trade same direction on ETH/SOL before it catches up.
    FX and commodity markets are excluded — they don't correlate with BTC.
    Momentum of the target asset boosts/reduces certainty by ±20%.
    """
    if market["asset"] not in _CRYPTO_ASSETS or market["asset"] == "BTC":
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

    # Momentum of target asset confirms or weakens the correlation signal
    mom       = _momentum_score(market["asset"], direction == "UP" and "YES" or "NO")
    certainty = min(0.60 * freshness * (1.0 + 0.20 * mom), 0.99)

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
            f"age={age:.0f}s freshness={freshness:.2f} mom={mom:+.2f} "
            f"market={market_price:.3f} ceiling={ev_ceiling:.3f}"
        ),
        title=market["title"],
        momentum_at_entry=round(mom, 4),
        regime_at_entry=round(_regime_score(market["asset"]), 4),
        edge_at_entry=round(w_est - market_price, 4),
        realized_vol_at_entry=round(realized_vol_hourly(market["asset"]), 6),
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
    Momentum of the target asset provides a ±15% certainty adjustment.
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
        direction = "YES"
    else:
        outcome, outcome_id, market_price = "NO",  market["no_id"],  no_p
        direction = "NO"

    # Momentum adjustment: confirming price move boosts confidence
    mom          = _momentum_score(asset, direction)
    strength_adj = min(strength * (1.0 + 0.15 * mom), 0.99)

    w_est      = certainty_to_prob(strength_adj)
    fee_rate   = market.get("fee_rate", 0.04)
    ev_ceiling = max_ev_price(w_est, fee_rate)

    if market_price >= ev_ceiling:
        return None

    size = kelly_size(w_est, market_price, fee_rate, fraction=0.20)

    return TradeSignal(
        strategy="NEWS",
        event_id=market["event_id"],
        market_id=market["market_id"],
        asset=asset,
        timeframe=market["timeframe"],
        outcome=outcome,
        outcome_id=outcome_id,
        certainty=strength_adj,
        market_price=market_price,
        size_pct=size,
        reason=(
            f"News [{sig.direction}] src={sig.source} score={strength:.2f} "
            f"mom={mom:+.2f} adj={strength_adj:.2f} "
            f"market={market_price:.3f} ceiling={ev_ceiling:.3f}: {sig.headline[:60]}"
        ),
        title=market["title"],
        momentum_at_entry=round(mom, 4),
        regime_at_entry=round(_regime_score(asset), 4),
        edge_at_entry=round(w_est - market_price, 4),
        realized_vol_at_entry=round(realized_vol_hourly(asset), 6),
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
        f"{_u()}Convergence: {top.converged_with} → {top.outcome} "
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
