# Why nothing entered: SNIPE's four edge gates compose into a 7–58 point model-vs-market disagreement

Date: 2026-09-26
Scope: `strategies/snipe.py` (gate telemetry and the effective-threshold arithmetic), `strategies/maker.py` (same lumped counter), `config.py` comments.
Constraint honoured: **no threshold, weight, band or ceiling was changed.** SNIPE's 0.035/0.025/0.35/0.27 set is exactly as it was; MAKER's `fv ≥ 0.62` / 2c edge / 0.58 cap are untouched. Everything below is arithmetic made visible.

## The funnel in the paste

One process, 10,615 evaluations, 12 signals, 2 orders, 0 fills:

| gate | count | share |
|---|---|---|
| `SNIPE:no_raw_edge_or_trend_alignment` | 9998 | 94% of evaluations |
| `SNIPE:too_close_to_settle` | 923 | |
| `SNIPE:outside_entry_window` | 856 | |
| `MAKER:distance_below_calibration` | 7083 | 67% of MAKER's evaluations |
| `MAKER:late_candle_window` | 1875 | |
| `MAKER:candle_warmup_window` | 1634 | |
| `MAKER:no_trend_or_edge_alignment` | 475 | |

All 12 signals were MAKER (`📊 MAKER BTC 15min`, `📊 MAKER SOL 15min`); 2 became resting quotes at 0.574 and 0.580 and expired unfilled. The other ten never became orders — the report's tail shows two of them skipped as `market_cooldown` one second apart, and the remaining eight are not attributable from the report at all, which is the gap the new `🛠 Executor outcomes` line closes. No SNIPE gate *downstream* of the edge test appears in the top six, and SNIPE produced none of the 12 signals: it was evaluated ~10,000 times and stopped at its first substantive gate almost every time (94%).

## The part that was not visible: the gates are not independent

SNIPE applies, in order: a raw-edge floor → a shrinkage toward the market → a second edge floor measured from that same market price → a certainty floor applied to the shrunk number → a fee-adjusted EV ceiling. Solved against the real functions (`blend_with_market`, `probability_to_certainty`, `max_ev_price`, fee 2%, `FEE_FLOOR` 0.5, balanced margin 3%), the **minimum raw model probability that produces a signal** is:

| market price | raw model needed | disagreement | blended result | binding gate | `SNIPE_MIN_RAW_MODEL_EDGE` says |
|---|---|---|---|---|---|
| 0.35 | 0.929 | **58 points** | 0.6215 | `certainty_below_floor` | 0.035 |
| 0.45 | 0.857 | 41 points | 0.6215 | `certainty_below_floor` | 0.035 |
| 0.50 | 0.805 | 30 points | 0.6215 | `certainty_below_floor` | 0.035 |
| 0.55 | 0.740 | **19 points** | 0.6215 | `certainty_below_floor` | 0.035 |
| 0.58 | 0.694 | 11 points | 0.6215 | `certainty_below_floor` | 0.035 |
| 0.60 | 0.673 | 7 points | 0.626 | `price_at_or_above_ev_ceiling` | 0.035 |
| 0.62 | 0.696 | 8 points | 0.647 | `price_at_or_above_ev_ceiling` | 0.035 |
| 0.65 | — | **impossible** | — | `price_at_or_above_ev_ceiling` | 0.035 |

Three separate findings:

**1. `SNIPE_MIN_RAW_MODEL_EDGE` is not the operative gate.** The blend gives the market 65% of the log-odds, and `SNIPE_MIN_BLENDED_EDGE` then re-imposes a gap from that same market price. The raw edge actually required is 0.069–0.073 across the whole entry band — about **2× the advertised 0.035** (`snipe.effective_raw_edge_floor()`). Lowering `SNIPE_MIN_RAW_MODEL_EDGE` on its own changes nothing at all, which makes it the kind of knob that looks like a fix and is not. Pinned by `test_a_five_cent_raw_edge_clears_the_raw_gate_and_dies_in_the_blend`: a 0.60 model against a 0.55 market passes the 0.035 gate and dies at `shrunk_edge_below_requirement`, and still dies when the raw floor is dropped to 0.005.

**2. The certainty floor dominates.** `SNIPE_MIN_CERTAINTY = 0.27` — "maps to a win probability of ≥ 62%" — is applied to `w_est`, the *blended* estimate, i.e. to a number the market already owns 65% of. Requiring 62% after shrinking toward the market is close to requiring the market itself to be at 62%, at which point the edge is gone. That is why the required disagreement grows as the market price falls: 19 points at 0.55, 58 points at 0.35. Verified end to end through the real strategy: at a 0.55 market, a 0.73 model is refused with `certainty_below_floor` and a 0.74 model produces a signal.

**3. The top of the entry band is dead.** `ev_ceil = min(SNIPE_MAX_MARKET_PRICE, max_ev_price(...))` is compared with `market_price >= ev_ceil`, so a market priced exactly at 0.65 is always refused regardless of the model. The usable band is [0.35, 0.65), not [0.35, 0.65]. One line, no behaviour worth changing on its own, but it belongs in the record.

