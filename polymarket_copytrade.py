"""
Polymarket Copy-Trading Module
===============================
Polls a configured Polymarket wallet's active positions and mirrors them
onto equivalent Bayse markets.

Design decisions:
- Sizing: We use our own NGN risk_pct system — Poly trades in USD so
  position sizes are incomparable. We use the Polymarket price as a
  certainty signal only (higher poly price = higher certainty).
- Scope: Only mirror BTC, ETH, SOL on 5min and 15min Bayse markets.
  These are the highest-edge, fastest-resolving markets. Long-timeframe
  Polymarket positions are not actionable on a 15min Bayse window.
- Deduplication: A position is only mirrored once per 5-minute window.
  After that, the cooldown prevents re-entry even if the heartbeat fires.
- Currency: Polymarket is USD; we don't convert — we treat the Poly
  price (0.0–1.0) as a win_probability to feed into our executor.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, List

import aiohttp
from strategies.base import TradeSignal

log = logging.getLogger("poly_copytrade")

POLY_CLOB_URL = "https://clob.polymarket.com"
POLY_GAMMA_URL = "https://gamma-api.polymarket.com"

# Assets we care about (must match Bayse asset names)
COPY_ASSETS = {"BTC", "ETH", "SOL"}

# Only mirror into these timeframes on Bayse
COPY_TIMEFRAMES = {"5min", "15min"}

# Mapping from Polymarket condition keywords → Bayse asset
_ASSET_KEYWORDS: Dict[str, str] = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
}

# Dedup: position_key → last_mirrored timestamp
_copied_positions: Dict[str, float] = {}
COPY_COOLDOWN_SEC = 300  # 5 minutes — don't re-mirror same position


class PolymarketCopyClient:
    def __init__(self, wallet_address: str):
        self.wallet = wallet_address.lower().strip()
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=8)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_wallet_positions(self) -> List[Dict]:
        """
        Fetch all open positions held by the tracked wallet.
        Returns list of position dicts with keys:
          conditionId, outcome, size, avgPrice, asset (if we can map it)
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{POLY_CLOB_URL}/positions",
                params={"user": self.wallet, "sizeThreshold": "0.01"}
            ) as r:
                if r.status != 200:
                    log.debug(f"Poly positions returned {r.status} for {self.wallet[:8]}...")
                    return []
                data = await r.json()
                positions = data if isinstance(data, list) else data.get("positions", [])
                return positions
        except asyncio.TimeoutError:
            log.debug(f"Poly positions timeout for {self.wallet[:8]}...")
            return []
        except Exception as e:
            log.debug(f"Poly positions error: {e}")
            return []

    async def get_market_info(self, condition_id: str) -> Optional[Dict]:
        """
        Resolve a Polymarket conditionId to a market question + current price.
        Used to determine which asset the position is on and its current probability.
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{POLY_GAMMA_URL}/markets",
                params={"conditionId": condition_id, "limit": 1}
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                return markets[0] if markets else None
        except Exception:
            return None


def _identify_asset(question: str) -> Optional[str]:
    """
    Extract the Bayse asset name from a Polymarket market question string.
    e.g. "Will Bitcoin be above $100k on June 5?" → "BTC"
    """
    q = question.lower()
    for keyword, asset in _ASSET_KEYWORDS.items():
        if keyword in q:
            return asset
    return None


async def get_copy_signals(
    wallet_address: str,
    active_markets: List[Dict],
) -> List[Dict]:
    """
    Main entry point — returns a list of synthetic signal dicts that the bot
    can feed into execute_trade().

    Each signal dict contains:
      asset, outcome, certainty, win_prob, market_id, event_id,
      outcome_id, timeframe, market_price, reason
    """
    if not wallet_address or len(wallet_address) < 10:
        return []

    client = PolymarketCopyClient(wallet_address)
    signals = []

    try:
        positions = await client.get_wallet_positions()
        if not positions:
            log.debug(f"No Polymarket positions found for wallet {wallet_address[:8]}...")
            return []

        log.info(f"[POLY_COPY] {len(positions)} positions found for wallet {wallet_address[:8]}...")

        for pos in positions:
            # ── Step 1: Identify asset ──────────────────────────────────────
            condition_id = pos.get("conditionId") or pos.get("condition_id", "")
            outcome_label = (pos.get("outcome") or "YES").upper()
            poly_price = float(pos.get("currentPrice") or pos.get("price") or pos.get("avgPrice") or 0.0)
            size_usd = float(pos.get("size") or pos.get("quantity") or 0.0)

            if poly_price <= 0.05 or poly_price >= 0.95:
                # Skip near-resolved markets — the edge is gone
                continue

            if size_usd < 1.0:
                # Skip dust positions
                continue

            # Dedup check — don't re-mirror within cooldown window
            dedup_key = f"{condition_id}:{outcome_label}"
            last_copied = _copied_positions.get(dedup_key, 0.0)
            if time.time() - last_copied < COPY_COOLDOWN_SEC:
                log.debug(f"[POLY_COPY] Skipping {dedup_key} — in cooldown ({(time.time()-last_copied):.0f}s ago)")
                continue

            # ── Step 2: Resolve market question to get asset ────────────────
            market_info = await client.get_market_info(condition_id)
            if not market_info:
                continue

            question = market_info.get("question", "")
            asset = _identify_asset(question)
            if not asset or asset not in COPY_ASSETS:
                log.debug(f"[POLY_COPY] Skipping unrecognized asset in: '{question[:60]}'")
                continue

            # ── Step 3: Find matching Bayse market ──────────────────────────
            # We only target 5min and 15min Bayse markets for this asset
            matching_markets = [
                m for m in active_markets
                if m.get("asset") == asset
                and m.get("timeframe") in COPY_TIMEFRAMES
                and m.get("status") == "open"
                and m.get("secs_to_close", 0) > 90  # need enough time to enter
            ]

            if not matching_markets:
                log.debug(f"[POLY_COPY] No matching 5min/15min Bayse market for {asset}")
                continue

            # ── Step 4: Determine direction ─────────────────────────────────
            # Polymarket YES position → we buy YES on Bayse
            # Polymarket NO position → we buy NO on Bayse
            # Use Polymarket price as certainty signal:
            #   poly_price=0.75 → strong conviction → certainty 0.75
            #   poly_price=0.55 → moderate → certainty 0.58 (slight premium)
            certainty = min(poly_price + 0.08, 0.92)  # add 8% conviction premium
            win_prob = poly_price

            # For each matching Bayse market, generate a signal
            for bm in matching_markets[:2]:  # max 2 markets per asset
                if outcome_label == "YES":
                    outcome = "YES"
                    outcome_id = bm.get("yes_id", "")
                    market_price = bm.get("yes_price", 0.5)
                else:
                    outcome = "NO"
                    outcome_id = bm.get("no_id", "")
                    market_price = bm.get("no_price", 0.5)

                if market_price <= 0 or market_price >= 0.95:
                    continue  # skip fully-resolved Bayse markets

                sig = TradeSignal(
                    strategy="POLY_COPY",
                    event_id=bm["event_id"],
                    market_id=bm["market_id"],
                    asset=asset,
                    timeframe=bm["timeframe"],
                    outcome=outcome,
                    outcome_id=outcome_id,
                    certainty=certainty,
                    win_prob=win_prob,
                    market_price=market_price,
                    size_pct=0.02,  # default standard sizing
                    reason=(
                        f"Poly copy: {question[:50]} | "
                        f"Poly price={poly_price:.2f} | "
                        f"Size=${size_usd:.0f}"
                    ),
                    title=bm.get("title", "")
                )
                signals.append(sig)
                # Mark successfully generated signal cooldown immediately
                _copied_positions[dedup_key] = time.time()

        if signals:
            log.info(
                f"[POLY_COPY] Generated {len(signals)} mirror signal(s): "
                f"{[f'{s.asset} {s.timeframe} {s.outcome}' for s in signals]}"
            )

    finally:
        await client.close()

    return signals
