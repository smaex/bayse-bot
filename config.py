import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # run: pip install -r requirements.txt

# ── Server-level credentials (shared across all users) ────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
ENCRYPTION_KEY  = os.getenv("ENCRYPTION_KEY", "")   # Fernet key — see .env.example

# ── API ───────────────────────────────────────────────────────────────────────
BASE_URL = "https://relay.bayse.markets"
WS_MARKETS_URL = "wss://socket.bayse.markets/ws/v1/markets"
WS_REALTIME_URL = "wss://socket.bayse.markets/ws/v1/realtime"

# ── Oracle Price Feeds ────────────────────────────────────────────────────────
# All spot prices come from the Bayse realtime WebSocket (wss://socket.bayse.markets/ws/v1/realtime)
# Crypto: sourced from Binance  |  FX + Gold: sourced from TwelveData

# ── Market Series Slugs ────────────────────────────────────────────────────────
SERIES = {
    # ── Crypto ────────────────────────────────────────────────────────────────
    "BTC": {
        "5min":  "crypto-btc-5min",
        "15min": "crypto-btc-15min",
        "1h":    "crypto-btc-1h",
        "6h":    "crypto-btc-6h",
        "1d":    "crypto-btc-1d",
    },
    "ETH": {
        "5min":  "crypto-eth-5min",
        "15min": "crypto-eth-15min",
        "1h":    "crypto-eth-1h",
        "6h":    "crypto-eth-6h",
        "1d":    "crypto-eth-1d",
    },
    "SOL": {
        "5min":  "crypto-sol-5min",
        "15min": "crypto-sol-15min",
        "1h":    "crypto-sol-1h",
        "6h":    "crypto-sol-6h",
        "1d":    "crypto-sol-1d",
    },
    "BNB": {
        "5min":  "crypto-bnb-5min",
        "15min": "crypto-bnb-15min",
        "1h":    "crypto-bnb-1h",
        "6h":    "crypto-bnb-6h",
        "1d":    "crypto-bnb-1d",
    },
    # ── FX (confirmed slugs from live API — 1h only as of 2026-05-09) ─────────
    "EURUSD": {"1h": "fx-eurusd-1h"},
    "GBPUSD": {"1h": "fx-gbpusd-1h"},
    "USDJPY": {"1h": "fx-usdjpy-1h"},
    "EURJPY": {"1h": "fx-eurjpy-1h"},
    "GBPJPY": {"1h": "fx-gbpjpy-1h"},
    "EURGBP": {"1h": "fx-eurgbp-1h"},   # derived price: EURUSD / GBPUSD
    # ── Commodities ────────────────────────────────────────────────────────────
    "XAUUSD": {"1h": "commodity-xauusd-1h"},
}

# Which oracle each asset uses for resolution
ASSET_ORACLE = {
    "BTC": "BINANCE", "ETH": "BINANCE", "SOL": "BINANCE", "BNB": "BINANCE",
    "EURUSD": "TWELVEDATA", "GBPUSD": "TWELVEDATA", "USDJPY": "TWELVEDATA",
    "EURJPY": "TWELVEDATA", "GBPJPY": "TWELVEDATA", "EURGBP": "TWELVEDATA", 
    "XAUUSD": "TWELVEDATA",
}

# News & sentiment
NEWSAPI_KEY   = os.getenv("NEWSAPI_KEY", "")  # free at newsapi.org
NEWS_POLL_SEC = 900       # poll every 15 minutes — free tier allows 100 requests/day
NEWS_SENTIMENT_THRESHOLD = 0.80  # only near-unanimous sentiment creates a signal
NEWS_SIGNAL_DECAY_MIN = 3        # news signal expires after 3 min — stale news is dangerous

# NEWS strategy certainty dampening — VADER scores are NOT win probabilities.
# A raw VADER compound of 0.80 × 0.55 = effective certainty of 0.44 → win_prob ≈ 70%.
# Without dampening, 0.80 → certainty 0.80 → win_prob 86% which is wildly overconfident.
NEWS_CERTAINTY_DAMPEN = 0.55     # multiply VADER strength by this before using as certainty
NEWS_MAX_MARKET_PRICE  = 0.55    # reject if market already repriced past this (guarantees ~80% profit margin)
NEWS_MIN_REGIME        = 0.25    # reject in choppy markets (news shock gets absorbed)
NEWS_MIN_SECS_LEFT     = 120     # need at least 2 min for news to play out
NEWS_KELLY_FRACTION    = 0.12    # conservative sizing for sentiment-based signals
NEWS_REQUIRE_CRYPTO_CATALYST = True  # only trade on headlines with direct crypto catalysts

