# "HEALTHY" with 0 orders, "check /settings" every 15 minutes, and a drought clock that disagreed with itself

Date: 2026-09-26
Scope: the trading-stall report and `/why` (`stall.py`), the watchdog that sends it (`bot.py`), and one telemetry counter in `executor.py`.
Constraint honoured throughout: **no gate, ceiling, price cap or sizing value was changed.** Every fix below is a sentence the report was getting wrong. `MAKER_MAX_BID` is still 0.58, SNIPE's gates are untouched, and nothing here makes the bot trade more.

## What the operator saw

Twenty-six stall alerts between 1431 and 1604 minutes (≈24–27 h) after the last confirmed fill, containing:

```
🩺 Trading stall — 1450 min without a confirmed fill
🟢 The trading pipeline is evaluating and has placed orders.
Code: HEALTHY
1214 evaluation(s), 0 signal(s), 0 order(s) placed in this process.
```

```
🩺 Trading stall — 1467 min without a confirmed fill
🟠 None of the 6 open markets match this account's scope.
Code: SCOPE_EMPTY
→ Check /settings assets, timeframes and strategies; …
```

```
🩺 Trading stall — 1573 min without a confirmed fill
🟠 None of the 3 open markets match this account's scope.
Code: SCOPE_EMPTY
Last confirmed fill: 1572 min ago
```

Three things were false, and a fourth number was wrong by a factor of two.

---

## 1. `HEALTHY` was the fall-through, so it asserted a placement that never happened

`stall.verdict()` ends in an unconditional `HEALTHY`: *"The trading pipeline is evaluating and has placed orders."* Nothing in that branch checked `orders_placed`. The line below it printed the truth — `0 signal(s), 0 order(s) placed` — so one alert contradicted itself.

Why it fired at all: `NO_EDGE` was gated on `markets_evaluated > 0` **in the last pass**. A feed-triggered pass evaluates one asset; a scanner-triggered pass evaluates none. So the same account alternated:

| minute in the paste | `markets … evaluated in the last pass` | verdict |
|---|---|---|
| 1450 | 0 | `HEALTHY` |
| 1451 | 1 | `NO_EDGE` |
| 1452 | 0 | `HEALTHY` |
| 1453 | 1 | `NO_EDGE` |

`stall.note_alert()` deliberately bypasses its rate limit when the verdict *code changes* ("the previous explanation was wrong"), so each flip sent an alert: four alerts in four minutes, forever, on an account whose state had not changed.

**Fix.** The fall-through now states only what the counters show:

* no order ever placed and no signal ever produced → `NO_EDGE` computed from **process** counters (`1214 evaluation(s) in this process, 0 market(s) in the last pass; no candidate has cleared its gates` + the dominant gates);
* signals produced but the executor recorded no attempt for any of them → new `SIGNALS_NOT_EXECUTED` (warn). `merge_signals` legitimately collapses a burst of raw signals — one per market/strategy, weaker opposing side dropped — so `12 signals | 2 orders placed` is not by itself a fault, but "healthy pipeline" was the wrong word for it;
* `HEALTHY` is now only reachable once an order has been placed **and** a fill confirmed (an order with no fill already returns `NO_CONFIRMED_FILL` above it), and its wording says so: *"evaluating, placing orders and recording fills"*, with the fill count and the age of the last fill in the detail.

Reproduced on the pre-fix code by `tests/test_stall_false_verdicts.py`: the verdict sequence for alternating passes comes back `['NO_EDGE', 'HEALTHY', 'NO_EDGE', 'HEALTHY', 'NO_EDGE', 'HEALTHY']`, and after the fix `['NO_EDGE'] * 6` with exactly one alert.

## 2. `SCOPE_EMPTY` fired on the exchange's calendar, not on the account's settings

Every ~15 minutes the paste pairs two alerts one minute apart:

```
1467 SCOPE_EMPTY → 1468 NO_CONFIRMED_FILL     1542 SCOPE_EMPTY → 1543 NO_CONFIRMED_FILL
1482 SCOPE_EMPTY → 1483 NO_CONFIRMED_FILL     1557 SCOPE_EMPTY → 1558 NO_CONFIRMED_FILL
1497 SCOPE_EMPTY → 1498 NO_CONFIRMED_FILL     1573 SCOPE_EMPTY → 1574 NO_CONFIRMED_FILL
1512 SCOPE_EMPTY → 1513 NO_CONFIRMED_FILL     1588 SCOPE_EMPTY → 1589 NO_CONFIRMED_FILL
1527 SCOPE_EMPTY → 1528 NO_CONFIRMED_FILL     1603 SCOPE_EMPTY → 1604 NO_CONFIRMED_FILL
```

