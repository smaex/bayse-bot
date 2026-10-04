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

# Settlement reference for the crypto series. Operator notice 2026-09-26: these
# markets resolve on a Chainlink 60-second time-weighted average price, not the
# Binance spot print at close. Two consequences the code has to respect:
#   1. The random variable is the average over the final minute, so the last
#      60s of diffusion is partly averaged away — see
#      strategies.utils.twap_win_probability. Set 0 to model the close print.
#   2. ASSET_ORACLE above is now the *independent* feed we cross-check with, not
#      the settlement source. Binance spot and the Chainlink TWAP differ by
#      both aggregation basis and the averaging lag, so the minimum-distance
#      calibrations are measured against a proxy, not against the settling
#      series.
SETTLEMENT_TWAP_SEC = _env_float("SETTLEMENT_TWAP_SEC", 60.0)

# ── Strategies ────────────────────────────────────────────────────────────────
# Two strategies, because there are two ways to be on the right side of a
# price: take it (and pay for the privilege) or post it (and get paid).
#
#   TAKER — crosses the spread. Pays the Bayse taker fee. Wins by being right
#           about the settling quantity.
#   MAKER — posts passive bids on BOTH outcomes. Makers pay no fee on Bayse
#           CLOB, earn liquidity rewards for resting two-sided, and earn maker
#           rebates when takers hit them. The pair is a complete set: it
#           settles to 1.00 whichever outcome wins.
#
# Every other strategy that used to live here was either quarantined for
# non-atomic multi-leg execution risk or never produced out-of-sample
# evidence that its edge survived fees.
ACTIVE_STRATEGIES = ["TAKER", "MAKER"]
# Which family each strategy belongs to. Risk rules treat them differently:
# a maker posts liquidity and pays no fee, a taker crosses and does.
MAKER_STRATEGIES = ("MAKER",)
TAKER_STRATEGIES = ("TAKER",)
# Default scope for new accounts (which start paused).
DEFAULT_STRATEGIES = ["TAKER", "MAKER"]
DEFAULT_ASSETS = ["BTC", "ETH", "SOL"]
DEFAULT_TIMEFRAMES = ["15min", "5min"]
PERMITTED_STRATEGIES = list(ACTIVE_STRATEGIES)

# ── Currency ──────────────────────────────────────────────────────────────────
CURRENCY = "NGN"
CURRENCY_BASE_MULTIPLIER = 100.0 if CURRENCY == "NGN" else 1.0

# ── TAKER ─────────────────────────────────────────────────────────────────────
# A taker pays the fee, so a taker entry has to clear the fee plus a margin for
# model error before it is a trade. These are the numbers that decide.
TAKER_ENTRY_WINDOWS = {
    "5min":  270,    # evaluate in final 4.5 minutes of 5min market
    "15min": 810,    # final 13.5 minutes of a 15min market
    "1h":    1800,   # final 30 minutes of a 1h market
    "6h":    7200,
    "1d":    21600,
}
TAKER_ALLOWED_ASSETS = _env_csv_set("TAKER_ALLOWED_ASSETS", {"BTC", "ETH", "SOL"})
TAKER_ALLOWED_TIMEFRAMES = _env_csv_set("TAKER_ALLOWED_TIMEFRAMES", {"15MIN", "5MIN"})
TAKER_MIN_SECS_TO_CLOSE = 45
# Never take a side the model calls a coin flip. 0.55 is already generous:
# the edge requirement below, not this floor, is what normally binds.
TAKER_MIN_MODEL_PROB = _env_float("TAKER_MIN_MODEL_PROB", 0.55)
# The audit's hard-won ceiling, kept. Entries >= 0.80 were the -₦314 bucket:
# at 0.85 a win pays +17.6% while a loss costs 100%, so a 93%+ win rate is
# needed just to break even after fees. Above this price the fee structure,
# not the forecast, decides the outcome.
TAKER_MAX_EFFECTIVE_PRICE = _env_float("TAKER_MAX_EFFECTIVE_PRICE", 0.65)
TAKER_MIN_EFFECTIVE_PRICE = _env_float("TAKER_MIN_EFFECTIVE_PRICE", 0.35)
# Minimum spot/threshold separation before the read is trusted. Inside this
# band the outcome is close to a coin flip and the model is mostly restating
# noise. Per-asset values live in strategies/taker.py's _min_distance().
TAKER_MIN_DISTANCE_PCT = _env_float("TAKER_MIN_DISTANCE_PCT", 0.0010)
# Momentum veto, on the +/-1 normalised score. This is NOT a requirement that
# momentum agree -- demanding agreement blocked ~94% of evaluations while
# adding nothing. It is a refusal to buy into a tape moving hard the other
# way, which is the definition of adverse selection.
TAKER_MOMENTUM_VETO = _env_float("TAKER_MOMENTUM_VETO", 0.40)
# Default required net EV. The learner adjusts this per account from settled
# outcomes, within the bounds in learner.py -- but a fresh account starts here.
# It is the number that decides most taker entries, so it is config, not a
# constant buried in the strategy.
TAKER_MIN_NET_EV_DEFAULT = _env_float("TAKER_MIN_NET_EV", 0.06)
TAKER_MIN_SIZE_PCT = 0.01
TAKER_MAX_SIZE_PCT = 0.05
# Structural complete-set take: buy both outcomes when their fee-inclusive
# asks sum below one. Each leg must still clear the directional EV gate on its
# own (see strategies/taker.py) so a partial batch fill is never a loss.
COMPLETE_SET_TAKER_MIN_EDGE = _env_float("COMPLETE_SET_TAKER_MIN_EDGE", 0.015)
TAKER_COMPLETE_SET_SIZE_PCT = _env_float("TAKER_COMPLETE_SET_SIZE_PCT", 0.02)

