import asyncio
import json
import unittest
import time
import sys
sys.path.append("/Users/user/bayse-bot")

import config
from feeds_hardened import HardenedWebsocketPool, validate_ticks_window, check_delta_limit

class TestHardenedWS(unittest.IsolatedAsyncTestCase):
    async def test_validation_logic(self):
        # Test Layer 3: Delta Guard
        self.assertTrue(check_delta_limit("BTC", 100.1, 100.0, 0.002))
        self.assertFalse(check_delta_limit("BTC", 100.3, 100.0, 0.002))
        
        # Test Layer 1: Window Validation
        now = time.time()
        history = [(now - 4.0, 100.0), (now - 2.0, 100.02), (now - 0.5, 100.04)]
        self.assertTrue(validate_ticks_window("BTC", history, duration_sec=5.0, min_ticks=3, max_jump_pct=0.001))
        
        # Jump too large (> 0.1%)
        history_jump = [(now - 4.0, 100.0), (now - 2.0, 100.2), (now - 0.5, 100.21)]
        self.assertFalse(validate_ticks_window("BTC", history_jump, duration_sec=5.0, min_ticks=3, max_jump_pct=0.001))
        
        # Not enough ticks (only 2 in window)
        history_few = [(now - 1.0, 100.0), (now - 0.5, 100.01)]
        self.assertFalse(validate_ticks_window("BTC", history_few, duration_sec=5.0, min_ticks=3, max_jump_pct=0.001))

    async def test_stagger_and_drop_first(self):
        # Test staggered connection starts and drop first logic by calling the connection loop directly with mocked websocket
        msgs = []
        def handler(m):
            msgs.append(m)

        def dedup(m):
            return m.get("id")

        pool = HardenedWebsocketPool(
            url="ws://mock",
            pool_size=2,
            sub_payload=None,
            message_handler=handler,
            dedup_key_fn=dedup
        )

        class MockWS:
            def __init__(self):
                self.sent = [
                    '{"id": 1, "price": 100}',  # first tick: dropped
                    '{"id": 2, "price": 101}',  # second tick: processed
                    '{"id": 2, "price": 101}'   # duplicate: deduplicated
                ]
                self.idx = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.idx >= len(self.sent):
                    raise StopAsyncIteration
                val = self.sent[self.idx]
                self.idx += 1
                return val

        # Directly test the message processing inside connection loop context
        pool.is_running = True
        
        # Mock websockets.connect inside connection loop
        class MockConnectContext:
            async def __aenter__(self):
                return MockWS()
            async def __aexit__(self, exc_type, exc, tb):
                pass

        def mock_connect(url, **kwargs):
            return MockConnectContext()

        import websockets
        original_connect = websockets.connect
        websockets.connect = mock_connect

        try:
            # Run one connection loop iteration
            task = asyncio.create_task(pool._conn_loop(0))
            await asyncio.sleep(0.5)
            task.cancel()
        finally:
            websockets.connect = original_connect

        # Dropped 1st message (id=1), processed 2nd message (id=2), deduplicated 3rd message (id=2 duplicate)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["id"], 2)

if __name__ == '__main__':
    unittest.main()
