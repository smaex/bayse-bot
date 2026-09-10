# Monte Carlo risk simulation

Generated with `20,000` paths, `30` days, and ₦100 per simulated fill.

This is not a claim that the new SNIPE policy has been backtested. The repository does not contain raw tick/quote history, so the simulation uses audited aggregate economics and explicitly includes uncertainty in the true win rate.

| Scenario | Implied effective price | P(month profit) | P(month loss) | Median 30d PnL | 5%–95% range | Losing trading days | P(no losing trading day) |
|---|---:|---:|---:|---:|---:|---:|---:|
| MAKER BTC+SOL (audited aggregate) | 0.527 | 90.9% | 9.1% | ₦+1,780 | ₦-417 to ₦+3,967 | 42.9% | 0.005% |
| SNIPE all assets (legacy policy) | 0.659 | 7.8% | 92.2% | ₦-1,274 | ₦-2,777 to ₦+202 | 54.9% | 0.000% |
| SNIPE SOL only (legacy policy) | 0.664 | 51.3% | 48.7% | ₦+10 | ₦-842 to ₦+766 | 40.0% | 0.050% |

## Interpretation

- Positive expectancy does **not** mean daily profit. Binary losses are lumpy; even the profitable MAKER BTC+SOL aggregate produces losing days in the simulation.
- The probability of every calendar day being profitable is effectively zero because some days have losses or no fills.
- Legacy all-asset SNIPE remains negative. SOL-only SNIPE is too close to break-even to justify material risk without fresh out-of-sample evidence.
- The tightened SNIPE policy should be judged by shadow/live-minimum fills recorded after deployment; aggregate historical results cannot reveal which old rows would pass every new tick-level and quote-level gate.

## Model

For each path, the unknown win probability is sampled from the Jeffreys posterior `Beta(wins + 0.5, losses + 0.5)`. Daily fill count is Poisson with the observed average rate. A winning ₦A trade at effective price p earns `A(1/p - 1)` and a loss loses `A`.

The simulator is deterministic at the committed seed and can be rerun with:

```bash
python tools/simulate_economics.py --paths 20000 --days 30 --stake 100 --output reports/monte_carlo_simulation.md
```
