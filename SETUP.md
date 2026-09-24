# Setup Guide

## 1. Install dependencies

```bash
cd bayse-bot
pip install -r requirements.txt
```

---

## 2. Bayse API keys

1. Go to https://app.bayse.markets
2. Click **More** → **Account Settings** → **API Keys**
3. Click **Create API Key**
4. Copy both the **Public Key** and **Secret Key** — the secret is shown only once

---

## 3. Telegram bot setup (5 minutes)

### Step 1 — Create your bot
1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name: e.g. `Bayse Trading Bot`
4. Choose a username: e.g. `mybayse_bot` (must end in `bot`)
5. BotFather sends you a token like:
   ```
   1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   Copy this — it's your `TELEGRAM_BOT_TOKEN`

### Step 2 — Get your chat ID
1. Search Telegram for **@userinfobot**
2. Send `/start`
3. It replies with your user ID like: `Your id: 123456789`
4. That number is your `TELEGRAM_CHAT_ID`

### Step 3 — Test your bot
1. Search for your bot username in Telegram
2. Click **Start**
3. After the bot is running, send `/start` — you should see the welcome message

---

## 4. News feed setup (optional but recommended)

Free API key from CryptoPanic:
1. Go to https://cryptopanic.com/developers/api/
2. Sign up for a free account
3. Copy your API key → `CRYPTOPANIC_API_KEY`

---

## 5. Fill in .env

```bash
cp .env.example .env
```

Edit `.env`:
```
BAYSE_PUBLIC_KEY=pk_live_...
BAYSE_SECRET_KEY=sk_live_...
TELEGRAM_BOT_TOKEN=1234567890:AA...
TELEGRAM_CHAT_ID=123456789
CRYPTOPANIC_API_KEY=...        # optional
```

---

## 6. Run the bot

```bash
python bot.py
```

You should see:
```
=== Bayse Bot Starting ===
Starting balance: ₦...
Binance feed connected (BTC, ETH)
Chainlink SOL feed polling started
Bayse market feed connected (N markets)
Telegram bot running
```

---

## 7. Telegram commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome screen with buttons |
| `/status` | Balance, PnL, drawdown, positions |
| `/balance` | Wallet balance only |
| `/trades` | Last 10 trades |
| `/markets` | Active markets being watched |
| `/analysis` | Full performance report |
| `/settings` | Show current configuration |
| `/set assets BTC` | Trade BTC only |
| `/set assets BTC ETH SOL` | Trade all three |
| `/set timeframes 5min 15min` | Short-term only |
| `/set timeframes 5min 15min 1h` | Short + medium |
| `/set strategies SNIPE ARB` | Safest mode |
| `/set strategies SNIPE CORRELATE ARB NEWS` | All strategies |
| `/set risk 1` | 1% of bankroll per trade (conservative) |
| `/set risk 3` | 3% per trade (default) |
| `/set maxexposure 20` | Max 20% deployed at once |
| `/pause` | Pause all trading |
| `/resume` | Resume trading |

---

## 8. Deposit & withdrawal

**Deposit:**
- Go to https://app.bayse.markets → Wallet → Deposit
- Supported networks: BEP20, TRON, Solana
- Send crypto to your deposit address
- The bot monitors your balance and alerts you on Telegram when a deposit is detected

**Withdrawal:**
- The Bayse withdrawal API is not yet public
- Go to https://app.bayse.markets → Wallet → Withdraw
- The bot will alert you on Telegram when your profit hits the milestone threshold (₦20,000 default)
- Use `/set` to change the threshold once that setting is added

---

## 9. Market selection examples

```
# Conservative — BTC only, short timeframes, safest strategies
/set assets BTC
/set timeframes 5min 15min
/set strategies SNIPE ARB
/set risk 1

# Balanced — all assets, medium timeframes
/set assets BTC ETH SOL
/set timeframes 5min 15min 1h
/set strategies SNIPE CORRELATE ARB
/set risk 3

# Aggressive — everything on
/set assets BTC ETH SOL
/set timeframes 5min 15min 1h 6h
/set strategies SNIPE CORRELATE ARB NEWS
/set risk 5
/set maxexposure 40
```