# ── MAKER ─────────────────────────────────────────────────────────────────────
# Makers pay no fee on Bayse CLOB, so a maker fill at price b against a fair
# value fv is worth (fv - b) with no fee drag. That single fact is why this leg
# exists, and it is why the required edge is small compared with the taker's.
MAKER_MIN_SECS_TO_CLOSE = 45    # never quote into settlement
MAKER_MAX_SECS_TO_CLOSE = 720   # and not beyond 80% of a 15-minute candle
# Required (fv - bid) per leg, before the price scaling below.
MAKER_MIN_LEG_EDGE = _env_float("MAKER_MIN_LEG_EDGE", 0.020)
# Extra edge required per unit of price above 0.50. Two real effects: a fill
# that resolves against loses the whole price paid, so capital at risk scales
# with price; and the model's calibration is least trustworthy in the tails,
# which is where a high price lives.
MAKER_EDGE_PRICE_COEF = _env_float("MAKER_EDGE_PRICE_COEF", 0.10)
# Hard floor on the locked complete set: (1 - bid_yes - bid_no). Independent
# leg pricing implies 2x MAKER_MIN_LEG_EDGE, so this is a backstop against
# tick rounding and clamps, not the binding constraint.
MAKER_PAIR_MIN_EDGE = _env_float("MAKER_PAIR_MIN_EDGE", 0.020)
MAKER_MIN_LEG_BID = _env_float("MAKER_MIN_LEG_BID", 0.05)
MAKER_MAX_LEG_BID = _env_float("MAKER_MAX_LEG_BID", 0.90)
if not (0.0 < MAKER_MIN_LEG_BID < MAKER_MAX_LEG_BID <= 0.99):
    raise RuntimeError(
        f"MAKER_MIN_LEG_BID must be below MAKER_MAX_LEG_BID (<= 0.99), got "
        f"{MAKER_MIN_LEG_BID} / {MAKER_MAX_LEG_BID}"
    )
