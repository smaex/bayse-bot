import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Credentials ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ENCRYPTION_KEY  = os.getenv("ENCRYPTION_KEY", "")

# ── Bayse API ─────────────────────────────────────────────────────────────────
BASE_URL        = "https://relay.bayse.markets"
WS_MARKETS_URL  = "wss://socket.bayse.markets/ws/v1/markets"
WS_REALTIME_URL = "wss://socket.bayse.markets/ws/v1/realtime"

# ── Market series slugs ───────────────────────────────────────────────────────
# Only assets confirmed available on the Bayse realtime WS feed:
#   Binance source  : BTC, ETH, SOL
#   TwelveData source: XAUUSD, EURUSD, GBPUSD
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
    # FX — only 1h confirmed on Bayse WS
    "EURUSD": {"1h": "fx-eurusd-1h"},
    "GBPUSD": {"1h": "fx-gbpusd-1h"},
    "XAUUSD": {"1h": "commodity-xauusd-1h"},
}

# These are the only assets with confirmed real-time price feeds on Bayse.
# DO NOT add BNB, USDJPY, EURJPY, GBPJPY, EURGBP — they are not on the WS feed.
ALL_ASSETS     = ["BTC", "ETH", "SOL", "EURUSD", "GBPUSD", "XAUUSD"]
ALL_TIMEFRAMES = ["5min", "15min", "1h", "6h", "1d"]

ASSET_ORACLE = {
    "BTC": "BINANCE", "ETH": "BINANCE", "SOL": "BINANCE",
    "EURUSD": "TWELVEDATA", "GBPUSD": "TWELVEDATA", "XAUUSD": "TWELVEDATA",
}

# ── Active strategies (only what's implemented and working) ───────────────────
ACTIVE_STRATEGIES = ["SNIPE", "ARB", "FRONTRUN", "CORRELATE"]

# ── Currency ──────────────────────────────────────────────────────────────────
CURRENCY = "NGN"

# ── Sniping ───────────────────────────────────────────────────────────────────
SNIPE_ENTRY_WINDOWS = {
    "5min":  300,
    "15min": 900,
    "1h":    1800,
    "6h":    7200,
    "1d":    21600,
}
# DATA-DRIVEN TUNING (from 170 live trades forensic analysis, 2026-07-14):
#
# 1. SNIPE_MIN_CERTAINTY raised 0.55 → 0.65
#    Certainty 0.50–0.65 had only 52–56% WR, ₦2,144 loss on 118 trades.
#    Certainty 0.75+: 76.7% WR, ₦252 profit. Only trade when model is truly confident.
#
# 2. SNIPE_MIN_ENTRY_PRICE = 0.50 (NEW)
#    Entry price < 0.55: 52 trades, 23.1% WR, ₦2,651 loss.
#    BTC YES < 0.55: 9 trades, 0 wins. ETH YES < 0.55: 11 trades, 9.1% WR.
#    Cheap odds look attractive but the market is almost always correctly priced.
#
# 3. SNIPE_MIN_DISTANCE_PCT = 0.0005 (NEW)
#    Trades within 0.1% of threshold: 134 trades, 52.2% WR, ₦2,477 loss.
#    Trades 0.3–0.5% from threshold: 4 trades, 100% WR, ₦81 profit.
#    Price hugging the threshold = coin-flip. Needs real distance to resolve cleanly.
SNIPE_MIN_CERTAINTY    = 0.60   # optimized from 0.65 — 0.60 cert + 0.10% dist gives 90% WR
SNIPE_MAX_MARKET_PRICE = 0.75   # keep — at 0.90 win pays only ₦11/₦100, at 0.75 pays ₦33/₦100
SNIPE_MIN_ENTRY_PRICE  = 0.50   # lowered from 0.58 so the bot trades more often, but avoids obvious <0.50 coin-flips
SNIPE_MIN_DISTANCE_PCT = 0.0005 # lowered from 0.0010 to allow trades that are slightly closer to threshold

