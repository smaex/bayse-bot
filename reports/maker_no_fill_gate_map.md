# MAKER: where the 12 signals, 2 orders and 0 fills come from

**Run analysed:** the Telegram stall alert of 2026-09-26 — 10,615 evaluations,
12 signals (all MAKER), 2 orders placed, 0 confirmed fills.

**Method:** every number below is produced by running this repository's own
code (`MakerStrategy.evaluate`, `_maker_quote_against_book`,
`gbm_win_probability`, `clob_buy_effective_price`) over controlled inputs. No
live or recorded evaluation data was available, so **nothing here is a
measurement of the production run** — it is what the code does when driven the
way the loop drives it.

---

## 1. The funnel, from the counters in the alert

| Gate | Count | Share of 10,615 |
|---|---|---|
| `MAKER:candle_warmup_window` (>750 s left) | 1,634 | 15% |
| `MAKER:late_candle_window` (<180 s left) | 1,875 | 18% |
| `MAKER:distance_below_calibration` | 7,083 | 67% |
| `MAKER:too_close_to_settle` (<45 s) | 161 | 2% |
| `MAKER:no_trend_or_edge_alignment` (now split, see §5) | 475 | 4% |
| signals | 12 | 0.11% |

The two window gates are not defects: MAKER hardcodes a quoting window of
**180 ≤ secs ≤ 750** inside a 900-second candle, so 37% of every round is out
of bounds by design, and the observed 33% is consistent with roughly one
evaluation per second. `distance_below_calibration` then refuses two thirds of
what remains. Twelve candidates survive, two become resting quotes, and both
expire.

---

## 2. An order can only reach the exchange in a narrow band of market prices

`our_bid = clamp(min(fv − 0.025, max(mid + 0.01, fv − 0.05, 0.520)), 0.50, 0.58)`
and the executor then re-checks the price against the live book
(`_maker_quote_against_book`: step one tick inside the ask rather than cross;
reject if more than `MAKER_MAX_TICKS_BEHIND_BEST_BID` = 1 tick under the best
bid). Running the real strategy over a grid, the price actually sent is:

| fv \ Bayse mid | 0.50 | 0.52 | 0.54 | 0.56 | 0.57 | 0.58 | 0.59 | 0.60 | 0.62 | 0.64 |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.620 | 0.50 | 0.52 | 0.54 | 0.56 | 0.57 | · | · | · | · | · |
| 0.640 | 0.50 | 0.52 | 0.54 | 0.56 | 0.57 | 0.58 | 0.58 | · | · | · |
| 0.650 | 0.50 | 0.52 | 0.54 | 0.56 | 0.57 | 0.58 | 0.58 | 0.58 | behind | behind |
| 0.700 | 0.50 | 0.52 | 0.54 | 0.56 | 0.57 | 0.58 | 0.58 | 0.58 | behind | behind |

(`·` = the strategy produced no signal; `behind` = `maker_quote_behind_book`.)

Read it as three facts:

* **The bid saturates at the 0.58 ceiling for any fv ≥ 0.64.** Above that,
  more conviction does not buy a better price — it buys the same 0.58.
* **Quoting stops at a market mid of ~0.60**, because a 0.58 bid is then more
  than a tick under the best bid. Combined with the 2c edge gate
  (`fv − mid ≥ 0.02`), MAKER only ever quotes markets the exchange prices
  between roughly 0.50 and 0.60 — near coin flips. That is exactly what the
  two production quotes were: 0.574 and 0.580.
* At a 0.58 bid, `clob_buy_effective_price(0.58, 0.02)` = 0.5859, so the
  **break-even true win rate is 58.6%** (58.0% at 0.574). The ceiling is not
  generous; it is the price at which the strategy is roughly break-even if its
  fair value is honest.

---

## 3. Fair value is mostly extrapolated drift, not price position

MAKER prices YES with `gbm_win_probability(..., hourly_drift=<Kalman velocity>,
horizon_cap=180)`. At BTC's minimum qualifying distance (0.08%) the split is:

| secs left | fv from position | fv with a 3%/h drift | drift's share |
|---|---|---|---|
| 700 | 0.539 | 0.612 | +7.4 pts of +11.2 |
| 400 | 0.552 | 0.648 | +9.6 pts of +14.8 |
| 200 | 0.574 | 0.705 | +13.1 pts of +20.5 |

A 3%/h drift is an ordinary 0.05%/min move. Inside the normal quoting window it
contributes **more than twice** what the price distance does, and at 400 s it
alone carries fv over the direction gate on a distance worth ~5 points. This is
precisely the failure mode `projected_drift_pct`'s own docstring records —
"across 6 real trades, drift was 5x to 379x larger than the actual raw price
distance". SNIPE calls the same function with `hourly_drift=0.0`; MAKER does
not. So a 0.58 bid is a bet that a 3-minute extrapolation of momentum holds for
the remaining 3-10 minutes, against counterparties who are filling it because
they disagree.

**This is the most likely reason the 2 quotes did not fill and why filling
them would not obviously be profitable.** It is a risk question, not a bug, and
it is not changed here.

