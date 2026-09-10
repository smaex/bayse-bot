# Bayse Markets Trading Bot

A multi-user prediction-market trading service controlled through Telegram. It watches Bayse markets, checks fresh market/oracle data, sizes orders under global risk limits, records fills in PostgreSQL, and monitors open positions.

> **Important:** this software cannot guarantee profit or zero losses. Directional prediction-market trades can lose their entire stake. Fresh installations are therefore **dry-run by default** (`LIVE_TRADING=false`).

## Safe operating model

The production path is intentionally narrow:

- New accounts start **paused**, limited to BTC/SOL 15-minute single-leg MAKER—the combinations supported by the current production audit.
- A trade requires fresh data, a complete executable quote, sufficient modeled edge, and room under both per-trade and portfolio limits.
- Requested order size is never treated as proof of a fill; exposure is created only from exchange-confirmed filled quantity.
- The default global ceilings are 2% per trade, 15% total exposure, and a 3% daily realized-loss stop.
- Single-leg CLOB MAKER is permitted and remains subject to per-asset performance controls. ARB, paired-sniper, oracle-arb, and dual-leg midmarket-maker remain experimental and are blocked unless the operator explicitly sets `ALLOW_EXPERIMENTAL_STRATEGIES=true`.
- Telegram polling, feed tasks, scanning, and user loops are supervised. `/live` reports process liveness; `/ready` reports whether startup and the singleton lease are healthy.
- One database-backed owner lease prevents two deployments from trading the same users at once.

These are ceilings, not profit targets. Start smaller, review exchange fills and realized net PnL, and promote strategies only after enough out-of-sample evidence.

## Setup

Requires Python 3.11+ and PostgreSQL.

```bash
git clone https://github.com/smaex/bayse-bot.git
cd bayse-bot
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set at least:

- `TELEGRAM_TOKEN`
- `DATABASE_URL`
- `ENCRYPTION_KEY` (a Fernet key; the example file includes a generation command)
- `DASHBOARD_PASSWORD`

Run the test suite before starting:

```bash
pytest -q
python bot.py
```

Leave `LIVE_TRADING=false` while validating feeds, market discovery, Telegram, and portfolio reconciliation. Enabling real orders is a deliberate operator action:

```env
LIVE_TRADING=true
```

Users connect their own Bayse API keys through `/start`. Keys are encrypted before being stored. New users must explicitly `/resume` because accounts start paused.

## Main Telegram commands

| Command | Purpose |
|---|---|
| `/start` | Connect a Bayse account |
| `/status` | Equity, free cash, PnL, drawdown, and open positions |
| `/balance` | Fetch wallet balance |
| `/trades` | Show recent trades |
| `/markets` | Show currently watched markets |
| `/settings` | Show account configuration |
| `/mode` | Apply a bounded safe, balanced, or aggressive preset |
| `/set ...` | Change assets, timeframes, strategies, or risk within operator limits |
| `/pause` | Stop new entries; position monitoring continues |
| `/resume` | Allow new entries |
| `/rekey` | Replace Bayse API credentials |
| `/debug` | Show feed, strategy, and risk diagnostics |

## Risk and execution controls

Environment-level controls override looser saved user preferences:

```env
MAX_TRADE_RISK=0.02
MAX_PORTFOLIO_EXPOSURE=0.15
DEFAULT_DAILY_LOSS_LIMIT_PCT=3.0
MAX_DAILY_LOSS_LIMIT_PCT=5.0
MAX_DRAWDOWN_STOP=0.10
REQUIRE_DIRECT_ORACLE=true
ALLOW_EXPERIMENTAL_STRATEGIES=false
TRADING_TIMEZONE=Africa/Lagos
```

Additional safeguards include bounded HTTP/WebSocket waits, conservative retries, user-scoped cooldowns, per-user evaluation locks, market-specific minimum orders, stale-feed rejection, fee-aware EV checks, capped slippage, partial-fill reconciliation, and exchange-side portfolio checks before exits.

## Health and dashboard

- `GET /live` (or legacy `/ping`): event loop is reachable.
- `GET /ready`: startup finished and core ownership/main-loop heartbeats are fresh.
- `GET /dashboard`: static dashboard.
- `GET /api/stats`: requires `Authorization: Bearer <DASHBOARD_PASSWORD>`.

Point the deployment platform's liveness probe at `/live` and readiness probe at `/ready`. A process can be alive while Telegram or trading tasks are dead, so these signals are intentionally separate.

## Architecture

| Area | Files |
|---|---|
| Orchestration and supervision | `bot.py`, `health.py`, `server.py` |
| Exchange API and persistence | `client.py`, `database.py` |
| Market/oracle inputs | `feeds.py`, `feeds_direct.py`, `scanner.py` |
| Signals and aggregation | `strategies/`, `strategy.py` |
| Orders and reconciliation | `executor.py`, `learner.py` |
| Account controls | `telegram_bot.py`, `risk.py`, `config.py` |
| Regression checks | `tests/` |

## Profitability standard

Do not evaluate the bot using win rate alone. A high win rate can still lose money when entries are expensive. Use net realized PnL after fees/slippage, return on deployed capital, maximum drawdown, fill rate, partial-fill/orphan frequency, and results split by strategy/asset/timeframe. No experimental strategy should be enabled from backtest claims alone; require exchange-confirmed out-of-sample evidence.
