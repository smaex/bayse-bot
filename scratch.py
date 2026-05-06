import asyncio
import database
database.init_db()
users = database.get_all_active()
for u in users:
    cid = u['chat_id']
    trades = database.recent_trades(cid, limit=5)
    for t in trades:
        print(f"[{cid}] Trade {t['trade_id']}: {t['strategy']} {t['asset']} - Won: {t['won']} - PnL: {t['pnl_ngn']} - Entry: {t['entry_price']} - Created: {t['created_at']}")
