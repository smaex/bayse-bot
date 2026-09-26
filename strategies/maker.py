"""
MAKER — Passive Market Making (Spread Capture)
================================================
Inspired by pbot-6's Polymarket strategy, adapted for Bayse CLOB.

Instead of predicting price direction, the MAKER acts as a liquidity
provider, placing passive LIMIT orders at prices that are slightly better
than the current best bid. When retail traders use market orders, they
cross our spread, and we capture the difference.

Mathematical Model: Avellaneda-Stoikov Market Making
- Calculates Fair Value of the binary option using our private Binance oracle.
- Quotes a bid at Fair Value - half_spread (we earn the spread when filled).
- Skews fair value up or down based on real-time Binance momentum.
- Cancels and replaces orders if the oracle price shifts > REQUOTE_THRESHOLD.

Adverse Selection Protection:
- Cancels all open maker orders immediately if Binance volatility spikes,
  preventing a large trader from "picking us off" at a stale price.
- Uses a small minimum order size to limit per-trade risk.

Bayse API Compatibility (VERIFIED):
- client.get_orderbook(outcome_id) → live bid/ask book ✅
- client.place_order(..., order_type="LIMIT", price=..., time_in_force="GTC") ✅
- client.cancel_order(order_id) → cancel specific order ✅
- market has liquidityReward.maxSpreadCents → Bayse actually PAYS us to provide liquidity ✅
"""

import asyncio
import logging
import math
import time
from typing import Optional

import config
import feeds_direct
import feeds
from strategies.base import TradeSignal, BaseStrategy
from strategies.utils import (
    gbm_win_probability,
    note_reject,
    realized_twap_integral,
    realized_vol_hourly,
    twap_win_probability,
)

log = logging.getLogger("strat.maker")

# ── Parameters ────────────────────────────────────────────────────────────────
# Half-spread we quote around Fair Value.
# e.g. Fair Value = 0.50 → bid=0.475, capturing 0.025 per filled share
HALF_SPREAD       = 0.025

# If Binance price moves more than this % since we placed orders, requote.
REQUOTE_THRESHOLD = 0.0010   # 0.10% (more responsive cancellation on adverse move)

# Minimum secs to market close. Don't make-market in last 45s (AMM locking risk).
MIN_SECS_TO_CLOSE = 45

# Max secs to market close. Don't open new maker positions if >80% of market life is over.
# NOT WIRED: the quoting window is the literal 750/180 pair in evaluate()
# (minutes 2.5-12 of a 15-minute candle). Same for BOOK_DEPTH, REQUOTE_INTERVAL
# and MAX_REWARDED_SPREAD_CENTS below — they read like tuning knobs and change
# nothing, which is how SNIPE_MIN_RAW_MODEL_EDGE already misled once.
MAX_MAKER_WINDOW  = 720      # Quote for first 12 minutes of a 15-min market
MARKET_LIFE_SEC   = 900      # Standard 15-min market

# Bayse CLOB liquidityReward max spread (in cents / probability units).
# Markets pay a rebate if our spread is within this range.
MAX_REWARDED_SPREAD_CENTS = 5   # From API: "maxSpreadCents": 5

# Volatility threshold — if realized vol is very high, widen spread or skip.
# NOT EFFECTIVE as written: _realized_vol() returns the mean *per-tick*
# absolute return over the last 120s, so at roughly one tick a second this
# fires only if price moves 0.3% every second (~18% a minute). It has never
# appeared in a production gate counter. Left alone deliberately — making it
# fire would suppress quoting, which is a risk decision, not a cleanup.
HIGH_VOL_THRESHOLD = 0.003  # 0.3% per minute = very volatile

# The certainty floor (cert = max(fv, 0.50 + 3.5*edge) >= 0.65) is the gate
# that actually decides MAKER entries. Solving it for the edge term gives the
# alternative to fv >= 0.65; the fv >= 0.62 direction gate is never binding.
CERT_FLOOR        = 0.65
CERT_EDGE_WEIGHT  = 3.5
MIN_EDGE_FOR_CERT_FLOOR = (CERT_FLOOR - 0.50) / CERT_EDGE_WEIGHT   # 0.0429

# Order book depth to check for existing liquidity.
BOOK_DEPTH        = 10

# How often to reassess open maker quotes (seconds).
REQUOTE_INTERVAL  = 5.0


