import asyncio
import database
import json

async def check_trades():
    chat_id = "8264282870"
    # Fetch recent trades from database
    trades = database.recent_trades(chat_id, limit=50)
    for t in trades:
        # Filter for BTC 15m trades
        if t.get('asset') == 'BTC' and t.get('timeframe') == '15min':
            print(json.dumps(t, indent=2, default=str))

if __name__ == "__main__":
    asyncio.run(check_trades())
