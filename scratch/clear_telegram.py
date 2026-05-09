import asyncio
from telegram import Bot
import os
import sys

# Add current dir to path to find config/env
sys.path.append(os.getcwd())

async def clear_conflict():
    # Try to get token from environment or config
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        try:
            import config
            token = config.TELEGRAM_TOKEN
        except ImportError:
            print("Error: TELEGRAM_TOKEN not found.")
            return

    if not token:
        print("Error: TELEGRAM_TOKEN is empty.")
        return

    bot = Bot(token=token)
    print(f"Targeting bot token: {token[:10]}...")
    
    try:
        print("1. Forcing a webhook to disable all 'getUpdates' instances...")
        # Setting a webhook immediately kills all active 'getUpdates' (polling) sessions
        await bot.set_webhook(url="https://render.com/ghost-instance-kill-v1")
        await asyncio.sleep(3)
        
        print("2. Deleting webhook to re-enable polling...")
        await bot.delete_webhook(drop_pending_updates=True)
        
        print("\n✅ updates cleared and ghost instances disconnected.")
        print("You can now restart your main bot process.")
    except Exception as e:
        print(f"❌ Failed to clear conflict: {e}")

if __name__ == "__main__":
    asyncio.run(clear_conflict())
