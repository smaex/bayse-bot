# SNIPE and structural-strategy hardening

Date: 2026-09-10

Scope: read-only production aggregates, official Bayse documentation, deterministic tests, and simulation. No live order was submitted.

## Executive decision

The legacy all-asset SNIPE policy should not be restored. Its supplied production aggregate was 129 wins from 225 settled trades with **-₦2,956.55** net PnL. Restricting the same historical sample to SOL produced 52 wins from 78 trades and **+₦28.01** on ₦8,017.40 deployed, which is economically indistinguishable from break-even at this sample size.

The replacement SNIPE policy is therefore conservative and testable, but not yet proven profitable. It defaults to SOL 15-minute markets, excludes the final minute, demands independent and shrunk edge, limits entries to 0.45–0.65, and uses fee-adjusted admission. New accounts still default to the historically profitable BTC/SOL 15-minute single-leg MAKER strategy and remain paused until explicitly started.

No directional bot can guarantee or lock profit every day. Positive expected value can coexist with many losing days, adverse selection, missed fills, API failures, and drawdowns.

## SNIPE probability model

Let `S` be the current independent spot price, `K` the Bayse settlement threshold, `T` time to close in hours, and `sigma` estimated hourly volatility. The independent probability uses a zero-drift geometric-Brownian approximation:

`q_model = Phi( ln(S/K) / (sigma * sqrt(T)) )`

for YES, with `1 - q_model` for NO. Drift is deliberately zero because short-window velocity extrapolation was creating confidence from noise. Realized volatility is multiplied by 1.25 and inflated again near close to reduce overconfidence.

A market price is informative consensus, not merely an obstacle to beat. The model is therefore shrunk in log-odds space:

`logit(q_blend) = 0.35 * logit(q_model) + 0.65 * logit(p_market)`

This is stricter than averaging raw percentages near zero or one. The model must first exceed market price by at least 8 percentage points, and the blended probability must still exceed the displayed entry by at least 3 points. A degraded independent oracle raises that edge requirement further.

Additional default gates are:

- SOL only;
- 15-minute markets only;
- 60–450 seconds remaining;
- spot must be at least 0.10% from the threshold;
- direction must agree with the side of the threshold;
- displayed entry must be 0.45–0.65;
- the learned certainty floor cannot be weakened by an aggressive mode;
- poor settled strategy/combo performance shrinks `q_blend` toward 50% before the final EV check, rather than changing only labels or stake size.

These gates can make SNIPE trade infrequently. That is intentional: absence of a qualifying edge is preferable to forced activity.

## Exact fee and admission economics

For a fee-bearing CLOB BUY at price `p` and fee rate `r`, Bayse reduces shares received. Define:

`f = r * max(1 - p, 0.5)`

Then the effective wallet cost per net share is:

`p_effective = p / (1 - f)`

and expected return on wallet cost is:

`EV = q / p_effective - 1`.

The old approximation multiplied price by `(1 + f)`, which is close only for tiny fees and is not the documented share-reducing mechanic. CLOB admission, the dynamic limit cap, and Kelly sizing now use the exact expression.

For AMM quote admission, effective price is calculated from the documented response fields:

`p_effective = amount / (quantity * currencyBaseMultiplier)`.

This includes wallet spend and fees without guessing. The public documentation fixture (`amount=100`, `quantity=138.21`, `currencyBaseMultiplier=1`) is covered by a deterministic contract test, as is an NGN multiplier fixture.

CLOB takers now fail closed when a book request errors, when asks are absent, when fee-adjusted EV is insufficient, or when executable depth is below the market minimum. If a book response supplies `timestamp`, `updatedAt`, or `updated_at`, timestamps older than five seconds (configurable) or malformed timestamps are rejected. The official level schema does not guarantee a timestamp, so a freshly fetched response without one cannot be age-validated; the network request timeout and exact book/depth check still apply.

## Honest take-profit behavior

The prior exit code could treat a high internal diffusion probability as if it were executable market value. That could label a sale as “take profit” even when the market price did not show a profit.

Profit peaks and take-profit triggers now use market price only. Before placing a TAKE_PROFIT SELL, the reconciled portfolio value must support at least a 5% net gain over recorded cost. A reversal “profit lock” is forbidden from realizing a loss; if the actual thesis later breaks, the ordinary stop-loss path remains available. SELL quantity and proceeds are still reconciled against the exchange portfolio and quote before submission.

