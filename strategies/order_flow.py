import logging
import math

log = logging.getLogger("strategies.order_flow")

def get_order_flow_imbalance(asset: str, depth: dict) -> float:
    """
    Calculates the Order Flow Imbalance (OFI) from top 10 levels.
    Returns a score from -1.0 (Heavy Sell Imbalance) to +1.0 (Heavy Buy Imbalance).
    """
    if not depth or "bids" not in depth or "asks" not in depth:
        return 0.0
        
    # bids: [[price, size], ...], asks: [[price, size], ...]
    bids = depth["bids"][:10]
    asks = depth["asks"][:10]
    
    bid_vol = sum(float(b[1]) for b in bids)
    ask_vol = sum(float(a[1]) for a in asks)
    
    if bid_vol + ask_vol == 0:
        return 0.0
        
    # Raw imbalance ratio
    imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    
    # Weight by distance from mid-price (closer levels matter more)
    # But for a simple V1, the raw ratio is already a powerful leading indicator.
    return max(-1.0, min(1.0, imbalance))

def get_ofi_boost(asset: str, direction: str, depth: dict) -> float:
    """
    Returns a certainty boost based on order flow alignment.
    Max boost: +0.10, Max penalty: -0.15.
    """
    ofi = get_order_flow_imbalance(asset, depth)
    
    # Alignment check
    if direction == "YES": # We want price to go UP
        # Positive OFI (more buyers) is good
        if ofi > 0.30: return 0.10 * ofi
        if ofi < -0.50: return -0.15 # Heavy selling against us
    else: # We want price to go DOWN
        # Negative OFI (more sellers) is good
        if ofi < -0.30: return 0.10 * abs(ofi)
        if ofi > 0.50: return -0.15 # Heavy buying against us
        
    return 0.0
