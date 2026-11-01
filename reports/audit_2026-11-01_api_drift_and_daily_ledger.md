# Audit 2026-11-01: Bayse API drift, daily PnL and balance accounting

Branch: `arena/c6b485e3-bayse-bot` (from `main` @ c77e2e5).
Test suite: 417 passing at baseline, 433 passing after fixes (new file
`tests/test_api_drift_and_daily_ledger.py`, 16 tests; two existing tests that
encoded the old behaviour were updated, see "Test changes").

The docs were read against the code at `docs.bayse.markets` (changelog
v0.1.6 to v0.1.16, auth, rate limits, batch orders, list orders, burn, mint,
PnL, WebSocket market data). Live API calls were not possible from this sandbox,
so every claim below rests on the docs and the offline test suite. See
"Verify live before trusting" at the end.

## 1. Fixed bugs

Severity reflects the effect on real money or on the daily stop.

| # | Severity | Area | Problem | Fix |
|---|----------|------|---------|-----|
| A | Critical | Burn / balance | `burn_shares` and `mint_shares` sent a SHARE count as the request `quantity`. The docs say the request `quantity` is a WALLET amount in `currency` (a complete set is ₦100 or $1). For NGN, 100 sets were requested as ₦100, which burns 1 set. The bot then compared the response payout with the guaranteed payout (`shares × 100`). On mismatch, `_complete_set_payout` returned the GUARANTEED amount and flagged for review. So the bot credited cash and PnL the wallet may never have received, and the exchange kept the unburned shares. | `client.wallet_quantity_for_shares()` converts shares to wallet amount (× base multiplier, rounded DOWN to the kobo so we never burn shares we do not hold). Both burn and mint use it. |
| B | Critical | Balance accounting | `_complete_set_payout` on a missing or inconsistent payout returned the guaranteed amount as proceeds. Cash the wallet may not hold was booked on a guess. | Unverifiable payouts now return `None`, which routes to the existing reconciliation pause (`burn_reconciliation_pending`). Tolerance widened to 2 kobo to cover the rounding in A. |
| C | High | Panic button | `panic_button.py` called `list_orders(limit=100)` (the API ignores `limit`; it takes `size`), so it saw only the first 20 orders. Its filter used uppercase statuses (`OPEN`, `PARTIALLY_FILLED`); the API returns lowercase, so nothing matched. It logged "No open orders" and cancelled nothing. It also logged "Successfully cancelled" without checking the batch `success` flag. | `client.list_open_orders()` paginates `open` and `partial_filled` with `size`/`page`. Cancel failures are counted and logged as errors. The client is closed in `finally`. |
| D | High | Daily stop (loss and target) | Partial exits realize PnL through `update_trade_remaining` on a row that is still unresolved. `get_daily_resolved_pnl` summed only resolved rows, so a partial stop-out did not count toward the daily loss stop until the whole trade resolved. | New append-only ledger `trade_pnl_events`. Every realized movement (partial exit or final resolution) writes a row in the SAME statement as the trade update, and only when that update applies. The daily sum reads the ledger by the day each movement happened. One-time backfill from `trades` for existing databases, guarded by an atomic `config` row. |
| E | High | Daily stop (fail-open) | `get_daily_resolved_pnl` caught every exception and returned `0.0`. In `_user_loop`, a DB error therefore disabled the loss stop and the target for that cycle. | The read now raises. `_user_loop` skips NEW entries for the cycle and records `daily_pnl_unavailable` in the stall report. Exits are unaffected. |
| F | Medium | Cancel semantics | `client.cancel_order` returned the batch body without reading `results[].success`. Batch cancel returns HTTP 200 even when one ID is refused. | Per-ID `success: false` now raises `RuntimeError("cancel rejected ...")`. Existing callers already catch and reconcile through `get_order`. |
| G | Medium | Order listing | `list_orders(limit=...)` used a parameter the API does not accept. | Renamed to `size`, added a `status` parameter (lowercased), and array key `orders`. |
| H | Medium | PnL reporting | `get_pnl()` defaulted to USD. An NGN-only account sees all zeros (changelog v0.1.7). | `get_pnl(currency=CURRENCY)`. |
| I | Low | Drawdown peak | `reset_daily_if_needed` set the peak to free cash. Equity is free cash plus deployed capital everywhere else. The next `update_balance` corrected it, so the effect was transient. | Peak uses equity. |
| J | Low | WebSocket | `bayse_feed` sent one subscribe per event with no pacing. The API allows 10 messages/s per connection and drops the excess with an `error` frame, which was ignored. | 0.15 s between subscribes. Error frames are logged. |
| K | Low | Reward summary | `summarize_liquidity_rewards` summed USD payouts with no currency label. It has no production caller today. | Output carries `"currency": "USD"` and the docstring states the unit. |

Confirmed NOT bugs, checked against the code:
- Candidate 4 (`/status` target drift): `telegram_bot._calc_target` and `bot._daily_target` are identical.
- Market-data subscription: the `prices` channel with `eventId` is valid per docs. It is not the `orderbook` channel with `marketIds`.
- NGN price scaling: prices stay 0–1 probabilities. Only cost and payout use `× CURRENCY_BASE_MULTIPLIER`, consistently across `bot.py`, `executor.py`, `learner.py` and `strategies/book.py`.
- Fee formula, `minimumOrderAmount` default, `outcomeId` and the `order` response key, batch place cap of 20, `Idempotency-Key` handling, HMAC signature scheme, timestamp window, `parse_filled_shares`: all match the docs.
- Unfilled or phantom orders settle with `won=None, pnl=0`. This is harmless.