**In spot terms** (using the pipeline's own vol blend — BTC ≈ 2.3%/h, SOL ≈ 4.1%/h from the EWMA seeds): at a 0.55 market, SNIPE needs BTC **0.50–0.67% above the strike** with 6.7–11.7 min left, or SOL **0.89–1.19%**, *while Bayse still prices the side at 0.55*. A market that had repriced to 0.62 would leave no edge. That combination is what a stale or broken quote looks like — and the data-quality guard (`yes+no` outside 0.90–1.05) and the oracle-staleness fail-closed exist precisely to reject those. So on a functioning tape SNIPE's feasible set is essentially "market 0.59–0.64 *and* the model 7+ points higher", which in 10,615 evaluations never occurred. That is a design outcome, not a fault — but it was invisible, and the config reads as though 3.5 cents were enough.

## MAKER, for contrast

MAKER did signal, so its gates are satisfiable; it is thin rather than blocked. It only quotes in `180 ≤ secs ≤ 750` of a 900 s round (`candle_warmup_window` 1634 + `late_candle_window` 1875 = 33% of evaluations, close to the 37% of the round those two windows cover), then requires `|dist| ≥ 0.08%` BTC / 0.20% SOL / 0.25% ETH — which refused 67% of what was left — then `fv ≥ 0.62` with ≥ 2c of edge and supporting momentum (475). Twelve signals survived, and the two quotes that went out sat at the 0.58 ceiling and expired (see `reports/maker_zero_fill_diagnosis.md` for why a 0.58 post-only bid usually cannot fill).

## What changed

* `strategies/snipe.py`: the lumped `no_raw_edge_or_trend_alignment` is split into `model_prob_below_floor`, `raw_edge_below_floor`, `momentum_opposing`, `side_mismatch`, `spot_on_threshold`, and the detail now carries the effective raw-edge floor next to the advertised one:
  `YES p=57.0% vs mkt=0.550 edge=+0.020 (needs >=0.035 raw, >=0.070 after 0.35-weight shrinkage) dist=+0.200% mom_5m=+0.0000`.
  Same treatment for MAKER's `no_trend_or_edge_alignment` → `fair_value_below_floor`, `edge_below_floor`, `momentum_not_supporting`, `side_mismatch`, `spot_on_threshold`.
  Telemetry only: every threshold, comparison and ordering is unchanged.
* `snipe.effective_raw_edge_floor()`: solves the real blend by bisection, so the number tracks config changes instead of being a comment that rots.
* `config.py`: the `SNIPE_MIN_RAW_MODEL_EDGE` / `SNIPE_MIN_BLENDED_EDGE` comments now say which of them decides. Values unchanged.

The point of the split is that the next drought report distinguishes "the tape never moved off the strike" (`model_prob_below_floor`) from "it moved and the edge was thin" (`raw_edge_below_floor`) from "the momentum veto is mis-tuned" (`momentum_opposing`). Those have completely different remedies and one counter could not tell them apart.

## Deliberately not changed

`SNIPE_MIN_RAW_MODEL_EDGE`, `SNIPE_MIN_BLENDED_EDGE`, `SNIPE_MODEL_WEIGHT`, `SNIPE_MIN_CERTAINTY`, the entry band, `MAKER_MAX_BID`, and every window. The shrinkage and the certainty floor exist because the production sample showed raw directional confidence was not calibrated; relaxing them to manufacture activity is the one move the evidence argues against.

If the operator wants SNIPE to trade more, the honest sequence is: (1) read the split counters for a day to see which condition binds in practice; (2) check calibration of the raw model against settled outcomes (`analysis.py`, `learner.py`) — the 62%-after-shrinkage floor is only harsh if the raw model is trustworthy, and that is the open question; (3) only then move `SNIPE_MODEL_WEIGHT` (which controls how much the shrinkage costs) rather than the raw floor (which is inert). Each step needs out-of-sample fills, not this report.

## Verification

* `tests/test_snipe_entry_gates.py` — 12 tests through the real `SnipeStrategy.evaluate`: the effective floor is ~2× the advertised gate at five market prices; a 5c raw edge dies in the blend and still dies with the raw floor at 0.005; the certainty floor refuses a 0.73 model and accepts 0.74 against a 0.55 market; the band's upper edge is dead; each new counter is named and reaches `/why` as a gate, not as a structural exclusion.
* `tests/test_stall_recovery.py::test_snipe_gates_are_satisfiable_and_counted` updated for the rename — it asserted the old lumped marker `no_raw_edge` and now asserts the new names plus that the old code is gone. This is the only existing test the change touched.
* Mutation check: against the pre-split code, 8 of the 12 fail (the 5 effective-floor cases and the 3 named-counter cases). The other 4 pass both ways by design — they pin the gate *arithmetic*, which this change deliberately does not touch, so they are controls: the 5c-edge-dies-in-the-blend case, the 0.73-refused/0.74-accepted certainty boundary, the dead 0.65 band edge, and the `/why` wiring.
* Full suite: `222 passed` (210 before this change, 189 before the reporting fixes).
* Not checked: live market data. The distance/vol translation uses the EWMA seeds and config baselines, not observed BTC/SOL vol from the deployment, so treat the "0.5–0.67% above the strike" figures as order-of-magnitude.