Same mechanism as §1: the code changed, so the rate limit was bypassed twice per cycle — ten false "your settings are wrong" alerts per 2.5 h, each one *replacing* the verdict that was actually true (`NO_CONFIRMED_FILL`, 2 orders placed, 0 fills).

**What the counters say about the cause.** The account's scope is `assets=[BTC, ETH, SOL, EURUSD, GBPUSD, XAUUSD]`, `tfs=[15min, 5min]`. In the healthy passes the report reads `9 open, 3 in scope`; in the SCOPE_EMPTY passes, `6 open, 0 in scope` (once `3 open, 0 in scope`). The lifetime skip counters printed in the same alerts are only `timeframe` and `trigger` — `asset`, `status`, `stale_feed` and `no_spot` are all zero for the whole process, or they would appear in the top five. So the 6 remaining markets were all excluded **by timeframe**: they are in-scope assets on 1h/6h/1d series, and the 3 that disappeared were exactly the account's 15-minute markets. That is consistent with the two placements in the paste, both on `15min` (`BTC 15min`, `SOL 15min`).

`scanner._enrich` drops an event when `secs_to_open > 0` or `secs_to_close < 0`, so in the minutes around a 15-minute boundary the round has closed and the next has not opened: the account genuinely has nothing in scope for a moment. (A partial scan would look the same — `scan_all` swallows per-series fetch failures and returns whatever succeeded. Both are "the market list shrank", and the fix below covers both. Which of the two it was on this tape is **inferred from the counters, not observed**: I cannot query Bayse from here.)

**Fix.** An empty scope has to *persist* to be called a configuration error. `note_evaluation` now timestamps the start of a run of empty-scope passes, and `verdict()` raises `SCOPE_EMPTY` only after `SCOPE_EMPTY_CONFIRM_SEC` (900 s — one full cycle of the longest in-scope timeframe). A boundary gap stays a one-line note on the Markets line instead: `Markets: 6 discovered (6 open), 0 in scope, 0 evaluated in the last pass — nothing in scope for 2 min`. A real misconfiguration still fires, and now says *why* instead of guessing:

> None of the 6 discovered markets match this account's scope, and none has for 16 min.
> Last pass saw 6 market(s): 6 excluded by timeframe. Scope: strategies=['SNIPE','MAKER'] assets=[…] tfs=['15min','5min']

The reason comes from the pass that found nothing (`last_pass_skips`, new), not from lifetime counters that cannot answer "why did *this* pass match nothing".

## 3. The drought clock was printed from two different values

One alert read `Trading stall — 1573 min` over `Last confirmed fill: 1572 min ago`; another read `1513` over `1514`. The paste has both directions, which ruled out my first explanation (the header and the body reading `time.time()` a few microseconds apart can only differ upwards).

The real cause is **double rounding**. `verdict()` pre-rounded the gap to one decimal, `format_report` then rendered it with `f"{x:.0f}"`, and Python rounds halves to *even*:

| raw gap | header `f"{raw:.0f}"` | body: `round(raw,1)` then `.0f` |
|---|---|---|
| 1512.5001 | `1513` | 1512.5 → `1512` |
| 1513.4999 | `1513` | 1513.5 → `1514` |
| 1572.5001 | `1573` | 1572.5 → `1572` |

Every observed pair in the paste is one of those two rows. The `NO_CONFIRMED_FILL` detail (`No confirmed fill for N min`) formatted the *unrounded* value, so a single message could print the gap three times with two different answers.

**Fix.** `stall.format_gap_minutes()` is now the only renderer of that number — half-up, from the unrounded value — and the verdict no longer pre-rounds. The watchdog header, the log line, the "Last confirmed fill" line and the `NO_CONFIRMED_FILL` detail all go through it. The watchdog also reads one `time.time()` and passes it to `report()`, `trade_gap_minutes()` and `note_alert()`, so a sub-second drift cannot straddle a minute boundary either.

