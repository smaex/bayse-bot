import asyncio
import database
import json

async def check():
    database.init_db()
    users = database.get_all_active()
    for u in users:
        s = u.get("settings", {})
        ua = s.get("assets")
        ut = s.get("timeframes")
        print(f"User {u['chat_id']}:")
        print(f"  assets: {type(ua).__name__} {ua}")
        print(f"  timeframes: {type(ut).__name__} {ut}")

if __name__ == "__main__":
    asyncio.run(check())
