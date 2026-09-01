"""
PAIRED_SNIPER — Directional Taker & Matched-Pair Hedging Engine
================================================================
Synthesized from @ohioism's verified $5.52M Polymarket trading architecture.

Core Mathematical Principles:
1. Directional Momentum Taking:
   - Evaluates short-horizon continuous Geometric Brownian Motion (GBM) diffusion.
   - Leverages sub-second Binance oracle feeds vs Polymarket/Bayse CLOB state.
   - Sizing scales dynamically with calculated statistical certainty (probes on cheap OTM,
     fractional Kelly on core momentum, full conviction on near-settlement locks).

2. Matched-Pair Lock-in Hedging:
   - If an open position is held on Outcome 1 at cost basis C1, and market volatility allows
     acquiring Outcome 2 at cost C2 such that C1 + C2 + Fees <= 0.950, it fires a hedge order.
   - Redeems at $1.00 at candle close, locking in a guaranteed risk-free spread (+5.0% Net EV).
"""

import logging
import time
from typing import Optional

import config
import feeds
import feeds_direct
from strategies.base import BaseStrategy, TradeSignal, global_state
from strategies.utils import (
    realized_vol_hourly, gbm_win_probability,
    probability_to_certainty,
)

log = logging.getLogger("strat.paired_sniper")

ALLOWED_TFS = {"5min", "15min"}
ALLOWED_ASSETS = {"BTC", "ETH", "SOL"}


class PairedSniperStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("PAIRED_SNIPER")

    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float = None) -> Optional[TradeSignal]:
        tf = market.get("timeframe", "")
        asset = market.get("asset", "")
        market_id = market.get("market_id", "")
        secs = market.get("secs_to_close", 0)

        # ── 1. Target Market Filters ──────────────────────────────────────────
        if tf not in ALLOWED_TFS or asset not in ALLOWED_ASSETS:
            return None

        # Settlement safety window: don't open brand new directional trades in the final 10s
        if secs < 10:
            return None

        # ── 2. Price Data & Strike ────────────────────────────────────────────
        threshold = market.get("threshold", 0.0)
        if not threshold or threshold <= 0:
            return None

        spot, t = feeds_direct.get_direct_price(asset)
        if not spot or (time.time() - t) > 10:
            spot = spot_price or feeds.spot.get(asset, 0.0)
        if not spot or spot <= 0:
            return None

        dist_pct = (spot - threshold) / threshold

        # ── 3. Check for Matched-Pair Hedging Opportunity ─────────────────────
        yes_price = float(market.get("yes_price") or 0.5)
        no_price  = float(market.get("no_price") or 0.5)
        fee_rate  = float(market.get("feePercentage", 2.0)) / 100.0

        open_pos = None
        if hasattr(state, "open_positions") and market_id in state.open_positions:
            open_pos = state.open_positions[market_id]

        if open_pos:
            existing_outcome = open_pos.get("outcome", "").upper()
            c1 = float(open_pos.get("entry_price", 0.5))

            if existing_outcome == "YES":
                c2 = no_price
                eff_fee = fee_rate * (max(1.0 - c1, 0.5) + max(1.0 - c2, 0.5))
                total_cost = c1 + c2 + eff_fee
                if total_cost <= 0.950 and c2 <= 0.50:
                    locked_edge = 1.0 - total_cost
                    log.info(
                        f"PAIRED_SNIPER MATCHED-PAIR HEDGE {asset} {tf} | YES cost={c1:.3f} + "
                        f"NO ask={c2:.3f} (total={total_cost:.3f}) | Locked Net Edge = +{locked_edge:.1%}"
                    )
                    return TradeSignal(
                        strategy="PAIRED_SNIPER",
                        asset=asset,
                        timeframe=tf,
                        outcome="NO",
                        outcome_id=market.get("no_token_id") or market.get("outcome2Id", ""),
                        market_id=market_id,
                        event_id=market.get("event_id", ""),
                        market_price=c2,
                        certainty=0.99,
                        win_prob=1.0,
                        edge=locked_edge,
                        size_pct=0.05,
                        reason=f"PAIR_HEDGE: C1(YES)={c1:.3f} + C2(NO)={c2:.3f} < 0.95 (edge=+{locked_edge:.1%})",
                        title=market.get("title", ""),
                        mode_floor=0.50,
                    )

            elif existing_outcome == "NO":
                c2 = yes_price
                eff_fee = fee_rate * (max(1.0 - c1, 0.5) + max(1.0 - c2, 0.5))
                total_cost = c1 + c2 + eff_fee
                if total_cost <= 0.950 and c2 <= 0.50:
                    locked_edge = 1.0 - total_cost
                    log.info(
                        f"PAIRED_SNIPER MATCHED-PAIR HEDGE {asset} {tf} | NO cost={c1:.3f} + "
                        f"YES ask={c2:.3f} (total={total_cost:.3f}) | Locked Net Edge = +{locked_edge:.1%}"
                    )
                    return TradeSignal(
                        strategy="PAIRED_SNIPER",
                        asset=asset,
                        timeframe=tf,
                        outcome="YES",
                        outcome_id=market.get("yes_token_id") or market.get("outcome1Id", ""),
                        market_id=market_id,
                        event_id=market.get("event_id", ""),
                        market_price=c2,
                        certainty=0.99,
                        win_prob=1.0,
                        edge=locked_edge,
                        size_pct=0.05,
                        reason=f"PAIR_HEDGE: C1(NO)={c1:.3f} + C2(YES)={c2:.3f} < 0.95 (edge=+{locked_edge:.1%})",
                        title=market.get("title", ""),
                        mode_floor=0.50,
                    )

        # ── 4. Directional Momentum & Latency Taking Mode ─────────────────────
        rv = realized_vol_hourly(asset, state) if state else 0.022
        
        kalman = state.kalman_state.get(asset) if (state and hasattr(state, "kalman_state")) else None
        if kalman:
            k_price, k_velocity = kalman["x"]
            hourly_drift = (k_velocity / k_price) * 3600.0 if k_price > 0 else 0.0
        else:
            hourly_drift = 0.0

        w_yes = gbm_win_probability(
            spot=spot,
            threshold=threshold,
            secs=secs,
            hourly_vol=rv,
            hourly_drift=hourly_drift,
            horizon_cap=120.0,
        )
        w_no = 1.0 - w_yes

        # ── 5. Conviction Scoring & Outcome Selection ─────────────────────────
        # Directional Strike Boundary: Never buy YES if below strike, never buy NO if above strike
        if dist_pct >= 0.0000 and w_yes >= 0.55:
            chosen_outcome = "YES"
            chosen_token_id = market.get("yes_token_id") or market.get("outcome1Id", "")
            win_prob = w_yes
            quote_price = yes_price
        elif dist_pct <= -0.0000 and w_no >= 0.55:
            chosen_outcome = "NO"
            chosen_token_id = market.get("no_token_id") or market.get("outcome2Id", "")
            win_prob = w_no
            quote_price = no_price
        else:
            return None

        if quote_price <= 0.0 or quote_price >= 0.95:
            return None

        # ── 6. Expected Value Calculation ─────────────────────────────────────
        eff_fee = fee_rate * max(1.0 - quote_price, 0.5)
        cost_basis = quote_price * (1.0 + eff_fee)
        ev = (win_prob / cost_basis) - 1.0

        # Minimum required positive EV threshold (at least +3% Net EV)
        if ev < 0.03:
            return None

        # ── 7. Dynamic Conviction Sizing (Ohioism Sizing Curve) ────────────────
        if win_prob >= 0.85 and secs <= 90:
            size_pct = 0.08
        elif quote_price <= 0.30:
            size_pct = 0.01  # Cheap probe
        else:
            size_pct = min(0.05, max(0.02, ev * 0.25))

        cert = probability_to_certainty(win_prob)

        log.info(
            f"PAIRED_SNIPER SIGNAL {asset} {tf} | {chosen_outcome} @ {quote_price:.3f} | "
            f"w_est={win_prob:.1%} EV={ev:+.1%} size={size_pct:.1%} | dist={dist_pct:+.3%} secs={secs:.0f}s"
        )

        return TradeSignal(
            strategy="PAIRED_SNIPER",
            asset=asset,
            timeframe=tf,
            outcome=chosen_outcome,
            outcome_id=chosen_token_id,
            market_id=market_id,
            event_id=market.get("event_id", ""),
            market_price=quote_price,
            certainty=cert,
            win_prob=win_prob,
            edge=ev,
            size_pct=size_pct,
            reason=f"OHIO_TAKER {chosen_outcome} w_est={win_prob:.1%} ev={ev:+.1%} (dist={dist_pct:+.3%})",
            title=market.get("title", ""),
            mode_floor=0.50,
        )


paired_sniper_strategy = PairedSniperStrategy()
