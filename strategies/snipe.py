import math
import logging
from datetime import datetime, timezone
from typing import Optional
import config
import feeds
import feeds_direct
from strategies.base import BaseStrategy, TradeSignal
from strategies.utils import (
    realized_vol_hourly, momentum_score, velocity_score, 
    regime_score, fx_distance_trend, win_probability, probability_to_certainty
)
from strategies.manager import kelly_size, max_ev_price

log = logging.getLogger("strat.snipe")

class SnipeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("SNIPE")

    async def evaluate(self, market: dict, learned: dict, state: any, spot_price: float = None) -> Optional[TradeSignal]:
        tf    = market["timeframe"]
        secs  = market["secs_to_close"]
        asset = market["asset"]
        learned = learned or {}
        mode    = learned.get("mode", "balanced")
        
        # 1. Mode-based Thresholds
        min_certainty = {"safe": 0.65, "balanced": 0.55, "aggressive": 0.45, "full_send": 0.35}.get(mode, 0.55)
        if "snipe_min_certainty" in learned:
            min_certainty = max(min_certainty - 0.05, min(learned["snipe_min_certainty"], min_certainty + 0.15))

        # 2. Basic Filtering
        if mode == "safe":
            if tf not in ["1h", "6h", "1d"]: return None
            if asset not in ["BTC", "EURUSD", "GBPUSD", "XAUUSD"]: return None

        entry_window = config.SNIPE_ENTRY_WINDOWS.get(tf)
        if asset in config.FX_SESSION_UTC and tf == "1h": entry_window = config.FX_ENTRY_WINDOW_1H
        if entry_window is None or secs > entry_window or secs < 0: return None
        if mode != "full_send" and secs < 90: return None

        threshold = market.get("threshold")
        live_spot = spot_price if spot_price is not None else feeds.spot.get(asset)
        if not threshold or not live_spot: return None

        # 3. Quant Models
        distance_pct = (live_spot - threshold) / threshold
        direction = "YES" if distance_pct > 0 else "NO"
        
        rv = realized_vol_hourly(asset, state)
        if secs < 300: rv *= (1.0 + 0.5 * ((300 - secs) / 210.0))
        
        w_est = win_probability(distance_pct, secs, asset, sigma_override=rv)
        base = probability_to_certainty(w_est)
        
        mom = momentum_score(asset, direction, state)
        regime = regime_score(asset, state)
        velocity = velocity_score(asset, threshold, direction, state)
        
        # 4. Vetoes
        if mode != "full_send":
            # Smart Shield (Pin Risk & Volatility Guard)
            base_dist = config.CRYPTO_MIN_DISTANCE.get(asset) or config.FX_MIN_DISTANCE.get(asset, 0.0010)
            base_vol = config.ASSET_HOURLY_VOL.get(asset, 0.02)
            
            # Dynamic Volatility Scaling: shrink requirement in calm markets, expand in chaotic ones
            vol_ratio = rv / base_vol if base_vol > 0 else 1.0
            dynamic_min_dist = max(base_dist * 0.5, min(base_dist * vol_ratio, base_dist * 1.5))
            
            if abs(distance_pct) < dynamic_min_dist: 
                return None
            
            # Momentum Veto: If inside the ORIGINAL base buffer, demand positive momentum (moving away from danger)
            if abs(distance_pct) < base_dist and mom <= 0:
                return None

            if velocity < -config.SNIPE_VELOCITY_VETO: return None
            if mom < -0.7 and base < 0.55: return None

        if asset in config.FX_SESSION_UTC and regime < config.FX_MIN_REGIME: return None

        # 5. Composite Calculation
        raw_edge = w_est - market.get("yes_price" if direction == "YES" else "no_price", 0.5)
        edge_bonus = min(max(raw_edge * 0.40, -0.08), 0.12)
        mom_bonus = (0.18 if mode == "aggressive" else 0.12) * mom
        regime_factor = 0.75 + 0.50 * regime
        
        composite = min((base + mom_bonus + edge_bonus) * regime_factor, 0.99)

        # 6. Macro Bias
        biases = feeds_direct.get_macro_bias()
        if "USD_SPIKE" in biases:
            composite = min(composite + 0.10, 0.99) if direction == "NO" else max(composite - 0.15, 0.0)

        # 7. Final Verification
        # Relax strategy-level hurdle to 0.30 so orchestrator-level Mode Floors and Discovery Probes can process the signals
        if composite < 0.30: return None

        fee_rate = market.get("fee_rate", 0.04)
        margin_map = {"safe": 0.20, "balanced": 0.06, "aggressive": 0.04, "full_send": 0.02}
        market_price = market["yes_price"] if direction == "YES" else market["no_price"]
        ev_ceiling = max_ev_price(w_est, market_price, fee_rate, min_margin=margin_map.get(mode, 0.10))
        if market_price >= ev_ceiling: return None
        if market_price > config.SNIPE_MAX_MARKET_PRICE: return None

        # 8. Sizing
        size = kelly_size(w_est, market_price, fee_rate, asset=asset, state=state, learned=learned, strategy_name="SNIPE")

        return TradeSignal(
            strategy="SNIPE",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=tf,
            outcome=direction,
            outcome_id=market["yes_id"] if direction == "YES" else market["no_id"],
            certainty=composite,
            win_prob=w_est,
            market_price=market_price,
            size_pct=size,
            reason=f"Composite={composite:.2f} w={w_est:.1%} mom={mom:+.2f} edge={raw_edge:+.3f}",
            title=market["title"],
            momentum_at_entry=mom,
            regime_at_entry=regime
        )
