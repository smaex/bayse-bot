"""
SNIPE — near-close certainty trading.

REBUILT for simplicity. Previous version had SIX separate, partially
redundant adjustment layers stacked on top of the core probability:
  1. A hard distance veto (CRYPTO_MIN_DISTANCE, time-scaled)
  2. A momentum veto (separate condition, separate scale)
  3. A velocity veto (ANOTHER separate condition, different scale)
  4. An additive momentum "bonus" (mom_bonus = 0.12 * mom)
  5. An additive edge "bonus" (edge_bonus, derived from win_prob - price —
     which the EV gate downstream ALSO independently checks)
  6. A regime multiplier (regime_fac) — applied AGAIN externally in
     strategies/__init__.py via regime_controller, double-counting the
     same volatility-regime signal twice.

These interacted in ways that were hard to reason about and occasionally
self-contradicting (one gate effectively vetoing what the probability math
already correctly priced in). Verified by hand: the distance veto was
ALWAYS looser than what the probability gate independently required, so
it never did useful work — just extra surface area for bugs.

NEW design: ONE diffusion model. Momentum is folded into the model as a
proper drift term (the textbook-correct way to add momentum to a boundary-
crossing probability — not a bolted-on bonus). Regime is handled exactly
once, externally. The EV gate is the only "is this price attractive" check.
Certainty IS the drift-adjusted win probability, full stop — no further
multipliers inside this file.
"""
import logging
import time
from typing import Optional

import config
import feeds
from strategies.base import BaseStrategy, TradeSignal, global_state
from strategies.utils import (
    realized_vol_hourly, projected_drift_pct, win_probability,
    probability_to_certainty,
)
from strategies.manager import kelly_size, max_ev_price

log = logging.getLogger("strat.snipe")

# SNIPE is hard-restricted to fast-cycle crypto markets only. 1h/1d candles
# don't fit a "trade every 5-15 minutes" cadence, and FX assets only exist
# on the 1h timeframe in config.SERIES — so this restriction also makes
# SNIPE crypto-only (BTC/ETH/SOL) by construction.
ALLOWED_TFS = {"5min", "15min"}

