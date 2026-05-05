"""
News & sentiment engine.

Sources:
  - NewsAPI.org (free tier: 100 requests/day, polled every 15 minutes)
  - Economic calendar (FOMC/CPI scheduled events)

Signals:
  - BULLISH  → buy UP/YES on BTC/ETH/SOL
  - BEARISH  → buy DOWN/NO on BTC/ETH/SOL
  - NEUTRAL  → no trade

Reaction windows (from research):
  - Breaking crypto news : 10–20 min repricing window
  - FOMC / CPI          :  2–5  min repricing window
  - Geopolitical        : 15–60 min repricing window
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import aiohttp
from config import (
    NEWSAPI_KEY, NEWS_POLL_SEC,
    NEWS_SENTIMENT_THRESHOLD, NEWS_SIGNAL_DECAY_MIN,
    FOMC_DATES_2026, NEWS_REQUIRE_CRYPTO_CATALYST,
)

log = logging.getLogger(__name__)

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
    _vader_ok = True
except ImportError:
    _vader_ok = False
    log.warning("vaderSentiment not installed — sentiment scoring disabled")

# Direct crypto catalyst keywords — headlines must match at least one to trigger a trade.
# Geopolitical noise (Iran, war, tariffs) scores high on VADER but has no reliable
# crypto price correlation.  We still NOTIFY users about those headlines but don't trade.
_CRYPTO_CATALYSTS = {
    # Regulatory / legal
    "etf", "sec", "cftc", "regulation", "regulatory", "ban", "lawsuit", "legal",
    "approval", "approved", "reject", "rejected", "compliance", "enforcement",
    # Market structure
    "exchange", "binance", "coinbase", "kraken", "ftx", "hack", "hacked", "exploit",
    "breach", "insolvent", "insolvency", "bankrupt", "bankruptcy", "delisted", "delist",
    "liquidity", "liquidation", "liquidated", "whale", "dump", "dumped",
    # Monetary policy (direct crypto impact)
    "fed", "federal reserve", "fomc", "rate cut", "rate hike", "interest rate",
    "inflation", "cpi", "monetary", "quantitative", "tightening", "easing",
    "treasury", "yield", "bond",
    # Crypto-specific events
    "halving", "halvening", "fork", "upgrade", "merge", "staking", "unstaking",
    "airdrop", "token", "defi", "nft", "layer 2", "l2", "rollup", "bridge",
    "stablecoin", "tether", "usdt", "usdc", "depeg", "peg",
    # Adoption / institutional
    "adoption", "institutional", "microstrategy", "saylor", "tesla", "payment",
    "custody", "etf approval", "spot etf", "blackrock", "fidelity", "grayscale",
    # Direct asset mentions (redundant with _CRYPTO_KEYWORDS but explicit for catalyst check)
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "cryptocurrency",
}


@dataclass
class NewsSignal:
    direction: str        # "BULLISH" or "BEARISH"
    assets: list[str]    # ["BTC", "ETH", "SOL"] or specific assets
    score: float          # absolute sentiment score 0–1
    source: str           # e.g. "CryptoPanic", "FOMC", "CPI"
    headline: str
    timestamp: float = field(default_factory=time.time)
    decay_minutes: int = NEWS_SIGNAL_DECAY_MIN
    _notify_only: bool = False

    def is_alive(self) -> bool:
        return (time.time() - self.timestamp) < self.decay_minutes * 60

    def strength(self) -> float:
        """Score decays linearly to 0 over decay_minutes."""
        age_frac = (time.time() - self.timestamp) / (self.decay_minutes * 60)
        return self.score * max(0.0, 1.0 - age_frac)


# Current live signals — read by strategy.py
active_signals: list[NewsSignal] = []


def _score(text: str) -> float:
    """VADER compound score, -1 to +1. Sign = direction, magnitude = confidence."""
    if not _vader_ok:
        return 0.0
    return _vader.polarity_scores(text)["compound"]


def _assets_from_text(text: str) -> list[str]:
    text_lower = text.lower()
    found = []
    if any(w in text_lower for w in ["bitcoin", "btc"]):
        found.append("BTC")
    if any(w in text_lower for w in ["ethereum", "eth"]):
        found.append("ETH")
    if any(w in text_lower for w in ["solana", "sol"]):
        found.append("SOL")
    return found or ["BTC", "ETH", "SOL"]  # generic crypto news → all assets


def _has_crypto_catalyst(text: str) -> bool:
    """Check if headline contains a direct crypto catalyst keyword."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in _CRYPTO_CATALYSTS)


