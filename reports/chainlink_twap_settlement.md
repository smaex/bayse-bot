# Bayse moved crypto settlement to a Chainlink 60-second TWAP

**Source:** operator email, 2026-09-26 — "From today, these markets will use the
Chainlink 60-second TWAP (time-weighted average price) instead of Binance spot
prices."

**Specification gap worth closing:** the public docs at `docs.bayse.markets`
still describe resolution only as "the outcome is determined based on the
real-world result" (`concepts/event-series`). The settlement source is not
documented anywhere in the API reference, so the email is the only
specification we have, and three details are still unconfirmed: whether the
window is exactly `[close − 60s, close]`, whether it applies to every crypto
series or only some, and which Chainlink feed (aggregation set, deviation
threshold, heartbeat) is the reference.

---

## 1. The model was pricing the wrong random variable — fixed

The whole bot prices `P(S_T ≥ K)`: terminal spot against the threshold. The
settled quantity is now `A = (1/60)∫_{T-60}^{T} S_u du`. Matching log-normal
moments for that average, with `a = secs − 60` the time until the window opens:

```
E[ln A]   = ln S_t + (μ − σ²/2)·(a + 30)      → drift acts over  secs − 30
Var[ln A] = σ²·(a + 20)                       → variance acts over secs − 40
```

So the final minute of diffusion is averaged away. `strategies.utils` now has
`twap_effective_horizons()`, `twap_win_probability()` and
`realized_twap_integral()`; `SnipeStrategy` prices the TWAP when
`SETTLEMENT_TWAP_SEC > 0` (default 60; `0` restores the close-print model
exactly, pinned by a test).

**Checked against simulation, not against the algebra's reputation.** A
two-stage Monte Carlo of the actual average (one exact jump to the window, then
a discretised path across it) agrees with the closed form to within **0.3
percentage points** across five cases, while the old terminal-spot model is
materially wrong at the deadline:

| 60 s to close, spot 0.1% above the strike, σ = 10%/h | value |
|---|---|
| simulation (truth) | 0.5556 |
| terminal spot (what we priced before) | 0.5283 — **2.3 pts low** |
| TWAP model (now) | 0.5512 |

The direction matters: with spot *above* the strike, pricing the terminal spot
**understated** our win probability, because the extra variance pulls the
estimate toward 0.5. SNIPE was systematically underconfident at exactly the
horizon where it enters latest, on top of the constant-vol problem in
`model_and_api_audit.md`.

Inside the window the model also handles the part of the average that is
already banked: with 30 s elapsed at an average of 1.0167 against a 1.0 strike
it returns 0.9993 — a probability, not a verdict, because the remaining 30 s
still has to average above 0.9833.

## 2. The oracle is no longer the settlement source

`ASSET_ORACLE` maps crypto to `BINANCE` and `REQUIRE_DIRECT_ORACLE` fails
closed without it. That is still the right *independent cross-check*, but it is
no longer the series that pays. Two consequences:

* **Basis risk against our own distance gates.** `SNIPE_MIN_DISTANCE_PCT` and
  the asset calibrations (BTC 0.08%, and the 0.387% minimum entry distance
  measured in `model_and_api_audit.md`) are now measured against a proxy. A
  Chainlink aggregate differs from Binance by both aggregation basis and the
  averaging lag, so part of any "edge" is oracle basis rather than
  mispricing. Unmeasured here — no Chainlink feed in this sandbox.
* **`get_latency_bias()` is dead code.** `(oracle − bayse)/bayse` would now be
  measuring a deliberate smoothing lag rather than a stale quote. Verified
  unused: no strategy calls it, so nothing is currently trading on it. Good —
  it should stay that way.

## 3. The feed-health thresholds were tuned for a relay that tracked Binance

`feeds_direct.check_lag()` grades the feed by `|oracle − relay| / relay`
against `INFRA_DEGRADED_DIFF_PCT = 0.15%` and `INFRA_STALE_DIFF_PCT = 0.80%`.
A 60-second TWAP relay *legitimately* differs from the current Binance print by
roughly half of whatever price did in the last minute — so a 0.3%/min BTC move,
which is unremarkable, puts the divergence at the degraded line and applies the
safety spread plus the 0.0010 evaluation penalty in `bot._on_spot_price`.

**Not changed here**, because raising an infra threshold on a guess is how a
genuinely broken feed gets waved through. What to watch instead: `check_lag`
already computes `diff_pct`; log its distribution for a day and set the
thresholds from the data. The 2026-09-26 stall alert's `stale_feed` /
`degraded` counters will show it if this starts firing.

## 4. Execution and strategy windows

* `SNIPE_MIN_SECS_TO_CLOSE = 60` is exactly the averaging window, so **every
  SNIPE entry has the whole window in front of it** — the partial-window branch
  exists for correctness (and for anyone who lowers that floor), not for
  today's behaviour. MAKER's 180 s quoting floor is likewise outside it.
* The Bayse mid we compare against is now derived from a smoothed series, so it
  lags Binance by design. MAKER resting a bid priced off Binance spot is more
  exposed to being picked off when Binance moves first and the relay has not
  caught up. That is a reason to *widen* maker spreads or shorten
  `MAKER_ORDER_TIMEOUT`, not a code defect — flagged, not changed.
* The threshold semantics are unchanged: `eventThreshold` is the opening
  reference and the comparison is settlement-value-vs-threshold.
* The late-candle vol inflation in `snipe.py`
  (`rv *= 1 + 0.25·(300 − secs)/240` below 300 s) now pushes in the wrong
  direction: under a TWAP, uncertainty *falls* over the final minute because it
  is averaged. It is a deliberate safety buffer and it stays until the
  reliability curve in `analysis.reliability_table()` says otherwise — but it is
  the first thing to revisit once there are resolved trades.

## 5. How to confirm the rule from the market itself

`eventCloseValue` is documented as "Closing price at resolution. Present on
resolved crypto events." For the next several resolved markets, compare three
numbers: `eventCloseValue`, our own 60-second average of the oracle ticks
(`realized_twap_integral` already computes it), and the Binance print at close.
Whichever of the last two `eventCloseValue` matches identifies the window and
the source empirically — and if it matches neither, the email means something
we have not guessed. That is a few lines against data we already store, and it
turns an unverified notice into a measured one.

## 6. Verification

`pytest -q` → **266 passed** (252 before). 14 new tests in
`tests/test_twap_settlement.py`: the closed form against simulation (5 cases,
tolerance 1.5 pts, observed worst 0.3), the terminal-spot model shown
materially wrong at the entry deadline, `window_sec = 0` reproducing
`gbm_win_probability` to 1e-12, the effective-horizon arithmetic, both
partial-window branches, the integral helper, and SNIPE's pipeline end to end
with `SETTLEMENT_TWAP_SEC` at 60 and at 0.

**Unverified:** everything about the live Chainlink feed — its basis against
Binance, its update cadence, the exact averaging window, and which series the
change covers. No credentials, no feed and no resolved market exist in this
sandbox.
