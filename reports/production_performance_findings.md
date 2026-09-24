# Production performance findings

> Follow-up: the 2026-09-10 SNIPE rewrite, fee/execution hardening,
> complete-set shadow research, and Monte Carlo results are documented in
> `reports/snipe_hardening_findings.md`.

Audit date: 2026-09-10  
Scope: 515 resolved production records supplied through the read-only aggregate reports. All observed records were BTC, ETH, or SOL on the 15-minute timeframe. No 5-minute, EURUSD, GBPUSD, or XAUUSD record appeared.

This is an accounting and decision report, not a promise of future profit. Stored historical PnL contains legacy accounting effects described below.

## Executive decision

- Keep regular, single-leg **MAKER** permitted. It is the only profitable strategy in the stored all-time result.
- Use **MAKER on BTC and SOL, 15-minute** as the paused new-account default. This does not overwrite existing users' saved settings.
- Do not treat regular MAKER as a locked-in/multi-leg strategy. It is a directional binary position placed with a passive post-only limit order, so it must use per-strategy and per-asset performance controls.
- Strongly throttle **MAKER/ETH/15min** until genuinely new fills establish positive net economics. ETH MAKER lost 23.91% of deployed capital in the historical sample.
- Do not use SNIPE as the default. Keep its tighter price/certainty safeguards for deliberate testing, because historical SNIPE was negative overall.
- Keep ARB, ORACLE_ARB, PAIRED_SNIPER, and MIDMARKET_MAKER quarantined. Their evidence does not justify risking production money.

## Portfolio totals

| Asset | Trades | Wins | Win rate | Deployed | Stored PnL | Stored ROI |
|---|---:|---:|---:|---:|---:|---:|
| SOL | 203 | 127 | 62.56% | ₦20,917.60 | +₦1,188.52 | +5.68% |
| BTC | 146 | 82 | 56.16% | ₦16,351.20 | -₦1,081.89 | -6.62% |
| ETH | 166 | 78 | 46.99% | ₦16,891.60 | -₦3,889.18 | -23.02% |
| **Total** | **515** | **287** | **55.73%** | **₦54,160.40** | **about -₦3,782.55** | **-6.98%** |

The asset totals alone are not enough to choose assets: BTC lost overall but BTC MAKER was profitable. Decisions must use the strategy × asset interaction.

## Strategy totals

| Strategy | Trades | Wins | Win rate | Deployed | Stored PnL | Stored ROI |
|---|---:|---:|---:|---:|---:|---:|
| MAKER | 273 | 148 | 54.21% | ₦27,640.00 | +₦722.38 | +2.61% |
| SNIPE | 225 | 129 | 57.33% | ₦22,690.98 | -₦2,956.55 | -13.03% |
| ARB | 13 | 7 | 53.85% | ₦3,429.63 | -₦1,438.56 | -41.95% |
| ORACLE_ARB | 3 | 3 | 100.00% | ₦299.89 | -₦9.89 | -3.30% |
| FRONTRUN | 1 | 0 | 0.00% | ₦99.90 | -₦99.90 | -100.00% |

A high hit rate is not necessarily profitable. ORACLE_ARB won every recorded market but still lost money because entries around 0.97–0.99 left less upside than the deductions paid.

## The combinations that explain the result

### MAKER

| Asset | Trades | Wins | Win rate | Wilson 95% interval | Deployed | Stored PnL | Stored ROI |
|---|---:|---:|---:|---:|---:|---:|---:|
| BTC | 66 | 40 | 60.61% | 48.55%–71.50% | ₦6,600 | +₦1,314.39 | +19.92% |
| SOL | 123 | 74 | 60.16% | 51.33%–68.38% | ₦12,520 | +₦1,444.74 | +11.54% |
| ETH | 84 | 34 | 40.48% | 30.62%–51.17% | ₦8,520 | -₦2,036.75 | -23.91% |