## 4. Every executor-outcome count was double

`executor._stall_skip` called `stall.note_order(..., placed=False, reason=code)` **and** `stall.reject(chat_id, "exec", code, detail)`. Both bump the same `exec:<code>` counter, so every skip counted twice. The numbers an operator sees for execution outcomes — `exec:maker_quote_behind_book ×37` in a `MAKER_QUOTE_UNCOMPETITIVE` detail, quoted in `reports/maker_zero_fill_diagnosis.md` — were twice the real count. `note_order` now takes the detail and records the skip once; `_stall_skip` makes one call. Asserted through the real executor helper: 9 skips → `count == 9`.

## 5. Two labels that were not true

* `Markets: 6 open` counted every market the scanner was holding, including ones whose status is not open. The line now reads `6 discovered (4 open)`, with the open count taken from the last pass; when no pass has reported one it prints only the discovery count rather than guessing.
* The report explained nothing about execution. `🚪 Gates that stopped candidates` is process-lifetime and dominated by strategy gates (`SNIPE:no_raw_edge_or_trend_alignment` reached 9998), so `12 signals | 2 orders placed` had no explanation anywhere in the message. New line, recency-windowed like the other valves:

```
🛠 Executor outcomes (last 15 min):
  exec:market_cooldown: 9× (last 4s ago)
  exec:maker_quote_behind_book: 1× (last 61s ago)
```

  `market_cooldown` is included here even though it is still excluded from *naming a root cause* — it is the echo of every placement and skip.

---

## The actual trading question: 2 orders in 27 h, 0 fills

Nothing above changes that, and it should not be "fixed" by loosening a gate. What the paste shows:

* MAKER produced 12 signals and placed 2 resting quotes, at **0.574** (model fv 0.624) and **0.580** (fv 0.742) — both at or within one tick of `MAKER_MAX_BID = 0.58`. Neither filled inside `MAKER_ORDER_TIMEOUT`.
* The dominant MAKER gates were `distance_below_calibration` (7083×), `late_candle_window` (1875×) and `candle_warmup_window` (1634×) — i.e. most candidates never became quotes at all.
* A post-only bid at 0.58 only rests competitively when the book's best bid is at or below ~0.59; the live-book check added in `reports/maker_zero_fill_diagnosis.md` skips the rest as `exec:maker_quote_behind_book`. With §5 above, `/why` will now *show* how often that happens instead of leaving it invisible.

So the next step is evidence, not a ceiling change: run `/makershadow` and `tools/simulate_economics.py` against signal-time books, and let the observed fill prices decide whether 0.58 is too low. Raising `MAKER_MAX_BID` spends the model's claimed edge on price and is only safe if that fv is calibrated.

## Deliberately not changed

`MAKER_MAX_BID` (0.58), the `fv ≥ 0.62` / 2-cent edge entry rule, every SNIPE gate, `TRADE_COOLDOWN_SEC`, the alert rate limit, and the rule that a changed verdict code alerts immediately.

## Verification

* `tests/test_stall_false_verdicts.py` — 21 new tests: replays of the pasted counters (false `HEALTHY`, the `NO_EDGE`/`HEALTHY` flap, the boundary `SCOPE_EMPTY`), the persistent-scope case that must still fire, the half-up rounding table, a watchdog run asserting header == body and that both clocks are the same reading, the discovered-vs-open label, executor-outcome listing and expiry, Telegram legacy-Markdown validity of the new lines, and JSON serialisability of the new report fields.
* Mutation check: with `stall.py`/`bot.py`/`executor.py` reverted to `HEAD` (and the new `open_markets=` argument stripped so the tests fail on behaviour, not on API shape), **all 21 fail**. The reverted code returns `{'code': 'HEALTHY', 'headline': 'The trading pipeline is evaluating and has placed orders.', 'detail': '1214 evaluation(s), 0 signal(s), 0 order(s) placed…'}` and the flap sequence `['NO_EDGE', 'HEALTHY', 'NO_EDGE', 'HEALTHY', …]`.
* Full suite: `210 passed` (189 before). No existing test was modified.
* Not checked here: live behaviour against Bayse. Whether the 15-minute `SCOPE_EMPTY` cadence was a series boundary or a partial scan cannot be confirmed without querying the exchange; both produce the same signature and both are handled.
