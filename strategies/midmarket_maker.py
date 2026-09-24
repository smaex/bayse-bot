"""Experimental two-sided CLOB maker strategy.

Two resting bids are not atomic. One side can fill while the other is cancelled
or moves away, leaving directional and adverse-selection risk. This strategy is
blocked by default until fill and orphan-reconciliation evidence supports it.
"""

import time
import logging
from typing import Optional
import feeds
import feeds_direct
from strategies.base import BaseStrategy, TradeSignal
from strategies.utils import realized_vol_hourly, gbm_win_probability

log = logging.getLogger("strat.midmarket_maker")

TARGET_COMBINED_COST = 0.940  # Locks in 6.38% gross profit
MIN_SECS_TO_CLOSE = 180       # Do not place resting orders in final 3 minutes


class MidmarketMakerStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("MIDMARKET_MAKER")

    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float = None) -> Optional[TradeSignal]:
        if str(market.get("engine") or "").upper() != "CLOB":
            return None
        secs = market.get("secs_to_close", 0)
        if secs < MIN_SECS_TO_CLOSE:
            return None

        asset = market.get("asset", "")
        timeframe = market.get("timeframe", "")
        market_id = market.get("market_id", "")
        threshold = market.get("threshold", 0.0)

        yes_p = float(market.get("yes_price") or 0.5)
        no_p  = float(market.get("no_price")  or 0.5)

        # ── 1. Liquidity Dislocation Gate ──────────────────────────────────────
        # Only activate when the orderbook is dislocated or wide
        # (e.g. YES ask + NO ask > 1.15, or either side has collapsed bids < 0.20)
        from strategies.liquidity_regime import classify_regime
        # Check if market pricing or book indicates dislocated/empty mid
        is_dislocated = (yes_p + no_p > 1.15) or (min(yes_p, no_p) < 0.20 and max(yes_p, no_p) > 0.80)

        # Also inspect top of book if available in market dict
        ob_yes = market.get("ob_yes")
        ob_no  = market.get("ob_no")
        if ob_yes and ob_no:
            regime, _ = classify_regime(ob_yes, ob_no)
            if regime == "TIGHT_LIQUID":
                # Do not run midmarket maker in tight books; SNIPE/ARB handle tight books
                return None
            if regime == "DISLOCATED_WIDE":
                is_dislocated = True

        if not is_dislocated:
            return None

        # ── 2. Fair Value Skew Calibration ─────────────────────────────────────
        # Compute fair probability to center our two bids around true expectation
        spot, t = feeds_direct.get_direct_price(asset)
        if not spot or (time.time() - t) > 10:
            spot = spot_price or feeds.spot.get(asset, 0.0)

        fv_yes = 0.50
        if spot and threshold and threshold > 0:
            vol = realized_vol_hourly(asset, state) or 0.015
            fv_yes = gbm_win_probability(spot, threshold, max(secs, 30.0), vol, hourly_drift=0.0)

        fv_no = 1.0 - fv_yes

        # Skew bids around fair value while keeping combined cost <= TARGET_COMBINED_COST
        # Bounded between 0.40 and 0.54 per leg
        bid_yes = round(min(0.54, max(0.40, fv_yes * TARGET_COMBINED_COST)), 3)
        bid_no  = round(min(0.54, max(0.40, TARGET_COMBINED_COST - bid_yes)), 3)

        # Re-verify pair sum
        if (bid_yes + bid_no) > 0.945:
            bid_no = round(0.945 - bid_yes, 3)

        # Ensure we are not paying higher than current market ask
        if bid_yes >= yes_p or bid_no >= no_p:
            return None

        locked_edge = round(1.0 - (bid_yes + bid_no), 3)
        log.info(
            f"MIDMARKET_MAKER SIGNAL {asset} {timeframe} | YES bid={bid_yes:.3f} + "
            f"NO bid={bid_no:.3f} (sum={bid_yes+bid_no:.3f}) | Locked Spread = +{locked_edge:.1%}"
        )

        sig = TradeSignal(
            strategy="MIDMARKET_MAKER",
            asset=asset,
            timeframe=timeframe,
            outcome="DUAL_LIMIT",
            outcome_id=market.get("yes_id", ""),
            market_id=market_id,
            event_id=market.get("event_id", ""),
            market_price=round((bid_yes + bid_no) / 2.0, 3),
            certainty=0.95,
            win_prob=1.0,
            edge_at_entry=locked_edge,
            size_pct=0.04,  # Small, conservative 4% allocation
            reason=f"MIDMARKET: YES@{bid_yes:.3f} + NO@{bid_no:.3f} (locked_edge=+{locked_edge:.1%})",
            title=market.get("title", ""),
            mode_floor=0.0,
        )
        sig.converged_with = [bid_yes, bid_no, market.get("yes_id"), market.get("no_id")]
        return sig
