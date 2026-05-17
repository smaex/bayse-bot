import asyncio
import aiohttp
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scanner
import client

async def check():
    async with aiohttp.ClientSession() as session:
        c = client.BayseClient(session, "", "")
        markets = await scanner.scan_all(c)
        print(f"Total Scanned Markets from API: {len(markets)}")
        for m in markets:
            print(f"- Asset: {m.get('asset')} | TF: {m.get('timeframe')} | Status: {m.get('status')} | Title: {m.get('title')}")

if __name__ == "__main__":
    asyncio.run(check())
