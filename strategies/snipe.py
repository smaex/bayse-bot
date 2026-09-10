"""Conservative near-close directional strategy.

SNIPE compares a zero-drift diffusion estimate with Bayse's market price,
then shrinks the model toward market consensus before risking capital. The
shrinkage is deliberate: the production sample showed that raw directional
confidence was not calibrated well enough to treat as truth.
"""
import logging
import math
import time

import config
import feeds
from strategies.base import BaseStrategy, TradeSignal, global_state
from strategies.manager import kelly_size, max_ev_price
from strategies.utils import (
    gbm_win_probability,
    probability_to_certainty,
    realized_vol_hourly,
)

log = logging.getLogger("strat.snipe")


def blend_with_market(
    model_probability: float, market_probability: float,
    model_weight: float = config.SNIPE_MODEL_WEIGHT,
) -> float:
    """Geometrically blend odds so an uncalibrated model cannot dominate.

    Combining log-odds is stable near 0/1 and makes the configured weight
    explicit. Bayse consensus receives most of the weight until fresh SNIPE
    fills demonstrate reliable out-of-sample calibration.
    """
    eps = 1e-6
    q = min(1.0 - eps, max(eps, float(model_probability)))
    p = min(1.0 - eps, max(eps, float(market_probability)))
    weight = min(1.0, max(0.0, float(model_weight)))
    log_odds = weight * math.log(q / (1.0 - q))
    log_odds += (1.0 - weight) * math.log(p / (1.0 - p))
    return 1.0 / (1.0 + math.exp(-log_odds))


class SnipeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("SNIPE")

    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float | None = None) -> TradeSignal | None:
        tf      = market["timeframe"]
        secs    = market.get("secs_to_close", 0)
        asset   = market["asset"]
        mkt_id  = market["market_id"]
        learned = learned or {}
        mode    = learned.get("mode", "balanced")

        # ── Evidence-backed scope restriction ──────────────────────────────
        if asset.upper() not in config.SNIPE_ALLOWED_ASSETS:
            return None
        if tf.upper() not in config.SNIPE_ALLOWED_TIMEFRAMES:
            return None

        # ── Entry window check ────────────────────────────────────────────
        window = config.SNIPE_ENTRY_WINDOWS.get(tf)
        if window is None or secs > window:
            return None
        # Never open inside the final minute. Settlement/oracle timing and
        # order round-trip uncertainty dominate any apparent last-second edge.
        if secs < config.SNIPE_MIN_SECS_TO_CLOSE:
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

        # ── Conservative probability model ────────────────────────────────
        # Use zero drift. An instantaneous Kalman velocity is too unstable to
        # extrapolate into a settlement probability and was a source of false
        # confidence. Inflate observed volatility instead, with an additional
        # near-close cushion for oracle and microstructure uncertainty.
        rv = realized_vol_hourly(asset, state)
        rv *= config.SNIPE_VOL_SAFETY_MULTIPLIER
        if secs < 300:
            rv *= 1.0 + 0.5 * ((300.0 - secs) / 240.0)

        raw_w_yes = gbm_win_probability(
            spot=live_spot,
            threshold=threshold,
            secs=secs,
            hourly_vol=rv,
            hourly_drift=0.0,
            horizon_cap=0.0,
        )
        raw_w_no = 1.0 - raw_w_yes

        yes_price = market.get("yes_price", 0.50)
        no_price  = market.get("no_price", 0.50)

        raw_edge_yes = raw_w_yes - yes_price
        raw_edge_no = raw_w_no - no_price
        distance_pct = (live_spot - threshold) / threshold

        # Direction must agree with spot's side of the settlement threshold;
        # never buy the apparent underdog against the current oracle position.
        if (
            distance_pct > 0
            and raw_w_yes >= 0.55
            and raw_edge_yes >= config.SNIPE_MIN_RAW_MODEL_EDGE
        ):
            direction = "YES"
            raw_probability = raw_w_yes
            market_price = yes_price
        elif (
            distance_pct < 0
            and raw_w_no >= 0.55
            and raw_edge_no >= config.SNIPE_MIN_RAW_MODEL_EDGE
        ):
            direction = "NO"
            raw_probability = raw_w_no
            market_price = no_price
        else:
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — no raw edge "
                f"(raw_yes={raw_w_yes:.1%} vs {yes_price:.3f}, "
                f"raw_no={raw_w_no:.1%} vs {no_price:.3f}, "
                f"dist={distance_pct:+.3%})"
            )
            return None

        if abs(distance_pct) < config.SNIPE_MIN_DISTANCE_PCT:
            return None
        if not (
            config.SNIPE_MIN_ENTRY_PRICE
            <= market_price
            <= config.SNIPE_MAX_MARKET_PRICE
        ):
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — entry outside "
                f"[{config.SNIPE_MIN_ENTRY_PRICE:.2f}, "
                f"{config.SNIPE_MAX_MARKET_PRICE:.2f}] ({market_price:.3f})"
            )
            return None

        # Shrink the independent model toward Bayse consensus. This prevents a
        # noisy diffusion estimate from manufacturing a large executable edge.
        w_est = blend_with_market(raw_probability, market_price)
        degraded_oracle_penalty = max(
            0.0, float(learned.get("oracle_penalty", 0.0) or 0.0)
        )
        required_edge = config.SNIPE_MIN_BLENDED_EDGE + degraded_oracle_penalty
        blended_edge = w_est - market_price
        if blended_edge < required_edge:
            log.info(
                f"SNIPE {asset} {tf} mkt={mkt_id[:8]} — shrunk edge "
                f"{blended_edge:.1%} < {required_edge:.1%}"
            )
            return None

        composite = probability_to_certainty(w_est)
        learned_min = learned.get(
            "snipe_min_certainty", config.SNIPE_MIN_CERTAINTY
        )
        effective_floor = max(config.SNIPE_MIN_CERTAINTY, float(learned_min))
        if composite < effective_floor:
            return None

        # The strategy gate uses the same fee-adjusted economics as sizing.
        fee_rate = float(market.get("fee_rate", 0.02) or 0.0)
        margin = {
            "safe": 0.05,
            "balanced": 0.03,
            "aggressive": 0.03,
            "full_send": 0.03,
            "custom": 0.03,
        }.get(mode, 0.03)
        ev_ceil = min(
            config.SNIPE_MAX_MARKET_PRICE,
            max_ev_price(w_est, market_price, fee_rate, min_margin=margin),
        )
        if market_price >= ev_ceil:
            return None

        # ── Size ──────────────────────────────────────────────────────────
        size = kelly_size(w_est, market_price, fee_rate,
                          asset=asset, state=state, learned=learned,
                          strategy_name="SNIPE")

        log.info(
            f"SNIPE ✅ {asset} {tf} | dist={distance_pct:+.3%} "
            f"secs={secs:.0f} raw={raw_probability:.1%} blended={w_est:.1%} "
            f"edge={blended_edge:.1%} price={market_price:.3f}"
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
                f"dist={distance_pct:+.3%} raw={raw_probability:.1%} "
                f"blended={w_est:.1%} edge={blended_edge:.1%} "
                f"secs={secs:.0f}"
            ),
            title=market.get("title", ""),
            momentum_at_entry=0.0,
            regime_at_entry=0.0,
            edge_at_entry=w_est - market_price,
            realized_vol_at_entry=rv,
        )
