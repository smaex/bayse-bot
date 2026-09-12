import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_csv_set(name: str, default: set[str]) -> set[str]:
    raw = os.getenv(name)
    if raw is None:
        return set(default)
    values = {item.strip().upper() for item in raw.split(",") if item.strip()}
    if not values:
        raise RuntimeError(f"{name} must contain at least one comma-separated value")
    return values

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
    # Commodities & FX
    "EURUSD": {"1h": "fx-eurusd-1h"},
    "GBPUSD": {"1h": "fx-gbpusd-1h"},
    "XAUUSD": {
        "15min": "commodity-xauusd-15min",
        "1h":    "commodity-xauusd-1h",
    },
}

# These are the only assets with confirmed real-time price feeds on Bayse.
# DO NOT add BNB, USDJPY, EURJPY, GBPJPY, EURGBP — they are not on the WS feed.
ALL_ASSETS     = ["BTC", "ETH", "SOL", "EURUSD", "GBPUSD", "XAUUSD"]
ALL_TIMEFRAMES = ["5min", "15min", "1h", "6h", "1d"]

ASSET_ORACLE = {
    "BTC": "BINANCE", "ETH": "BINANCE", "SOL": "BINANCE",
    "EURUSD": "TWELVEDATA", "GBPUSD": "TWELVEDATA", "XAUUSD": "TWELVEDATA",
}

# ── Strategies ────────────────────────────────────────────────────────────────
ACTIVE_STRATEGIES = [
    "SNIPE", "MAKER", "ORACLE_ARB", "FRONTRUN", "CORRELATE",
    "ARB", "PAIRED_SNIPER", "MIDMARKET_MAKER",
]
# Multi-leg strategies with non-atomic dual-order execution risk are quarantined
# until exchange-confirmed paired fills validate them. ORACLE_ARB is a
# single-leg final-seconds latency strategy and is permitted alongside the
# other active single-leg strategies.
EXPERIMENTAL_STRATEGIES = {
    "ARB", "PAIRED_SNIPER", "MIDMARKET_MAKER",
}
# Default scope for new accounts (which start paused).
DEFAULT_STRATEGIES = ["SNIPE", "MAKER", "ORACLE_ARB", "FRONTRUN", "CORRELATE"]
DEFAULT_ASSETS = ["BTC", "ETH", "SOL"]
DEFAULT_TIMEFRAMES = ["15min", "5min"]
ALLOW_EXPERIMENTAL_STRATEGIES = _env_bool("ALLOW_EXPERIMENTAL_STRATEGIES", False)
PERMITTED_STRATEGIES = [
    name for name in ACTIVE_STRATEGIES
    if ALLOW_EXPERIMENTAL_STRATEGIES or name not in EXPERIMENTAL_STRATEGIES
]

# ── Currency ──────────────────────────────────────────────────────────────────
CURRENCY = "NGN"
CURRENCY_BASE_MULTIPLIER = 100.0 if CURRENCY == "NGN" else 1.0

# ── Sniping ───────────────────────────────────────────────────────────────────
SNIPE_ENTRY_WINDOWS = {
    "5min":  240,    # evaluate in final 4 minutes of 5min market
    "15min": 450,    # evaluate in final 7.5 minutes of 15min market (not minute-0)
    "1h":    1800,   # evaluate in final 30 minutes of 1h market
    "6h":    7200,
    "1d":    21600,
}
# SNIPE is enabled on BTC, ETH, and SOL with asset-calibrated strike distance buffers,
# momentum confirmation, and fee-adjusted expected value gates.
SNIPE_ALLOWED_ASSETS = _env_csv_set("SNIPE_ALLOWED_ASSETS", {"BTC", "ETH", "SOL"})
SNIPE_ALLOWED_TIMEFRAMES = _env_csv_set(
    "SNIPE_ALLOWED_TIMEFRAMES", {"15MIN", "5MIN"}
)
SNIPE_MIN_SECS_TO_CLOSE = 60
SNIPE_MIN_CERTAINTY    = 0.27   # Maps to a conservative win probability of >= 62%.
SNIPE_MAX_MARKET_PRICE = 0.65   # Avoid expensive, strongly asymmetric payoffs.
SNIPE_MIN_ENTRY_PRICE  = 0.40   # Block low-probability underdog entries.
SNIPE_MIN_DISTANCE_PCT = 0.0010 # Base minimum spot/threshold separation (calibrated by asset).
SNIPE_MIN_RAW_MODEL_EDGE = 0.06 # Independent model must disagree materially with market.
SNIPE_MIN_BLENDED_EDGE = 0.025  # Required after shrinking toward market consensus.
SNIPE_MODEL_WEIGHT = 0.35       # Market gets 65% weight until calibration improves.
SNIPE_VOL_SAFETY_MULTIPLIER = 1.25
MAKER_ORDER_TIMEOUT = 120       # Seconds before cancelling stale resting maker quote.
TAKE_PROFIT_PRICE_TARGET = 0.82 # Absolute take-profit price target.

