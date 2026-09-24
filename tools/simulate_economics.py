#!/usr/bin/env python3
"""Monte Carlo economics simulator for audited Bayse strategy aggregates.

This is a risk illustration, not a backtest of the newly tightened signal
policy. It uses only non-sensitive aggregate counts/PnL from the production
audit and a Jeffreys beta posterior for uncertain win probability.
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scenario:
    name: str
    wins: int
    trades: int
    roi: float
    trades_per_day: float
    note: str

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades

    @property
    def implied_effective_price(self) -> float:
        # E[ROI] = q/p - 1 for a common-price binary approximation.
        return self.win_rate / (1.0 + self.roi)


def _poisson(rng: random.Random, mean: float) -> int:
    threshold = math.exp(-mean)
    product = 1.0
    count = 0
    while product > threshold:
        count += 1
        product *= rng.random()
    return count - 1


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * probability)))
    return ordered[index]


def simulate(
    scenario: Scenario, *, days: int = 30, paths: int = 20_000,
    stake: float = 100.0, seed: int = 20260910,
) -> dict:
    rng = random.Random(seed)
    p = scenario.implied_effective_price
    alpha = scenario.wins + 0.5
    beta = scenario.trades - scenario.wins + 0.5
    totals = []
    losing_day_rates = []
    all_trading_days_positive = 0
    all_calendar_days_positive = 0

    for _ in range(paths):
        # One latent forward win rate per path reflects parameter uncertainty;
        # outcomes then add ordinary trade-to-trade variance.
        q = rng.betavariate(alpha, beta)
        total = 0.0
        losing_days = 0
        trading_days = 0
        every_trading_day_positive = True
        every_calendar_day_positive = True
        for _day in range(days):
            count = _poisson(rng, scenario.trades_per_day)
            daily = 0.0
            for _trade in range(count):
                daily += stake * (1.0 / p - 1.0) if rng.random() < q else -stake
            total += daily
            if count:
                trading_days += 1
                if daily <= 0:
                    every_trading_day_positive = False
                if daily < 0:
                    losing_days += 1
            if daily <= 0:
                every_calendar_day_positive = False
        totals.append(total)
        losing_day_rates.append(losing_days / max(trading_days, 1))
        all_trading_days_positive += int(every_trading_day_positive and trading_days > 0)
        all_calendar_days_positive += int(every_calendar_day_positive)

    return {
        "price": p,
        "month_positive": sum(value > 0 for value in totals) / paths,
        "month_loss": sum(value < 0 for value in totals) / paths,
        "median": _quantile(totals, 0.50),
        "p05": _quantile(totals, 0.05),
        "p95": _quantile(totals, 0.95),
        "mean_losing_trading_day_rate": sum(losing_day_rates) / paths,
        "all_trading_days_positive": all_trading_days_positive / paths,
        "all_calendar_days_positive": all_calendar_days_positive / paths,
    }


def render(paths: int, days: int, stake: float) -> str:
    scenarios = [
        Scenario(
            "MAKER BTC+SOL (audited aggregate)", 114, 189,
            2759.13 / 19120.0, 189 / 46.0,
            "Conservative because legacy stored MAKER PnL understated fee-free wins.",
        ),
        Scenario(
            "SNIPE all assets (legacy policy)", 129, 225,
            -2956.55 / 22690.98, 225 / 69.0,
            "Describes the losing historical policy, not the tightened code.",
        ),
        Scenario(
            "SNIPE SOL only (legacy policy)", 52, 78,
            28.01 / 8017.40, 78 / 69.0,
            "Approximately break-even historical evidence.",
        ),
    ]

    lines = [
        "# Monte Carlo risk simulation",
        "",
        f"Generated with `{paths:,}` paths, `{days}` days, and ₦{stake:,.0f} per simulated fill.",
        "",
        "This is not a claim that the new SNIPE policy has been backtested. The repository does not contain raw tick/quote history, so the simulation uses audited aggregate economics and explicitly includes uncertainty in the true win rate.",
        "",
        "| Scenario | Implied effective price | P(month profit) | P(month loss) | Median 30d PnL | 5%–95% range | Losing trading days | P(no losing trading day) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    results = []
    for index, scenario in enumerate(scenarios):
        result = simulate(
            scenario, days=days, paths=paths, stake=stake,
            seed=20260910 + index,
        )
        results.append((scenario, result))
        lines.append(
            f"| {scenario.name} | {result['price']:.3f} | "
            f"{result['month_positive']:.1%} | {result['month_loss']:.1%} | "
            f"₦{result['median']:+,.0f} | "
            f"₦{result['p05']:+,.0f} to ₦{result['p95']:+,.0f} | "
            f"{result['mean_losing_trading_day_rate']:.1%} | "
            f"{result['all_trading_days_positive']:.3%} |"
        )

    lines += [
        "",
        "## Interpretation",
        "",
        "- Positive expectancy does **not** mean daily profit. Binary losses are lumpy; even the profitable MAKER BTC+SOL aggregate produces losing days in the simulation.",
        "- The probability of every calendar day being profitable is effectively zero because some days have losses or no fills.",
        "- Legacy all-asset SNIPE remains negative. SOL-only SNIPE is too close to break-even to justify material risk without fresh out-of-sample evidence.",
        "- The tightened SNIPE policy should be judged by shadow/live-minimum fills recorded after deployment; aggregate historical results cannot reveal which old rows would pass every new tick-level and quote-level gate.",
        "",
        "## Model",
        "",
        "For each path, the unknown win probability is sampled from the Jeffreys posterior `Beta(wins + 0.5, losses + 0.5)`. Daily fill count is Poisson with the observed average rate. A winning ₦A trade at effective price p earns `A(1/p - 1)` and a loss loses `A`.",
        "",
        "The simulator is deterministic at the committed seed and can be rerun with:",
        "",
        "```bash",
        f"python tools/simulate_economics.py --paths {paths} --days {days} --stake {stake:g} --output reports/monte_carlo_simulation.md",
        "```",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths", type=int, default=20_000)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--stake", type=float, default=100.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = render(args.paths, args.days, args.stake)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
