# Model, economics and Bayse API audit

**Question asked:** make the SNIPE model better, price out lowering
`SNIPE_MIN_CERTAINTY`, decide what to do about markets at ≥ 0.65, and check
the Bayse API for changes.

**Method:** the API facts below come from the official docs
(`docs.bayse.markets`, index at `/llms.txt`); this sandbox has no API
credentials, so **nothing here is response-verified** — it is documentation-
verified against the code. The model numbers are offline runs of this
repository's own functions.

---

## 1. The Bayse API: what matches, what was wrong, what is unused

**Matches — no change needed:**

* Base URL `https://relay.bayse.markets` matches `config.BASE_URL`.
* The fee formula. Docs: `fee = feeRate × C × P × max(1 − P, 0.5)`, i.e. fee as
  a fraction of trade value `= feeRate × max(1 − P, 0.5)`. That is exactly
  `manager._effective_fee = fee_rate * max(1.0 - market_price, config.FEE_FLOOR)`
  with `FEE_FLOOR = 0.5`. Buy-side handling is also right: the docs say the fee
  *reduces shares received*, which is `clob_buy_effective_price = p/(1 − fee)`.
* `feePercentage` is a real market field (the documented example response
  carries `"feePercentage": 0.5`) and `scanner.py:144` reads it, dividing by
  100. The `2` in `market.get("feePercentage", 2)` is only a fallback, and it is
  *conservative* against the documented 0.5 example.
* Order statuses are lowercase and include `partial_filled`. The bot's
  `client.parse_filled_shares` lowercases, treats anything outside
  {pending, open, new, cancelled, canceled, killed, rejected, expired} as a
  possible fill, and `bot.py:691` also accepts `shares > 0` — so partial fills
  are caught. Not a bug.
* Minimum order 100 NGN matches `executor.MIN_TRADE_NGN = 100.0`, and the
  per-market `minimumOrderAmount` is read with a 100/1.0 fallback.

**Wrong — fixed or corrected here:**

* **Makers pay no fee on CLOB.** Docs, *Fees*: "fees apply only to **takers**…
  Makers — orders that add liquidity to the book — pay no fee." This corrects a
  number I gave earlier: MAKER's break-even at a 0.58 bid is **58.0%**, not the
  58.6% that `clob_buy_effective_price` implies, because that function prices a
  taker. SNIPE crosses the book, so charging it the taker fee is correct.
* **`GET /v1/pm/liquidity-rewards` was never called.** MAKER's entire premise
  is earning these rewards, and nothing in the bot could tell whether a day of
  resting quotes earned ₦0 or ₦50. Added `BayseClient.get_liquidity_rewards()`
  plus a pure `summarize_liquidity_rewards()` (path and record shape verified
  from the docs: `epochId, eventId, marketId, accumulatedShares, sampleCount,
  payout, isPaid, epochStart, epochEnd, status`).

**Exists and still unused** (each with what it would buy):

| Endpoint | Why it matters here |
|---|---|
| `/v1/pm/liquidity-rewards/active` | In-progress reward accumulation — tells you *now* whether a quote is qualifying, not after the epoch |
| `/v1/pm/maker-rebates` (+ active) | The other half of MAKER's income |
| `/v1/pm/price-history` | Historical prices for calibrating the model instead of guessing vol |
| `/v1/pm/trades` | Executed CLOB trades — the only way to measure MAKER's adverse selection |
| `/v1/pm/ticker` | Real-time price/volume per outcome |
| `/v1/pm/activities` | Full trading activity history |
| batch amend orders | Requote without cancel+place, keeping queue position — MAKER currently cancels and re-posts |
| `/v1/system/version`, `/health` | Log the deployed API version so a platform change is visible in the stall report |

I did not add client methods for these: I verified they exist in the docs index
but not their exact paths and payloads, and inventing an API path is how a bot
silently starts reading nothing.

## 2. The model: volatility was a constant pretending to be a measurement

`realized_vol_hourly` returned `max(ASSET_HOURLY_VOL[asset], sqrt(garch_var * 720))`.
The `720` is an assumption of **one tick every five seconds**. The oracle is a
Binance `bookTicker` stream — many ticks a second, with evaluations debounced at
250 ms — so that factor understated annualised vol by `sqrt(ticks_per_hour/720)`,
and because the result was then compared with `max()` against the config
baseline, in any calm tape **the hard-coded 1.8%/h BTC constant won outright**.

Vol is the denominator of d2, so it directly sets how far spot must be from the
strike before the model will claim an edge. At a 0.15% distance with 10 minutes
left (verified with the real `gbm_win_probability`):

| vol used | P(YES) |
|---|---|
| 0.018/h (the constant) | 0.579 |
| 0.012/h (measured, 0.02%/tick at 1 tick/s) | 0.619 |
| 0.005/h (calm measured tape) | 0.768 |

To reach 0.768 at the constant, spot has to be **0.543%** above the strike
instead of 0.15% — the constant demanded 3.6× the move.

`strategies.utils.measured_vol_hourly()` now measures it: mean squared log
return divided by the **measured** mean tick interval, scaled to an hour, so it
is correct at whatever cadence the feed runs. Guard rails for degenerate data
only (≥ 20 ticks over ≥ 20 s, result kept within 0.1×-10× the config baseline).
`USE_MEASURED_VOL=false` restores the old behaviour.