# Complete-set arbitrage remains shadow-only. The edge must clear two taker
# fees plus execution uncertainty before an observation is counted.
COMPLETE_SET_MIN_EDGE     = _env_float("COMPLETE_SET_MIN_EDGE", 0.02)
CLOB_MAX_BOOK_AGE_SECONDS = _env_float("CLOB_MAX_BOOK_AGE_SECONDS", 5.0)

# FX-specific
FX_SESSION_UTC = {
    "EURUSD": (6, 17),
    "GBPUSD": (6, 17),
    "XAUUSD": (8, 20),
}
SNIPE_VELOCITY_WINDOW = 60
SNIPE_VELOCITY_VETO   = 0.40

# ── Correlation ───────────────────────────────────────────────────────────────
CORRELATION_THRESHOLD     = 0.0015  # 0.15% — lowered to give CORRELATE realistic firing chance
CORRELATION_WINDOW_SEC    = 180
CORRELATE_BASE_CERTAINTY  = 0.55
CORRELATE_MAX_MARKET_PRICE= 0.65
CORRELATE_MIN_REGIME      = 0.15

# ── Frontrun ──────────────────────────────────────────────────────────────────
FRONTRUN_ALLOWED_TFS       = {"5min", "15min", "1h"}
FRONTRUN_BIAS_TRIGGER      = float(os.getenv("FRONTRUN_BIAS_TRIGGER", "0.0003"))  # 0.03% — catches real relay lag of 50-150ms (≈0.03-0.05% BTC move)

# ── ARB ───────────────────────────────────────────────────────────────────────
ARB_TRIGGER      = 0.97    # 3% edge — enough to be profitable after fees on ₦100 test trades
ARB_MIN_TIME_SECS = 120    # raised from 30s — need time for both legs to fill safely
ARB_MAX_SIZE_NGN  = 50_000

# ── Fee formula ───────────────────────────────────────────────────────────────
# Bayse fee formula: fee = feeRate × max(1 - price, 0.5)
# The floor is 0.5 as specified in the Bayse fees documentation.
FEE_FLOOR = 0.5

# ── Soft Stop-Loss / Exit Strategy ───────────────────────────────────────────
EXIT_EV_THRESHOLD = -0.15          # Exit if EV drops below -15% (thesis wrong)
MIN_EXIT_TIME_REMAINING = 45       # Allow exits down to 45s remaining (was 90s)
                                   # Audit showed profitable exits happen in 60-90s window before resolution

# Take-profit exit: trigger on market price, then require a fee-adjusted quote
# that realizes at least MIN_TAKE_PROFIT_NET_GAIN.
# This is how MAKER exits generated PROFITS (not just cut losses):
#   Entry at 0.35, price rises to 0.72 → exit at 0.72 locks +₦106 instead of gambling on resolution.
TAKE_PROFIT_GAIN_PCT      = 0.15   # Trigger only on executable market-price gain.
TAKE_PROFIT_MIN_SECS_REMAINING = 450
MIN_TAKE_PROFIT_NET_GAIN  = 0.05   # Quote must lock at least 5% after costs.


# ── Risk ─────────────────────────────────────────────────────────────────────
# Environment overrides are fractions: 0.10 means 10%.
MAX_DRAWDOWN_STOP      = _env_float("MAX_DRAWDOWN_STOP", 0.10)
MAX_PORTFOLIO_EXPOSURE = _env_float("MAX_PORTFOLIO_EXPOSURE", 0.15)
MAX_TRADE_RISK         = _env_float("MAX_TRADE_RISK", 0.02)
DEFAULT_DAILY_LOSS_LIMIT_PCT = _env_float("DEFAULT_DAILY_LOSS_LIMIT_PCT", 3.0)
MAX_DAILY_LOSS_LIMIT_PCT = _env_float("MAX_DAILY_LOSS_LIMIT_PCT", 5.0)
TRADING_TIMEZONE = os.getenv("TRADING_TIMEZONE", "Africa/Lagos")

