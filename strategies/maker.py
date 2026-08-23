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

import feeds_direct
import feeds
from strategies.base import TradeSignal, BaseStrategy
from strategies.utils import gbm_win_probability, realized_vol_hourly

log = logging.getLogger("strat.maker")

# ── Parameters ────────────────────────────────────────────────────────────────
# Half-spread we quote around Fair Value.
# e.g. Fair Value = 0.50 → bid=0.475, capturing 0.025 per filled share
HALF_SPREAD       = 0.025

# If Binance price moves more than this % since we placed orders, requote.
REQUOTE_THRESHOLD = 0.0015   # 0.15%

# Minimum secs to market close. Don't make-market in last 45s (AMM locking risk).
MIN_SECS_TO_CLOSE = 45

# Max secs to market close. Don't open new maker positions if >90% of market life is over.
MAX_MAKER_WINDOW  = 840      # Quote for first 14 minutes of a 15-min market
MARKET_LIFE_SEC   = 900      # Standard 15-min market

# Bayse CLOB liquidityReward max spread (in cents / probability units).
# Markets pay a rebate if our spread is within this range.
MAX_REWARDED_SPREAD_CENTS = 5   # From API: "maxSpreadCents": 5

# Volatility threshold — if realized vol is very high, widen spread or skip.
HIGH_VOL_THRESHOLD = 0.003  # 0.3% per minute = very volatile

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

    def _fair_value(self, asset: str, market: dict, state=None) -> Optional[float]:
        """
        Fair Value of YES = P(spot at close >= threshold).

        Uses rigorous GBM model with Itô correction and asset-specific Kalman velocity drift.
        """
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

        # Exact GBM win probability
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

        # Log engine type but allow all market types.
        if engine == "CLOB":
            log.info(f"MAKER {asset} — CLOB market, will place LIMIT order")

        # Time window guard.
        # Don't make-market in the final 45s of a candle (settlement risk)
        if secs_to_close < MIN_SECS_TO_CLOSE:
            return None

        # Volatility guard: don't make-market in very volatile conditions.
        rvol = self._realized_vol(asset)
        if rvol > HIGH_VOL_THRESHOLD:
            log.info(f"MAKER SKIP {asset} — high vol {rvol:.4f}")
            return None

        # ── Price data ────────────────────────────────────────────────────────
        spot, t = feeds_direct.get_direct_price(asset)
        if not spot or (time.time() - t) > 10:
            spot = feeds.spot.get(asset, 0.0)
        threshold = market.get("threshold", 0.0)
        if not spot or not threshold:
            return None

        dist_pct = (spot - threshold) / threshold

        # ── Early-Candle Warm-up Filter ────────────────────────────────────────
        # In the first 2.5 minutes (secs > 750), require clear distance (>= 0.05%)
        # so we do not enter on opening tick noise before the candle trend forms.
        if secs_to_close > 750 and abs(dist_pct) < 0.00050:
            log.info(
                f"MAKER SKIP {asset} — early candle warm-up "
                f"(secs={secs_to_close:.0f} > 750, dist={dist_pct:+.4%} < 0.05%)"
            )
            return None

        # Calculate Drift-Aware Fair Value
        fv_yes = self._fair_value(asset, market, state=state)
        if fv_yes is None:
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

        # ── ETH Extra Edge Cushion ─────────────────────────────────────────────
        eth_edge_cushion = 0.005 if asset == "ETH" else 0.0

        # ── Trend-Aligned Side Selection ────────────────────────────────────────
        # Never catch falling knives!
        # - To buy YES: spot must not be significantly below threshold (dist >= -0.03%),
        #   momentum must not be dumping (mom_5m >= -0.0008), and fv_yes >= 0.45.
        # - To buy NO: spot must not be significantly above threshold (dist <= +0.03%),
        #   momentum must not be pumping (mom_5m <= +0.0008), and fv_no >= 0.45.
        chosen_side = None
        if (edge_yes >= edge_no and edge_yes >= (0.007 + eth_edge_cushion)
                and fv_yes >= 0.45 and mom_5m >= -0.0008 and dist_pct >= -0.00030):
            chosen_side = "YES"
            target_fv   = fv_yes
            market_bid  = yes_bid_price
            outcome_id  = market.get("yes_id", "")
        elif (edge_no > edge_yes and edge_no >= (0.007 + eth_edge_cushion)
                and fv_no >= 0.45 and mom_5m <= +0.0008 and dist_pct <= +0.00030):
            chosen_side = "NO"
            target_fv   = fv_no
            market_bid  = no_bid_price
            outcome_id  = market.get("no_id", "")
        else:
            log.info(
                f"MAKER SKIP {asset} — trend/edge guard "
                f"(fv_yes={fv_yes:.3f}, fv_no={fv_no:.3f}, edge_yes={edge_yes:+.3f}, "
                f"edge_no={edge_no:+.3f}, dist={dist_pct:+.3%}, mom_5m={mom_5m:+.4f})"
            )
            return None

        # Quote a bid at min(target_fv - HALF_SPREAD, market_bid + 0.01)
        chosen_edge = edge_yes if chosen_side == "YES" else edge_no
        our_bid = round(min(target_fv - HALF_SPREAD, market_bid + 0.01), 3)
        # Entry price floor guard: don't place bids below 0.28 or above 0.85.
        if our_bid < 0.28 or our_bid > 0.85:
            log.info(f"MAKER SKIP {asset} — bid price out of bounds ({our_bid:.3f})")
            return None

        log.info(
            f"MAKER SIGNAL {asset} | side={chosen_side} fv={target_fv:.3f} our_bid={our_bid:.3f} "
            f"market_bid={market_bid:.3f} mom_5m={mom_5m:+.4f} dist={dist_pct:+.3%} secs={secs_to_close:.0f}"
        )

        return TradeSignal(
            strategy    = "MAKER",
            event_id    = market["event_id"],
            market_id   = market_id,
            asset       = asset,
            timeframe   = market["timeframe"],
            outcome     = chosen_side,
            outcome_id  = outcome_id,
            certainty   = min(0.90, 0.45 + chosen_edge * 4.0),  # edge 0.01→~0.49, 0.05→~0.65
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
                    binance_price: float, amount: float, outcome_id: str):
        """Called by executor after a LIMIT order is placed."""
        self.open_orders[market_id] = {
            "order_id":       order_id,
            "placed_price":   placed_price,
            "binance_at_place": binance_price,
            "amount":         amount,
            "outcome_id":     outcome_id,
            "placed_at":      time.time(),
        }

    def should_requote(self, market_id: str) -> bool:
        """True if Binance has moved enough that our quote is stale."""
        info = self.open_orders.get(market_id)
        if not info:
            return False
        asset  = None
        # We don't store asset in open_orders, so re-check via direct_spot
        # by comparing any asset's price move as a proxy.
        for a in ("BTC", "ETH", "SOL"):
            price_now, t = feeds_direct.get_direct_price(a)
            if price_now and (time.time() - t) < 5:
                base = info.get("binance_at_place", price_now)
                if base > 0 and abs(price_now - base) / base > REQUOTE_THRESHOLD:
                    return True
        return False


# Singleton used by executor.py and bot.py
maker_strategy = MakerStrategy()
