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
    # ── FX (confirmed slugs from live API — 1h only as of 2026-04-28) ─────────
    "EURUSD": {"1h": "fx-eurusd-1h"},
    "GBPUSD": {"1h": "fx-gbpusd-1h"},
    "EURGBP": {"1h": "fx-eurgbp-1h"},   # derived price: EURUSD / GBPUSD
    # ── Commodities ────────────────────────────────────────────────────────────
    "XAUUSD": {"1h": "commodity-xauusd-1h"},
}

# Which oracle each asset uses for resolution
ASSET_ORACLE = {
    "BTC": "BINANCE", "ETH": "BINANCE", "SOL": "BINANCE",
    "EURUSD": "TWELVEDATA", "GBPUSD": "TWELVEDATA",
    "EURGBP": "TWELVEDATA", "XAUUSD": "TWELVEDATA",
}

# News & sentiment
NEWSAPI_KEY   = os.getenv("NEWSAPI_KEY", "")  # free at newsapi.org
NEWS_POLL_SEC = 900       # poll every 15 minutes — free tier allows 100 requests/day
NEWS_SENTIMENT_THRESHOLD = 0.50  # VADER compound score to trigger a signal (0–1)
NEWS_SIGNAL_DECAY_MIN = 10       # news signal expires after 10 minutes

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
ALL_ASSETS     = ["BTC", "ETH", "SOL", "EURUSD", "GBPUSD", "EURGBP", "XAUUSD"]
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

# Asset hourly volatility (1σ, fractional) — used in diffusion model for win probability.
# P(win) = Φ( |spot_distance| / (σ_h × √T_hours) )  ← same math as options pricing
ASSET_HOURLY_VOL = {
    # Crypto
    "BTC":    0.018,   # ~1.8% per hour (annualised ~28%)
    "ETH":    0.022,   # ~2.2% per hour (annualised ~34%)
    "SOL":    0.028,   # ~2.8% per hour (annualised ~43%)
    # FX — much lower vol; realized vol will refine these quickly
    "EURUSD": 0.0006,  # ~0.06% per hour (major pair, very stable)
    "GBPUSD": 0.0007,  # ~0.07% per hour
    "EURGBP": 0.0004,  # ~0.04% per hour (cross rate, tightest range)
    # Commodities
    "XAUUSD": 0.0015,  # ~0.15% per hour (Gold)
}

SNIPE_MIN_CERTAINTY = 0.40   # min certainty = min win_prob of 68% (0.50 + 0.45×0.40)

# ── Correlation Signal ─────────────────────────────────────────────────────────
CORRELATION_THRESHOLD = 0.04  # BTC market must move ≥4% for cross-asset signal (was 8% — never fired)
CORRELATION_WINDOW_SEC = 180  # signal valid for 3 minutes after BTC move (was 2 min — too short)

# ── Arbitrage (Mint/Burn) ─────────────────────────────────────────────────────
ARB_TRIGGER = 0.97           # enter burn arb when YES+NO sum ≤ this
ARB_MAX_SIZE_NGN = 50_000    # max per arb trade

# ── Risk Management ───────────────────────────────────────────────────────────
BANKROLL_PCT_PER_TRADE = 0.03  # 3% of bankroll per trade (Kelly-conservative)
MAX_PORTFOLIO_EXPOSURE = 0.30  # never have >30% of bankroll in open positions
MAX_DRAWDOWN_STOP = 0.20       # pause all trading at 20% drawdown from peak
PROFIT_ALERT_NGN = 20_000      # Telegram alert when unrealized profit hits this

# ── Rate Limiting (stay well under 20 write/sec, 30 read/sec) ─────────────────
WRITE_RATE_LIMIT = 15          # max write requests/second (buffer below 20)
READ_RATE_LIMIT = 25           # max read requests/second (buffer below 30)
SCAN_INTERVAL_SECONDS = 60     # re-scan for new markets every 60s