MAKER_LEG_SIZE_PCT = _env_float("MAKER_LEG_SIZE_PCT", 0.02)
# Single-leg fallback: quote one side when the other cannot be priced.
#
# On by default, because the alternative is close to never quoting at all. In
# an arbitrage-free book (yes + no ~ 1) a model that disagrees with the market
# can only ever clear its edge requirement on ONE side: the side it thinks is
# cheap. The other side is, by construction, priced above what we think it is
# worth, so requiring both legs means the bot may only quote when it holds no
# opinion -- which is precisely when it has the least edge.
#
# It is genuinely the weaker trade: no pair lock, full directional risk, and
# roughly a third of the liquidity-reward score. What makes it acceptable is
# that it is not a relaxed version of the pair -- the same fair-value band, the
# same required edge, the same passive-and-competitive book check apply -- and
# a one-sided fill immediately skews the next quote toward the other leg, so
# inventory self-corrects into a set rather than accumulating.
MAKER_ALLOW_SINGLE_LEG = _env_bool("MAKER_ALLOW_SINGLE_LEG", True)
# Inventory skew: how far (in ticks) a one-sided fill pushes the next quote
# toward completing the set. Positive inventory means long YES.
MAKER_MAX_SKEW_TICKS = _env_int("MAKER_MAX_SKEW_TICKS", 2)
MAKER_REQUOTE_THRESHOLD = _env_float("MAKER_REQUOTE_THRESHOLD", 0.0010)
MAKER_QUOTE_MAX_AGE_SEC = _env_float("MAKER_QUOTE_MAX_AGE_SEC", 45.0)
MAKER_ORDER_TIMEOUT = 60        # seconds before a resting quote is withdrawn
# Maker capital budget, separate from directional exposure. A resting quote
# has no directional risk until it fills, but it does reserve wallet funds,
# and a one-sided fill does carry risk. Both are bounded here.
MAX_MAKER_NOTIONAL_PCT = _env_float("MAX_MAKER_NOTIONAL_PCT", 0.20)
MAX_MAKER_UNPAIRED_PCT = _env_float("MAX_MAKER_UNPAIRED_PCT", 0.10)
# Price grid used when stepping a passive bid against the live book.
MAKER_TICK = 0.01
# A post-only bid this many ticks (or fewer) below the best bid still has a
# realistic chance to fill before timeout; anything deeper is buried behind
# the queue and is skipped with a named reason.
MAKER_MAX_TICKS_BEHIND_BEST_BID = 1
TAKE_PROFIT_PRICE_TARGET = 0.82  # absolute take-profit price target

# Order-book freshness. Bayse's documented level schema does not promise a
# timestamp, so an undated book is not treated as stale -- but a book whose
# timestamp proves it is old is not a price at all.
CLOB_MAX_BOOK_AGE_SECONDS = _env_float("CLOB_MAX_BOOK_AGE_SECONDS", 5.0)

# ── Fee formula ───────────────────────────────────────────────────────────────
# Bayse fee formula: fee = feeRate × max(1 - price, 0.5)
# The floor is 0.5 as specified in the Bayse fees documentation.
FEE_FLOOR = 0.5

