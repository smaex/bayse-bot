# Bayse Markets Trading Bot

Automated prediction market trading bot for [Bayse Markets](https://bayse.markets) — trades BTC, ETH, and SOL UP/DOWN markets using four strategies, learns from every trade, and is fully controlled via Telegram.

---

## How it works

### Market structure
Bayse runs automated binary markets every 5, 15, 60, 360, and 1440 minutes:

> "Will BTC be **UP or DOWN** from its opening price in the next 5 minutes?"

- **YES/UP wins** → BTC closes above the opening Binance price
- **NO/DOWN wins** → BTC closes below the opening Binance price
- Resolution: verified on Binance (BTC/ETH) or Chainlink oracle (SOL)
- Fee: variance-based, ~1–1.8% effective per trade

### Four trading strategies

| Strategy | How it works | Edge |
|----------|-------------|------|
| 🎯 **SNIPE** | In the last 90 seconds of a candle, compare live Binance/Chainlink price to the opening threshold. If BTC is clearly above, buy UP with near-certainty | 80–99% certainty near close |
| 🔗 **CORRELATE** | BTC, ETH, SOL move together (~0.85 correlation). When BTC's market reprices sharply, the bot immediately trades ETH and SOL in the same direction before they catch up | 60–70% edge, 30–120s window |
| ⚖️ **ARB** | If YES + NO prices sum to less than ₦1.00, buy both sides then burn for ₦1.00 — guaranteed risk-free profit | 100% certainty, zero risk |
| 📰 **NEWS** | Polls CryptoPanic for breaking crypto news, scores sentiment with VADER AI, trades bullish/bearish direction before the market reprices | ~60% edge, 10-min decay |

### Intelligence loop (daily self-improvement)
Every night at midnight UTC, the bot:
1. Analyses all trades from the last 30 days per strategy/asset/timeframe
2. Computes win rate and expected value for each combination
3. Tightens or loosens thresholds based on performance (e.g. raises SNIPE certainty if win rate drops below 60%)
4. Scales position sizes up (2×) for strategies performing well, down (0.25×) for underperformers
5. Suspends any strategy with win rate below 48% for 20+ trades
6. Sends a full report to your Telegram

### Rate limits
- 30 read requests/second → bot uses 25/sec (buffer)
- 20 write requests/second (orders) → bot uses 15/sec
- 429 responses handled with automatic exponential backoff

---

## Setup (5 minutes)

### 1. Clone the repo
```bash
git clone https://github.com/YOUR_USERNAME/bayse-bot.git
cd bayse-bot
pip install -r requirements.txt
```

### 2. Get your API keys
Go to [app.bayse.markets](https://app.bayse.markets) → More → Account Settings → API Keys → Create

### 3. Create your Telegram bot
1. Open Telegram → search `@BotFather` → `/newbot`
2. Choose name + username → copy the token
3. Search `@userinfobot` → `/start` → copy your ID

### 4. Configure
```bash
cp .env.example .env
# Edit .env with your keys
```

### 5. Run
```bash
python bot.py
```

See [SETUP.md](SETUP.md) for full detailed instructions.

---

## Telegram commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome screen with quick-action buttons |
| `/status` | Balance, PnL, drawdown, active positions |
| `/balance` | Wallet balance |
| `/trades` | Last 10 trades |
| `/markets` | Active markets being watched right now |
| `/analysis` | Full performance report |
| `/learning` | Run the intelligence cycle now + show what changed |
| `/learnstats` | Win rate and PnL per strategy (7-day breakdown) |
| `/settings` | Show current configuration |
| `/set assets BTC` | Trade BTC only |
| `/set assets BTC ETH SOL` | Trade all three (default) |
| `/set timeframes 5min 15min` | Short-term candles only |
| `/set timeframes 5min 15min 1h` | Short + medium (default) |
| `/set strategies SNIPE ARB` | Safest mode — no directional risk |
| `/set strategies SNIPE CORRELATE ARB NEWS` | All strategies (default) |
| `/set risk 1` | 1% of bankroll per trade (conservative) |
| `/set risk 3` | 3% per trade (default) |
| `/set risk 5` | 5% per trade (aggressive) |
| `/set mintrade 100` | Minimum ₦100 per trade |
| `/set maxtrade 50000` | Maximum ₦50,000 per trade |
| `/set maxexposure 25` | Max 25% of bankroll deployed at once |
| `/pause` | Pause all trading |
| `/resume` | Resume trading |

---

## Risk controls

- **Max drawdown stop**: trading pauses automatically at 20% loss from peak balance
- **Position exposure cap**: never deploys more than 30% of bankroll simultaneously
- **Per-trade cap**: maximum 3% of bankroll per trade (Kelly-conservative)
- **Minimum trade**: ₦100 (configurable)
- **Maximum trade**: ₦500,000 (configurable)
- **Fee drag**: ~1–1.8% effective per trade (lower than the displayed 4% base rate)

---

## What moves these markets

| Event | Reaction time | Bot response |
|-------|--------------|-------------|
| FOMC / CPI data release | 2–5 minutes | NEWS strategy enters next candle |
| Crypto exchange hack | 10–20 minutes | NEWS + SNIPE on next candle |
| Whale transfer (>$10M) | 5–15 minutes | CORRELATE fires across all assets |
| Geopolitical shock | 15–60 minutes | NEWS + CORRELATE |
| Normal price movement | Continuous | SNIPE in final 90s of each candle |

---

## For multiple users

Each user runs their **own instance** with their **own `.env`** file:
```
User 1: BAYSE_PUBLIC_KEY=pk_live_user1... TELEGRAM_CHAT_ID=111111
User 2: BAYSE_PUBLIC_KEY=pk_live_user2... TELEGRAM_CHAT_ID=222222
```
Trade history, learned parameters, and Telegram notifications are fully isolated per instance. The `.env` and `data/` folder are in `.gitignore` and will never be pushed to GitHub.

---

## File structure

```
bayse-bot/
├── bot.py          — main orchestration loop
├── client.py       — Bayse REST API client (auth, all endpoints)
├── feeds.py        — Binance WS + Chainlink poll + Bayse WS price feeds
├── scanner.py      — discovers active BTC/ETH/SOL markets
├── strategy.py     — SNIPE, CORRELATE, ARB, NEWS signal generators
├── risk.py         — position sizing, drawdown control, exposure limits
├── learner.py      — SQLite trade history + daily self-improvement loop
├── news.py         — CryptoPanic feed + VADER sentiment + econ calendar
├── analysis.py     — performance reports
├── telegram_bot.py — Telegram interface (commands + trade alerts)
├── config.py       — all settings and constants
├── .env.example    — template for your credentials
├── SETUP.md        — step-by-step setup guide
└── requirements.txt
```