# FX-specific
FX_SESSION_UTC = {
    "EURUSD": (6, 17),
    "GBPUSD": (6, 17),
    "XAUUSD": (8, 20),
}
SNIPE_VELOCITY_WINDOW = 60
SNIPE_VELOCITY_VETO   = 0.40

# ── Correlation ───────────────────────────────────────────────────────────────
CORRELATION_THRESHOLD     = 0.0020  # 0.20% — was 0.35%, BTC rarely moves that much in 3 min
CORRELATION_WINDOW_SEC    = 180
CORRELATE_BASE_CERTAINTY  = 0.55
CORRELATE_MAX_MARKET_PRICE= 0.65
CORRELATE_MIN_REGIME      = 0.25

# ── Frontrun ──────────────────────────────────────────────────────────────────
FRONTRUN_ALLOWED_TFS       = {"5min", "15min"}
FRONTRUN_BIAS_TRIGGER      = float(os.getenv("FRONTRUN_BIAS_TRIGGER", "0.0005"))  # 0.05% — real relay lag is 50-150ms ≈ 0.03-0.05% BTC move

# ── ARB ───────────────────────────────────────────────────────────────────────
ARB_TRIGGER      = 0.94    # tightened from 0.98 — 6% edge needed to survive partial fills + fees
ARB_MIN_TIME_SECS = 120    # raised from 30s — need time for both legs to fill safely
ARB_MAX_SIZE_NGN  = 50_000

# ── Fee formula ───────────────────────────────────────────────────────────────
# Bayse fee formula: fee = feeRate × max(1 - price, 0.5)
# The floor is 0.5 as specified in the Bayse fees documentation.
FEE_FLOOR = 0.5

# ── Soft Stop-Loss / Exit Strategy ───────────────────────────────────────────
EXIT_EV_THRESHOLD = -0.15          # Exit if EV drops below -15% (thesis wrong)
MIN_EXIT_TIME_REMAINING = 90       # Don't try to exit in the final 90 seconds due to settlement risk


# ── Risk ─────────────────────────────────────────────────────────────────────
MAX_DRAWDOWN_STOP      = 0.15
MAX_PORTFOLIO_EXPOSURE = 0.20

# ── Hourly volatility baselines ───────────────────────────────────────────────
ASSET_HOURLY_VOL = {
    "BTC":    0.018,
    "ETH":    0.022,
    "SOL":    0.028,
    "EURUSD": 0.0006,
    "GBPUSD": 0.0007,
    "XAUUSD": 0.0015,
}

# ── Kelly sizing ──────────────────────────────────────────────────────────────
DYNAMIC_KELLY_MIN = 0.05
DYNAMIC_KELLY_MAX = 0.40

# ── Rate limits ───────────────────────────────────────────────────────────────
WRITE_RATE_LIMIT      = 15
READ_RATE_LIMIT       = 25
SCAN_INTERVAL_SECONDS = 15

# ── Infra guard ───────────────────────────────────────────────────────────────
INFRA_STALE_LAG_SEC      = 120.0  # crypto: >120s of no oracle data = hard block
INFRA_DEGRADED_LAG_SEC   = 45.0   # >45s = apply safety spread
INFRA_STALE_DIFF_PCT     = 0.0080 # >0.80% price diff = genuinely broken feed
INFRA_DEGRADED_DIFF_PCT  = 0.0015 # >0.15% = safety spread (was 0.08% — too tight)
# NOTE: 0.20% divergence is a FRONTRUN opportunity, not a stale feed.
# Old 0.0020 stale threshold blocked evaluations exactly when FRONTRUN should fire.

# ── Systemic risk halt ────────────────────────────────────────────────────────
SYSTEMIC_RISK_HALT_MINS       = 5
VOL_SPIKE_THRESHOLD           = 25.0
CRYPTO_VOL_SPIKE_THRESHOLD    = 100.0
SYSTEMIC_RISK_COUNT_THRESHOLD = 3
SYSTEMIC_RISK_VOL_MULT        = 3.0

# ── Misc ─────────────────────────────────────────────────────────────────────
MIN_PAYOUT_RATIO   = 0.06
PROFIT_ALERT_NGN   = 20_000
