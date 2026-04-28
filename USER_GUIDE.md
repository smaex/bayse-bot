# Bayse Bot — User Setup Guide

## What is this bot?

Bayse Bot automatically trades prediction markets on Bayse (app.bayse.markets) on your behalf.
It watches Bitcoin, Ethereum, Solana, Gold, and FX currency pairs 24/7, enters positions when
the math strongly favours one side, and sends you a Telegram alert for every trade.

You do nothing after setup — just fund your Bayse account and the bot handles everything.

---

## What you need before starting

1. A Bayse account — sign up at **app.bayse.markets**
2. Nigerian Naira (NGN) funded in your Bayse wallet
3. Telegram app on your phone

---

## Step 1 — Fund your Bayse wallet

Deposit NGN into your Bayse account before connecting the bot.

**Minimum recommended starting amounts:**

| Starting Capital | Mode to Use  | Expected Daily Target | What each trade is |
|-----------------|--------------|----------------------|-------------------|
| ₦3,000          | 🟢 Safe       | ₦150 – ₦300          | ₦100 per trade     |
| ₦5,000          | 🟢 Safe       | ₦250 – ₦500          | ₦100 per trade     |
| ₦10,000         | 🔵 Balanced   | ₦500 – ₦1,000        | ₦300 per trade     |
| ₦25,000         | 🔵 Balanced   | ₦1,250 – ₦2,500      | ₦750 per trade     |
| ₦50,000         | 🟠 Aggressive | ₦2,500 – ₦5,000      | ₦1,500 per trade   |
| ₦100,000+       | 🟠 Aggressive | ₦5,000 – ₦15,000     | ₦3,000+ per trade  |

> **Important:** Bayse's minimum trade is ₦100. The bot will never place a trade below this.
> At ₦3,000 starting capital, every trade is exactly ₦100 regardless of mode.

---

## Step 2 — Get your Bayse API keys

1. Open **app.bayse.markets** in your browser
2. Tap **More → Account Settings → API Keys**
3. Tap **Create new key**
4. Copy your **Public Key** (starts with `pk_`)
5. Copy your **Secret Key** (starts with `sk_`) — save this somewhere safe, it only shows once

> Keep your secret key private. Never share it with anyone except this bot.

---

## Step 3 — Connect to the bot

1. Open Telegram and find the bot (your admin will share the link)
2. Type `/start`
3. Paste your **Public Key** when asked
4. Paste your **Secret Key** when asked
5. The bot confirms your balance and starts trading immediately

---

## Step 4 — Choose your mode

Type `/mode` and tap one of the buttons:

**🟢 Safe** — Best for beginners or small balances (₦3,000 – ₦10,000)
- Trades BTC and ETH only
- Smaller positions, tighter risk limits
- Daily target: 5% of your starting balance

**🔵 Balanced** — Good all-round setup (₦10,000 – ₦50,000)
- Trades BTC, ETH, SOL
- Three strategies active
- Daily target: 10% of your starting balance

**🟠 Aggressive** — For users who've seen the bot run (₦50,000+)
- All crypto assets, all four strategies
- Larger positions
- Daily target: 20% of your starting balance

**💱 FX + Crypto** — Adds Gold and currency pair markets (any balance)
- Trades BTC, ETH, SOL + EUR/USD, GBP/USD, EUR/GBP, Gold
- FX markets are calmer and more predictable
- More markets open = more trade opportunities per day
- Daily target: 10% of your starting balance

**🔴 Full Send** — Maximum risk (₦100,000+, experienced only)
- Everything on, largest positions
- Daily target: 50% of starting balance

---

## Step 5 — Set your daily target (optional)

The bot stops trading automatically when it hits your daily profit target.

To set a fixed naira target:
```
/set dailytarget 250
```

To set a percentage of your balance:
```
/set dailymultiplier 10
```
(this means 10% of whatever your balance was at midnight)

---

## Commands you'll use daily

| Command | What it does |
|---------|-------------|
| `/status` | Balance, today's profit, open positions |
| `/trades` | Your last 10 trades |
| `/mode` | Change trading mode |
| `/pause` | Stop all trading immediately |
| `/resume` | Start trading again |
| `/balance` | Check your wallet balance |
| `/analysis` | Full performance report |
| `/learnstats` | Win rate by strategy (last 7 days) |
| `/settings` | See all current settings |

---

## What to expect

**The bot trades when conditions are right — not on a fixed schedule.**
On some days it fires 8–12 trades. On slow days (low market volatility) it may fire 2–4.

**Win rate target: 70–80%**
This means roughly 7–8 of every 10 trades win. Losing trades happen — the bot is sized so
a string of 3 losses in a row does not significantly impact your balance.

**The bot pauses automatically when:**
- Your daily profit target is reached (resumes at midnight)
- Your balance drops 20% from its peak (sends you an alert)

**You will get Telegram notifications for:**
- Every trade placed (what was bought, at what price, how much)
- Every win (with profit amount)
- Every loss (with loss amount)
- Daily intelligence report at midnight
- Drawdown alert if losses accumulate

---

## Realistic daily target by starting capital

| Starting Capital | Safe daily target | What that looks like |
|-----------------|------------------|---------------------|
| ₦3,000          | ₦150             | 2–3 winning trades  |
| ₦5,000          | ₦250             | 3–4 winning trades  |
| ₦10,000         | ₦500 – ₦1,000    | 4–6 winning trades  |
| ₦25,000         | ₦1,250 – ₦2,500  | 4–6 winning trades  |
| ₦50,000         | ₦2,500 – ₦5,000  | 4–8 winning trades  |

> These are targets, not guarantees. The bot is designed to grow your balance steadily
> over weeks and months — not to double it in a day.

---

## Frequently asked questions

**Can I withdraw while the bot is running?**
Yes. The bot only uses your available balance. Withdrawals reduce what it trades with.

**What if I want to take a break?**
Type `/pause`. The bot stops immediately. Type `/resume` when you're ready.

**Why did the bot not trade today?**
Markets need to be in the right position for the bot to enter. On quiet days where prices
sit close to thresholds (neither clearly up nor down), the bot waits rather than guess.
This is intentional — it only trades when it has a real edge.

**Is my money safe?**
Your funds stay in your Bayse account at all times. The bot only places orders using your
Bayse API key — it cannot withdraw or transfer your money.

**How does the bot get smarter over time?**
Every night at midnight it reviews the last 30 days of trades and adjusts its own thresholds
based on what has been winning and losing. Over weeks it becomes more calibrated to current
market conditions.