# Economic calendar events (UTC times — add to this list as needed)
FOMC_DATES_2026 = [
    "2026-01-28T19:00:00Z", "2026-03-18T18:00:00Z", "2026-05-06T18:00:00Z",
    "2026-06-10T18:00:00Z", "2026-07-29T18:00:00Z", "2026-09-16T18:00:00Z",
    "2026-11-04T19:00:00Z", "2026-12-16T19:00:00Z",
]
# CPI drops at 8:30 AM ET = 12:30 UTC (approx monthly)
CPI_ADVANCE_MINUTES = 2  # enter position 2 min before scheduled release

# ── Trading ───────────────────────────────────────────────────────────────────
CURRENCY = "NGN"

# ── All possible assets / timeframes (superset — per-user settings stored in DB) ─
ALL_ASSETS     = [
    "BTC", "ETH", "SOL", "BNB",
    "EURUSD", "GBPUSD", "USDJPY", "EURJPY", "GBPJPY", "EURGBP",
    "XAUUSD"
]
ALL_TIMEFRAMES = ["5min", "15min", "1h", "6h", "1d"]

# ── Sniping ───────────────────────────────────────────────────────────────────
# Per-timeframe entry windows: each timeframe has its own optimal entry point.
# Short timeframes move fast — enter late when signal is strong.
# Long timeframes need early entry to catch markets before they fully price in.
SNIPE_ENTRY_WINDOWS = {
    "5min":  240,    # last 4 min — BTC 1% above threshold gives ~98% win prob here
    "15min": 600,    # last 10 min — good balance of certainty vs market price
    "1h":    1800,   # last 30 min — catches 1h markets while still at 0.60-0.75
    "6h":    7200,   # last 2 hours
    "1d":    21600,  # last 6 hours
}

# DO NOT buy if the price is already this high.
# Prevents buying "certain wins" that actually lose money after fees (e.g. 0.98 price).
SNIPE_MAX_MARKET_PRICE = 0.80

# Asset hourly volatility (1σ, fractional) — used in diffusion model for win probability.
# P(win) = Φ( |spot_distance| / (σ_h × √T_hours) )  ← same math as options pricing
ASSET_HOURLY_VOL = {
    # Crypto
    "BTC":    0.018,   # ~1.8% per hour
    "ETH":    0.022,   # ~2.2% per hour
    "SOL":    0.028,   # ~2.8% per hour
    "BNB":    0.025,   # ~2.5% per hour
    # FX
    "EURUSD": 0.0006,  # ~0.06% per hour
    "GBPUSD": 0.0007,  # ~0.07% per hour
    "USDJPY": 0.0007,  # ~0.07% per hour
    "EURJPY": 0.0006,  # ~0.06% per hour
    "GBPJPY": 0.0008,  # ~0.08% per hour
    "EURGBP": 0.0004,  # ~0.04% per hour
    # Commodities
    "XAUUSD": 0.0015,  # ~0.15% per hour
}

# ── Tiered Certainty (Adaptive Frequency) ───────────────────────────────────
SNIPE_MIN_CERTAINTY        = 0.55   # global default
SNIPE_MIN_CERTAINTY_CRYPTO = 0.55   # high conviction for volatile crypto
SNIPE_MIN_CERTAINTY_FX     = 0.45   # allow higher frequency for stable FX

# ── FX-specific trading rules ─────────────────────────────────────────────────
# Only trade FX/Gold during their active market sessions (UTC).
# Outside these windows, vol is dominated by noise — false breakouts are common.
FX_SESSION_UTC = {
    "EURUSD": (6, 17),   # London open → NY close
    "GBPUSD": (6, 17),
    "USDJPY": (0, 24),   # Tokyo overlap; 24h liquidity is generally fine
    "EURJPY": (0, 24),
    "GBPJPY": (0, 24),
    "EURGBP": (6, 17),
    "XAUUSD": (8, 20),   # London overlap → NY afternoon
}

# Minimum distance from threshold before entering an FX trade.
# Set to 1× hourly σ per asset — ensures we have a genuine move, not noise.
FX_MIN_DISTANCE = {
    "EURUSD": 0.0006,  # 0.06% — 1σ/hr
    "GBPUSD": 0.0007,  # 0.07%
    "USDJPY": 0.0007,
    "EURJPY": 0.0006,
    "GBPJPY": 0.0008,
    "EURGBP": 0.0004,  # 0.04%
    "XAUUSD": 0.0015,  # 0.15%
}