class MakerStrategy(BaseStrategy):
    """
    Passive CLOB market maker. Places a limit buy-order on the cheap side
    of each binary outcome and earns the Bayse liquidity reward when filled.

    One open order is tracked per (market_id, side). Orders are refreshed
    every REQUOTE_INTERVAL or when oracle moves REQUOTE_THRESHOLD.

    open_orders: { market_id → {"order_id", "placed_price", "binance_at_place", "amount", "outcome_id", "side"} }
    """

    def __init__(self):
        super().__init__("MAKER")
        self.open_orders: dict[str, dict] = {}   # market_id → order info

    def _fair_value(self, asset: str, market: dict, state=None,
                    spot: float | None = None) -> Optional[float]:
        """
        Fair Value of YES = P(spot at close >= threshold).

        Uses rigorous GBM model with Itô correction and asset-specific Kalman velocity drift.

        ``spot`` is the price the caller already resolved. It used to re-read
        the feeds here with its own 10s staleness rule, so one decision could
        compare a ``dist_pct`` from one price against a fair value computed
        from another — and both against Bayse's own relay price when the
        independent oracle was 10-30s old.
        """
        if not spot:
            spot, t = feeds_direct.get_direct_price(asset)
            if not spot or (time.time() - t) > 10:
                # Fall back to Bayse relay price
                spot = feeds.spot.get(asset, 0.0)
        if not spot:
            return None

        threshold     = market.get("threshold")
        secs_to_close = market.get("secs_to_close", 0)
        if not threshold or secs_to_close <= 0:
            return None

        # Realized volatility (GARCH-blended)
        rv = realized_vol_hourly(asset, state) if state else 0.022

        # Hourly drift from asset's Kalman filter velocity
        kalman = state.kalman_state.get(asset) if (state and hasattr(state, "kalman_state")) else None
        if kalman:
            k_price, k_velocity = kalman["x"]
            hourly_drift = (k_velocity / k_price) * 3600.0 if k_price > 0 else 0.0
        else:
            hourly_drift = 0.0

        # Fair value of the *settled* quantity. Bayse resolves these markets on
        # a Chainlink 60-second TWAP, not the close print, so the average — not
        # the terminal spot — is what a resting quote is paid on. Pricing the
        # close print overstates how much the final minute can still move
        # against us, which matters more for a maker than for a taker: the
        # quote has to survive until close. The Kalman drift cap is unchanged.
        twap_sec = float(getattr(config, "SETTLEMENT_TWAP_SEC", 0.0) or 0.0)
        if twap_sec > 0:
            integral, elapsed = (
                realized_twap_integral(asset, state, twap_sec - secs_to_close)
                if secs_to_close < twap_sec else (0.0, 0.0)
            )
            fv = twap_win_probability(
                spot=spot,
                threshold=threshold,
                secs=secs_to_close,
                hourly_vol=rv,
                window_sec=twap_sec,
                realized_integral=integral,
                realized_secs=elapsed,
                hourly_drift=hourly_drift,
                horizon_cap=180.0,
            )
        else:
            fv = gbm_win_probability(
                spot=spot,
                threshold=threshold,
                secs=secs_to_close,
                hourly_vol=rv,
                hourly_drift=hourly_drift,
                horizon_cap=180.0,
            )

        return max(0.03, min(0.97, fv))

    def _realized_vol(self, asset: str) -> float:
        """Estimate recent realized vol from price_history."""
        try:
            from strategy import global_state
            hist = global_state.price_history.get(asset)
            if not hist or len(hist) < 10:
                return 0.0
            now = time.time()
            recent = [(t, p) for t, p in hist if now - t < 120]
            if len(recent) < 5:
                return 0.0
            returns = [
                abs((recent[i][1] - recent[i-1][1]) / recent[i-1][1])
                for i in range(1, len(recent))
                if recent[i-1][1] > 0
            ]
            return sum(returns) / len(returns) if returns else 0.0
        except Exception:
            return 0.0

    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float = None) -> Optional[TradeSignal]:
        """
        Returns a TradeSignal if there is a good quoting opportunity.
        Called by bot.py on every market tick.
        """
        asset         = market["asset"]
        secs_to_close = market.get("secs_to_close", 0)
        market_id     = market["market_id"]
        engine        = market.get("engine", "AMM")

        # Passive maker orders only exist on a CLOB. Sending LIMIT/GTC to an
        # AMM is invalid and was a major source of repeated zero execution.
        if str(engine).upper() != "CLOB":
            note_reject(learned, "MAKER", "engine_not_clob", str(engine))
            return None

        # Time window guard.
        # Don't make-market in the final 45s of a candle (settlement risk)
        if secs_to_close < MIN_SECS_TO_CLOSE:
            note_reject(learned, "MAKER", "too_close_to_settle", f"secs={secs_to_close:.0f}")
            return None

        # Volatility guard: don't make-market in very volatile conditions.
        rvol = self._realized_vol(asset)
        if rvol > HIGH_VOL_THRESHOLD:
            note_reject(learned, "MAKER", "high_realized_volatility", f"{rvol:.4f}")
            log.info(f"MAKER SKIP {asset} — high vol {rvol:.4f}")
            return None

        # ── Price data ────────────────────────────────────────────────────────
        # The evaluation loop already picked the oracle for this pass — direct
        # Binance price while fresh, relay as a documented fallback, or it
        # skipped the market as stale — and hands it to every strategy. Use it.
        # The local re-read this replaces kept a private 10s staleness rule and
        # fell back to the Bayse relay, so for an oracle aged 10-30s
        # (FEED_STALE_SEC) MAKER computed "fair value" from the same source as
        # the market price it compares against, while SNIPE used the
        # independent oracle. feeds_direct.get_direct_price documents exactly
        # this: "Never substitute the Bayse relay here."
        spot = spot_price
        if not spot:
            spot, t = feeds_direct.get_direct_price(asset)
            if not spot or (time.time() - t) > 10:
                spot = feeds.spot.get(asset, 0.0)
        threshold = market.get("threshold", 0.0)
        if not spot or not threshold:
            note_reject(learned, "MAKER", "missing_spot_or_threshold")
            return None

        dist_pct = (spot - threshold) / threshold

        # ── Quoting Window Guard (Minutes 2.5 to 12.0 of a 15-min candle) ─────────
        # - Don't quote in the first 2.5 minutes (secs > 750): wait for initial direction.
        # - Don't open new maker limit bids in the final 3 minutes (secs < 180): settlement risk
        #   and ensures plenty of time for order to fill and manage exit if thesis shifts.
        if secs_to_close > 750:
            note_reject(learned, "MAKER", "candle_warmup_window", f"secs={secs_to_close:.0f}")
            return None
        if secs_to_close < 180:
            note_reject(learned, "MAKER", "late_candle_window", f"secs={secs_to_close:.0f}")
            return None

        # Calculate Drift-Aware Fair Value
        fv_yes = self._fair_value(asset, market, state=state, spot=spot)
        if fv_yes is None:
            note_reject(learned, "MAKER", "fair_value_unavailable")
            return None
        fv_no = 1.0 - fv_yes

        # ── 5-Minute Price Momentum ───────────────────────────────────────────
        mom_5m = 0.0
        try:
            hist = getattr(state, "price_history", {}).get(asset, []) if state else []
            if not hist:
                from strategy import global_state
                hist = global_state.price_history.get(asset, [])
            if hist and len(hist) >= 5:
                now_t = time.time()
                old_prices = [p for t, p in hist if 240 <= (now_t - t) <= 360]
                if old_prices and spot:
                    mom_5m = (spot - old_prices[-1]) / old_prices[-1]
        except Exception:
            mom_5m = 0.0

        yes_bid_price = market.get("yes_price", 0)
        no_bid_price  = market.get("no_price", 0)

        edge_yes = fv_yes - yes_bid_price if yes_bid_price > 0 else 0.0
        edge_no  = fv_no  - no_bid_price  if no_bid_price > 0 else 0.0

        # ── Asset-Specific Edge & Distance Calibration ────────────────────────
        # Calibrated for adaptive market making:
        # Require strong separation buffer to avoid chop whipsawing positions.
        # - ETH: requires >= 0.25% buffer due to micro-volatility chop.
        # - SOL: requires >= 0.20% buffer ($0.25+ on SOL).
        # - BTC: requires >= 0.08% buffer ($65+ on BTC) calibrated for lower BTC baseline variance.
        if asset == "ETH":
            min_dist_req = 0.0025
            min_mom_req = 0.0005
        elif asset == "BTC":
            min_dist_req = 0.0008
            min_mom_req = 0.0002
        else:
            min_dist_req = 0.0020
            min_mom_req = 0.0005
        eth_edge_cushion = 0.020 if asset == "ETH" else 0.0

        if abs(dist_pct) < min_dist_req:
            note_reject(learned, "MAKER", "distance_below_calibration",
                        f"{abs(dist_pct):.4%} < {min_dist_req:.4%}")
            log.info(
                f"MAKER SKIP {asset} — below calibrated distance threshold "
                f"(dist={dist_pct:+.4%} < {min_dist_req:+.4%})"
            )
            return None

        # ── Strict Directional Alignment & Non-Opposing Momentum ───────────────
        # NEVER trade against the spot side or enter when momentum actively opposes!
        # Requires true high-probability thesis (Fair Value >= 0.62, edge >= 0.020)
        # AND strictly supporting momentum:
        # NOTE: fv >= 0.62 here is not the operative threshold. The certainty
        # floor further down needs max(fv, 0.50 + 3.5*edge) >= 0.65, so entries
        # happen on fv >= 0.65 or on a 4.29c edge; an fv of 0.62-0.65 with a
        # thin edge is refused there instead. Same trap as SNIPE's raw-edge
        # floor — the number in the comment is not the number that binds.
        # - For YES: momentum must be positive (mom_5m >= +0.0005)
        # - For NO: momentum must be negative (mom_5m <= -0.0005)
        chosen_side = None
        min_maker_edge = 0.020 + eth_edge_cushion  # at least 2.0 cents of real edge
        if (dist_pct > 0 and edge_yes >= min_maker_edge
                and fv_yes >= 0.62 and mom_5m >= min_mom_req):
            chosen_side = "YES"
            target_fv   = fv_yes
            market_bid  = yes_bid_price
            outcome_id  = market.get("yes_id", "")
        elif (dist_pct < 0 and edge_no >= min_maker_edge
                and fv_no >= 0.62 and mom_5m <= -min_mom_req):
            chosen_side = "NO"
            target_fv   = fv_no
            market_bid  = no_bid_price
            outcome_id  = market.get("no_id", "")
        else:
            # Same defect as SNIPE's lumped gate: four independent conditions,
            # one counter, so the report could not say which one bound.
            if dist_pct > 0:
                side, fv, edge, bid = "YES", fv_yes, edge_yes, yes_bid_price
                momentum_ok = mom_5m >= min_mom_req
            elif dist_pct < 0:
                side, fv, edge, bid = "NO", fv_no, edge_no, no_bid_price
                momentum_ok = mom_5m <= -min_mom_req
            else:
                side, fv, edge, bid, momentum_ok = "NEITHER", fv_yes, edge_yes, yes_bid_price, True
            detail = (
                f"{side} fv={fv:.3f} (needs >=0.620) edge={edge:+.3f} "
                f"(needs >={min_maker_edge:+.3f}) mkt={bid:.3f} "
                f"dist={dist_pct:+.3%} mom_5m={mom_5m:+.4f} (needs "
                f"{'>=' if side == 'YES' else '<='}{min_mom_req:+.4f})"
            )
            if side == "NEITHER":
                code = "spot_on_threshold"
            elif fv < 0.62:
                code = "fair_value_below_floor"
            elif edge < min_maker_edge:
                code = "edge_below_floor"
            elif not momentum_ok:
                code = "momentum_not_supporting"
            else:
                code = "side_mismatch"
            note_reject(learned, "MAKER", code, detail)
            log.info(
                f"MAKER SKIP {asset} — {code} "
                f"(fv_yes={fv_yes:.3f}, fv_no={fv_no:.3f}, edge_yes={edge_yes:+.3f}, "
                f"edge_no={edge_no:+.3f}, dist={dist_pct:+.3%}, mom_5m={mom_5m:+.4f})"
            )
            return None

        # Quote a competitive bid:
        # Instead of pinning to 0.510 when market_bid is 0.50, calculate a competitive bid:
        # our_bid = min(target_fv - HALF_SPREAD, max(market_bid + 0.01, target_fv - 0.05, 0.520))
        chosen_edge = edge_yes if chosen_side == "YES" else edge_no
        competitive_bid = max(market_bid + 0.01, target_fv - 0.05, 0.520)
        our_bid = round(min(target_fv - HALF_SPREAD, competitive_bid), 3)

        # Clamp into the executable band with asymmetric positive expected value.
        # Max bid 0.580 guarantees payout is at least +72% on win (₦100 * (1/0.58 - 1) = +₦72.41),
        # preventing bad risk/reward where ₦100 risk only yields ₦53 win.
        #
        # NOTE: ``market_bid`` above is Bayse's outcome *probability* price (a
        # mid, not the best bid), so this is the most MAKER is willing to pay,
        # not a price that is known to be competitive. The executor re-prices it
        # against the live order book before anything is sent (see
        # executor._maker_quote_against_book).
        our_bid = round(max(config.MAKER_MIN_BID, min(config.MAKER_MAX_BID, our_bid)), 3)

        # Data-driven certainty calibration: combines true statistical win probability and spread edge
        cert = min(0.95, max(target_fv, 0.50 + chosen_edge * CERT_EDGE_WEIGHT))
        if cert < CERT_FLOOR:
            # This is the gate that actually decides MAKER's entries, not the
            # fv >= 0.62 direction gate above: cert = max(fv, 0.50 + 3.5*edge),
            # so a candidate needs fv >= 0.65 OR an edge of 4.29c. Say so,
            # because tuning the 0.62 looks like a lever and is not.
            note_reject(
                learned, "MAKER", "certainty_below_floor",
                f"{cert:.1%} < {CERT_FLOOR:.0%} — cert=max(fv, 0.50+{CERT_EDGE_WEIGHT}*edge), "
                f"so this needs fv>={CERT_FLOOR:.3f} or edge>={MIN_EDGE_FOR_CERT_FLOOR:+.3f} "
                f"(fv={target_fv:.3f}, edge={chosen_edge:+.3f})",
            )
            log.info(f"MAKER SKIP {asset} — certainty {cert:.1%} below 65% conviction floor")
            return None

        log.info(
            f"MAKER SIGNAL {asset} | side={chosen_side} fv={target_fv:.3f} our_bid={our_bid:.3f} "
            f"market_bid={market_bid:.3f} mom_5m={mom_5m:+.4f} dist={dist_pct:+.3%} cert={cert:.1%} secs={secs_to_close:.0f}"
        )

        return TradeSignal(
            strategy    = "MAKER",
            event_id    = market["event_id"],
            market_id   = market_id,
            asset       = asset,
            timeframe   = market["timeframe"],
            outcome     = chosen_side,
            outcome_id  = outcome_id,
            certainty   = cert,
            win_prob    = target_fv,
            market_price= our_bid,    # executor will place LIMIT at this price
            size_pct    = 0.02,       # 2% of bankroll per maker order (small, high frequency)
            reason      = f"MAKER {chosen_side} fv={target_fv:.3f} spread_capture bid={our_bid:.3f}",
            title       = market.get("title", ""),
            momentum_at_entry    = mom_5m,
            realized_vol_at_entry= rvol,
        )

    async def cancel_all(self, client, market_id: str = None):
        """Cancel all open maker orders (called on vol spike or market close)."""
        targets = {market_id: self.open_orders[market_id]} if market_id and market_id in self.open_orders else dict(self.open_orders)
        for mid, info in list(targets.items()):
            try:
                await client.cancel_order(info["order_id"])
                log.info(f"MAKER cancelled order {info['order_id']} on {mid}")
            except Exception as e:
                log.warning(f"MAKER cancel failed for {info['order_id']}: {e}")
            self.open_orders.pop(mid, None)

    def track_order(self, market_id: str, order_id: str, placed_price: float,
                    binance_price: float, amount: float, outcome_id: str, asset: str = ""):
        """Called by executor after a LIMIT order is placed."""
        self.open_orders[market_id] = {
            "order_id":         order_id,
            "placed_price":     placed_price,
            "binance_at_place": binance_price,
            "amount":           amount,
            "outcome_id":       outcome_id,
            "asset":            asset,
            "placed_at":        time.time(),
        }

    def should_requote(self, market_id: str) -> bool:
        """True if Binance has moved enough that our quote is stale."""
        info = self.open_orders.get(market_id)
        if not info:
            return False
        asset = info.get("asset")
        if not asset:
            return False
        price_now, t = feeds_direct.get_direct_price(asset)
        if not price_now or (time.time() - t) > 5:
            price_now = feeds.spot.get(asset, 0.0)
        base = info.get("binance_at_place", price_now)
        if base > 0 and abs(price_now - base) / base > REQUOTE_THRESHOLD:
            return True
        return False

    def is_stale(self, market_id: str, timeout_sec: float = 120.0) -> bool:
        """True if order has been resting too long without fill or oracle moved."""
        info = self.open_orders.get(market_id)
        if not info:
            return False
        if time.time() - info.get("placed_at", 0) > timeout_sec:
            return True
        return self.should_requote(market_id)


# Singleton used by executor.py and bot.py
maker_strategy = MakerStrategy()
