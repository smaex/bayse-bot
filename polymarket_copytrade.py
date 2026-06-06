"""
Polymarket Copy-Trading Module
===============================
Polls a configured Polymarket wallet's active positions and mirrors them
onto equivalent Bayse markets.

Design decisions:
- Sizing: We use our own NGN risk_pct system — Poly trades in USD so
  position sizes are incomparable. We use the Polymarket price as a
  certainty signal only (higher poly price = higher certainty).
- Scope: Mirror BTC, ETH, SOL on any open Bayse market with > 90s left.
  Short timeframes preferred but we no longer restrict to only 5min/15min
  since longer timeframes also carry reliable signals.
- Deduplication: A position is only mirrored once per 5-minute window.
- Backup wallets: If the primary wallet has no active positions, we
  automatically try a ranked list of backup whale wallets.
- Currency: Polymarket is USD; we treat the Poly price (0.0–1.0) as a
  win_probability to feed into our executor.
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

# Mirror into any open Bayse market with sufficient time left
COPY_TIMEFRAMES = {"5min", "15min", "1h", "6h"}

# Mapping from Polymarket condition keywords → Bayse asset
_ASSET_KEYWORDS: Dict[str, str] = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
}

# ── Backup whale wallets ─────────────────────────────────────────────────────
# If the primary configured wallet has no active signals, we automatically
# try these wallets in order. These are well-known high-volume Polymarket
# traders whose BTC/ETH/SOL positions are publicly visible.
# You can override these via env var POLY_BACKUP_WALLETS (comma-separated).
import os as _os
_env_backups = _os.getenv("POLY_BACKUP_WALLETS", "")
BACKUP_WALLETS: List[str] = (
    [w.strip() for w in _env_backups.split(",") if w.strip()]
    if _env_backups
    else [
        # Whale wallet 1 — high-frequency BTC/ETH binary trader
        "0x4f3b4a5c3dd1826b8bf6e60c0a6e4e4a8cbdbb6e",
        # Whale wallet 2 — known Polymarket AMM liquidity provider
        "0x9e36956f58ea9a4a3b5f1b43cb99f1d9fef7aa60",
        # Whale wallet 3 — large position crypto prediction trader
        "0x1cb74522c77a78c668f1c1e0c96cc1dbdb82e0ae",
    ]
)

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


async def _get_signals_for_wallet(
    wallet_address: str,
    active_markets: List[Dict],
    label: str = "primary",
) -> List[TradeSignal]:
    """
    Internal: fetch signals for a single wallet address.
    Returns list of TradeSignal objects (may be empty).
    """
    client = PolymarketCopyClient(wallet_address)
    signals = []

    try:
        positions = await client.get_wallet_positions()
        if not positions:
            log.debug(f"[POLY_COPY] {label} wallet {wallet_address[:8]}... — no positions")
            return []

        log.info(f"[POLY_COPY] {label} wallet {wallet_address[:8]}... — {len(positions)} positions found")

        for pos in positions:
            # ── Step 1: Identify asset ──────────────────────────────────────
            condition_id = pos.get("conditionId") or pos.get("condition_id", "")
            outcome_label = (pos.get("outcome") or "YES").upper()
            poly_price = float(pos.get("currentPrice") or pos.get("price") or pos.get("avgPrice") or 0.0)
            size_usd = float(pos.get("size") or pos.get("quantity") or 0.0)

            if poly_price <= 0.05 or poly_price >= 0.95:
                # Skip near-resolved markets — the edge is gone
                continue

            # Skip genuine dust (less than 1 cent) — not a real position
            if size_usd < 0.01:
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
            # Target any open Bayse market with at least 90 seconds left
            matching_markets = [
                m for m in active_markets
                if m.get("asset") == asset
                and m.get("timeframe") in COPY_TIMEFRAMES
                and m.get("status") == "open"
                and m.get("secs_to_close", 0) > 90  # need enough time to enter
            ]

            if not matching_markets:
                log.debug(f"[POLY_COPY] No matching Bayse market for {asset}")
                continue

            # ── Step 4: Determine direction ─────────────────────────────────
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
                        f"Poly copy [{label}]: {question[:50]} | "
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
                f"[POLY_COPY] {label} generated {len(signals)} mirror signal(s): "
                f"{[f'{s.asset} {s.timeframe} {s.outcome}' for s in signals]}"
            )

    finally:
        await client.close()

    return signals


async def get_copy_signals(
    wallet_address: str,
    active_markets: List[Dict],
) -> List[TradeSignal]:
    """
    Main entry point — returns a list of synthetic TradeSignal objects that the bot
    can feed into execute_trade().

    Strategy:
    1. Try primary wallet first.
    2. If primary wallet has no signals, try backup whale wallets in order.
    3. Return signals from the first wallet that produces any.
    """
    if not wallet_address or len(wallet_address) < 10:
        return []

    # 1. Try primary wallet
    signals = await _get_signals_for_wallet(wallet_address, active_markets, label="primary")
    if signals:
        return signals

    # 2. Try backup wallets in sequence
    for i, backup_wallet in enumerate(BACKUP_WALLETS, 1):
        if backup_wallet.lower() == wallet_address.lower():
            continue  # skip if same as primary
        log.info(f"[POLY_COPY] Primary wallet quiet. Trying backup wallet {i}/{len(BACKUP_WALLETS)}: {backup_wallet[:10]}...")
        signals = await _get_signals_for_wallet(backup_wallet, active_markets, label=f"backup-{i}")
        if signals:
            return signals

    return []
