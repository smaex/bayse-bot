"""
SNIPE — core strategy.

Key fix: time-dependent minimum distance.
The old flat 0.10% distance filter was blocking ~50% of valid entry windows.
BTC cannot move $12 in 30 seconds. BTC cannot move $25 in 60 seconds.
The minimum distance shrinks as time runs out — mathematically correct
Brownian motion behavior. EV check still gates every trade.
"""
import logging
import math
from typing import Optional

import config
import feeds
from strategies.base import BaseStrategy, TradeSignal, global_state
from strategies.utils import (
    realized_vol_hourly, momentum_score, velocity_score,
    regime_score, win_probability, probability_to_certainty,
)
from strategies.manager import kelly_size, max_ev_price

log = logging.getLogger("strat.snipe")


def _time_adjusted_min_dist(base_dist: float, secs: float) -> float:
    """
    Scale minimum distance with time remaining.

    Rationale: at 300s left, BTC could move 0.52% (σ√t at 1.8% hourly).
    At 60s left, it can only move ~0.23%. At 30s, ~0.16%.
    So the minimum "safe" distance shrinks as close approaches.

    Formula: min_dist × sqrt(secs / entry_window)
    This matches the Brownian motion scaling — same math as win_probability.

    Examples at base_dist=0.10% for BTC:
      300s → 0.10%  (full requirement)
      120s → 0.063% (still strong edge)
       60s → 0.045% (BTC barely moves this in 60s)
       30s → 0.032% (near-certain)
    """
    entry_window = 300.0  # reference window in seconds
    if secs <= 0:
        return 0.0
    scale = math.sqrt(min(secs, entry_window) / entry_window)
    return base_dist * scale


class SnipeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("SNIPE")

    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float = None) -> Optional[TradeSignal]:
        tf    = market["timeframe"]
        secs  = market.get("secs_to_close", 0)
        asset = market["asset"]
        learned = learned or {}
        mode    = learned.get("mode", "balanced")

        # ── Entry window ──────────────────────────────────────────────────
        window = config.SNIPE_ENTRY_WINDOWS.get(tf)
        if asset in config.FX_SESSION_UTC and tf == "1h":
            window = config.FX_ENTRY_WINDOW_1H
        if window is None or secs > window or secs < 0:
            return None
        if secs < 30 and mode != "full_send":
            return None

        # ── FX session guard ──────────────────────────────────────────────
        if asset in config.FX_SESSION_UTC:
            from datetime import datetime, timezone
            hour = datetime.now(timezone.utc).hour
            lo, hi = config.FX_SESSION_UTC[asset]
            if not (lo <= hour < hi):
                return None

        threshold = market.get("threshold")
        live_spot = spot_price if spot_price is not None else feeds.spot.get(asset)
        if not live_spot and asset not in config.FX_SESSION_UTC:
            # Fallback to Binance oracle during Bayse relay startup / reconnect gaps.
            # Only crypto — FX oracle (TwelveData) has different timing guarantees.
            import time as _t
            import feeds_direct as _fd
            oracle_p, oracle_t = _fd.get_direct_price(asset)
            if oracle_p and (_t.time() - oracle_t) < 30:
                live_spot = oracle_p
        if not threshold or not live_spot:
            return None

        # ── Core math ─────────────────────────────────────────────────────
        distance_pct = (live_spot - threshold) / threshold
        direction    = "YES" if distance_pct > 0 else "NO"

        rv = realized_vol_hourly(asset, state)
        # Vol expands near close — conservative adjustment
        if secs < 300:
            rv *= (1.0 + 0.5 * ((300 - secs) / 210.0))

        raw_prob = win_probability(distance_pct, secs, asset, sigma_override=rv)
        w_est    = raw_prob if direction == "YES" else 1.0 - raw_prob
        base     = probability_to_certainty(w_est)

        mom      = momentum_score(asset, direction, state)
        regime   = regime_score(asset, state)
        velocity = velocity_score(asset, threshold, direction, state)

        # ── Vetoes ────────────────────────────────────────────────────────
        market_id = market["market_id"]
        flips     = global_state.market_flips.get(market_id, 0)

        # Chaos veto: many favourite flips near close
        if secs < 210 and flips >= 5:
            return None

        if mode != "full_send":
            # Time-dependent minimum distance — the key fix for trade frequency.
            # At 300s: full distance required. At 60s: 45% of distance required.
            # Mathematically correct — BTC physically cannot cross back over in
            # less time than the distance implies given its actual volatility.
            base_dist = (
                config.CRYPTO_MIN_DISTANCE.get(asset)
                or config.FX_MIN_DISTANCE.get(asset, 0.0010)
            )
            dyn_dist = _time_adjusted_min_dist(base_dist, secs)

            # Apply vol scaling on top of time adjustment
            base_vol  = config.ASSET_HOURLY_VOL.get(asset, 0.022)
            vol_ratio = rv / base_vol if base_vol > 0 else 1.0
            dyn_dist  = max(dyn_dist * 0.5, min(dyn_dist * vol_ratio, dyn_dist * 1.5))

            if abs(distance_pct) < dyn_dist:
                return None

            # Momentum veto: inside base buffer with adverse momentum
            if abs(distance_pct) < base_dist and mom <= 0:
                return None

            # Velocity veto: price crashing toward threshold
            if velocity < -config.SNIPE_VELOCITY_VETO:
                return None

        if asset in config.FX_SESSION_UTC and regime < config.FX_MIN_REGIME:
            return None

        # ── Composite certainty ───────────────────────────────────────────
        market_price = market["yes_price"] if direction == "YES" else market["no_price"]
        raw_edge     = w_est - market_price
        edge_bonus   = min(max(raw_edge * 0.40, -0.08), 0.12)
        mom_bonus    = 0.12 * mom
        regime_fac   = 0.75 + 0.50 * regime

        composite = min((base + mom_bonus + edge_bonus) * regime_fac, 0.99)

        # Conviction boost: stable market (few flips) late in candle
        if secs < 210 and flips <= 1:
            composite = min(composite + 0.10, 0.99)

        if composite < 0.30:
            return None

        # ── EV gate ───────────────────────────────────────────────────────
        fee_rate = market.get("fee_rate", 0.02)
        margin   = {"safe": 0.15, "balanced": 0.06,
                    "aggressive": 0.04, "full_send": 0.02}.get(mode, 0.06)
        ev_ceil  = max_ev_price(w_est, market_price, fee_rate, min_margin=margin)
        if market_price >= ev_ceil:
            return None
        if market_price > config.SNIPE_MAX_MARKET_PRICE:
            return None

        # ── Sizing ────────────────────────────────────────────────────────
        size = kelly_size(w_est, market_price, fee_rate,
                          asset=asset, state=state, learned=learned,
                          strategy_name="SNIPE")

        log.debug(
            f"SNIPE {asset} {tf} | dist={distance_pct:+.3%} dyn_min={dyn_dist:.3%} "
            f"secs={secs:.0f} w={w_est:.1%} composite={composite:.2f}"
        )

        return TradeSignal(
            strategy="SNIPE",
            event_id=market["event_id"],
            market_id=market_id,
            asset=asset,
            timeframe=tf,
            outcome=direction,
            outcome_id=market["yes_id"] if direction == "YES" else market["no_id"],
            certainty=composite,
            win_prob=w_est,
            market_price=market_price,
            size_pct=size,
            reason=(
                f"composite={composite:.2f} w={w_est:.1%} "
                f"dist={distance_pct:+.3%} secs={secs:.0f} "
                f"mom={mom:+.2f} edge={raw_edge:+.3f}"
            ),
            title=market.get("title", ""),
            momentum_at_entry=mom,
            regime_at_entry=regime,
            edge_at_entry=raw_edge,
            realized_vol_at_entry=rv,
        )
