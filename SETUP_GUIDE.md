# Bayse Bot — User Setup Guide

Bayse Bot automatically trades BTC, ETH, and SOL prediction markets on Bayse.
It runs 24/7 on the cloud — you only need Telegram to control it.

---

## What You Need

- A Bayse account at **app.bayse.markets**
- A Telegram account
- A minimum wallet balance of **₦100** (Bayse's minimum trade size)

---

## Step 1 — Get Your Bayse API Keys

1. Open **app.bayse.markets** and log in
2. Go to **More → Account Settings → API Keys → Create**
3. Copy your **Public Key** (starts with `pk_`)
4. Copy your **Secret Key** (starts with `sk_`)

> Keep your Secret Key private. It is encrypted before being stored.

---

## Step 2 — Connect to the Bot

1. Open Telegram and search for the bot (your admin will give you the link)
2. Send `/start`
3. Paste your **Public Key** when prompted
4. Paste your **Secret Key** when prompted

The bot confirms your connection and starts trading immediately.

---

## Step 3 — Choose a Trading Mode

Send `/mode` and pick one of four modes:

| Mode | Assets | Strategies | Risk/Trade | Daily Target |
|------|--------|------------|------------|--------------|
| 🟢 Safe | BTC, ETH | SNIPE, ARB | 2% | 5% |
| 🔵 Balanced | BTC, ETH, SOL | SNIPE, ARB, CORRELATE | 3% | 10% |
| 🟠 Aggressive | BTC, ETH, SOL | All 4 | 4% | 20% |
| 🔴 Full Send | BTC, ETH, SOL + 6h | All 4 | 5% | 50% |

**Recommended for new users: 🔵 Balanced**

---

## Step 4 — Set Your Daily Target (Optional)

The bot pauses automatically when your daily profit target is reached.

**Option A — Fixed amount:**
```
/set dailytarget 500
```
Pauses when you are up ₦500 for the day.

**Option B — Percentage of balance:**
```
/set dailymultiplier 10
```
Pauses when you are up 10% of your starting balance for the day.

To disable the daily target:
```
/set dailytarget 0
```

---

## Daily Commands

| Command | What it does |
|---------|-------------|
| `/status` | Balance, PnL, open positions, drawdown |
| `/balance` | Wallet balance only |
| `/trades` | Last 10 trades |
| `/markets` | Active markets being watched |
| `/analysis` | Full performance report |
| `/settings` | Your current configuration |
| `/pause` | Stop all new trades |
| `/resume` | Resume trading |
| `/mode` | Switch risk mode |
| `/learnstats` | 30-day win rates by strategy |
| `/resetlearning` | Reset adaptive settings to defaults |
| `/help` | Full command list |

---

## How the Strategies Work

### SNIPE — Near-close certainty trading
Enters a trade when the live spot price has clearly crossed the market's threshold
with less than 5 minutes to close. Higher confidence = larger trade size.

### CORRELATE — BTC lead-lag signal
BTC often moves before ETH and SOL reprice. When BTC's market price jumps
significantly, the bot trades ETH and SOL in the same direction within 3 minutes.

### ARB — Risk-free mint/burn arbitrage
When YES + NO prices sum below 1.00, the bot buys both sides and burns
them for guaranteed profit. Rare but risk-free when it occurs.

### NEWS — Sentiment-driven trading
Monitors crypto news headlines and fires a directional trade when sentiment
is strongly bullish or bearish. Requires a NewsAPI key (optional).

---

## Risk Controls (Always Active)

- **Max exposure**: Never more than 25% of balance in open positions at once
- **Max drawdown**: Trading pauses automatically at 20% drawdown from peak
- **Min trade**: ₦100 (Bayse's minimum)
- **Max price**: Never buys an outcome already above 0.92 (no edge left)
- **Daily target**: Auto-pauses when profit goal is reached

---

## Adjusting Settings Manually

```
/set risk_pct 3        — risk 3% of balance per trade
/set mintrade 100      — minimum trade size ₦100
/set maxtrade 50000    — maximum trade size ₦50,000
/set maxexposure 25    — max 25% of balance in open trades
/set dailytarget 1000  — pause after ₦1,000 profit today
/set dailymultiplier 10 — pause after 10% profit today
```

---

## Troubleshooting

**Bot not trading after setup?**
- Send `/resume` — trading may be paused
- Send `/mode` and reselect your preferred mode
- Send `/resetlearning` — clears any adaptive settings that may be blocking trades
- Check `/status` to confirm your balance is loaded

**"Balance shows ₦0"?**
- Confirm funds are in your Bayse wallet at app.bayse.markets
- Your API keys may have been entered incorrectly — use `/disconnect` and `/start` to reconnect

**Want to stop the bot completely?**
- Send `/pause` — stops new trades, existing positions run to completion
- Send `/disconnect` — removes your account from the bot entirely

---

## Important Notes

- The bot trades **real money**. Start with Balanced mode and a small balance.
- Trades placed cannot be cancelled — prediction market positions run until market close.
- The bot learns from your trade history daily at midnight UTC and adjusts thresholds automatically.
- All API keys are encrypted with AES-256 before being stored.
