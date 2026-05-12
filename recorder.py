"""
Persistent Database Recorder — Batched writes to protect the connection pool.

Spot ticks are buffered in memory and flushed every FLUSH_INTERVAL seconds
in a single transaction, instead of one INSERT per tick (which was exhausting
the 5-connection CockroachDB pool and deadlocking the entire bot).
"""

import time
import threading
import logging
import database

log = logging.getLogger("recorder")

# ── Configuration ─────────────────────────────────────────────────────────────
FLUSH_INTERVAL = 60          # seconds between batch flushes
MAX_BUFFER_SIZE = 500        # safety cap — flush early if buffer gets huge

# ── In-memory buffer (thread-safe) ────────────────────────────────────────────
_tick_buffer: list[dict] = []
_buffer_lock = threading.Lock()
_last_flush = time.time()


def record_spot_tick(asset: str, price: float):
    """
    Buffer a spot tick in memory.  Does NOT touch the database.
    The flush_tick_buffer() function drains this periodically.
    """
    global _last_flush

    with _buffer_lock:
        _tick_buffer.append({
            "asset": asset,
            "price": price,
            "time":  time.time(),
        })

    # Check if we should flush (time-based or size-based)
    now = time.time()
    if now - _last_flush >= FLUSH_INTERVAL or len(_tick_buffer) >= MAX_BUFFER_SIZE:
        flush_tick_buffer()


def flush_tick_buffer():
    """
    Drain the tick buffer to DB in a single transaction.
    Called periodically — safe to call from any thread.
    """
    global _last_flush

    with _buffer_lock:
        if not _tick_buffer:
            return
        batch = _tick_buffer.copy()
        _tick_buffer.clear()
        _last_flush = time.time()

    # Write all ticks in one transaction (non-blocking — skip if pool is busy)
    try:
        database.save_recordings_batch(batch)
        log.debug(f"Flushed {len(batch)} spot ticks to DB")
    except Exception as e:
        log.warning(f"Tick flush failed (non-critical): {e}")
        # Ticks are expendable — don't re-queue, just drop them


def record_market_snapshot(markets: list):
    """Saves a snapshot of all active markets with their current AMM prices to the DB."""
    data = [
        {
            "market_id": m.get("market_id"),
            "asset": m.get("asset"),
            "timeframe": m.get("timeframe"),
            "threshold": m.get("threshold"),
            "yes_price": m.get("yes_price"),
            "no_price": m.get("no_price"),
            "secs_to_close": m.get("secs_to_close"),
            "expiry_time": m.get("expiry_time")
        }
        for m in markets
    ]
    try:
        database.save_recording_nonblocking("market_snapshot", data)
    except Exception as e:
        log.warning(f"Market snapshot save failed (non-critical): {e}")


log.info("Persistent Database Recorder initialized (batched mode)")