def _push_signal(signal: NewsSignal):
    # Always log the signal for user notifications
    log.info(
        f"News signal [{signal.direction}] score={signal.score:.2f} "
        f"src={signal.source}: {signal.headline[:80]}"
    )

    # Gate: only create a TRADING signal if it has a crypto catalyst
    if NEWS_REQUIRE_CRYPTO_CATALYST and not _has_crypto_catalyst(signal.headline):
        log.info(
            f"News [{signal.direction}] NOTIFY-ONLY — no crypto catalyst: "
            f"{signal.headline[:60]}"
        )
        # Still add to active_signals so users get Telegram notifications,
        # but mark it as non-tradeable
        signal._notify_only = True

    active_signals.append(signal)
    # Keep only live signals
    active_signals[:] = [s for s in active_signals if s.is_alive()]


def best_signal_for(asset: str) -> Optional[NewsSignal]:
    """Return strongest live signal relevant to this asset."""
    live = [s for s in active_signals if s.is_alive() and asset in s.assets and not getattr(s, "_notify_only", False)]
    return max(live, key=lambda s: s.strength(), default=None)


def active_signals_for(asset: str) -> list[NewsSignal]:
    """Return all live signals relevant to this asset."""
    return [s for s in active_signals if s.is_alive() and asset in s.assets and not getattr(s, "_notify_only", False)]


# ── NewsAPI.org feed ──────────────────────────────────────────────────────────

async def newsapi_feed():
    """
    Poll NewsAPI.org for crypto news every NEWS_POLL_SEC seconds.
    Free tier: 100 requests/day → poll every 15 minutes (96/day).
    Scores headlines + descriptions with VADER sentiment.
    """
    if not NEWSAPI_KEY:
        log.info("No NEWSAPI_KEY set — news feed disabled")
        return

    seen_urls: set = set()
    url = "https://newsapi.org/v2/everything"
    # Pin to known crypto outlets so off-topic articles never reach the signal engine.
    _CRYPTO_DOMAINS = (
        "coindesk.com,cointelegraph.com,decrypt.co,theblock.co,"
        "bitcoinist.com,cryptoslate.com,cryptobriefing.com,ambcrypto.com"
    )
    _CRYPTO_KEYWORDS = {
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
        "crypto", "blockchain", "defi", "altcoin", "stablecoin",
        "nft", "web3", "binance", "coinbase", "token", "wallet",
    }
    params = {
        "q": "bitcoin OR ethereum OR solana OR crypto",
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 20,
        "domains": _CRYPTO_DOMAINS,
        "apiKey": NEWSAPI_KEY,
    }

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data.get("status") != "ok":
                            log.warning(f"NewsAPI error response: {data.get('code')} {data.get('message')}")
                        for article in data.get("articles", []):
                            article_url = article.get("url", "")
                            if article_url in seen_urls:
                                continue
                            seen_urls.add(article_url)

                            title = article.get("title") or ""
                            desc  = article.get("description") or ""
                            text  = f"{title}. {desc}".strip()

                            # Hard gate: article must mention a crypto keyword
                            words = set(text.lower().split())
                            if not words & _CRYPTO_KEYWORDS:
                                continue

                            assets   = _assets_from_text(text)
                            compound = _score(text)

                            if compound > NEWS_SENTIMENT_THRESHOLD:
                                _push_signal(NewsSignal(
                                    direction="BULLISH", assets=assets,
                                    score=abs(compound), source="NewsAPI",
                                    headline=title,
                                ))
                            elif compound < -NEWS_SENTIMENT_THRESHOLD:
                                _push_signal(NewsSignal(
                                    direction="BEARISH", assets=assets,
                                    score=abs(compound), source="NewsAPI",
                                    headline=title,
                                ))
                    elif r.status == 429:
                        log.warning("NewsAPI rate limit hit — waiting 1 hour")
                        await asyncio.sleep(3600)
                    else:
                        log.debug(f"NewsAPI returned {r.status}")
        except Exception as e:
            log.warning(f"NewsAPI fetch error: {e}")

        await asyncio.sleep(NEWS_POLL_SEC)


# ── Economic calendar ────────────────────────────────────────────────────────

async def calendar_monitor():
    """
    Watch for scheduled macro events (FOMC, CPI) and fire a pre-event signal.
    We don't know the outcome in advance, so we use this to WIDEN spreads
    and reduce size rather than take a directional position — unless the
    release lands and we can score it.
    """
    log.info("Economic calendar monitor started")
    while True:
        try:
            now = datetime.now(timezone.utc)
            for date_str in FOMC_DATES_2026:
                event_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                diff_min = (event_dt - now).total_seconds() / 60
                if 0 < diff_min <= 5:
                    log.warning(
                        f"FOMC decision in {diff_min:.1f} min — "
                        "reducing trade sizes until signal direction confirmed"
                    )
        except Exception as e:
            log.warning(f"Calendar monitor error: {e}")
        await asyncio.sleep(60)


async def start_news_feeds():
    await asyncio.gather(
        newsapi_feed(),
        calendar_monitor(),
    )
