# User Guide

## Start safely

1. Send `/start` to the Telegram bot.
2. Paste your Bayse public key and then secret key. The bot attempts to delete the secret-key message and stores credentials encrypted.
3. Your account starts paused. Use `/status`, `/settings`, `/markets`, and `/debug` first.
4. Send `/resume` only when the operator has enabled live trading and you accept the risk.

## Useful commands

- `/status` — equity, free cash, daily PnL, drawdown, and positions.
- `/balance` — current Bayse free balance.
- `/trades` — recent exchange-tracked trades.
- `/markets` — watched markets matching your settings.
- `/settings` — saved account settings.
- `/mode` — safe, balanced, or aggressive presets within operator ceilings.
- `/set assets BTC ETH SOL` — choose assets.
- `/set timeframes 5min 15min` — choose timeframes.
- `/set strategies SNIPE` — choose permitted strategies.
- `/set risk 1` — request 1% risk per trade; the global ceiling still applies.
- `/set maxexposure 10` — request 10% maximum deployed exposure; the global ceiling still applies.
- `/pause` — block new entries while position monitoring continues.
- `/resume` — allow new entries.
- `/rekey` — replace API credentials.
- `/resetlearning` — clear learned overrides without silently resuming or broadening the account.
- `/disconnect` — deactivate the account while preserving history.

## What “paused” means

Paused accounts do not open new positions. Existing positions are still monitored so a pause does not abandon risk. The bot can also pause new entries after a daily loss limit, daily target, or sustained drawdown. Session-level safety pauses reset on the next configured trading day; a manual pause remains under your control.

## Expectations

No mode guarantees profit. Prediction-market positions can lose their entire stake, and high win rate alone does not prove profitability. Judge results by realized net PnL after fees and slippage, return on capital, and drawdown over a meaningful sample. Experimental arbitrage/maker strategies are unavailable unless the operator explicitly enables them.
