import logging
import strategies
from strategies.base import TradeSignal, MarketState, global_state

# Compatibility layer for the old strategy.py
# This allows other modules to still import 'strategy' while we migrate.

log = logging.getLogger("strategy.compat")

def evaluate_global_v2(market: dict, spot_price: float = None, state: MarketState = global_state) -> dict[str, TradeSignal]:
    """
    DEPRECATED: Use strategies.evaluate_all instead.
    Compatibility wrapper for bot.py
    """
    # We use empty learned dict for global evaluation
    # (replicates previous behavior where global signals used 'balanced' defaults)
    balanced_learned = {"mode": "balanced", "snipe_min_certainty": 0.0}
    
    # We need to run this synchronously for now as bot.py expects it
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # This is tricky in async. 
            # In a real refactor, we would make the caller await strategies.evaluate_all
            # For now, we'll return an empty dict and let bot.py's per-user loop handle it
            return {} 
        else:
            signals = loop.run_until_complete(strategies.evaluate_all(market, balanced_learned, state, spot_price))
            return {sig.strategy: sig for sig in signals}
    except Exception as e:
        log.error(f"Compat evaluate_global_v2 error: {e}")
        return {}

# Re-export key functions if needed
from strategies.manager import kelly_size, max_ev_price
from strategies.utils import certainty_to_prob, record_btc_move

def is_halted(asset: str) -> bool:
    """Compatibility for bot.py"""
    if global_state.systemic_halt_until > 0:
        import time
        return global_state.systemic_halt_until > time.time()
    return False

async def load_memory():
    """Compatibility for bot.py. Now handled by individual strategy plugins if needed."""
    pass

def update_price_history(asset: str, price: float):
    """Compatibility for bot.py. Updates the global MarketState."""
    from collections import deque
    if asset not in global_state.price_history:
        global_state.price_history[asset] = deque(maxlen=2000)
    global_state.price_history[asset].append((import_time().time(), price))

def import_time():
    import time
    return time

def set_user_context(chat_id: int):
    """Compatibility for bot.py logging context."""
    pass

def check_systemic_risk() -> str:
    """Compatibility for bot.py."""
    if global_state.systemic_halt_until > import_time().time():
        return "Systemic Halt Active"
    return ""

