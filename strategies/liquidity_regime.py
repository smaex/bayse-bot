"""
Orderbook Liquidity Regime Switching Orchestrator
=================================================
Classifies real-time CLOB orderbook liquidity on Bayse into 3 regimes:

1. TIGHT_LIQUID:
   - Tight spread (spread <= 0.08) and healthy resting depth (>= ₦400 on top levels).
   - Suitable for fast taker execution: SNIPE, FRONTRUN, ARB, CORRELATE.

2. DISLOCATED_WIDE:
   - Wide spread (spread > 0.15 or yes_ask + no_ask > 1.15) with hollow mid-market.
   - Taker orders are BLOCKED to avoid paying 0.95+ or taking instant negative EV.
   - Routes to MIDMARKET_MAKER to place two-sided limit orders capturing the wide spread.

3. THIN_ONE_SIDED:
   - Asymmetric or empty orderbook (depth < ₦200, missing bids/asks).
   - Taker orders are BLOCKED (prevents FAK zero-fills and empty book errors).
   - Only conservative resting limit maker quotes permitted.
"""

import time
import logging
import asyncio
from typing import Dict, Tuple, Optional

log = logging.getLogger("strategies.liquidity_regime")

# Cache to prevent hammering public books endpoint on every tick
# {outcome_id: (timestamp, orderbook_dict)}
_OB_CACHE: Dict[str, Tuple[float, dict]] = {}
_CACHE_TTL_SEC = 2.0


def parse_top_level(levels: list) -> Tuple[Optional[float], float]:
    """Returns (best_price, total_ngn_depth) from top levels."""
    if not levels:
        return None, 0.0
    best_p = float(levels[0].get("price", 0.0))
    depth_ngn = 0.0
    for lvl in levels[:5]:
        t = lvl.get("total")
        if t is not None:
            depth_ngn = max(depth_ngn, float(t))
        else:
            p = float(lvl.get("price", 0.0))
            q = float(lvl.get("quantity", 0.0))
            depth_ngn += p * q
    return best_p, depth_ngn


def classify_regime(ob_yes: dict, ob_no: dict) -> Tuple[str, dict]:
    """
    Evaluates YES and NO orderbooks and returns (regime, metrics).
    """
    yes_asks = ob_yes.get("asks", []) if ob_yes else []
    yes_bids = ob_yes.get("bids", []) if ob_yes else []
    no_asks  = ob_no.get("asks", []) if ob_no else []
    no_bids  = ob_no.get("bids", []) if ob_no else []

    best_yes_ask, depth_yes_ask = parse_top_level(yes_asks)
    best_yes_bid, depth_yes_bid = parse_top_level(yes_bids)
    best_no_ask, depth_no_ask   = parse_top_level(no_asks)
    best_no_bid, depth_no_bid   = parse_top_level(no_bids)

    yes_spread = (best_yes_ask - best_yes_bid) if (best_yes_ask is not None and best_yes_bid is not None) else None
    no_spread  = (best_no_ask - best_no_bid)   if (best_no_ask is not None and best_no_bid is not None) else None

    total_ask_depth = depth_yes_ask + depth_no_ask
    total_bid_depth = depth_yes_bid + depth_no_bid

    metrics = {
        "best_yes_ask": best_yes_ask,
        "best_yes_bid": best_yes_bid,
        "yes_spread": yes_spread,
        "best_no_ask": best_no_ask,
        "best_no_bid": best_no_bid,
        "no_spread": no_spread,
        "depth_yes_ask": depth_yes_ask,
        "depth_no_ask": depth_no_ask,
        "total_ask_depth": total_ask_depth,
        "total_bid_depth": total_bid_depth,
    }

    # Case 1: Empty or extremely thin book
    if not yes_asks and not no_asks:
        metrics["reason"] = "Empty orderbooks (zero resting asks)"
        return "THIN_ONE_SIDED", metrics

    if total_ask_depth < 200.0 or (best_yes_bid is None and best_no_bid is None):
        metrics["reason"] = f"Thin liquidity: total ask depth ₦{total_ask_depth:.1f} < ₦200 or missing bids"
        return "THIN_ONE_SIDED", metrics

    # Case 2: Dislocated wide spread (e.g. 0.05 bids vs 0.95 asks)
    is_wide_yes = yes_spread is not None and yes_spread > 0.15
    is_wide_no  = no_spread is not None and no_spread > 0.15
    asks_dislocated = (best_yes_ask is not None and best_no_ask is not None and (best_yes_ask + best_no_ask) > 1.15)
    bids_collapsed  = (best_yes_bid is not None and best_no_bid is not None and (best_yes_bid + best_no_bid) < 0.85)

    if is_wide_yes or is_wide_no or asks_dislocated or bids_collapsed:
        sum_str = f"{best_yes_ask + best_no_ask:.2f}" if (best_yes_ask and best_no_ask) else "N/A"
        metrics["reason"] = f"Wide dislocated spread: yes_spread={yes_spread}, no_spread={no_spread}, asks_sum={sum_str}"
        return "DISLOCATED_WIDE", metrics

    # Case 3: Tight and liquid
    metrics["reason"] = f"Tight liquid book: ask_depth=₦{total_ask_depth:.1f}"
    return "TIGHT_LIQUID", metrics


async def get_market_liquidity(client, yes_id: str, no_id: str) -> Tuple[str, dict]:
    """
    Asynchronously retrieves and caches orderbooks, returning the liquidity regime.
    """
    now = time.time()

    async def _fetch_ob(oid: str) -> dict:
        cached = _OB_CACHE.get(oid)
        if cached and (now - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]
        try:
            ob = await asyncio.wait_for(client.get_orderbook(oid, depth=5), timeout=1.0)
            _OB_CACHE[oid] = (now, ob if isinstance(ob, dict) else {})
            return _OB_CACHE[oid][1]
        except Exception:
            return cached[1] if cached else {}

    try:
        ob_yes, ob_no = await asyncio.gather(
            _fetch_ob(yes_id),
            _fetch_ob(no_id),
            return_exceptions=True,
        )
        if isinstance(ob_yes, Exception):
            ob_yes = {}
        if isinstance(ob_no, Exception):
            ob_no = {}
        return classify_regime(ob_yes, ob_no)
    except Exception as e:
        log.warning(f"Error fetching market liquidity for {yes_id}/{no_id}: {e}")
        return "THIN_ONE_SIDED", {"reason": f"Fetch error: {e}"}
