"""
SNIPE — near-close certainty trading.

Fires when spot price has clearly crossed the market threshold within the
entry window and Brownian motion probability gives meaningful edge.

All rejection reasons are now logged at INFO level so we can see exactly
why a market doesn't qualify — previously all returns were silent and
invisible in the log stream.
"""
import logging
import math
import time
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

# SNIPE is hard-restricted to fast-cycle crypto markets only. 1h/1d candles
# don't fit a "trade every 5-15 minutes" cadence, and FX assets only exist
# on the 1h timeframe in config.SERIES — so this restriction also makes
# SNIPE crypto-only (BTC/ETH/SOL) by construction. FRONTRUN already had
# this same restriction (config.FRONTRUN_ALLOWED_TFS); CORRELATE now does too.
ALLOWED_TFS = {"5min", "15min"}

# Suppress per-market rejection spam outside the entry window.
# Only log rejections when secs_to_close is within 2× the entry window.
_LOG_WITHIN_FACTOR = 2.0


def _time_adjusted_min_dist(base_dist: float, secs: float) -> float:
    """
    Scale minimum distance with time remaining (Brownian motion scaling).
    At 300s: full base_dist required. At 60s: ~45%. At 0s: 0.
    """
    entry_window = 300.0
    if secs <= 0:
        return 0.0
    return base_dist * math.sqrt(min(secs, entry_window) / entry_window)


class SnipeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("SNIPE")

    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float = None) -> Optional[TradeSignal]:
        tf      = market["timeframe"]
        secs    = market.get("secs_to_close", 0)
        asset   = market["asset"]
        mkt_id  = market["market_id"]
        learned = learned or {}
        mode    = learned.get("mode", "balanced")

        # ── Hard scope restriction ─────────────────────────────────────────
        # SNIPE only operates on 5min/15min markets. This is a deliberate
        # simplification: fast-cycle crypto candles are what give SNIPE its
        # edge (Brownian-motion math gets noisier over longer windows), and
        # it removes FX entirely (FX only has 1h granularity in config).
        if tf not in ALLOWED_TFS:
            return None

        # ── Entry window check ────────────────────────────────────────────
        window = config.SNIPE_ENTRY_WINDOWS.get(tf)

        near_window = window is not None and secs <= window * _LOG_WITHIN_FACTOR

        if window is None:
            # No entry window configured for this timeframe — skip silently
            return None
        if secs < 0:
            return None
        if secs > window:
            # Too early — only log if we are close to window opening
            if near_window:
                log.debug(f"SNIPE {asset} {tf} — not yet in window "
                          f"({secs:.0f}s left, window opens at {window}s)")
            return None
        if secs < 30 and mode != "full_send":
            log.info(f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — too close to close "
                     f"({secs:.0f}s left, need 30s)")
            return None

        # ── Price data ────────────────────────────────────────────────────
        threshold = market.get("threshold")
        live_spot = spot_price if spot_price is not None else feeds.spot.get(asset)
        if not live_spot:
            import feeds_direct as _fd
            oracle_p, oracle_t = _fd.get_direct_price(asset)
            if oracle_p and (time.time() - oracle_t) < 30:
                live_spot = oracle_p
                log.debug(f"SNIPE {asset} — using oracle fallback price {live_spot}")

        if not threshold:
            log.info(f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — NO THRESHOLD in market data "
                     f"(scanner did not extract it)")
            return None
        if not live_spot:
            log.info(f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — no live spot price available")
            return None

        # ── Core math ─────────────────────────────────────────────────────
        distance_pct = (live_spot - threshold) / threshold
        direction    = "YES" if distance_pct > 0 else "NO"

        rv = realized_vol_hourly(asset, state)
        if secs < 300:
            rv *= (1.0 + 0.5 * ((300 - secs) / 210.0))

        raw_prob = win_probability(distance_pct, secs, asset, sigma_override=rv)
        w_est    = raw_prob if direction == "YES" else 1.0 - raw_prob
        base     = probability_to_certainty(w_est)

        mom      = momentum_score(asset, direction, state)
        regime   = regime_score(asset, state)
        velocity = velocity_score(asset, threshold, direction, state)

        # ── Vetoes ────────────────────────────────────────────────────────
        flips = global_state.market_flips.get(mkt_id, 0)

        if secs < 210 and flips >= 5:
            log.info(f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — chaos veto "
                     f"({flips} flips in last {secs:.0f}s)")
            return None

        if mode != "full_send":
            base_dist = (
                config.CRYPTO_MIN_DISTANCE.get(asset)
                or config.FX_MIN_DISTANCE.get(asset, 0.0010)
            )
            dyn_dist = _time_adjusted_min_dist(base_dist, secs)
            base_vol  = config.ASSET_HOURLY_VOL.get(asset, 0.022)
            vol_ratio = rv / base_vol if base_vol > 0 else 1.0
            dyn_dist  = max(dyn_dist * 0.5, min(dyn_dist * vol_ratio, dyn_dist * 1.5))

            if abs(distance_pct) < dyn_dist:
                log.info(
                    f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — distance too small "
                    f"({distance_pct:+.4%} vs min {dyn_dist:.4%} | "
                    f"spot={live_spot:.2f} threshold={threshold:.2f} secs={secs:.0f})"
                )
                return None

            if abs(distance_pct) < base_dist and mom <= 0:
                log.info(
                    f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — momentum veto "
                    f"(dist={distance_pct:+.4%} < base {base_dist:.4%}, mom={mom:+.2f})"
                )
                return None

            if velocity < -config.SNIPE_VELOCITY_VETO:
                log.info(
                    f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — velocity veto "
                    f"(velocity={velocity:.2f} < -{config.SNIPE_VELOCITY_VETO})"
                )
                return None

        # ── Composite certainty ───────────────────────────────────────────
        market_price = market["yes_price"] if direction == "YES" else market["no_price"]
        raw_edge     = w_est - market_price
        edge_bonus   = min(max(raw_edge * 0.40, -0.08), 0.12)
        mom_bonus    = 0.12 * mom
        regime_fac   = 0.75 + 0.50 * regime

        composite = min((base + mom_bonus + edge_bonus) * regime_fac, 0.99)

        if secs < 210 and flips <= 1:
            composite = min(composite + 0.10, 0.99)

        if composite < 0.30:
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — composite too low "
                f"({composite:.2f} < 0.30 | w={w_est:.1%} dist={distance_pct:+.3%})"
            )
            return None

        # ── EV gate ───────────────────────────────────────────────────────
        fee_rate = market.get("fee_rate", 0.02)
        margin   = {"safe": 0.15, "balanced": 0.06,
                    "aggressive": 0.04, "full_send": 0.02}.get(mode, 0.06)
        ev_ceil  = max_ev_price(w_est, market_price, fee_rate, min_margin=margin)

        if market_price >= ev_ceil:
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — EV gate: "
                f"market_price={market_price:.3f} >= ev_ceil={ev_ceil:.3f} "
                f"(w={w_est:.1%} margin={margin:.0%})"
            )
            return None

        if market_price > config.SNIPE_MAX_MARKET_PRICE:
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — price ceiling "
                f"({market_price:.3f} > {config.SNIPE_MAX_MARKET_PRICE})"
            )
            return None

        # ── Size ──────────────────────────────────────────────────────────
        size = kelly_size(w_est, market_price, fee_rate,
                          asset=asset, state=state, learned=learned,
                          strategy_name="SNIPE")

        log.info(
            f"SNIPE ✅ {asset} {tf} | dist={distance_pct:+.3%} "
            f"secs={secs:.0f} w={w_est:.1%} composite={composite:.2f} "
            f"market_price={market_price:.3f}"
        )

        return TradeSignal(
            strategy="SNIPE",
            event_id=market["event_id"],
            market_id=mkt_id,
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
