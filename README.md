# Bayse Markets Trading Bot

A multi-user prediction-market trading service controlled through Telegram. It watches Bayse markets, checks fresh market/oracle data, sizes orders under global risk limits, records fills in PostgreSQL, and monitors open positions.

> **Important:** this software cannot guarantee profit or zero losses. Directional prediction-market trades can lose their entire stake. Fresh installations are therefore **dry-run by default** (`LIVE_TRADING=false`).

## Safe operating model

The production path is intentionally narrow:

- New accounts start **paused**, limited to BTC/SOL 15-minute single-leg MAKER—the combinations supported by the current production audit.
- A trade requires fresh data, a complete executable quote, sufficient modeled edge, and room under both per-trade and portfolio limits.
- Requested order size is never treated as proof of a fill; exposure is created only from exchange-confirmed filled quantity.
- The default global ceilings are 2% per trade, 15% total exposure, and a 3% daily realized-loss stop.
- Single-leg CLOB MAKER is permitted and remains subject to per-asset performance controls. SNIPE is available only in the conservative SOL/15-minute scope by default; broaden it only during paper validation. ARB, paired-sniper, oracle-arb, and dual-leg midmarket-maker remain experimental and are blocked unless the operator explicitly sets `ALLOW_EXPERIMENTAL_STRATEGIES=true`.
- A read-only complete-set monitor looks for fee-adjusted BUY→BURN and MINT→SELL CLOB discrepancies. It never submits orders because Bayse batches are best-effort rather than atomic.
- Telegram polling, feed tasks, scanning, and user loops are supervised. `/live` reports process liveness; `/ready` reports whether startup and the singleton lease are healthy.
- A trading drought is a reported condition, not a mystery. Per-gate rejection counters plus a prioritised verdict (`/why`) distinguish "no candidate cleared its edge and risk gates" from "the account is paused", "feeds are stale", "no markets were discovered", and "LIVE_TRADING=false means we never send orders". Day-scoped safety stops (`daily_loss_limit`, `daily_target`, `drawdown`) expire at the configured trading-day boundary; a manual `/pause` never expires by itself.
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
| `/why` | The specific reason nothing has traded, with the gate counters behind it |
| `/shadow` | Show the legacy two-sided price-touch paper study |
| `/arbshadow` | Show read-only complete-set CLOB opportunity observations |

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

- `GET /live` (or legacy `/ping`): event loop is reachable. Answers 200 even while this instance is a standby waiting for the singleton lease.
- `GET /ready`: startup finished and core ownership/main-loop heartbeats are fresh.
- `GET /dashboard`: static dashboard.
- `GET /api/stats`: requires `Authorization: Bearer <DASHBOARD_PASSWORD>`.

Both probes also report the instance `role` — `starting`, `standby`, `active`, or
`stopping` — so during a deploy you can tell which of the two running containers
is the one trading.

Point the deployment platform's liveness probe at `/live` and readiness probe at `/ready`. A process can be alive while Telegram or trading tasks are dead, so these signals are intentionally separate. `/api/stats` additionally carries a per-account stall report and the current `live_trading` flag.

**Never point a container healthcheck at `/ready`.** A rolling update runs two
containers at once: the new one is correctly *not ready* until the old one hands
over the lease, and a readiness-based restart policy turns that into a
crash-loop. See `reports/coolify_rolling_update_lease.md`.

### When the bot goes quiet

Run `/why` first. Then, in order: is the process up (`/ready`), is `LIVE_TRADING` what you
think it is, can the account's equity cover the platform minimum order inside the per-trade
risk ceiling, and are the feeds fresh. Absence of a qualifying edge is a normal outcome for
these gates and is reported as such — it is not an invitation to lower a gate.
`reports/trading_stall_runbook.md` is the full procedure.

### Deploys

Production runs as a Docker container under Coolify. Two rules keep a rolling
update from deadlocking; both come from a deploy that rolled back on 2026-09-25
(`reports/coolify_rolling_update_lease.md`):

- the image must ship a client the platform's *injected* probe can run — Coolify
  health-checks Dockerfile deployments with `wget`, which `python:3.11-slim` does
  not have, so `wget` and `curl` are both installed;
- the health port is bound **before** the singleton lease is contested, and a
  container that finds the lease held by a live instance **stands by** — alive,
  `/live` 200, `/ready` 503 — instead of exiting after a deadline. Coolify stops
  the old container only once the new one is healthy, so exiting on "lease held"
  left both containers waiting on each other and the new one crash-looping.

`SIGTERM` unwinds in a fixed order: trading loops stop, Telegram polling stops,
the lease is released, clients close. That makes the handover about a second; if
the process is SIGKILLed instead, the standby takes over when the lease expires
(`LOCK_LEASE_SEC`). `LOCK_ACQUIRE_TIMEOUT_SEC` (default 900, `0` = forever) caps
how long a container will stand by, so two live deployments sharing one database
surface as a loud error and a restart rather than a healthy-looking spare.

Database startup is retried, not fatal. `init_db` opens the pool and runs
migrations, and on an unreachable Postgres it raises — which used to kill the
process in under a second, before `/live` answered a single probe, so a
Supabase pause or a connection blip failed the platform health check and rolled
the deploy back. Binding the health port early does not help if the process then
exits: a closed port is closed no matter when it was opened. Startup now retries
inside `DB_INIT_TIMEOUT_SEC` (default 120, `0` = forever) while `/live` keeps
answering 200 and `/ready` reports 503, and exits non-zero only if the database
never returns. See `reports/database_startup_crash.md`.

Watchdog configuration (GitHub → Settings → Secrets and variables → Actions):
`VPS_HOST`/`VPS_USER`/`VPS_SSH_KEY`/`VPS_PORT` secrets for a systemd host, and/or an
`APP_URL` **variable** for a platform that exposes `/live` publicly. Missing
configuration degrades to a warning rather than a failing run.

`Deploy to VPS` is **manual-only** (`workflow_dispatch`). It SSHes to a systemd
host, which is not how this bot runs — production is a Coolify container — and
the host behind `VPS_HOST` no longer accepts sessions. It used to fire on every
push to `main`, fail at the SSH step, and send "❌ Bayse Bot deploy FAILED" to
Telegram for merges Coolify had already shipped. Keep it for a host you actually
operate; run it by hand. When it does run, it executes the suite and an
import/config sanity check first, then `scripts/zero_downtime_deploy.sh`, whose
invariant is that it never exits with the service stopped: it verifies `/live`
and `/ready` and, on any failure, rolls the checkout back to the previously
running commit and restarts before reporting the failure.

`Bot watchdog` probes `/live` every 15 minutes — over `APP_URL` for a container,
over SSH for a systemd host — and alerts only when something was actually wrong,
saying which path ran rather than assuming a restart happened. Neither workflow
places, cancels, or modifies orders.

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

## Simulation and read-only API verification

```bash
python tools/simulate_economics.py --output reports/monte_carlo_simulation.md
python tools/bayse_contract_probe.py
```

The Monte Carlo report illustrates loss probability from audited aggregate economics; it is not a tick-level backtest of the new policy. The API probe makes only public series/event/quote/order-book reads, loads no credentials, and submits no orders.

## Profitability standard

Do not evaluate the bot using win rate alone. A high win rate can still lose money when entries are expensive. Use net realized PnL after fees/slippage, return on deployed capital, maximum drawdown, fill rate, partial-fill/orphan frequency, and results split by strategy/asset/timeframe. No experimental strategy should be enabled from backtest claims alone; require exchange-confirmed out-of-sample evidence. No directional strategy can guarantee profit every day.