BTC and SOL MAKER generated +₦2,759.13. ETH MAKER gave back ₦2,036.75 of that gain. A strategy-wide multiplier cannot handle this correctly; the learner now also adjusts size by strategy × asset × timeframe.

There is a visible regime change around 2026-08-25, when reported average MAKER entries move into roughly the current 0.50–0.65 price range:

| Period used as proxy | Trades | Wins | Win rate | Wilson 95% interval | Deployed | Stored PnL | Stored ROI |
|---|---:|---:|---:|---:|---:|---:|---:|
| Before 2026-08-25 | 133 | 55 | 41.35% | 33.34%–49.85% | ₦13,300 | -₦474.92 | -3.57% |
| From 2026-08-25 | 140 | 93 | 66.43% | 58.26%–73.72% | ₦14,340 | +₦1,197.30 | +8.35% |
| From 2026-08-28 | 51 | 39 | 76.47% | 63.24%–86.00% | ₦5,440 | +₦1,113.95 | +20.48% |

The last line is encouraging but is a short, selected window and must not be presented as a guaranteed forward rate. Its reconstructed maker PnL is about +₦1,318.45 after removing the legacy synthetic 5% deductions, but confirmed exchange-side reconciliation remains the ground truth.

### SNIPE

| Asset | Trades | Wins | Win rate | Wilson 95% interval | Deployed | Stored PnL | Stored ROI |
|---|---:|---:|---:|---:|---:|---:|---:|
| BTC | 68 | 34 | 50.00% | 38.43%–61.57% | ₦6,751.82 | -₦1,483.97 | -21.98% |
| SOL | 78 | 52 | 66.67% | 55.64%–76.12% | ₦8,017.40 | +₦28.01 | +0.35% |
| ETH | 79 | 43 | 54.43% | 43.49%–64.96% | ₦7,921.76 | -₦1,500.59 | -18.94% |

SOL SNIPE was approximately break-even; BTC and ETH SNIPE were clearly negative in stored economics. The new entry-price and quote checks exclude several historical traps, so old aggregate performance is not an exact backtest of the new policy. It is still not evidence for making SNIPE the default before out-of-sample results arrive.

## Correct binary-market mathematics

Let:

- `A` be wallet stake in naira,
- `p` be effective price per share on a 0–1 probability scale,
- `q` be the true probability that the selected outcome wins.

Bayse's NGN multiplier means one normalized winning share pays ₦100. A fee-free purchase gets `A / (100p)` shares. Therefore:

- win profit = `A(1/p - 1)`;
- loss = `-A`;
- expected profit = `A(q/p - 1)`;
- positive expected value requires `q > p`.

For varying stakes and prices, the capital-weighted break-even hit rate is:

`q_BE = ΣA / Σ(A/p)`.

This is why a fixed claim such as “SNIPE needs 87%” or “break-even is 50% plus a fee” is mathematically wrong. The required hit rate depends on what was actually paid. The live report and learner now calculate break-even from recorded effective prices.

For a fee-free binary bet, the uncapped Kelly fraction is:

`f* = (q - p) / (1 - p)`.

Kelly becomes dangerous when `q` is overestimated, so the executor applies much smaller global risk ceilings rather than trusting full Kelly.

### MAKER

Regular MAKER estimates fair probability `q`, posts a post-only bid `p < q`, and hopes another trader fills it. Its modeled edge is `q - p`; its expected return on stake is `q/p - 1`. Passive execution avoids the documented CLOB taker fee, but it does not remove directional settlement risk. It also faces adverse selection: informed traders are most likely to fill a stale quote when the maker is wrong.

### ARB

A complete complementary pair with one YES share at `pY` and one NO share at `pN` pays exactly one unit at settlement. Its locked-in gross profit per pair would be:

`1 - pY - pN - all fees/slippage`,

but only if both legs fill in the intended quantities. Bayse batch placement is per-item best effort, not atomic. A single filled leg is an ordinary directional bet, so partial-fill/orphan risk can overwhelm the apparent spread. The production sample lost 41.95%, including full-stake losses; quarantine remains justified even though order-unit and fill handling have been repaired.

