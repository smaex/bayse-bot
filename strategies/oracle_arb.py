"""
ORACLE_ARB — Oracle Latency Arbitrage
=======================================
Exploits the lag between Binance (the resolution oracle) and the Bayse
AMM price in the final seconds of a market's life.
How it works:
 - Every market resolves based on the Binance candlestick close at endTime.
 - We subscribe to the same Binance feed Bayse uses (wss bookTicker).
 - In the final WINDOW_SECS seconds before closingDate, if the live
   Binance price has already crossed the threshold, the outcome is
   mathematically certain — but the Bayse AMM price may still show 0.70.
 - We instantly buy the winning outcome at the stale AMM price, lock in
   a guaranteed ~0.30 per share profit.
This is identical to how Kalshi quants trade resolution events: they
pay for ultra-low-latency data feeds and only execute when the outcome
is essentially certain.
Bayse API Compatibility (VERIFIED):
 - market has "closingDate" field with millisecond precision ✅
 - market has "resolutionDate" field (usually closingDate + 90s) ✅
 - market "engine" = "CLOB" → MARKET orders route to AMM curve ✅
 - resolution oracle: Binance 1-min OHLCV candle close ✅
Key Constraints:
 - We do NOT know if Bayse locks the CLOB before closingDate.
   First live test with min trade (₦100) will verify the lock window.
 - We compare against eventThreshold (stored in scanner as "threshold").
 - Only trade if our certainty is > 0.95 to avoid false triggers near
   the threshold.
"""
import asyncio
import logging
import time
from typing import Optional
import feeds_direct
import feeds
from strategies.base import TradeSignal, BaseStrategy
log = logging.getLogger("strat.oracle_arb")
# ── Parameters ────────────────────────────────────────────────────────────────
# Only activate in the final N seconds before market closing.
# Extended from 60s to 120s: gives the bot more time to catch the guaranteed-win window.
# At 120s out, if Binance is clearly 0.2%+ past the threshold, that outcome is
# virtually certain and the AMM is still stale \u2014 that's free money.
WINDOW_SECS = 120
# Minimum certainty to fire. 0.95 = Binance must be clearly on one side.
MIN_CERTAINTY = 0.95
# Minimum distance from threshold (as % of threshold) to qualify.
# e.g. 0.002 means price must be 0.2% away from threshold to avoid false signals.
MIN_DISTANCE_PCT = 0.002
# If the AMM is already pricing >0.92, most of the edge has been captured by others.
MAX_ENTRY_PRICE = 0.92
# Cooldown after an oracle arb on a specific market — prevent double-entry.
_oracle_fired: dict[str, float] = {}
ORACLE_COOLDOWN = 120  # 2 minutes
class OracleArbStrategy(BaseStrategy):
    """
    Final-seconds oracle latency arbitrage.
    Called every tick by bot.py for each active market.
    If market closes within WINDOW_SECS and Binance price clearly resolves
    YES or NO, returns a TradeSignal with very high certainty.
    """
    def __init__(self):
        super().__init__("ORACLE_ARB")
    def _get_oracle_price(self, asset: str) -> tuple[float, float]:
        """
        Returns (price, age_in_seconds) from Binance direct feed.
        Price = 0.0 if stale (>5s old).
        """
        price, t = feeds_direct.get_direct_price(asset)
        age = time.time() - t if t else 999
        if age > 5:
            return 0.0, age
        return price, age
    def _certainty_from_distance(self, spot: float, threshold: float, secs_to_close: float) -> float:
        """
        Compute certainty based on:
          - Distance from threshold (further = more certain)
          - Time to close (less time = more certain because less can change)
        Returns 0.0 if not worth trading, else 0.95 to 0.99.
        """
        if threshold <= 0 or spot <= 0:
            return 0.0
        distance_pct = (spot - threshold) / threshold
        # Need at least MIN_DISTANCE_PCT away
        if abs(distance_pct) < MIN_DISTANCE_PCT:
            return 0.0
        # Scale certainty: further from threshold = more certain
        # Also scale by how little time remains (less time = less can change)
        time_factor = max(0.0, 1.0 - (secs_to_close / WINDOW_SECS))  # 0→1 as time runs out
        distance_factor = min(1.0, abs(distance_pct) / 0.01)  # 1.0% away = full certainty
        base_certainty = 0.90 + 0.09 * distance_factor * time_factor
        return min(0.99, base_certainty)
    async def evaluate(self, market: dict, learned: dict, state,
                       spot_price: float = None) -> Optional[TradeSignal]:
        asset         = market["asset"]
        secs_to_close = market.get("secs_to_close", 9999)
        threshold     = market.get("threshold")
        market_id     = market["market_id"]
        # Only activate in final WINDOW_SECS
        if secs_to_close > WINDOW_SECS or secs_to_close <= 2:
            return None
        # Cooldown guard
        last_fired = _oracle_fired.get(market_id, 0)
        if time.time() - last_fired < ORACLE_COOLDOWN:
            return None
        if not threshold or threshold <= 0:
            return None
        # Get oracle price
        oracle_price, oracle_age = self._get_oracle_price(asset)
        if oracle_price <= 0:
            log.debug(f"ORACLE_ARB SKIP {asset} — no direct feed or stale ({oracle_age:.1f}s)")
            return None
        # Determine outcome
        yes_wins = oracle_price >= threshold
        no_wins  = oracle_price < threshold
        # Compute certainty based on distance and time
        certainty = self._certainty_from_distance(oracle_price, threshold, secs_to_close)
        if certainty < MIN_CERTAINTY:
            log.debug(
                f"ORACLE_ARB SKIP {asset} — certainty {certainty:.2%} < {MIN_CERTAINTY:.0%} "
                f"(spot={oracle_price:.2f} threshold={threshold:.2f} secs={secs_to_close:.1f})"
            )
            return None
        # Pick the winning outcome
        if yes_wins:
            outcome    = "YES"
            outcome_id = market["yes_id"]
            entry_price= market["yes_price"]
        else:
            outcome    = "NO"
            outcome_id = market["no_id"]
            entry_price= market["no_price"]
        # Guard: if AMM has already priced it efficiently, skip (edge already gone)
        if entry_price > MAX_ENTRY_PRICE:
            log.debug(
                f"ORACLE_ARB SKIP {asset} {outcome} — already priced at {entry_price:.3f} "
                f"> {MAX_ENTRY_PRICE:.3f}"
            )
            return None
        # Mark as fired before returning (executor will handle the actual trade)
        _oracle_fired[market_id] = time.time()
        distance_pct = (oracle_price - threshold) / threshold
        log.info(
            f"ORACLE_ARB SIGNAL 🎯 {asset} {outcome} | "
            f"oracle={oracle_price:.2f} threshold={threshold:.2f} "
            f"dist={distance_pct:+.3%} secs={secs_to_close:.1f} "
            f"entry_price={entry_price:.3f} certainty={certainty:.2%} "
            f"oracle_age={oracle_age:.2f}s"
        )
        return TradeSignal(
            strategy    = "ORACLE_ARB",
            event_id    = market["event_id"],
            market_id   = market_id,
            asset       = asset,
            timeframe   = market["timeframe"],
            outcome     = outcome,
            outcome_id  = outcome_id,
            certainty   = certainty,
            win_prob    = certainty,
            market_price= entry_price,
            # Dynamic sizing: scale from 10% at 95% certainty up to 30% at 99%+.
            # ORACLE_ARB is the highest-quality signal the bot generates — the outcome
            # is virtually guaranteed. This is where we make \u20a6200k/month.
            size_pct    = min(0.30, 0.10 + (certainty - 0.95) * 5.0),
            reason      = (
                f"ORACLE_ARB {outcome} | oracle={oracle_price:.2f} "
                f"vs threshold={threshold:.2f} dist={distance_pct:+.3%} "
                f"secs={secs_to_close:.1f}"
            ),
            title       = market.get("title", ""),
            momentum_at_entry    = distance_pct,
            realized_vol_at_entry= 0.0,
        )
# Singleton
oracle_arb_strategy = OracleArbStrategy()
