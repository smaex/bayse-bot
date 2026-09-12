# "No trades for two days" — diagnosis, fixes, and the triage runbook

Date: 2026-09-12
Scope: the trading pipeline, the daily risk-stop lifecycle, the deploy pipeline, and the observability that was missing around all three.
Constraint honoured throughout: no gate was loosened, no risk ceiling raised, and no pause was auto-cleared that the operator did not design to expire.

## What was actually found

### 1. The deploy pipeline could stop the bot and then fail (highest-confidence outage cause)

`gh run list` on `smaex/bayse-bot` shows **every** `Deploy to VPS` run failing, including
the two most recent (each ~11–19s long), and the failing step is always
`Deploy & restart bot`.

The old workflow's remote script did this, in order:

```bash
set -e
sudo systemctl stop bayse-bot
pkill -9 -f "python.*bot\.py" ; sleep 3
if pgrep -f "bot\.py" > /dev/null; then … exit 1; fi     # ← can abort here
cd /opt/bayse-bot
git fetch --all                                          # ← or here (auth/network/repo layout)
…
sudo systemctl start bayse-bot                            # ← never reached
```

`set -e` plus a stop-first sequence is the whole bug: any failure between the stop and
the start leaves the unit cleanly stopped. `Restart=always` does **not** restart a unit
that was stopped on purpose. The bot then sits dark indefinitely, with an exit code in
Actions and no trades. A deploy failure is supposed to be a non-event for capital safety.

**Fixed by** `scripts/zero_downtime_deploy.sh` + a rewritten workflow:

* no `set -e`: every step falls through to a recovery path;
* an `EXIT` trap that must leave a running bot — if the service is down or `//live`
  does not answer, it rolls the checkout back to the previously running commit, starts
  it again, and *then* reports failure;
* a pre-start `config.validate()` + import sanity check on the candidate code, so code
  that cannot start never takes the working code down;
