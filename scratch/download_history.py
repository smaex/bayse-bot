import asyncio
import aiohttp
import json
import os
from datetime import datetime, timedelta, timezone

DATA_DIR = "data/history"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

async def fetch_binance_klines(symbol, interval, start_time, end_time):
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": int(start_time.timestamp() * 1000),
        "endTime": int(end_time.timestamp() * 1000),
        "limit": 1000
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                print(f"Error fetching {symbol}: {resp.status}")
                return []

async def download_history(symbol="BTCUSDT", days=30):
    print(f"Downloading {days} days of {symbol} 1m data...")
    
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)
    
    all_klines = []
    current_start = start_time
    
    while current_start < end_time:
        print(f"  Fetching from {current_start.strftime('%Y-%m-%d %H:%M')}...")
        klines = await fetch_binance_klines(symbol, "1m", current_start, end_time)
        if not klines:
            break
        
        all_klines.extend(klines)
        # Last kline timestamp + 1ms for next start
        last_ts = klines[-1][0]
        current_start = datetime.fromtimestamp((last_ts + 60000) / 1000, tz=timezone.utc)
        
        if len(klines) < 1000:
            break
        
        # Rate limit friendly
        await asyncio.sleep(0.5)

    filename = f"{DATA_DIR}/{symbol}_1m_{days}d.json"
    with open(filename, "w") as f:
        json.dump(all_klines, f)
    
    print(f"✅ Success! Saved {len(all_klines)} minutes of data to {filename}")

if __name__ == "__main__":
    asyncio.run(download_history())