### ORACLE_ARB

ORACLE_ARB acts on the belief that an external spot/oracle move makes `q` much larger than Bayse price `p` just before close. It still needs `q` above the fee-adjusted effective entry. At an effective price of 0.99, even a 99% hit rate is only break-even before any additional friction. Three wins out of three have a very wide Wilson 95% interval of about 43.85%–100%, and those three trades lost net money. This is not validation.

### PAIRED_SNIPER

The intended mathematics resembles complementary arbitrage: both legs together must cost less than their certain combined payout after all costs. In practice, non-atomic execution can leave one leg exposed, fills can differ in quantity, and the second quote can move before placement. There are no resolved production records, so the implementation has execution theory but no production evidence.

### MIDMARKET_MAKER

This strategy posts complementary resting bids near the middle. If both fill at a combined cost below one, the pair can lock a spread. If only one fills, the position is directional and may have been selected by a better-informed counterparty. There are no resolved production records. A routing bug that could auto-add this quarantined strategy in a wide market has now been removed.

## Why the strategies remain quarantined

- **ARB:** 13 records, -41.95% stored ROI, plus documented non-atomic per-item batch behavior and orphan-leg risk.
- **ORACLE_ARB:** only three records; 3/3 wins still produced -3.30% ROI at extreme prices. The new 0.75 cap removes the observed trap, but there is no out-of-sample proof.
- **PAIRED_SNIPER:** zero resolved production records; non-atomic dual-leg execution remains unvalidated.
- **MIDMARKET_MAKER:** zero resolved production records; resting two-leg fill, cancellation, adverse-selection, and reconciliation behavior remain unvalidated.

Regular MAKER is deliberately not in this list. It has 273 records and positive stored PnL, and it is a single directional maker order rather than a claim of atomic paired profit.

## Accounting correction and official Bayse semantics

The production version used a fallback settlement formula that applied the market fee formula to winners even when the trade was a post-only CLOB maker. With a 10% configured fee rate and prices above 0.50, this created an artificial deduction equal to 5% of winning stake. The daily records expose the pattern exactly: a ₦100 maker win is understated by ₦5, and a ₦120 maker win by ₦6.

The PR settlement path no longer invents a resolution fee. It uses exchange-confirmed wallet cost and normalized quantity. It preserves a real `fee` reported by the exchange for fee-bearing fills. This follows Bayse's official documentation:

- CLOB makers are fee-free; takers pay the variance-based fee: <https://docs.bayse.markets/concepts/fees>
- `postOnly=true` rejects an order rather than letting it cross the spread: <https://docs.bayse.markets/api-reference/pm/place-order>
- CLOB statuses and partial-fill fields must be read from the order object: <https://docs.bayse.markets/api-reference/pm/get-order>
- A BUY `amount` is wallet spend and a SELL `amount` is desired wallet proceeds: <https://docs.bayse.markets/api-reference/pm/place-order>

Historical stored rows are not rewritten automatically. Early exits, requested-versus-filled amount history, and aggregate reconstruction from average prices can cause other differences, so the old ledger remains caveated.

## Statistical limitations

Wilson intervals quantify binomial sampling uncertainty better than the simple `p ± 1.96√(p(1-p)/n)` approximation, especially for small samples. They do not solve these larger issues:

1. Trades close together in time share the same market regime and are not fully independent.
2. Parameters changed over the sample, so all-time rows do not estimate one fixed strategy.
3. Looking at many strategies, assets, dates, and cutoffs creates multiple-comparison and selection bias.
4. Stored historical PnL contains legacy accounting behavior.
5. A profitable in-sample combination can decay after deployment.

Promotion should therefore require exchange-confirmed, out-of-sample PnL—not only win rate—with at least 30 fills as an initial operational checkpoint and a longer 100+ fill review before increasing risk.
