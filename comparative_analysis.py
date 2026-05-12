"""
Comparative analysis module.
Checks Bayse pricing against external prediction markets (Polymarket).
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
        """
        Find a Polymarket event that closely matches the Bayse market.
        Bayse: "Will [Asset] be above [Threshold] at [Expiry]?"
        Polymarket: "Will [Asset] be above $[Price] on [Date]?"
        """
        session = await self._get_session()
        query = ASSET_MAPPING.get(asset, asset)
        
        # Search for active markets
        try:
            async with session.get(f"{POLY_GAMMA_URL}/markets", params={
                "active": "true",
                "closed": "false",
                "limit": 20,
                "query": query
            }) as r:
                r.raise_for_status()
                markets = await r.json()
                
                best_match = None
                min_diff = float('inf')
                
                for m in markets:
                    # Look for price-based markets
                    # Usually title contains "Bitcoin above $65,000"
                    title = m.get("question", "")
                    if "above" in title.lower() or "price" in title.lower():
                        # Simple logic: pick the one closest to our threshold
                        # This is a heuristic; real mapping would parse the strike price.
                        best_match = m
                        break # For now, just take the first relevant one
                
                return best_match
        except Exception as e:
            log.error(f"Error searching Polymarket: {e}")
            return None

    async def get_price(self, market_id: str) -> Optional[float]:
        """Get the current mid-price for a Polymarket market."""
        session = await self._get_session()
        try:
            # We use the Gamma API for simple price info if available, 
            # or we could use the CLOB API for order book depth.
            async with session.get(f"{POLY_GAMMA_URL}/markets/{market_id}") as r:
                r.raise_for_status()
                data = await r.json()
                # Outcome prices are usually in 'outcomePrices' or 'lastTradePrice'
                prices = data.get("outcomePrices")
                if prices and len(prices) > 0:
                    return float(prices[0]) # 'Yes' price
                return None
        except Exception as e:
            log.error(f"Error fetching Polymarket price: {e}")
            return None

async def get_comparative_price(asset: str, threshold: float) -> Optional[float]:
    """Helper to get a comparative price snapshot."""
    client = PolymarketClient()
    try:
        market = await client.find_market(asset, threshold)
        if market:
            price = await client.get_price(market["id"])
            return price
        return None
    finally:
        await client.close()

if __name__ == "__main__":
    # Test
    async def test():
        logging.basicConfig(level=logging.INFO)
        price = await get_comparative_price("BTC", 65000)
        print(f"Polymarket BTC price: {price}")
    
    asyncio.run(test())
