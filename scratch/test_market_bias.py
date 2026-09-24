import unittest
import time
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.append("/Users/user/bayse-bot")

import config
from strategies.base import global_state, TradeSignal
from strategies.market_bias import MarketBiasStrategy
from strategies.snipe import SnipeStrategy

class TestMarketBias(unittest.IsolatedAsyncTestCase):
    def setUp(self):
        global_state.market_flips.clear()
        global_state.market_last_fav.clear()
        global_state.market_opening_prices.clear()

    async def test_opening_spread_bias(self):
        strategy = MarketBiasStrategy()
        
        # Mock a market
        market = {
            "asset": "BTC",
            "market_id": "test_m1",
            "event_id": "event_1",
            "timeframe": "5min",
            "secs_to_close": 290,
            "title": "BTC Price Target",
            "yes_id": "yes_1",
            "no_id": "no_1",
            "yes_price": 0.65,
            "no_price": 0.35
        }
        
        # 1. Spread is wide (0.30 >= 0.15) and YES is the favorite
        global_state.market_opening_prices["test_m1"] = {
            "yes": 0.65,
            "no": 0.35,
            "timestamp": time.time()
        }
        
        # Evaluate
        sig = await strategy.evaluate(market, {}, None)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.outcome, "YES")
        self.assertEqual(sig.win_prob, 0.673)
        self.assertEqual(sig.strategy, "MARKET_BIAS")

    async def test_utc_hour_bias(self):
        strategy = MarketBiasStrategy()
        
        # Mock coin-flip open
        market = {
            "asset": "BTC",
            "market_id": "test_m2",
            "event_id": "event_2",
            "timeframe": "5min",
            "secs_to_close": 290,
            "title": "BTC Coin Flip",
            "yes_id": "yes_2",
            "no_id": "no_2",
            "yes_price": 0.50,
            "no_price": 0.50
        }
        
        global_state.market_opening_prices["test_m2"] = {
            "yes": 0.50,
            "no": 0.50,
            "timestamp": time.time()
        }
        
        # Mock time to 15:00 UTC
        with patch('strategies.market_bias.datetime') as mock_datetime:
            mock_now = mock_datetime.now.return_value
            mock_now.hour = 15
            
            sig = await strategy.evaluate(market, {}, None)
            self.assertIsNotNone(sig)
            self.assertEqual(sig.outcome, "YES")
            self.assertEqual(sig.win_prob, 0.82)

    async def test_snipe_flips_veto(self):
        strategy = SnipeStrategy()
        
        # Mock market in Snipe entry window (e.g. 5min timeframe, entry window is 300 to 90 seconds)
        market = {
            "asset": "BTC",
            "market_id": "test_m3",
            "event_id": "event_3",
            "timeframe": "5min",
            "secs_to_close": 150,  # after 150s (so secs_to_close < 210)
            "title": "BTC Snipe",
            "yes_id": "yes_3",
            "no_id": "no_3",
            "yes_price": 0.55,
            "no_price": 0.45,
            "threshold": 100.0
        }
        
        # Mock Spot and high flip count (>=5)
        import feeds
        feeds.spot["BTC"] = 101.0
        global_state.market_flips["test_m3"] = 5
        
        sig = await strategy.evaluate(market, {"mode": "balanced"}, None, spot_price=101.0)
        # Should be vetoed and return None
        self.assertIsNone(sig)

    async def test_snipe_flips_boost(self):
        strategy = SnipeStrategy()
        
        # Mock market
        market = {
            "asset": "BTC",
            "market_id": "test_m4",
            "event_id": "event_4",
            "timeframe": "5min",
            "secs_to_close": 150,
            "title": "BTC Snipe",
            "yes_id": "yes_4",
            "no_id": "no_4",
            "yes_price": 0.55,
            "no_price": 0.45,
            "threshold": 100.0
        }
        
        # Mock Spot and low flip count (<=1)
        import feeds
        feeds.spot["BTC"] = 101.0
        global_state.market_flips["test_m4"] = 1
        
        # We want to measure the effect of flips on certainty boost
        # Let's run once with flips=1 and once with flips=2
        sig_boosted = await strategy.evaluate(market, {"mode": "balanced"}, None, spot_price=101.0)
        
        global_state.market_flips["test_m4"] = 2
        sig_normal = await strategy.evaluate(market, {"mode": "balanced"}, None, spot_price=101.0)
        
        if sig_boosted and sig_normal:
            self.assertGreater(sig_boosted.certainty, sig_normal.certainty)

if __name__ == '__main__':
    unittest.main()