# Minimum distance for Crypto SNIPE entries to protect against Pin Risk (jumps near expiration).
CRYPTO_MIN_DISTANCE = {
    "BTC": 0.0010,  # 0.10%
    "ETH": 0.0015,  # 0.15%
    "SOL": 0.0020,  # 0.20%
    "BNB": 0.0015,
}

# Velocity Guard: Reject if price is crashing toward the threshold too fast.
# Measures distance change over the last 60 seconds.
SNIPE_VELOCITY_WINDOW = 60
SNIPE_VELOCITY_VETO   = 0.40  # reject if 40% of the safety gap is closed in 60s

# FX requires a cleaner trend than crypto — minimum efficiency ratio.
FX_MIN_REGIME = 0.30   # below this = too choppy to trade FX reliably

# FX entry window: last 20 min of the hour (crypto 1h uses 30 min).
# More elapsed time = more confirmation the move is real.
FX_ENTRY_WINDOW_1H = 1200  # seconds (20 min)

# FX distance trend: reject if price has converged back toward the threshold
# by more than 1× the minimum distance over the last 10 minutes.
# Positive trend = move is holding. Negative = price reversing.
FX_TREND_VETO_MULT = 1.0   # reject if 10-min convergence > FX_MIN_DISTANCE × this

# ── Correlation Signal ─────────────────────────────────────────────────────────
CORRELATION_THRESHOLD = 0.015  # BTC spot must move ≥1.5% — spot-based, much more reactive
CORRELATION_WINDOW_SEC = 180   # signal valid for 3 minutes (edge evaporates fast)

# CORRELATE strategy guards — the target asset may have already followed BTC
CORRELATE_BASE_CERTAINTY   = 0.40   # base certainty (was 0.60 — too high, caused losses)
CORRELATE_ALREADY_MOVED    = 0.50   # reject if target moved > 50% of BTC's move
CORRELATE_MAX_MARKET_PRICE = 0.55   # reject if market already repriced past this (guarantees ~80% profit margin)
CORRELATE_MIN_REGIME       = 0.25   # reject choppy target assets

# ── Arbitrage (Mint/Burn) ─────────────────────────────────────────────────────
ARB_TRIGGER = 0.94           # enter burn arb when YES+NO sum ≤ this (demands wider 6% spread for safety)
ARB_MAX_SIZE_NGN = 50_000    # max per arb trade

# ── Risk Management ───────────────────────────────────────────────────────────
BANKROLL_PCT_PER_TRADE = 0.02  # 2% of bankroll per trade (was 3% — too aggressive)
MAX_PORTFOLIO_EXPOSURE = 0.20  # never have >20% of bankroll in open positions (was 30%)
MAX_DRAWDOWN_STOP = 0.15       # pause all trading at 15% drawdown (was 20%)

# ── Infra Guard (Data Staleness & Mispricing) ──────────────────────────────────
# Prevents trading on stale or mispriced relay feeds by comparing to Ground Truth.
INFRA_STALE_LAG_SEC    = 45.0   # > 45s lag = Hard Block
INFRA_DEGRADED_LAG_SEC = 15.0   # > 15s lag = 0.1% Safety Spread
INFRA_STALE_DIFF_PCT   = 0.0020 # > 0.2% price diff = Hard Block
INFRA_DEGRADED_DIFF_PCT = 0.0008 # > 0.08% price diff = 0.1% Safety Spread

# Systemic Risk: If 3+ assets spike >50% above baseline vol, it's a global shock.
# Halt all new entries for 60 minutes to let the market settle.
SYSTEMIC_RISK_COUNT_THRESHOLD = 3
SYSTEMIC_RISK_VOL_MULT        = 1.5
SYSTEMIC_RISK_HALT_MINS       = 60
VOL_SPIKE_THRESHOLD           = 3.0   # trigger halt if variance acceleration > 3.0
DYNAMIC_KELLY_MIN             = 0.05  # floor for kelly sizing in stressed markets
DYNAMIC_KELLY_MAX             = 0.40  # ceiling for kelly sizing in ideal markets

# Minimum Net Payout: Ensure for every 100 spent, we get at least 115 back (15% net profit).
# This prevents the "risk 100 to win 5" trades that wipe out bankrolls.
MIN_PAYOUT_RATIO = 0.15
PROFIT_ALERT_NGN = 20_000      # Telegram alert when unrealized profit hits this

# ── Rate Limiting (stay well under 20 write/sec, 30 read/sec) ─────────────────
WRITE_RATE_LIMIT = 15          # max write requests/second (buffer below 20)
READ_RATE_LIMIT = 25           # max read requests/second (buffer below 30)
SCAN_INTERVAL_SECONDS = 60     # re-scan for new markets every 60s