## 2. Decisions for the operator

1. **Burn payouts that cannot be verified now pause the account.** This is deliberate. A paused account is recoverable, while booked phantom cash is not. Expect more reconciliation pauses in the first days after deploy if the live API reports payouts in a shape the code does not expect. Check the first few notices.
2. **Drawdown resume threshold is inconsistent.** `risk.check_drawdown` resumes below `MAX_DRAWDOWN_STOP × 0.25`. `_user_loop` auto-recovers below `MAX_DRAWDOWN_STOP × 0.5`. The loop usually wins, because it clears `risk.paused` first. This is a risk-appetite choice, so it is NOT changed. Pick one value and align both sites (0.25 is the more conservative choice).
3. **Rate-limit constants are conservative.** `config.py` uses WRITE 15 and READ 25 per second. The docs allow 20 and 30. Left unchanged. Note that batch cancel and batch place charge one write token per item, and `client` charges one per HTTP call. Under-counting is possible if batch calls are ever used at scale. The bot does not batch cancels today.
4. **Historical burns may have booked phantom cash.** Any complete-set burn since this integration went live would have sent shares as the wallet amount. Look in the logs and notifications for `reported burn payout` and `used guaranteed set payout` (the old wording). Each such event means the bot credited a guaranteed payout the wallet may not hold. Reconcile the account's wallet balance against the bot's tracked free cash and the `trades` PnL for those dates.

## 3. Verify live before trusting

- Burn one complete set at a small size (for example, 1 set, ₦100 wallet amount) on a test account. Confirm the response `quantity` is 1 share and `proceeds` is ₦100, and that the wallet rises by ₦100. This confirms the unit fix against the real endpoint.
- Confirm `GET /v1/pm/orders?status=open&size=100` returns the expected `orders` array.
- Confirm `GET /v1/pm/pnl?currency=NGN` returns non-zero totals on an account with history.

## 4. Structure comparison with reference designs

Public profitability claims for Polymarket bots are self-reported, and I could not
verify any P&L. The comparison below is architectural only. References: the
NautilusTrader architecture and live-reconciliation docs, the Hummingbot
connector and strategy model, the `alexmann3k/polymarket_mm_bot` README
(startup reconciliation, safety path for ambiguous placements), and a 2026
writeup on Polymarket control planes (Strategy → Risk → Execute → Verify →
Reconcile → Recheck, with "pause" distinct from "kill").

Where the bot already matches the references:
- **Pause taxonomy.** `pause_reason` distinguishes manual, drawdown, daily loss, daily target and reconciliation. This matches the "pause is not kill" separation.
- **Ambiguous execution is not retried.** Timed-out placements and burns pause for reconciliation (`_pause_for_order_reconciliation`), and a regression test pins that a burn is not retried automatically.
- **Single-instance lease.** The DB lease with a random owner token prevents two processes trading one account.
- **Pre-trade gates.** The stall report names each rejected gate.

Gaps versus the references, in priority order:
1. **Mutable trade rows as the PnL source.** The `trades` table had several writers (partial exit, resolution, maintenance reversal). That design produced bug D. The new ledger moves toward NautilusTrader's event-derived portfolio. A full move to event sourcing would also cover fills and cost basis.
2. **No periodic exchange-position check.** NautilusTrader runs position-status checks on a timer and queries missing fills on discrepancy. The bot reads the exchange portfolio only on exit (`get_position`). A timer that compares `GET /v1/pm/portfolio` against tracked positions and pauses on drift would catch bugs A and B automatically.
3. **Cost-basis equity.** `deployed()` uses cost, not mark-to-market, so drawdown and daily target can drift from true account value while positions move. Mark open positions using the book midpoint, the way NautilusTrader's Portfolio does.
4. **Startup account sweep.** The polymarket market-maker reference cancels all account orders at startup and then reconciles balances. I did not find an equivalent in the startup path I read. Verify before relying on it.
5. **Batch amend not used.** `POST /v1/pm/orders/batch/amend` (max 20) keeps queue priority when quotes move. MAKER re-quotes by cancel and replace today, which loses priority. This is a revenue opportunity, not a correctness bug.
6. **User-trade WebSocket not used.** `/ws/v1/user` (`orders` channel) or `user_trades` would replace order polling for fills. Also an opportunity.

## 5. Test changes

- `tests/test_bayse_api_contract.py`: the mint and burn test asserted that 100 shares went out as `quantity: 100`. It now asserts the wallet amount (₦10,000).
- `tests/test_database_reconciliation.py`: the partial-exit and resolution tests asserted exact parameter tuples. They now include the ledger write.
- `tests/test_maker_pair_completion.py`: a malformed burn payout previously credited ₦1,000 of free cash and ₦400 of PnL. It now asserts a pause with no credit.
- `tests/test_model_and_calibration.py`: the reward summary assertion includes `"currency": "USD"`.
- `tests/test_api_drift_and_daily_ledger.py` (new): burn units, cancel rejection, `size`/`status` params, pagination, panic button end to end, `get_pnl` currency, fail-closed daily read, peak reset, and the ledger against real PostgreSQL 16 (embedded via `pgserver`, skipped if not installed). The ledger tests cover partial loss counted on its day, no double count on final resolution, idempotent resolution replay, and one-time backfill.

## 6. Not done in this pass

- Items 2 to 4 in section 4 (exchange position drift check, mark-to-market equity, startup account sweep). Each needs its own design and tests.
- Section 2, decision 2 (drawdown threshold) needs an operator choice.
- Live verification in section 3.
