"""
Recorder — now a no-op.

The previous version wrote every spot tick to a 'recordings' table in Supabase.
At ~6 assets × ~60 ticks/min this created ~500k rows/day and was the direct
cause of the database hitting 1487MB (3× the free-tier limit).

The recordings table serves no live trading function — it was only intended
for backtesting data that was never used.  All writes have been removed.

The public interface is preserved so bot.py doesn't need changes.
"""

import logging

log = logging.getLogger("recorder")


def record_spot_tick(asset: str, price: float):
    """No-op. Ticks are no longer persisted to DB."""
    pass


def flush_tick_buffer():
    """No-op."""
    pass


def record_market_snapshot(markets: list):
    """No-op. Market snapshots are no longer persisted."""
    pass
