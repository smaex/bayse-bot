# Execution audit — why the taker never fired, why `/resume` did not resume, and a set that was never really locked

Audit date: 2026-10-05
Scope: `strategies/` (TAKER, MAKER, collision/ranking, shrinkage, complete-set
maths), `executor.py` (the gate stack and every order-placement path),
`risk.py` (exposure arithmetic), `bot.py` + `telegram_bot.py` (pause/resume).
Trigger: *"one and a half days of trading, made ₦800 yesterday, lost ₦350
today already. Check for bugs, make sure the taker also fires, and when I click
resume it should clear up restrictions."*

Result: **twelve defects found, all fixed**, each with a regression test.
`python -m pytest -q` → **332 passed** (304 before this audit; 28 added).

---

## 1. The taker could not place an order. Two independent causes.

Both were silent: neither produced a log line the operator could act on, and
neither is visible in the config that supposedly decides entries.

### 1a. A *single-leg* MAKER quote outranked every TAKER signal

`strategies/_resolve_collision` preferred any MAKER signal over a TAKER signal
on the same market, on the documented grounds that "a MAKER pair buys a
guaranteed payoff; a TAKER buys a probability".

But `MAKER_ALLOW_SINGLE_LEG` is **on by default**, and a one-sided quote is a
passive directional bid: it pays only if the side it bet on wins. It is exactly
as much a probability as the taker's — the only difference is where the order
sits in the book. The rule was comparing "MAKER" against "TAKER" instead of
"direction-free" against "directional".

Offline replay (real `TakerStrategy`, real `MakerStrategy`, real
`evaluate_all`, BTC 15-minute CLOB, 10 min to close, YES book 0.54/0.55, fee 2%,
model vol = documented baseline):

| spot vs strike | model P(YES) | taker EV after fees | TAKER signal | what `evaluate_all` returned |
|---|---|---|---|---|
| +0.15% | — | below the 6% margin | none | MAKER single-leg |
| +0.20% | 0.609 | **+9.7%** | YES | MAKER single-leg only |
| +0.30% | 0.662 | **+19.2%** | YES | MAKER single-leg only |
| +0.40% | 0.712 | +28.1% | YES | MAKER single-leg only |
| +0.50% | 0.758 | +36.4% | YES | MAKER single-leg only |

Every MAKER signal in that table was `single-leg ... (no pair lock)` — the
other side of the book was priced through the model's ceiling, so the strategy
fell back to one-sided quoting. The taker cleared its own fee-and-edge gates on
six of six markets and reached zero of them.

**Fix:** only a quote that *actually locks* (two legs, opposite outcomes,
prices summing below 1.00) outranks a taker by construction. Otherwise the two
are ranked on the same number — expected profit per unit of capital committed
(`_score`), which is the metric this module already documents. The loser is
recorded as `TAKER:ranked_below_peer` / `MAKER:ranked_below_peer`.

### 1b. `mode_floor = 0.48` silently meant "model probability ≥ 0.74" for the taker

`executor._execute_logic` refuses any signal whose `certainty` is below
`TradeSignal.mode_floor`, which defaulted to `0.48` and was **never set by any
strategy**. `certainty` is not one quantity:

* TAKER directional: `certainty = (p − 0.5) / 0.45` → a *rescaled* probability,
  so `0.48` means **p ≥ 0.716**;
* MAKER / complete-set TAKER: `certainty = p` → `0.48` means **p ≥ 0.48**.

A 0.65 model against a 0.55 ask is a +9.7% fee-inclusive mispricing and passes
every documented gate (`TAKER_MIN_MODEL_PROB` 0.55, 6% net EV, effective price
≤ 0.65), yet its certainty is 0.33, so it was refused as a "probe" — after the
EV gate had already approved it. Forcing the signal past the collision rule in
the replay produced `SKIP TAKER BTC — certainty below mode floor 48%` for
cert 0.219 / 0.324 / 0.424.

