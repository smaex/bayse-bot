import asyncio
import logging
import sys
import os

# Add current dir to path
sys.path.append(os.getcwd())

import feeds_direct

async def test_feed():
    logging.basicConfig(level=logging.INFO)
    print("Testing Binance Direct Feed (miniTicker)...")
    
    # Start the feed in the background
    task = asyncio.create_task(feeds_direct.binance_feed())
    
    # Wait for some prices to come in
    for _ in range(30):
        await asyncio.sleep(2)
        if feeds_direct.direct_spot:
            print(f"\nReceived prices: {feeds_direct.direct_spot}")
            break
        else:
            print(".", end="", flush=True)
            
    task.cancel()
    print("\nTest complete.")

if __name__ == "__main__":
    asyncio.run(test_feed())
