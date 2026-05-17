import logging
from typing import Optional
import config
import news as news_mod
from strategies.base import BaseStrategy, TradeSignal
from strategies.utils import (
    momentum_score, regime_score, certainty_to_prob
)
from strategies.manager import kelly_size, max_ev_price

log = logging.getLogger("strat.news")

class NewsStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("NEWS")

    async def evaluate(self, market: dict, learned: dict, state: any, spot_price: float = None) -> Optional[TradeSignal]:
        asset = market["asset"]
        secs = market.get("secs_to_close", 0)
        
        sig = news_mod.best_signal_for(asset)
        if not sig: return None
        
        strength = sig.strength()
        sentiment_threshold = config.NEWS_SENTIMENT_THRESHOLD
        if strength < sentiment_threshold: return None

        # 1. Basic Guards
        if secs < config.NEWS_MIN_SECS_LEFT: return None
        if market.get("timeframe") == "5min": return None

        direction_raw = "YES" if sig.direction == "BULLISH" else "NO"
        market_price = market["yes_price"] if direction_raw == "YES" else market["no_price"]
        if market_price > config.NEWS_MAX_MARKET_PRICE: return None

        # 2. Regime & Momentum
        regime = regime_score(asset, state)
        if regime < config.NEWS_MIN_REGIME: return None
        
        mom = momentum_score(asset, direction_raw, state)
        if mom < -0.5: return None

        # 3. Sizing & EV
        dampened = strength * config.NEWS_CERTAINTY_DAMPEN
        strength_adj = min(dampened * (1.0 + 0.15 * mom), 0.99)
        
        w_est = certainty_to_prob(strength_adj)
        fee_rate = market.get("fee_rate", 0.04)
        ev_ceiling = max_ev_price(w_est, market_price, fee_rate)

        if market_price >= ev_ceiling: return None
        if market_price > ev_ceiling * 0.98: return None

        size = kelly_size(w_est, market_price, fee_rate, fraction=config.NEWS_KELLY_FRACTION, asset=asset, learned=learned, strategy_name="NEWS")

        return TradeSignal(
            strategy="NEWS",
            event_id=market["event_id"],
            market_id=market["market_id"],
            asset=asset,
            timeframe=market["timeframe"],
            outcome=direction_raw,
            outcome_id=market["yes_id"] if direction_raw == "YES" else market["no_id"],
            certainty=strength_adj,
            win_prob=w_est,
            market_price=market_price,
            size_pct=size,
            reason=f"News [{sig.direction}] raw={strength:.2f} mom={mom:+.2f} {sig.headline[:50]}",
            title=market["title"],
            momentum_at_entry=mom,
            regime_at_entry=regime
        )