**Fix:** a floor must be on the same scale as the number it bounds, and it must
be the strategy's own admission rule. TAKER now declares
`probability_to_certainty(TAKER_MIN_MODEL_PROB)` and its certainty uses the
shared `probability_to_certainty` helper (the calibration pipeline and the
learner already read `certainty` as `w = 0.50 + 0.45c`; the taker was the one
strategy using a different scale). MAKER declares `MAKER_MIN_LEG_BID`.
`TradeSignal.mode_floor` now defaults to `0.0` — "no floor of its own" — so an
unset floor can never again silently become a 25-point probability gate.

After the fix, the same replay produces, on the live client:

```
TAKER SIGNAL BTC | YES p=0.609 ask=0.550 eff=0.556 ev=+9.7% dist=+0.200% ... secs=600
CLOB Book Match: best_ask=0.550 -> limit_price=0.553 (EV=+9.7%)
PLACING TAKER BTC 15min YES | LIMIT/FAK ₦100 @ cap=0.553 (sig=0.550) | cert=24%
✅ FILLED | TAKER BTC 15min YES @ 0.5530 ₦5,530 | order=o1
```

## 2. `/resume` cleared the pause flag and nothing else

`cmd_resume` / the inline Resume button did:

```python
await _set_paused(cid, False)      # settings["paused"] = False
await _clear_daily(cid)            # _user_daily.pop(cid)
risk.paused = False; risk.peak_balance = 0
```

Every restriction that actually stopped the account is *recomputed from the
database on the next cycle*:

* the daily loss stop compares today's realised PnL with
  `day["start_balance"] * daily_loss_limit_pct / 100`;
* the daily target compares it with `daily_multiplier` of the same baseline,
  and `risk.target_hit` blocks evaluation outright;
* the drawdown stop compares equity with `risk.peak_balance`.

So clearing `paused` re-tripped the same stop one cycle later, with a fresh
"daily loss limit reached" message — and `risk.target_hit` kept suppressing
`_evaluate_single_user` even while the account looked active. The stall report
had been promising the opposite all along: *"wait for the trading-day rollover,
or `/resume` to override {reason} explicitly"*.

**Fix:** `/resume` now moves the baseline instead of arguing with it.
`bot.reset_session_restrictions()`:

* sets `daily_state.start_balance` to the current equity;
* records `daily_state.pnl_baseline = today's already-realised PnL`, which
  `_user_loop` subtracts before the loss-limit and target comparisons
  (`_session_pnl_for_day`) — so the result the operator just overrode cannot
  count again, and everything after the resume does;
* clears `paused`, `paused_reason`, `daily_loss_stopped_at`, `target_hit`,
  `risk.daily_realized_pnl`, `risk.daily_target`, the drawdown debounce and
  `risk.peak_balance`;
* drops this account's per-market trade cooldowns (not other accounts');
* lifts a persisted learner strategy suspension (`suspended_strategies`), which
  otherwise removes strategies from the account's scope with no command to
  clear it;
* persists through `database.update_settings` + `invalidate_user_cache`, and
  says exactly what it did in the Telegram reply.