# ── Exit policy ───────────────────────────────────────────────────────────────
# Position management is priced, not emotional. A binary held to resolution is
# worth `fv` per share; a binary sold now is worth `bid * (1 - fee)`. The bot
# exits when the second number is enough larger than the first to be worth
# giving up the option value of holding, and cuts when the first number has
# moved against the thesis.
#
# Two rules follow from that, and neither is a fixed percentage stop:
#
#   TAKE PROFIT — the market is offering more than the position is worth.
#   STOP        — the model's estimate has fallen below what we paid.
#
# A stop that triggers on P&L alone sells precisely when a binary is cheapest
# and its expected value is unchanged. A stop that triggers on the estimate
# sells when we were wrong, which is the only time selling is correct.
EXIT_MIN_SECS_REMAINING = 30       # below this, settlement dominates: let it resolve
# Take profit: sell when net proceeds exceed model value by this fraction of
# the entry cost. The premium pays for model error and for the optionality we
# are giving up by not holding to resolution.
EXIT_TAKE_PROFIT_PREMIUM = _env_float("EXIT_TAKE_PROFIT_PREMIUM", 0.05)
# Take profit is only checked when there is enough time left for the exit to
# be a choice rather than a scramble.
EXIT_TAKE_PROFIT_MIN_SECS = _env_float("EXIT_TAKE_PROFIT_MIN_SECS", 120.0)
# Stop: exit when fair value has fallen this far below the entry cost. 0.15
# means we cut once the model says the position is worth 15% less than we paid
# -- wide enough to survive ordinary re-estimation noise, tight enough to stop
# holding a thesis that has broken.
EXIT_STOP_DRAWDOWN = _env_float("EXIT_STOP_DRAWDOWN", 0.15)
# Hard backstop. Models are wrong in ways the drawdown rule cannot see, and a
# catastrophic adverse move should not require the model's permission to exit.
EXIT_HARD_STOP_LOSS_PCT = _env_float("EXIT_HARD_STOP_LOSS_PCT", 0.50)
# A gain that has started to evaporate is still a gain. This trailing rule
# only ever sells into profit -- a reversal below cost is the stop's job.
EXIT_TRAILING_DROP = _env_float("EXIT_TRAILING_DROP", 0.08)
# Below this price the remaining proceeds are not worth the round trip, and a
# market order into a near-worthless book is how you get a terrible fill.
EXIT_MIN_SALVAGE_PRICE = _env_float("EXIT_MIN_SALVAGE_PRICE", 0.05)
MIN_TAKE_PROFIT_NET_GAIN  = 0.05   # an exit quote must lock at least 5% after costs
# A resting maker quote this close to settlement is not a position we want to
# acquire: withdraw it rather than let someone fill us into the close.
MAKER_LATE_CANCEL_SECS = _env_float("MAKER_LATE_CANCEL_SECS", 180.0)
# Fee rate assumed when a market's own `feePercentage` is unavailable. The
# documented example is 0.5%; 2% is the conservative choice, because assuming
# a lower fee than the market charges turns marginal trades into losses.
DEFAULT_FEE_RATE = _env_float("DEFAULT_FEE_RATE", 0.02)


# ── Risk ─────────────────────────────────────────────────────────────────────
# Environment overrides are fractions: 0.10 means 10%.
MAX_DRAWDOWN_STOP      = _env_float("MAX_DRAWDOWN_STOP", 0.10)
MAX_PORTFOLIO_EXPOSURE = _env_float("MAX_PORTFOLIO_EXPOSURE", 0.15)
MAX_TRADE_RISK         = _env_float("MAX_TRADE_RISK", 0.05)
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

# These are priors, not measurements: BTC at 1.8%/h is roughly 4x a typical
# calm-market hourly vol, and every probability the diffusion model produces
# scales with it. realized_vol_hourly() now prefers a vol measured from the
# live tick history (see strategies.utils.measured_vol_hourly) and falls back
# to these values when there is not enough history. Set false to restore the
# constant/GARCH-only behaviour.
USE_MEASURED_VOL = _env_bool("USE_MEASURED_VOL", True)

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
    if TAKER_MIN_EFFECTIVE_PRICE >= TAKER_MAX_EFFECTIVE_PRICE:
        errors.append("TAKER_MIN_EFFECTIVE_PRICE must be below TAKER_MAX_EFFECTIVE_PRICE")
    if not 0 < TAKER_MIN_MODEL_PROB < 1:
        errors.append("TAKER_MIN_MODEL_PROB must be in (0, 1)")
    if not 0 <= TAKER_MOMENTUM_VETO <= 1:
        errors.append("TAKER_MOMENTUM_VETO must be in [0, 1]")
    if MAKER_PAIR_MIN_EDGE <= 0:
        errors.append("MAKER_PAIR_MIN_EDGE must be positive: a pair that does not lock is not a trade")
    if MAKER_MAX_SECS_TO_CLOSE <= MAKER_MIN_SECS_TO_CLOSE:
        errors.append("MAKER_MAX_SECS_TO_CLOSE must exceed MAKER_MIN_SECS_TO_CLOSE")
    if not 0 < MAX_MAKER_NOTIONAL_PCT <= 0.50:
        errors.append("MAX_MAKER_NOTIONAL_PCT must be in (0, 0.50]")
    if not 0 < MAX_MAKER_UNPAIRED_PCT <= MAX_MAKER_NOTIONAL_PCT:
        errors.append("MAX_MAKER_UNPAIRED_PCT must be in (0, MAX_MAKER_NOTIONAL_PCT]")
    if errors:
        raise RuntimeError("Invalid configuration: " + "; ".join(errors))