This protects gains when executable liquidity exists. It does not promise that every gain can be exited: prices can gap, books can disappear, and Bayse can reject or partially fill an order.

## Additional Bayse-compatible strategy research

The strongest established prediction-market pattern supported by Bayse's documented primitives is complete-set arbitrage:

1. **BUY + BURN:** buy equal YES and NO net shares when their fee-adjusted asks cost less than one complete-set payout, then burn the equal pair.
2. **MINT + SELL:** mint equal YES and NO shares when fee-adjusted bids can sell both sides for more than the mint cost.

For ask prices `a_yes`, `a_no`:

`edge_buy_burn = 1 - [a_yes/(1-f_yes) + a_no/(1-f_no)]`.

For bid prices `b_yes`, `b_no`:

`edge_mint_sell = b_yes*(1-f_yes) + b_no*(1-f_no) - 1`.

A new monitor records these calculations from both order books and exposes `/arbshadow`. It is intentionally read-only. Bayse documents batch order processing as per-order best effort, not atomic; a failed second leg can leave an unhedged position. No placement method is reachable from this monitor. At least 2% fee-adjusted edge is required before an observation is labelled an opportunity.

Promotion to live execution would require fresh observed opportunities, depth-weighted sizing across all legs, idempotency, conversion confirmation, orphan-leg recovery, a strict loss budget, and controlled low-balance testing. Current evidence does not justify that promotion.

## Simulation evidence

`tools/simulate_economics.py` runs a deterministic Jeffreys-posterior/Poisson Monte Carlo from the supplied aggregate outcomes. At 20,000 paths over 30 days with ₦100 constant stake:

| Historical aggregate | Profitable month | Losing month | Median month | 5%–95% month | Losing trading days |
|---|---:|---:|---:|---:|---:|
| BTC+SOL MAKER | 90.9% | 9.1% | +₦1,780 | -₦417 to +₦3,967 | 42.9% |
| Legacy all-asset SNIPE | 7.8% | 92.2% | -₦1,274 | -₦2,777 to +₦202 | 54.9% |
| Legacy SOL-only SNIPE | 51.3% | 48.7% | +₦10 | -₦842 to +₦766 | 40.0% |

Probability of no losing trading day was 0.005% for BTC+SOL MAKER, 0% for all-asset SNIPE, and 0.050% for SOL-only SNIPE. Even the profitable MAKER aggregate therefore does not support a daily-profit promise.

This is a risk simulation, not a tick-level replay of the rewritten SNIPE evaluator. Aggregate rows cannot reconstruct historical oracle snapshots, order books, quote depth, rejected candidates, or the new log-odds gates. Fresh shadow observations or raw timestamped market data are required to validate the replacement policy.

## API verification status

Implementation assumptions were checked against Bayse's official documentation for quote, order, order status, fees, order book, portfolio, mint, burn, batch, lifecycle, and NGN multiplier semantics. Deterministic tests capture the documented request shapes and quote fixture without making writes.

`tools/bayse_contract_probe.py` is a credential-free, write-free probe for public BTC/SOL series, events, quote schema, and CLOB book schema. In this sandbox it failed after three read retries because TLS could not connect to `relay.bayse.markets:443`; zero writes were attempted. It should be rerun from the deployment network before unpausing funds.

## Validation and rollout standard

The current automated suite covers SNIPE scope/timing/shrinkage, fee math, API request contracts, AMM fail-closed quotes, CLOB outages/staleness/depth/fee rejection, settlement accounting, partial exits, executable profit locks, strategy activation, and complete-set read-only behavior.

Recommended rollout:

1. Keep new accounts paused and `LIVE_TRADING=false` while checking logs and `/arbshadow`.
2. Run the public contract probe from the deployment environment.
3. Reconcile wallet balance, portfolio, pending orders, and stored confirmed fills.
4. If live testing is explicitly approved, start only BTC/SOL 15-minute MAKER at the minimum permitted amount with the existing daily-loss kill switch.
5. Keep rewritten SNIPE at shadow/minimum-risk status until enough fresh policy-compliant outcomes exist to estimate calibration, realized EV, fill rate, drawdown, and quote-to-fill slippage.
6. Never enable complete-set execution from shadow price observations alone.