# Suppress per-market rejection spam outside the entry window.
_LOG_WITHIN_FACTOR = 2.0


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
        if tf not in ALLOWED_TFS:
            return None

        # ── Entry window check ────────────────────────────────────────────
        window = config.SNIPE_ENTRY_WINDOWS.get(tf)
        if window is None:
            return None
        if secs < 0 or secs > window:
            return None
        if secs < 30 and mode != "full_send":
            return None

        # ── Price data ────────────────────────────────────────────────────
        threshold = market.get("threshold")
        live_spot = spot_price if spot_price is not None else feeds.spot.get(asset)
        if not live_spot:
            import feeds_direct as _fd
            oracle_p, oracle_t = _fd.get_direct_price(asset)
            if oracle_p and (time.time() - oracle_t) < 30:
                live_spot = oracle_p

        if not threshold:
            log.info(f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — no threshold in market data")
            return None
        if not live_spot:
            log.info(f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — no live spot price available")
            return None

        # ── Chaos guard ───────────────────────────────────────────────────
        flips = global_state.market_flips.get(mkt_id, 0)
        if secs < 210 and flips >= 5:
            log.info(f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — chaos veto ({flips} flips)")
            return None

        # ── Market data-quality guard ─────────────────────────────────────
        # YES+NO should sum close to 1.0 in any valid, liquid binary market.
        # A market priced at e.g. yes=0.030 no=0.020 (sum=0.05) is broken or
        # has effectively zero real liquidity behind those numbers — trusting
        # it produces a mathematically "huge edge" that isn't real (model
        # says 65% true probability, "market" says 3% — that gap is a data
        # artifact, not an opportunity). This was observed directly in
        # production: 54 evaluation cycles spent on a dead market like this,
        # none of which could ever have filled.
        price_sum = market.get("yes_price", 0) + market.get("no_price", 0)
        if not (0.90 <= price_sum <= 1.05):
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — bad market data "
                f"(yes={market.get('yes_price',0):.3f} no={market.get('no_price',0):.3f} "
                f"sum={price_sum:.3f}, expected ~1.0)"
            )
            return None

        # ── Liquidity-floor guard ──────────────────────────────────────────
        # Confirmed TWICE in production with otherwise-valid price data
        # (sum≈1.0): Bayse's AMM rejects MARKET orders at extreme prices
        # with "Your order could not be filled at the moment, please try
        # again later." Session 1: price=0.020. This session: price=0.050.
        # Both looked like huge mathematical edges and both were genuinely
        # unfillable. Matches ARB's existing 0.08 floor, added for the same
        # observed reason — this isn't a guess, it's the second confirmed
        # occurrence of the identical failure.
        min_side = min(market.get("yes_price", 1), market.get("no_price", 1))
        if min_side < 0.08:
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — extreme price, "
                f"likely unfillable (min_side={min_side:.3f} < 0.08)"
            )
            return None

        # ── Core probability model ───────────────────────────────────────
        # One diffusion model, drift-adjusted. distance_pct is the raw
        # current gap to threshold; drift is the Kalman-projected move over
        # the remaining time, folded directly into the same distance term
        # (proper GBM-with-drift, not a separate post-hoc bonus).
        distance_pct = (live_spot - threshold) / threshold

        rv = realized_vol_hourly(asset, state)
        if secs < 300:
            # Conservative cushion: real-world resolution/oracle risk spikes
            # in the final seconds in a way the pure sqrt(t) model doesn't
            # capture. One simple multiplicative factor, easy to reason about.
            rv *= (1.0 + 0.5 * ((300 - secs) / 210.0))

        t       = secs / 3600.0
        sigma_t = rv * (t ** 0.5) if t > 0 else 0.0

        drift = projected_drift_pct(asset, secs, state)
        if sigma_t > 0:
            # Cap drift at 2 standard deviations of the diffusion term itself.
            # Without this, a single noisy instantaneous Kalman velocity
            # reading (e.g. right after a price spike) could dominate the
            # estimate and produce overconfident certainty that's really
            # just measurement noise, not genuine sustained momentum.
            max_drift = 2.0 * sigma_t
            drift = max(-max_drift, min(drift, max_drift))

        adj_distance = distance_pct + drift
        direction    = "YES" if adj_distance > 0 else "NO"

        raw_prob = win_probability(adj_distance, secs, asset, sigma_override=rv)
        w_est    = raw_prob if direction == "YES" else 1.0 - raw_prob
        composite = probability_to_certainty(w_est)

        if composite < 0.30:
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — composite too low "
                f"({composite:.2f} | dist={distance_pct:+.3%} drift={drift:+.3%} "
                f"secs={secs:.0f} w={w_est:.1%})"
            )
            return None

        # ── EV gate ───────────────────────────────────────────────────────
        # The ONLY "is this price attractive" check — replaces the old
        # edge_bonus, which was double-counting the exact same comparison
        # (win_prob vs market_price) that this gate already makes definitive.
        market_price = market["yes_price"] if direction == "YES" else market["no_price"]
        fee_rate = market.get("fee_rate", 0.02)
        margin   = {
            "safe": 0.15, "balanced": 0.06, "aggressive": 0.04,
            "full_send": 0.02, "custom": 0.05,
        }.get(mode, 0.06)
        ev_ceil = max_ev_price(w_est, market_price, fee_rate, min_margin=margin)

        if market_price >= ev_ceil:
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — EV gate: "
                f"price={market_price:.3f} >= ceil={ev_ceil:.3f} (w={w_est:.1%})"
            )
            return None
        if market_price > config.SNIPE_MAX_MARKET_PRICE:
            log.info(f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — price ceiling "
                     f"({market_price:.3f} > {config.SNIPE_MAX_MARKET_PRICE})")
            return None

        # ── Size ──────────────────────────────────────────────────────────
        size = kelly_size(w_est, market_price, fee_rate,
                          asset=asset, state=state, learned=learned,
                          strategy_name="SNIPE")

        log.info(
            f"SNIPE ✅ {asset} {tf} | dist={distance_pct:+.3%} drift={drift:+.3%} "
            f"secs={secs:.0f} w={w_est:.1%} composite={composite:.2f} price={market_price:.3f}"
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
                f"dist={distance_pct:+.3%} drift={drift:+.3%} "
                f"w={w_est:.1%} composite={composite:.2f} secs={secs:.0f}"
            ),
            title=market.get("title", ""),
            momentum_at_entry=drift,
            regime_at_entry=0.0,
            edge_at_entry=w_est - market_price,
            realized_vol_at_entry=rv,
        )
