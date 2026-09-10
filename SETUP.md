# Deployment Setup

The current deployment and safety instructions live in [README.md](README.md). This file keeps the operational checklist in one place.

1. Install Python 3.11+, PostgreSQL, and `requirements.txt` in a virtual environment.
2. Copy `.env.example` to `.env`.
3. Set `TELEGRAM_TOKEN`, `DATABASE_URL`, `ENCRYPTION_KEY`, and `DASHBOARD_PASSWORD`.
4. Keep `LIVE_TRADING=false` and `ALLOW_EXPERIMENTAL_STRATEGIES=false`.
5. Run `pytest -q`; all checks must pass.
6. Start `python bot.py` and confirm `/live` returns 200 and `/ready` becomes 200.
7. Use Telegram `/start`, verify feeds with `/debug`, and inspect market discovery.
8. Connect only a low-balance test account first. New users start paused.
9. Enable live trading only after dry-run validation, then use `/resume` for the intended account.
10. Review exchange-confirmed fills, net PnL, slippage, and drawdown before increasing any limit.

Never run two deployments against the same database. The singleton lease is a backstop, not a substitute for controlled deployment. Never commit `.env` or API keys.