# Fail closed on crypto entries if the independent Binance oracle is missing.
# The Bayse relay remains useful for market pricing, but it must not be its own
# independent cross-check.
REQUIRE_DIRECT_ORACLE = _env_bool("REQUIRE_DIRECT_ORACLE", True)
FEED_STALE_SEC        = _env_float("FEED_STALE_SEC", 30.0)

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
# Min: 3% — smallest useful bet on Bayse (100₦ min, 3% of ₦30k = ₦900)
# Max: 50% — only hit on ORACLE_ARB near-certainty signals (95%+ confidence)
# SNIPE will typically size 5-20% depending on win_prob and market_price edge
DYNAMIC_KELLY_MIN = 0.03
DYNAMIC_KELLY_MAX = 0.50

# ── Rate limits / request bounds ──────────────────────────────────────────────
WRITE_RATE_LIMIT      = 15
READ_RATE_LIMIT       = 25
SCAN_INTERVAL_SECONDS = 15
API_REQUEST_TIMEOUT_SEC = _env_float("API_REQUEST_TIMEOUT_SEC", 12.0)
API_CONNECT_TIMEOUT_SEC = _env_float("API_CONNECT_TIMEOUT_SEC", 4.0)
API_READ_RETRIES        = max(1, _env_int("API_READ_RETRIES", 3))
LOCK_LEASE_SEC          = max(20, _env_int("LOCK_LEASE_SEC", 45))

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

# ── Trading-drought visibility ────────────────────────────────────────────────
# Silence is not a safe failure mode: a stopped deployment and a legitimate
# "no qualifying edge" day both look like an empty log. This watchdog reads the
# stall telemetry and tells the operator which one it is. It never forces a
# trade, loosens a gate, or lifts a manual pause.
TRADE_STALL_ALERT_MIN     = max(15.0, _env_float("TRADE_STALL_ALERT_MINUTES", 120.0))
TRADE_STALL_ALERT_REPEAT_MIN = max(5.0, _env_float("TRADE_STALL_ALERT_REPEAT_MINUTES", 360.0))
STALL_EVAL_MAX_AGE_SEC     = max(60.0, _env_float("STALL_EVAL_MAX_AGE_SEC", 180.0))

# ── Live/Test mode ────────────────────────────────────────────────────────────
# Fail safe: an omitted environment variable must never place real orders.
# Operators must deliberately enable live trading after dry-run validation.
LIVE_TRADING       = _env_bool("LIVE_TRADING", False)
TEST_MODE          = _env_bool("TEST_MODE", False)
TEST_MAX_TRADE_NGN = _env_float("TEST_MAX_TRADE_NGN", 500.0)
TEST_MIN_BANKROLL  = _env_float("TEST_MIN_BANKROLL", 1_000.0)


def validate() -> None:
    """Fail startup on missing secrets or internally unsafe settings."""
    errors = []
    if not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN is required")
    if not ENCRYPTION_KEY:
        errors.append("ENCRYPTION_KEY is required")
    else:
        try:
            from cryptography.fernet import Fernet
            Fernet(ENCRYPTION_KEY.encode())
        except (ImportError, ValueError):
            errors.append("ENCRYPTION_KEY is not a valid Fernet key")
    if not 0 < MAX_DRAWDOWN_STOP <= 0.50:
        errors.append("MAX_DRAWDOWN_STOP must be in (0, 0.50]")
    if not 0 < MAX_PORTFOLIO_EXPOSURE <= 0.50:
        errors.append("MAX_PORTFOLIO_EXPOSURE must be in (0, 0.50]")
    if not 0 < MAX_TRADE_RISK <= 0.10:
        errors.append("MAX_TRADE_RISK must be in (0, 0.10]")
    if not 0 < DEFAULT_DAILY_LOSS_LIMIT_PCT <= MAX_DAILY_LOSS_LIMIT_PCT:
        errors.append("DEFAULT_DAILY_LOSS_LIMIT_PCT must not exceed MAX_DAILY_LOSS_LIMIT_PCT")
    if not 0 < MAX_DAILY_LOSS_LIMIT_PCT <= 20:
        errors.append("MAX_DAILY_LOSS_LIMIT_PCT must be in (0, 20]")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(TRADING_TIMEZONE)
    except (ImportError, KeyError):
        errors.append("TRADING_TIMEZONE is invalid")
    if SNIPE_MIN_ENTRY_PRICE >= SNIPE_MAX_MARKET_PRICE:
        errors.append("SNIPE_MIN_ENTRY_PRICE must be below SNIPE_MAX_MARKET_PRICE")
    if not 0 < ARB_TRIGGER < 1:
        errors.append("ARB_TRIGGER must be between 0 and 1")
    if errors:
        raise RuntimeError("Invalid configuration: " + "; ".join(errors))