---

## 4. Fixed: MAKER priced itself off a different oracle than the loop used

`MakerStrategy.evaluate` accepted `spot_price` and never read it. It re-read the
feeds with a **private 10-second staleness rule** and fell back to the Bayse
relay:

```python
spot, t = feeds_direct.get_direct_price(asset)
if not spot or (time.time() - t) > 10:
    spot = feeds.spot.get(asset, 0.0)
```

The evaluation loop selects an oracle once per pass — direct Binance price
while fresh, relay as a documented fallback, else it skips the market as
`stale_feed` — using `FEED_STALE_SEC` = 30 s, and hands it to every strategy.
SNIPE uses it. MAKER discarded it, so:

* for an independent oracle aged **10-30 s**, SNIPE traded on the Binance price
  while MAKER computed "fair value" from the **Bayse relay — the same source as
  the market price it compares against**. `feeds_direct.get_direct_price`'s own
  docstring says "Never substitute the Bayse relay here", and
  `REQUIRE_DIRECT_ORACLE` exists to prevent it;
* `_fair_value` contained a **second, independent copy of the same re-read**, so
  one decision could compare a `dist_pct` from one price against a fair value
  computed from another.

Both now take the caller's price and keep the old re-read only as a fallback
when no price is passed. Covered by
`tests/test_maker_entry_gates.py::test_maker_uses_the_oracle_price_the_loop_handed_it`
(the loop's 100,200 above the strike vs a raw feed at 99,900 decides the side)
and `test_maker_still_falls_back_to_the_feeds_without_a_passed_price`.

## 5. The advertised `fv ≥ 0.62` is not the operative threshold

After the direction gate, a certainty floor runs:

```python
cert = min(0.95, max(target_fv, 0.50 + chosen_edge * 3.5))
if cert < 0.65: reject
```

so an entry needs **fv ≥ 0.65 or an edge ≥ 4.29c**. Verified against the real
strategy:

* fv 0.64 vs mid 0.60 (4c edge) → `certainty_below_floor`, cert 0.640;
* fv 0.64 vs mid 0.55 (9c edge) → signal, cert 0.815;
* fv 0.62 never signals at any mid — the row of `·` in the table above.

This is the same defect class as SNIPE's inert `SNIPE_MIN_RAW_MODEL_EDGE`: the
number in the comment is not the number that binds. The reject detail now says
what the gate actually requires, and the constants are named
(`CERT_FLOOR`, `CERT_EDGE_WEIGHT`, `MIN_EDGE_FOR_CERT_FLOOR`). No value changed.

The lumped `no_trend_or_edge_alignment` counter (475×) is likewise now split
into `fair_value_below_floor` / `edge_below_floor` / `momentum_not_supporting` /
`side_mismatch` / `spot_on_threshold`, so the next alert says which of the five
it was. Writing the test for that immediately showed the non-obvious one:
**a flat tape cannot quote** — `mom_5m = 0` fails BTC's `≥ +0.0002`
requirement (`test_a_flat_tape_cannot_quote_and_says_so`).

## 6. Documented, not changed

* `HIGH_VOL_THRESHOLD = 0.003` cannot fire. `_realized_vol` returns the mean
  **per-tick** absolute return over the last 120 s, not a per-minute volatility,
  so at ~1 tick/s it would need a 0.3% move *every second* (~18%/min). It does
  not appear in any production counter. Making it fire would suppress quoting —
  a risk decision, so it is annotated instead.
* `MAX_MAKER_WINDOW = 720`, `MARKET_LIFE_SEC`, `BOOK_DEPTH`, `REQUOTE_INTERVAL`
  and `MAX_REWARDED_SPREAD_CENTS` are dead (grep: no use outside their
  definitions), and `MAX_MAKER_WINDOW`'s "quote for first 12 minutes" comment
  contradicts the hardcoded 750/180 window. Marked `NOT WIRED` so they stop
  looking like tuning knobs.
* Doc/code mismatch, still open: `projected_drift_pct`'s docstring says SNIPE
  folds Kalman drift into the diffusion model; `snipe.py` calls
  `gbm_win_probability(..., hourly_drift=0.0, horizon_cap=0.0)`.

## 7. What was deliberately not changed

`MAKER_MAX_BID` stays 0.58, the 0.62/0.65/0.020 thresholds stay, the 180-750 s
window stays, the distance calibrations stay, and the drift model stays. §3
suggests the reason there are no fills may be that there is no edge at 0.58 —
and manufacturing activity by raising the ceiling or cutting the drift cap
would turn an honest "no edge" into a loss. The next alert will at least name
the gate that binds.

## 8. Verification

`pytest -q` → **236 passed** (222 before this change). The 14 new tests drive
the real `MakerStrategy.evaluate` and the real `_maker_quote_against_book`.
With `strategies/maker.py` reverted to its previous content, **10 of the 14
fail**, so they pin the fixes rather than restating them.

**Unverified:** every claim about the production run. No live order book, no
recorded evaluations, and no fills exist in this environment; §1 reads the
counts from the alert text and §2-§3 are offline runs of the code.
