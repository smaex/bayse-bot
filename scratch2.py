import asyncio
import database
import json
from client import BayseClient

async def main():
    database.init_db()
    users = database.get_all_active()
    if not users:
        print("No users")
        return
    u = users[0]
    cid = u['chat_id']
    client = BayseClient(u['public_key'], u['secret_key'])
    trades = database.recent_trades(cid, limit=20)
    for t in trades:
        oid = t.get("order_id")
        if oid:
            try:
                order_data = await client.get_order(oid)
                print(f"\nTrade {t['trade_id']} (Entry: {t['entry_price']}):")
                print("Order API response:")
                print(json.dumps(order_data, indent=2))
                break
            except Exception as e:
                print(e)

asyncio.run(main())
