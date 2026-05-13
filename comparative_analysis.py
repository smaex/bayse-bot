"""
Comparative analysis module.
Checks Bayse pricing against external prediction markets (Polymarket).

v2: Added Order Book Depth checking via the CLOB API.
    Mid-price alone is misleading — we now check actual available liquidity
    so the bot knows if an "edge" is real or a ghost.
"""

import aiohttp
import logging
import asyncio
import time
from typing import Optional, Dict

log = logging.getLogger(__name__)

POLY_GAMMA_URL = "https://gamma-api.polymarket.com"
POLY_CLOB_URL = "https://clob.polymarket.com"

# Mapping Bayse assets to Polymarket tags or search terms
ASSET_MAPPING = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
}

# Global Cache for Polymarket prices to stay under Render Free Tier limits
# Format: { asset: { "price": float, "market_id": str, "token_id": str,
#                     "depth_yes": float, "depth_no": float, "timestamp": float } }
CACHE: Dict[str, dict] = {}


class PolymarketClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Add a 5-second timeout to prevent blocking the execution loop
            timeout = aiohttp.ClientTimeout(total=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def find_market(self, asset: str, threshold: float) -> Optional[Dict]:
        """Find a Polymarket event that closely matches the Bayse market."""
        # Check cache first for market_id
        if asset in CACHE and CACHE[asset].get("market_id"):
            if time.time() - CACHE[asset]["timestamp"] < 3600:
                return {"id": CACHE[asset]["market_id"],
                        "clobTokenIds": CACHE[asset].get("token_ids", "")}

        session = await self._get_session()
        query = ASSET_MAPPING.get(asset, asset)

        try:
            async with session.get(f"{POLY_GAMMA_URL}/markets", params={
                "active": "true",
                "closed": "false",
                "limit": 20,
                "query": query
            }) as r:
                r.raise_for_status()
                markets = await r.json()
                for m in markets:
                    title = m.get("question", "")
                    if "above" in title.lower() or "price" in title.lower():
                        return m
                return None
        except Exception as e:
            log.error(f"Error searching Polymarket: {e}")
            return None

    async def get_price(self, market_id: str) -> Optional[float]:
        """Get the current mid-price for a Polymarket market."""
        session = await self._get_session()
        try:
            async with session.get(f"{POLY_GAMMA_URL}/markets/{market_id}") as r:
                r.raise_for_status()
                data = await r.json()
                prices = data.get("outcomePrices")
                if prices and len(prices) > 0:
                    return float(prices[0])
                return None
        except Exception as e:
            log.error(f"Error fetching Polymarket price: {e}")
            return None

    async def get_order_book_depth(self, token_id: str) -> Optional[Dict]:
        """
        Fetch the CLOB order book for a specific outcome token.
        Returns the total available liquidity on the best bid and ask sides.
        
        This prevents the 'Mid-Price Delusion':
        - Mid-price might show 0.70, but if there's only $5 of liquidity,
          the REAL price you'd get is 0.60 or worse.
        - We sum the top 3 levels of bids/asks to get "usable depth."
        """
        session = await self._get_session()
        try:
            async with session.get(f"{POLY_CLOB_URL}/book", params={
                "token_id": token_id
            }) as r:
                r.raise_for_status()
                book = await r.json()

                bids = book.get("bids", [])
                asks = book.get("asks", [])

                # Sum top 3 levels of depth (in USD)
                bid_depth = sum(
                    float(b.get("size", 0)) * float(b.get("price", 0))
                    for b in bids[:3]
                )
                ask_depth = sum(
                    float(a.get("size", 0)) * float(a.get("price", 0))
                    for a in asks[:3]
                )

                # Best bid/ask prices
                best_bid = float(bids[0]["price"]) if bids else 0.0
                best_ask = float(asks[0]["price"]) if asks else 1.0

                return {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "bid_depth_usd": bid_depth,
                    "ask_depth_usd": ask_depth,
                    "spread": best_ask - best_bid,
                    "mid_price": (best_bid + best_ask) / 2 if (best_bid and best_ask) else None,
                }
        except Exception as e:
            log.debug(f"CLOB depth fetch failed for {token_id}: {e}")
            return None


async def update_cache():
    """Background task to refresh global prices every 5 minutes."""
    client = PolymarketClient()
    try:
        for asset in ASSET_MAPPING.keys():
            market = await client.find_market(asset, 0)
            if market:
                price = await client.get_price(market["id"])
                if price is None:
                    continue

                # Extract token IDs for CLOB depth checking
                token_ids_raw = market.get("clobTokenIds", "")
                token_ids = []
                if isinstance(token_ids_raw, str) and token_ids_raw:
                    # Could be comma-separated or JSON array string
                    token_ids = [t.strip().strip('"[]') for t in token_ids_raw.split(",") if t.strip()]
                elif isinstance(token_ids_raw, list):
                    token_ids = token_ids_raw

                # Fetch order book depth for the YES token (first token ID)
                depth_info = None
                if token_ids:
                    depth_info = await client.get_order_book_depth(token_ids[0])

                cache_entry = {
                    "price": price,
                    "market_id": market["id"],
                    "token_ids": token_ids_raw,
                    "timestamp": time.time(),
                    # Depth data (defaults if CLOB unavailable)
                    "depth_yes": 0.0,
                    "depth_no": 0.0,
                    "spread": 0.0,
                    "depth_price": price,  # fallback to gamma price
                    "has_depth": False,
                }

                if depth_info:
                    cache_entry.update({
                        "depth_yes": depth_info["bid_depth_usd"],
                        "depth_no": depth_info["ask_depth_usd"],
                        "spread": depth_info["spread"],
                        "depth_price": depth_info["mid_price"] or price,
                        "has_depth": True,
                    })

                CACHE[asset] = cache_entry

                depth_str = ""
                if depth_info:
                    depth_str = (
                        f" | Depth: ${depth_info['bid_depth_usd']:.0f} bid / "
                        f"${depth_info['ask_depth_usd']:.0f} ask | "
                        f"Spread: {depth_info['spread']:.3f}"
                    )
                log.info(f"Polymarket Cache Updated: {asset} = {price:.3f}{depth_str}")
    finally:
        await client.close()


def get_edge_quality(asset: str) -> Dict:
    """
    Returns a quality assessment of the current Polymarket edge.
    Used by POLY_EDGE strategy to decide if the edge is 'real' or a 'ghost'.
    
    Returns:
        {
            "is_real": bool,      — True if there's real liquidity behind the price
            "price": float,       — Best available price (depth-adjusted)
            "depth_usd": float,   — Total available liquidity in USD
            "spread": float,      — Bid-ask spread (tighter = more reliable)
            "reason": str,        — Human-readable explanation
        }
    """
    info = CACHE.get(asset)
    if not info or (time.time() - info["timestamp"] > 600):
        return {"is_real": False, "price": 0.0, "depth_usd": 0.0,
                "spread": 1.0, "reason": "No data"}

    # If we don't have depth data, trust the mid-price but flag it
    if not info.get("has_depth"):
        return {
            "is_real": True,  # Assume real but with lower confidence
            "price": info["price"],
            "depth_usd": 0.0,
            "spread": 0.0,
            "reason": "Mid-price only (no depth data)",
        }

    depth = info["depth_yes"] + info["depth_no"]
    spread = info["spread"]

    # Ghost detection rules:
    # 1. Total depth < $50 = Ghost (not enough liquidity to be meaningful)
    # 2. Spread > 0.10 = Ghost (market is too wide — price is unreliable)
    if depth < 50:
        return {
            "is_real": False,
            "price": info["depth_price"],
            "depth_usd": depth,
            "spread": spread,
            "reason": f"Ghost: Only ${depth:.0f} depth (need $50+)",
        }

    if spread > 0.10:
        return {
            "is_real": False,
            "price": info["depth_price"],
            "depth_usd": depth,
            "spread": spread,
            "reason": f"Ghost: Spread too wide ({spread:.1%})",
        }

    return {
        "is_real": True,
        "price": info["depth_price"],
        "depth_usd": depth,
        "spread": spread,
        "reason": f"Real: ${depth:.0f} depth, {spread:.3f} spread",
    }


async def get_comparative_price(asset: str, threshold: float) -> Optional[float]:
    """Returns the cached price if fresh (< 5 mins), otherwise triggers a targeted fetch."""
    if asset in CACHE:
        if time.time() - CACHE[asset]["timestamp"] < 300:
            return CACHE[asset]["price"]

    # Fallback: Targeted fetch (same as before but updates cache)
    client = PolymarketClient()
    try:
        market = await client.find_market(asset, threshold)
        if market:
            price = await client.get_price(market["id"])
            if price is not None:
                CACHE[asset] = {
                    "price": price,
                    "market_id": market["id"],
                    "token_ids": market.get("clobTokenIds", ""),
                    "timestamp": time.time(),
                    "depth_yes": 0.0,
                    "depth_no": 0.0,
                    "spread": 0.0,
                    "depth_price": price,
                    "has_depth": False,
                }
            return price
        return None
    finally:
        await client.close()


if __name__ == "__main__":
    async def test():
        logging.basicConfig(level=logging.INFO)
        await update_cache()
        for asset in ASSET_MAPPING:
            quality = get_edge_quality(asset)
            print(f"{asset}: {quality}")

    asyncio.run(test())
