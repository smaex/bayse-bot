"""
SNIPE — core strategy.

In the final minutes of a candle, if the live spot price is clearly above/below
the opening threshold, we buy the winning side.  Certainty is driven by a
Brownian diffusion model (how likely the price stays on the right side until close).
"""
import logging
from typing import Optional
import config
import feeds
import feeds_direct
from strategies.base import BaseStrategy, TradeSignal, global_state
from strategies.utils import (
    realized_vol_hourly, momentum_score, velocity_score,
    regime_score, win_probability, probability_to_certainty,
)
from strategies.manager import kelly_size, max_ev_price

log = logging.getLogger("strat.snipe")


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

        # High chaos veto: many favourite flips near close
        if secs < 210 and flips >= 5:
            return None

        if mode != "full_send":
            min_dist  = (config.CRYPTO_MIN_DISTANCE.get(asset)
                         or config.FX_MIN_DISTANCE.get(asset, 0.0010))
            base_vol  = config.ASSET_HOURLY_VOL.get(asset, 0.022)
            vol_ratio = rv / base_vol if base_vol > 0 else 1.0
            dyn_dist  = max(min_dist * 0.5, min(min_dist * vol_ratio, min_dist * 1.5))
            if abs(distance_pct) < dyn_dist:
                return None
            if abs(distance_pct) < min_dist and mom <= 0:
                return None
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

        # ── EV check ──────────────────────────────────────────────────────
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
            reason=(f"composite={composite:.2f} w={w_est:.1%} "
                    f"mom={mom:+.2f} edge={raw_edge:+.3f}"),
            title=market.get("title", ""),
            momentum_at_entry=mom,
            regime_at_entry=regime,
            edge_at_entry=raw_edge,
            realized_vol_at_entry=rv,
        )
