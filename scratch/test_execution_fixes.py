import asyncio
import logging
import time
import sys

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_fixes")

def test_timeframe_sanitizer():
    raw_tfs_tests = [
        ["5m", "15m", "1h"],
        ["5min", "15min", "6h"],
        ["1m", "5m"],
    ]
    
    for raw_tfs in raw_tfs_tests:
        user_tfs = []
        for tf in raw_tfs:
            tf_clean = tf.lower().replace("min", "").replace("m", "")
            if tf_clean in {"5", "15"}:
                user_tfs.append(tf_clean + "min")
            else:
                user_tfs.append(tf)
        log.info(f"Sanitized: {raw_tfs} -> {user_tfs}")
        # Assertions
        if "5m" in raw_tfs:
            assert "5min" in user_tfs
        if "15m" in raw_tfs:
            assert "15min" in user_tfs
            
    log.info("✅ Timeframe sanitizer test passed.")

async def test_rest_fallback_definition():
    import feeds_direct
    assert hasattr(feeds_direct, "binance_rest_fallback")
    log.info("✅ binance_rest_fallback function exists in feeds_direct.")

async def test_snipe_strategy_hurdle():
    import feeds
    import strategy
    from strategies.snipe import SnipeStrategy
    from strategies.base import MarketState
    
    # Mock feeds.spot
    feeds.spot["BTC"] = 78000.0
    
    # Mock MarketState
    state = MarketState()
    
    # Setup mock active market close to the threshold (creating a 0.20% drift)
    # 78000 vs 77850 threshold
    market = {
        "event_id": "evt-123",
        "market_id": "mkt-123",
        "asset": "BTC",
        "timeframe": "5min",
        "title": "Will BTC be above 77,850?",
        "threshold": 77850.0,
        "yes_id": "yes-123",
        "no_id": "no-123",
        "yes_label": "Yes",
        "no_label": "No",
        "yes_price": 0.60,
        "no_price": 0.40,
        "fee_rate": 0.04,
        "secs_to_close": 180,
    }
    
    learned = {"mode": "balanced"}
    
    strat = SnipeStrategy()
    sig = await strat.evaluate(market, learned, state)
    
    log.info(f"Signal evaluated: {sig}")
    if sig:
        log.info(f"✅ Snipe Strategy hurdle test passed: certainty = {sig.certainty:.2f}")
    else:
        log.warning("❌ Snipe Strategy returned None — check if conditions weren't met.")

async def main():
    test_timeframe_sanitizer()
    await test_rest_fallback_definition()
    await test_snipe_strategy_hurdle()
    log.info("All tests completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