* targeted stray-process cleanup (matches this `BOT_DIR`'s `bot.py`, never any `bot.py`),
  and no abort when nothing matches;
* post-start verification of `/live` then `/ready`, with the result printed.

The workflow now also runs `pytest -q` plus that sanity check in a `verify` job, and
`deploy` is gated on it: broken code cannot reach the server at all.

### 2. A one-day safety stop could become a permanent stop

`_user_loop` gated on `settings["paused"]` **before** the only code that clears a
day-scoped pause:

```python
if settings.get("paused"):          # ← skips everything below, forever
    continue
…
day = _daily(chat_id, equity, settings)   # ← the auto-unpause lived in here
```

So the moment the account tripped the daily loss stop (default 3%), hit its daily
target, or was drawdown-stopped (10%), the pause persisted into the next trading day and
beyond, with nothing in the log but `PAUSED — skipping evaluation`. With 15-minute
binaries, one ordinary losing afternoon was enough to go dark for days.

**Fixed by** `_advance_trading_day()`, now called every cycle **before** the paused gate
(`_roll_trading_day`). Day-scoped reasons (`daily_loss_limit`, `daily_target`,
`drawdown`) expire at the configured trading-day boundary and notify the user; a manual
pause — now explicitly tagged `paused_reason="manual"` by `/pause` — is never cleared
automatically, and a pause with an unknown reason is treated as manual, because the bot
will not guess its way into trading.

The same change removes a sibling silent blocker: `risk.paused` (set by the drawdown
check and read by `if risk.target_hit or risk.max_drawdown_hit: return`) previously only
expired inside `is_in_strict_mode()`, i.e. only once a trade was already being placed.
It now expires with the same trading-day boundary.

### 3. Silence and correctness were indistinguishable

A dead task, a stale oracle, a `LIVE_TRADING=false` dry run, a ₦-too-small account, and
a genuinely edge-free tape all produced the same observable output: a quiet log. The
dead-man switch did not help, because it fired on "no evaluation attempted", which a
legitimately paused account also looks like — so it was either silent or wallpaper.

**Fixed by** `stall.py` plus hooks in `bot.py`, `executor.py`, `strategies/*`:

* counters per rejection reason (bounded, 48 codes/user) at every gate: scope, entry
  window, threshold/spot availability, market-data sanity, raw-edge, distance
  calibration, price band, shrunk edge, certainty floor, EV ceiling, plus executor
  outcomes (dry run, quote not `completeFill`, over max liability, EV below margin,
  price over ceiling, order rejected by the exchange) and cycle-level stops (paused,
  balance fetch failure, risk gate, learner suspension, policy-blocked strategies);
* a prioritised verdict with a *specific* next action, including `NO_EDGE`, whose advice
  is literally "lowering a gate to manufacture activity is not a fix";
* a trading-day drought watchdog (`_stall_watchdog`) that alerts after
  `TRADE_STALL_ALERT_MINUTES` (default 120) with the verdict and the dominant gate
  counters, repeating at most every `TRADE_STALL_ALERT_REPEAT_MINUTES`, and re-alerting
  immediately only when the *explanation changes*;
* `/why` (alias `/whytrading`) for the live answer, a verdict line appended to `/debug`,
  and the whole report exposed in `/api/stats` under `stalls` plus `live_trading`;
* the drought clock seeded from the ledger on startup, so a restart does not reset
  "how long since the last order" to zero;
* the dead-man switch redefined honestly as "the engine is turning", so the new alert is
  the one that means "we are not trading".

### 4. Scope drift between the repo and what is deployed

`main` (what the VPS runs) already carried PR #2/#3 — broadened default scope, CLOB
engine inference for crypto series, relaxed MAKER momentum requirement, clamped maker
bids — while this session's branch was cut from an earlier snapshot. The working branch
was synced to deployed `main` first, so nothing in this fix set re-tightens or reverts
that work.

## Triage runbook (use this when the bot goes quiet)

1. **Is it alive at all?** `curl -s $APP_URL/ready`. A `503`/no answer with a running
   process means a component failed to start; the JSON lists component ages.
   `sudo systemctl status bayse-bot` and `journalctl -u bayse-bot -n 200` on the VPS.
2. **Ask the bot why.** `/why`. Expected codes: `NO_EVALUATION`, `NO_MARKETS`,
   `FEEDS_STALE`, `DRY_RUN`, `PAUSED_MANUAL`, `PAUSED_SESSION`, `LOW_BALANCE`,
   `SCOPE_EMPTY`, `NO_EDGE`, `EXECUTION_BLOCKED`, `NO_CONFIRMED_FILL`, `HEALTHY`.
3. **Check the flag, not the vibe.** `LIVE_TRADING=true` in `/opt/bayse-bot/.env`.
   `DRY_RUN` is not a bug — it is the documented safe default, and `render.yaml` ships
   with it `false`.
4. **Check the money math.** With `MAX_TRADE_RISK=0.02` and a ₦100 platform minimum, the
   account needs roughly ₦5,000 equity before any order can fit the per-trade budget;
   below that, *every* valid signal is skipped on purpose. The fix is a deliberate
   deposit or an explicitly raised ceiling — not a silent override.
5. **Check the feeds.** `/debug` per-asset ages. A stale oracle makes every
   directional candidate fail closed; that is the correct behaviour.
6. **Only then** consider gates/scope, and only with the settled out-of-sample evidence
   the README's profitability standard requires.

## Verification performed here

* `pytest -q`: **97 passed** (68 pre-existing + 29 new), including a fake-`systemctl`
  harness that reproduces the mid-deploy failure and asserts the service is left running
  and the rollback happened.
* Deterministic proof that the live gate stack is still *satisfiable*: a favourable SOL
  15-minute market produces a SNIPE signal end-to-end, and a marginal one is refused with
  the refusing gate recorded. A strategy that can never fire is a bug, not caution — but
  the answer is never to loosen the gate without evidence.
* `python -m pyflakes` clean on the changed files (remaining notices are pre-existing).

## Not verified from here

The VPS itself was not reachable from this environment: no process state, no `.env`, no
`journalctl`. Bayse's relay could not be reached either (TLS connect failures were already
documented in the previous audit), so `tools/bayse_contract_probe.py` still has to be run
from the deployment network. Deploy secrets (`VPS_HOST/USER/SSH_KEY/PORT`) are also worth a
direct check: an 11-second failure is as consistent with an SSH/secret problem as with a
mid-script abort, and the new `verify`-then-deploy split will tell those apart in the log.

## Rollout order

1. Merge, then deploy **once** by hand (`bash scripts/zero_downtime_deploy.sh`) while
   watching the output — it prints the `/ready` body and journal tail, and it will not
   leave the service down if it fails.
2. Confirm `GET /ready` is 200, `/why` returns a verdict, and `/api/stats` shows `stalls`.
3. Enable the watchdog workflow (`Actions → Bot watchdog → Run workflow`) and confirm the
   schedule is enabled; without it, a future host-level outage is again only visible as
   silence.
4. Keep `LIVE_TRADING` and every risk ceiling exactly as they are. This change set does
   not need a risk decision, and if `/why` reports `NO_EDGE`, the correct action is none.
