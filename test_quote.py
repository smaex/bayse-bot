import asyncio
from client import BayseClient
import database

async def main():
    database.init_db()
    users = database.get_all_active()
    if not users:
        print("No users")
        return
    u = users[0]
    client = BayseClient(u['public_key'], u['secret_key'])
    
    # get active markets
    markets = await client.get_markets()
    if not markets:
        print("No markets")
        return
    m = markets[0]
    
    # try quote
    try:
        res = await client._get(f"/v1/pm/events/{m['eventId']}/markets/{m['id']}/quote", params={"outcomeId": m['outcomes'][0]['id'], "side": "BUY", "amount": 100})
        print("Quote success:", res)
    except Exception as e:
        print("Quote failed:", e)

asyncio.run(main())