Effect on the entry requirement — minimum spot distance that produces a real
SNIPE signal, driving the unmodified `SnipeStrategy.evaluate`:

| tape | measured vol | min dist @ mkt 0.55 | min dist @ mkt 0.45 |
|---|---|---|---|
| 0.020%/tick @1s (busy) | 1.33%/h | 0.454% | 0.753% |
| 0.008%/tick @1s (calm) | 0.72%/h | 0.387% | 0.643% |
| 0.004%/tick @1s (very calm) | 0.58%/h | 0.372% | 0.618% |
| **1.8%/h constant (old)** | 1.80%/h | **0.505%** | **0.837%** |

Roughly a quarter less distance required in a calm tape — and, importantly, the
measurement can now also go *above* the constant in a genuinely wild tape and
demand more, which the constant could never do. Tick-level estimates include
bid-ask bounce, so the measurement is conservative rather than flattering.

## 3. The ≥ 0.65 gap was real, and is fixed

```python
ev_ceil = min(config.SNIPE_MAX_MARKET_PRICE, max_ev_price(...))   # was
if market_price >= ev_ceil: reject
```

At `market_price == 0.65` the `min()` made `ev_ceil ≤ 0.65`, so the comparison
held for **any** model probability — verified: a raw 0.9995 was still refused
with `price_at_or_above_ev_ceiling`. The band cap and the economics ceiling were
conflated. The ceiling is now purely fee+margin economics; the band gate at
`SNIPE_MIN_ENTRY_PRICE..SNIPE_MAX_MARKET_PRICE` still owns the price limit
(`test_the_band_gate_still_caps_the_price` pins 0.66 → `entry_price_out_of_band`).
`max_ev_price(0.80, 0.65, 0.02, min_margin=0.03)` = 0.752 > 0.65, so 0.65 now
behaves like the rest of the band: it needs a blended ≈ 0.68, not infinity.

## 4. Priced out: lowering `SNIPE_MIN_CERTAINTY`

The floor admits `blended ≥ 0.50 + 0.45 × floor`. On a calm measured tape at a
0.55 market, the minimum spot distance each floor requires:

| floor | admits blended ≥ | min spot distance |
|---|---|---|
| 0.27 (today) | 0.622 | 0.387% |
| 0.20 | 0.590 | 0.251% |
| 0.15 | 0.568 | 0.185% |
| 0.10 | 0.545 | 0.185% — no further gain |

Two things fall out. Below ≈ 0.15 the certainty floor stops binding and
`SNIPE_MIN_BLENDED_EDGE = 0.025` takes over, so cutting further buys nothing.
And the economics are thin: at floor 0.15 the gate admits a blended 0.568
against a 0.55 market — 1.8 c of gross edge, and after the taker fee
(`0.02 × max(1−0.55, 0.5) = 1%`, cost/share 0.5556) that is **+1.2 c per share,
about 2.2% ROI**, *if* 0.568 is the true probability.

That conditional is the whole question, and it is now measurable.
`trades.certainty` stores the forecast and `trades.won` the outcome; the inverse
of `probability_to_certainty` recovers the predicted probability
(`w = 0.50 + 0.45c`), and `analysis.reliability_table(database.calibration_rows(...))`
buckets predicted against realised with the Brier score. If realised sits above
predicted in the 0.55-0.65 band, the floor is suppressing profitable entries and
lowering it is free money; if it sits below, the model is overconfident and the
floor is the only thing keeping the book safe. **I have not changed it** —
there are no resolved trades in this sandbox to measure against, and moving a
risk gate on a guess is exactly the mistake the rest of this report is about.

## 5. What is deliberately unchanged

`SNIPE_MIN_CERTAINTY` 0.27, `SNIPE_MODEL_WEIGHT` 0.35, `SNIPE_MIN_BLENDED_EDGE`
0.025, the 0.35-0.65 band, every distance calibration, `MAKER_MAX_BID` 0.58 and
MAKER's thresholds all stand. What changed is the *inputs* to the model (a
measured vol instead of a constant), one structurally broken comparison (the EV
ceiling), and the ability to check the model's honesty afterwards.

## 6. Verification

`pytest -q` → **252 passed** (236 before). 16 new tests in
`tests/test_model_and_calibration.py`:

* `measured_vol_hourly` recovers a known vol exactly (0.02%/tick at 1 tick/s →
  1.2%/h) and is `sqrt(5)` above the legacy 720 factor at that cadence;
* the kill switch, the short-history fallback and the flat-feed guard;
* the 0.65 band entry and the 0.66 band refusal, end to end through
  `SnipeStrategy.evaluate`;
* `reliability_table` flags a synthetic overconfident model (predicted 0.77 vs
  realised 0.75) and confirms a calibrated one (gap ≈ 0);
* `certainty_to_win_prob` inverts `probability_to_certainty` to 1e-9;
* the liquidity-reward summariser totals epochs, markets, paid and unpaid.

`tests/test_snipe_entry_gates.py::test_nothing_can_enter_at_the_top_of_the_price_band`
asserted the ≥ 0.65 defect as intended behaviour; it now fails, and has been
rewritten as `test_the_top_of_the_price_band_is_reachable`. That failure is the
fix working, and it is the reason this report exists rather than a guess.

**Unverified:** every live claim. No API credentials, no order book, no resolved
trades and no recorded evaluations exist here.
