import asyncio
import logging
import os
import sys
from datetime import datetime

# Add current directory to path so we can import our modules
sys.path.append(os.getcwd())

import database
import config
from client import BayseClient

# Setup logging to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
log = logging.getLogger("PANIC")

async def emergency_shutdown():
    log.info("🚨 EMERGENCY SHUTDOWN INITIATED 🚨")
    
    # 1. Initialize Database
    try:
        database.init_db()
    except Exception as e:
        log.error(f"Failed to connect to database: {e}")
        return

    # 2. Get all active users
    users = await asyncio.to_thread(database.get_all_active)
    if not users:
        log.warning("No active users found in database.")
        return

    log.info(f"Found {len(users)} active accounts to neutralize...")

    for user in users:
        cid = user['chat_id']
        pub = user['public_key']
        sec = user['secret_key']
        
        log.info(f"--- Processing User: {cid} ---")
        
        # A. Force-Pause in Database
        try:
            settings = user['settings']
            settings['paused'] = True
            await asyncio.to_thread(database.update_settings, cid, settings)
            log.info(f"✅ User {cid} PAUSED in database.")
        except Exception as e:
            log.error(f"❌ Failed to pause user {cid}: {e}")

        # B. Cancel all resting orders via API. The listing is paginated and
        # filtered server-side on lowercase statuses; a per-ID rejection is a
        # failure that must be reported, never logged as a success.
        client = None
        try:
            client = BayseClient(pub, sec)
            open_orders = await client.list_open_orders()
            if not open_orders:
                log.info(f"No open orders found for user {cid}.")
            else:
                log.info(f"Cancelling {len(open_orders)} open orders for {cid}...")
                cancelled = failed = 0
                for o in open_orders:
                    oid = o.get('id') or o.get('orderId')
                    try:
                        await client.cancel_order(oid)
                        cancelled += 1
                    except Exception as ex:
                        failed += 1
                        log.error(f"  Failed to cancel order {oid}: {ex}")
                log.info(f"  Cancelled {cancelled}, failed {failed} for {cid}")
        except Exception as e:
            log.error(f"❌ API connection failed for user {cid}: {e}")
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass

    log.info("🚨 EMERGENCY SHUTDOWN COMPLETE 🚨")
    log.info("Bot logic is now PAUSED for all users. Pending orders have been cleared.")

if __name__ == "__main__":
    asyncio.run(emergency_shutdown())