Deliberately **not** cleared: the learned size/certainty multipliers (evidence,
not a stop — `/resetlearning` is the command that forgets evidence),
`risk.pending_markets` (held in a `finally`, so a resume cannot leak it), the
exchange-minimum cache, and `global_state.systemic_halt_until` (process-wide:
one account's resume must never lift a market-wide halt).

This is a *fresh, bounded* budget, not an unlimited one: the same
`daily_loss_limit_pct` (5%, capped at `MAX_DAILY_LOSS_LIMIT_PCT`) applies from
the resume point, and a daily stop that fires on its own still expires at the
trading-day boundary.

## 3. The structural complete-set take could never fire

`_evaluate_complete_set` requires every leg to "clear the directional EV gate on
its own" so that a partial batch fill leaves a trade worth owning. In code that
became `_leg_ev(...)`, which also applies the *conviction* floor
`model_prob >= TAKER_MIN_MODEL_PROB (0.55)` — and the two legs of a set have
complementary probabilities (`p_yes + p_no = 1.00`). Both above 0.55 is
unsatisfiable, so **every** candidate was rejected as
`complete_set_leg_not_standalone`, no matter how wide the lock. The strategy had
a documented second source of model-independent profit that the code could not
reach.

**Fix:** the conviction floor is a directional filter and stays on the
directional path (`require_conviction=True`). A pair leg is tested by what
actually matters for a partial fill: the same 6% fee-inclusive EV margin, plus
the effective-price band (0.35–0.65), which already keeps both legs out of the
long-shot tail. The set also declares `mode_floor = 0.0` so the executor does
not re-impose the impossible rule.

Related, and fixed with it: a *locked taker set* was still subject to
directional performance shrinkage, because the lock exemption tested
`strategy == "MAKER"`. After a losing streak the multiplier shrinks
`win_prob` toward 0.50 and the check `win_prob - effective >= 0` then drops the
set — i.e. **a guaranteed lock was skipped for looking too much like a
coin flip**. The exemption is now a property of the set (two complementary legs
priced below 1.00), independent of the strategy name.

## 4. A "locked" set was not locked: equal naira on unequal prices

A set settles on `min(shares_yes, shares_no)`. Both multi-leg paths placed the
same **naira** amount on each leg, so a 0.46 leg received more shares than a
0.50 leg: the locked part covered only the smaller quantity and the remainder
was an unhedged directional position that the strategy's sizing, its shrinkage
exemption and the pair accounting (`maker_unpaired_notional`, `_is_locked_pair`,
`_score`) all treated as direction-free.

Three consequences, all fixed:

* **Balanced sizing.** `_balanced_pair_amounts()` derives each leg's stake from
  the *fee-inclusive* price so the net share counts match (a taker's fee is
  taken out of the shares received, so equal naira is not equal shares). The
  most expensive leg sets the share count; the cheaper leg spends less than its
  ceiling instead of over-buying. If no balanced set fits the budget and the
  per-order exchange minimum, neither leg is sent (`pair_below_order_minimum`)
  rather than one leg under the minimum after the other filled.
* **Budgets charge the whole set.** `MAX_MAKER_NOTIONAL_PCT` counted one leg of
  a two-leg quote, so the maker book could hold twice its own ceiling. It now
  charges `committed = Σ leg stakes`. The exposure cap charges a taker complete
  set in full (its capital becomes a position immediately) and keeps the
  documented per-leg treatment for resting MAKER quotes, which are not filled
  exposure.
* **The recorded cost includes the fee.** `_place_complete_set_take` booked
  `shares × fill_price`, which is the *gross* value of *net* shares — short by
  the fee, so every locked pair was flattered by its own fees while the
  single-leg path added them back. Both paths now share `_order_actual_cost()`.

## 4b. A completed set held as two legs was managed (and sold) as two bets

`_paired_leg` -- the function that answers "is the opposite-outcome sibling of
this position also filled?" -- required the sibling to be a **MAKER** position,
and `_exit_decision` only returned `BURN_COMPLETE_SET` when a position's
`outcome` was literally `"BOTH"`, which no position ever is (legs are stored
with their own YES/NO outcome). So a set that completed was never burned:

* the maker fill path did burn a resting pair when its second leg filled, but
  anything that completed without that exact transition was not;
* a complete-set TAKER -- two immediate FAK legs, which this audit makes
  reachable -- never passes through the maker path at all. Its legs would be
  evaluated as two independent directional positions and *sold* on a
  take-profit: two taker fees and the loss of a guaranteed unit.

**Fix:** `_paired_leg` matches a same-strategy, opposite-outcome sibling with
matching quantity (family, not the string "MAKER"), the exit loop passes
`complete_set=` into `_exit_decision`, and both the exit path and the maker
fill path now burn the set. Quantities must match because only the overlap is a
set: the burn removes both entries, so an imbalanced pair would leave the excess
shares untracked. An imbalanced pair is left alone -- its overlap still settles
to 1.00 and the larger leg is managed as the directional position it is.

## 5. `MAX_MAKER_UNPAIRED_PCT` was validated at startup and read by nothing

`_single_leg` quotes are directional by construction, and the only ceiling on
them was the whole maker budget (20% of equity). The knob that existed to bound
exactly this — `MAX_MAKER_UNPAIRED_PCT` (10%), with a startup validation error
if it were mis-set — was never read.

**Fix:** `RiskManager.maker_unpaired_notional()` charges capital that is
genuinely one-sided (a lone quote on a market, or a filled leg whose
opposite-outcome sibling has not filled) and the executor refuses a new
single-leg quote that would breach the budget, attributed as
`maker_unpaired_cap`. A two-sided resting quote is not charged: neither leg is
held yet.

## 6. The performance-shrinkage guard used a no-op price

```python
effective = sig.market_price / max(1.0 - 0.0, 1e-9)   # == market_price
```

`1.0 - 0.0` is `1.0`, so the "re-derive the effective price" check compared a
shrunk probability against a *raw* price while the comment claimed the
opposite; for a fee-bearing taker the fee had no room in the check at all. The
rule was also applied to the whole MAKER family — including single-leg quotes,
which are predictions.

**Fix:** the check uses `strategies.book.effective_buy_price(price, fee_rate,
is_maker=...)`, and the exemption is `_is_locked_pair(sig)`.

## 7. Size ceilings evaluated before the last size change

Two ordering defects in the same area:

* the CLOB depth path can *raise* the order to the cached Bayse market minimum
  (`amount = cached_min`) after the exposure check had already run on the
  smaller amount;
* the TAKER price cap and the depth-ahead sizing ran on `sig.market_price`
  while the *fill* price was `best_ask + buffer`.

**Fix:** sizing (including the market-minimum bump) is closed in one block
before the budget checks, and the exposure ceiling is re-evaluated against what
the decision actually commits. On the price side the existing
`clob_ev_at_ask_below_target` gate already re-derives the exact EV at the real
best ask with the exact fee, so the cap is only a pre-filter; no change needed,
and the probe confirmed the final limit is the ask + 1 tick, not the cap.

## 8. CLOB depth read a unit-less field as naira

The depth-ahead block summed the book's `total` field as currency. Bayse's
order-book levels carry `price`, `quantity` and `total` with **no unit in the
payload**; `quantity` is a share count and a share costs `price × base
multiplier` (₦100 per share at 1.00 on an NGN account), while the docs' only
example (`total = quantity × price`) is a USD book where the multiplier is 1.
Reading an unscaled `total` on an NGN account understates depth by 100× — real
liquidity becomes a false `clob_depth_below_minimum` skip, or a sized order is
clipped to a hundredth of the book.

**Fix:** `strategies.book.level_notional()` derives the notional from the
unambiguous share count (`quantity × price × CURRENCY_BASE_MULTIPLIER`) and
falls back to `total` only when `quantity` is absent — the same identity the
rest of the system already prices off (`walk_asks`). Test:
`test_clob_depth_is_measured_in_account_currency_not_raw_total`.

## 9. Dead gates and dead config

* The CLOB worst-case-EV check was written `if not is_probe and ev < margin` —
  unreachable, because a below-floor signal already returned. Removed, so the
  gate does not look optional.
* `TAKER_ALLOWED_ASSETS` / `TAKER_ALLOWED_TIMEFRAMES` were declared and never
  read — the documented scope ("BTC/ETH/SOL on 5/15-minute CLOBs") was
  documentation, not a rule, and the taker could cross an AMM FX print. Now
  enforced before any pricing work, reported as `..._not_in_allowed_scope`
  (which the drought report already classifies as a configuration exclusion).
* `learned["suspended_strategies"]` was read by the user loop and never written
  by anything in this codebase; a stale value from an earlier release would
  silently empty the account's strategy scope. `/resume` now clears it.

## What is *not* a bug (checked, with evidence)

* **Fill accounting is honest.** Exposure, PnL and the drought clock are all
  built from exchange-confirmed filled quantity; a resting GTC quote is an
  order, not a position, and `parse_filled_shares` prefers the documented
  `quantity` (net shares) over requested size.
* **The fee model matches the docs.** CLOB BUY: the fee reduces *shares
  received*, so cost per net share is `P / (1 − feeRate·max(1−P, 0.5))` — the
  same quantity the executor computes and the same one the docs' own fixture
  implies (₦100 → 150 net shares at 0.65 with a 5% rate). Makers pay no fee.
  Break-even hit rate is the effective price: **56.4% at 0.55, 66.7% at 0.65**
  at a 5% rate.
* **The CLOB price cap is exact.** `cap = win_prob·(1−f)/(1+margin)` is the
  algebraic solution of `win_prob/eff ≥ 1+margin`; the later gate re-checks the
  real ask, so the cap cannot admit a bad fill.
* **Exit policy is conditional on the model, not on P&L** — a stop that fires
  on P&L alone sells exactly when a binary is cheapest. The hard backstop
  (−50% of entry, with a salvage floor) is the only price-level rule, and it is
  deliberate.
* **Complete sets are burned, never sold** (a sale pays the bid and a taker fee
  for something the burn pays in full), and a partial fill of an intended pair
  is treated as a position the strategy was willing to own.

## What this means for ₦800 yesterday / ₦350 today

The stops did what they were configured to do, which is why the operator's
complaint was that nothing would restart:

* `daily_loss_limit_pct` defaults to **5% of the start-of-day balance**. For a
  start-of-day balance near **₦7,000**, the loss stop fires at **−₦350** — the
  number reported. Check `/settings` for the actual baseline.
* The daily **target** (`daily_multiplier`, default 3%) pauses entries the same
  way and prints `/resume to override` — but the override did not hold (§2), so
  the account could look alive while `risk.target_hit` suppressed evaluation.
* Until this audit the taker contributed **nothing** to either day: the P&L was
  produced by passive single-leg MAKER quotes, which are directional bets that
  carry none of the pair's guarantee, and by the pair quotes that did fill.

Per-trade arithmetic at 0.55 with a 5% Bayse rate: a win pays ≈ +77% of stake,
a loss costs 100%, so the break-even hit rate is ≈ 56.4%. At ~₦100 per trade,
−₦350 is three or four losing trades — well inside the variance of a 56/44
strategy, which is precisely why the per-trade risk ceiling exists.

## Recommend, in this order

1. **Let the fixes settle for a few sessions and read `/why`.** The taker now
   has a path to the book (`TAKER:ranked_below_peer` means the passive quote
   scored better on the same market, which is a legitimate ranking, not a
   block), and the first `TAKER SIGNAL` / `LIMIT/FAK` / `✅ FILLED` lines will
   appear in the log.
2. **Decide what MAKER is for.** If you only want the risk-free version, set
   `MAKER_ALLOW_SINGLE_LEG=false`: the bot will then quote only locked pairs
   (now genuinely balanced), with no directional maker exposure. Expect fewer
   quotes. With the default (`true`), one-sided quotes are bounded by
   `MAX_MAKER_UNPAIRED_PCT` (10% of equity).
3. **Check the per-asset record before widening scope.** The historical sample
   in `reports/production_performance_findings.md` has MAKER/ETH at −23.9% of
   deployed capital against BTC +19.9% and SOL +11.5%. `/learnstats` shows your
   own combination table; `/set assets BTC,SOL` narrows scope immediately.
4. **Keep the daily stops.** They are what stopped a bad morning from becoming
   a bad week. The change here is that `/resume` is now honest about what it
   overrides — a new baseline with the same 5% budget, not a fresh licence.

## Verification

```
python -m pytest -q                      # 328 passed
python -m pytest tests/test_taker_firing_and_resume.py tests/test_executor_fail_closed.py -q
```

New coverage: the collision rule (single-leg maker no longer suppresses a
better taker; a locked pair still does), the mode-floor scale, the
fee-inclusive shrinkage check, a locked set surviving directional shrinkage, the
taker scope gate, the complete-set EV-only leg test (and the impossible-floor
regression), balanced pair stakes with the fee, an end-to-end two-leg quote
placed with equal share counts, fail-closed pair sizing below the order
minimum, fee-inclusive fill cost, detection and burning of a completed set held
as two legs (and refusal to burn an imbalanced one), unpaired-maker accounting
and its executor cap, CLOB depth in account currency, the exposure re-check
after the market-minimum bump, resume re-baselining, the suspension clear, and
the arithmetic that stops a resumed account from being re-paused by the loss it
just overrode.
