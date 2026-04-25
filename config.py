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

# ── Oracle Price Feeds (each asset resolves on a DIFFERENT oracle) ─────────────
# BTC/ETH → Binance   |   SOL → Chainlink
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
BINANCE_SYMBOLS = ["btcusdt", "ethusdt"]  # BTC and ETH only on Binance
CHAINLINK_SOL_URL = "https://data.chain.link/feeds/sol-usd"  # polled every 10s
CHAINLINK_POLL_SEC = 10  # Chainlink updates every 10–30s or on 0.5% deviation

# ── Market Series Slugs ────────────────────────────────────────────────────────
SERIES = {
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
}

# Which oracle each asset uses for resolution (discovered from live API)
ASSET_ORACLE = {"BTC": "BINANCE", "ETH": "BINANCE", "SOL": "CHAINLINK"}

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
ALL_ASSETS     = ["BTC", "ETH", "SOL"]
ALL_TIMEFRAMES = ["5min", "15min", "1h", "6h", "1d"]

# ── Sniping ───────────────────────────────────────────────────────────────────
# Enter a snipe trade when this many seconds remain before market close.
# 90s was too late — by then other traders have pushed YES/NO to 0.97-0.98 (no edge).
# 300s (5 min) lets the bot enter while prices are still in the 0.60-0.85 range.
SNIPE_ENTRY_SECONDS = 300    # enter when 5 min remain
SNIPE_MIN_CERTAINTY = 0.65   # certainty threshold (was 0.80 — too strict, trades never fired)
SNIPE_MAX_PRICE = 0.92       # never pay more than 0.92 for a "sure thing" (fee protection)

# ── Correlation Signal ─────────────────────────────────────────────────────────
CORRELATION_THRESHOLD = 0.08  # BTC market must move ≥8% for cross-asset signal
CORRELATION_WINDOW_SEC = 120  # signal valid for 2 minutes after BTC move

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
