import asyncio
import feeds
import feeds_direct
import time

async def main():
    print("🚀 Starting Feed Verification...")
    print("Connecting to Bayse Relay and Direct Binance Feed...")
    
    # Start both feeds
    asyncio.create_task(feeds.start_feeds())
    asyncio.create_task(feeds_direct.binance_feed())
    
    print("\nSymbol | Relay Price | Direct Price | Diff % | Lag (s)")
    print("-" * 60)
    
    try:
        while True:
            await asyncio.sleep(2)
            for asset in ["BTC", "ETH", "SOL"]:
                relay_p = feeds.spot.get(asset, 0)
                direct_p, direct_t = feeds_direct.get_direct_price(asset)
                
                if relay_p and direct_p:
                    diff = abs(relay_p - direct_p) / direct_p
                    lag = time.time() - direct_t
                    print(f"{asset:<6} | {relay_p:<11,.2f} | {direct_p:<12,.2f} | {diff:<6.4%} | {lag:.2f}s")
    except KeyboardInterrupt:
        print("\nVerification stopped.")

if __name__ == "__main__":
    asyncio.run(main())
