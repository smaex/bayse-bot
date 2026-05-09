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

        # B. Cancel all Open Orders via API
        try:
            client = BayseClient(pub, sec)
            orders_resp = await client.list_orders(limit=100)
            orders = orders_resp if isinstance(orders_resp, list) else orders_resp.get('orders', [])
            
            # Filter for open orders only
            open_orders = [o for o in orders if o.get('status') in ['OPEN', 'PARTIALLY_FILLED', 'PENDING']]
            
            if not open_orders:
                log.info(f"No open orders found for user {cid}.")
            else:
                log.info(f"Cancelling {len(open_orders)} open orders for {cid}...")
                for o in open_orders:
                    oid = o.get('orderId') or o.get('id')
                    try:
                        await client.cancel_order(oid)
                        log.info(f"  Successfully cancelled order {oid}")
                    except Exception as ex:
                        log.error(f"  Failed to cancel order {oid}: {ex}")
            
            await client.close()
        except Exception as e:
            log.error(f"❌ API connection failed for user {cid}: {e}")

    log.info("🚨 EMERGENCY SHUTDOWN COMPLETE 🚨")
    log.info("Bot logic is now PAUSED for all users. Pending orders have been cleared.")

if __name__ == "__main__":
    asyncio.run(emergency_shutdown())
