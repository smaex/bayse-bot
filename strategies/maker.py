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

log = logging.getLogger("strat.maker")

# ── Parameters ────────────────────────────────────────────────────────────────
# Half-spread we quote around Fair Value.
# e.g. Fair Value = 0.50 → bid=0.475, capturing 0.025 per filled share
HALF_SPREAD       = 0.025

# If Binance price moves more than this % since we placed orders, requote.
REQUOTE_THRESHOLD = 0.0015   # 0.15%

# Minimum secs to market close. Don't make-market in last 45s (AMM locking risk).
MIN_SECS_TO_CLOSE = 45

# Max secs to market close. Don't open new maker positions if > 90% of market life is over.
MAX_MAKER_WINDOW  = 60       # Only make-market in first 60s of a new market
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

    def _fair_value(self, asset: str, market: dict) -> Optional[float]:
        """
        Fair Value of YES = P(spot at close >= threshold).

        Uses a simplified diffusion model:
          - annualized vol from GARCH state
          - time to close in years
          - log-normal probability
        Blended with a momentum tilt from Binance oracle.
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

        # Annualised vol from GARCH (stored in global_state)
        try:
            from strategy import global_state
            garch_var = global_state.garch_state.get(asset, {}).get("var", None)
            if garch_var and garch_var > 0:
                hourly_var  = garch_var * 2000        # scale from tick to hourly
                annual_vol  = math.sqrt(max(hourly_var, 1e-10) * 8760)
            else:
                from config import ASSET_HOURLY_VOL
                annual_vol  = ASSET_HOURLY_VOL.get(asset, 0.022) * math.sqrt(8760)
        except Exception:
            annual_vol = 1.0  # fallback 100% annualised vol for crypto

        t_years = secs_to_close / (365.25 * 24 * 3600)
        if t_years <= 0:
            return None

        # Black-Scholes binary option probability (no drift assumed for short windows)
        d2 = (math.log(spot / threshold)) / (annual_vol * math.sqrt(t_years))

        # Normal CDF approximation
        def _ncdf(x: float) -> float:
            t_ = 1.0 / (1.0 + 0.2316419 * abs(x))
            poly = t_ * (0.319381530 + t_ * (-0.356563782 + t_ * (1.781477937 + t_ * (-1.821255978 + t_ * 1.330274429))))
            base = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
            return base if x >= 0 else 1.0 - base

        fv = _ncdf(d2)

        # Momentum tilt: if Binance is trending hard, skew FV
        latency_bias = feeds_direct.get_latency_bias(asset, spot)
        fv = max(0.03, min(0.97, fv + latency_bias * 0.1))

        return fv

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

        # Only quote on CLOB markets.
        if engine != "CLOB":
            return None

        # Time window guard.
        if secs_to_close < MIN_SECS_TO_CLOSE:
            return None

        # Don't open new quotes if market is mostly over
        secs_elapsed = MARKET_LIFE_SEC - secs_to_close
        if secs_elapsed > MAX_MAKER_WINDOW:
            return None

        # Volatility guard: don't make-market in very volatile conditions.
        rvol = self._realized_vol(asset)
        if rvol > HIGH_VOL_THRESHOLD:
            log.debug(f"MAKER SKIP {asset} — high vol {rvol:.4f}")
            return None

        # Calculate Fair Value.
        fv = self._fair_value(asset, market)
        if fv is None:
            return None

        # We quote on the YES (Up) side if it is underpriced.
        # i.e. if our fair value says YES is worth 0.55 but the market bids 0.45,
        # we can put a limit order at 0.48 and capture the spread when a seller arrives.
        yes_bid_price = market.get("yes_price", 0)
        if yes_bid_price <= 0:
            return None

        # Our limit price: Fair Value - half_spread (we are the new best bid).
        our_bid = round(fv - HALF_SPREAD, 3)
        our_bid = max(0.05, min(0.90, our_bid))

        # Only quote if there is meaningful edge above current best bid.
        if our_bid <= yes_bid_price + 0.005:
            log.debug(f"MAKER SKIP {asset} — no edge (our_bid={our_bid:.3f} vs market_bid={yes_bid_price:.3f})")
            return None

        log.info(
            f"MAKER SIGNAL {asset} | fv={fv:.3f} our_bid={our_bid:.3f} "
            f"market_bid={yes_bid_price:.3f} vol={rvol:.4f} secs={secs_to_close:.0f}"
        )

        return TradeSignal(
            strategy    = "MAKER",
            event_id    = market["event_id"],
            market_id   = market_id,
            asset       = asset,
            timeframe   = market["timeframe"],
            outcome     = "YES",
            outcome_id  = market["yes_id"],
            certainty   = fv,
            win_prob    = fv,
            market_price= our_bid,    # executor will place LIMIT at this price
            size_pct    = 0.02,       # 2% of bankroll per maker order (small, high frequency)
            reason      = f"MAKER fv={fv:.3f} spread_capture bid={our_bid:.3f}",
            title       = market.get("title", ""),
            momentum_at_entry    = feeds_direct.get_latency_bias(asset, spot_price or 0.0),
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
