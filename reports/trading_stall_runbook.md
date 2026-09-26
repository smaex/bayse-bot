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
   `SCOPE_EMPTY`, `NO_EDGE`, `MAKER_QUOTE_UNCOMPETITIVE`, `EXECUTION_BLOCKED`,
   `EXPOSURE_CAPPED`, `NO_CONFIRMED_FILL`, `COOLDOWN_BLOCKED`, `HEALTHY`.
   (`MAKER_QUOTE_UNCOMPETITIVE`: MAKER's risk-capped bid is too far below the
   live book to compete for a fill — a policy boundary, see
   `reports/maker_zero_fill_diagnosis.md`. `/why` includes the signal's model FV
   and hypothetical gross ROI at the best bid; that FV is an estimate, not a
   measured win rate. `/makershadow` compares read-only cap levels against
   signal-time books; it does not establish fills or profitability. Do not raise
   `MAKER_MAX_BID` merely to chase the book.)
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

## Addendum — same day, second pass: the fix set itself never shipped

After the above was merged (PR #4), the bot was *still* not trading. Diagnosis:

* `gh run list` shows **every** `Deploy to VPS` run failing across the entire retained
  history (100+ runs back to 2026-08-05) — including PR #4's own merge run, which died
  9 s into the SSH step. **No commit ever reached the VPS through CI.** The stall fixes
  above existed only in the repository; production kept running the code that contains
  the permanent-pause bug and the stop-first deploy script.
* The PR #4 failure's own annotation named the bug: `Unexpected input(s) 'files'` —
  `appleboy/ssh-action@v1.0.3` has no `files:` input, so the deploy script was never
  copied to the host, and the fallback path (`/opt/bayse-bot/scripts/…`) does not exist
  there either (that script was added by PR #4, which never deployed).
* `port: ${{ secrets.VPS_PORT }}` with an unset secret also feeds an empty port into the
  action — another guaranteed fast failure.
* The 2026-09-10 deploy failures line up with the start of the two-day outage: the old
  stop-first script could stop the unit and fail before restarting it.

Fixed (PR #5): the deploy workflow now fails fast with an explicit message when the VPS
secrets are absent, encodes `zero_downtime_deploy.sh` as base64 and writes it over the
SSH command channel (no `files:`, no env-forwarding dependency), defaults the port to 22,
and raises `command_timeout` so a recovery path cannot be killed at 60 s. The deploy
script additionally verifies via `ActiveEnterTimestamp` that the unit actually
(re)started — a silently denied restart (e.g. sudoers written for `/bin/systemctl` on a
merged-`/usr` host) no longer masquerades as a successful deploy; it fails loudly while
leaving the bot running. `deploy_vps.sh` now grants every allowed verb under both
`/bin` and `/usr/bin`.

**If the deploy still fails after this:** the workflow now tells you which case it is —
"missing repository secrets: …" (set them under Settings → Secrets and variables →
Actions: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, optionally `VPS_PORT`) or an SSH/remote
error visible in the run log, linked from the Telegram failure notice. Until a deploy
succeeds, the single fastest recovery is on the VPS itself:
`sudo systemctl status bayse-bot` → if stopped, `sudo systemctl restart bayse-bot`,
then `/why` in Telegram.

### Third pass — the pipeline is now self-diagnosing; the remaining blocker is the host

PR #5–#8 removed every black box from the deploy path (no `files:` input, no
ssh-action wrapper: native `ssh` with staged DNS → TCP → SSH pre-flight, `ssh -v`,
unfiltered log tails as run annotations and in the Telegram failure notice). The
verdicts from the resulting runs:

* **secrets exist** — the config pre-check passes (`VPS_HOST/USER/SSH_KEY` set);
* **DNS resolves** and **TCP connects** to the host:port — a machine is there;
* that machine answers the SSH banner as `OpenSSH_8.9p1 Ubuntu-3ubuntu0.17` (Ubuntu
  22.04), completes the full key exchange, presents an ED25519 host key that is
  **unknown** to a fresh runner, and then **drops the session silently between NEWKEYS
  and authentication** — exit 255 with no `Permission denied`, no protocol error,
  at a slightly different point each attempt.

A host that completes the handshake and then kills the session before userauth is not
"wrong credentials" (that prints `Permission denied`). The consistent explanation is
that **the address behind `VPS_HOST` is no longer the bot's server**: the VPS was
terminated or reprovisioned and its old address now belongs to someone else (or a
honeypot), or a middlebox/firewall is killing encrypted sessions after handshake.
Note the deploy pipeline was *already* failing identically in early August while the
bot kept trading — deploys never mattered until the stall fixes needed them, so SSH
failure alone does not date the outage.

Operator checks, in order:

1. Open the **VPS provider's console/dashboard**: does the server still exist and is
   it running? Does its current IP/hostname match the `VPS_HOST` secret?
2. From your own machine: `ssh -p <port> <user>@<host>`. If this also dies after the
   banner, the server/address is the problem (provider ticket / reprovision via
   `deploy_vps.sh`). If it connects fine, the GitHub Actions secrets are stale —
   re-set `VPS_HOST/VPS_USER/VPS_SSH_KEY/VPS_PORT` and re-run Actions.
3. Once on the box: `sudo systemctl status bayse-bot`; if stopped,
   `sudo systemctl restart bayse-bot`; then `/why` in Telegram. Note that positions
   opened since the outage have already resolved on-exchange (15-minute binaries);
   nothing accumulates, but exit management was absent while the bot was down.

## Addendum — 2026-09-25: production runs in Coolify, and the deploy path now says so

The third pass above concluded that the host behind `VPS_HOST` is not the bot's
server. That was correct, and it had a second consequence nobody had written
down: **it was never the bot's server.** Production runs in Coolify as a Docker
container. `.github/workflows/deploy.yml` SSHes to a systemd unit at
`/opt/bayse-bot`; that is a different deployment model. The pipeline was not
"broken" — it was pointed at a machine that never hosted this bot, which is why
all ~200 retained runs failed and why the fixes in PR #4/#5 never shipped.

Current state:

* **Deploy:** Coolify's GitHub App on the application delivers the push event
  and redeploys on merge to `main`. `.github/workflows/deploy.yml` is retained
  but is explicitly marked as not the production path.
* **Manual redeploy / verification:** `.github/workflows/coolify-deploy.yml`
  (workflow_dispatch) triggers Coolify's documented deploy webhook
  (`GET /api/v1/deploy?uuid=…&force=…` with a Bearer token) and polls `/ready`
  afterwards when the `APP_URL` repository variable is set. It needs
  `COOLIFY_URL`, `COOLIFY_TOKEN` (deploy permission) and `COOLIFY_APP_UUID`.
* **Liveness:** set the `APP_URL` repository variable to the bot's public URL.
  The watchdog then probes `/live` and `/ready` over HTTP, and the SSH probe is
  skipped — a container has no systemd unit to restart, so that probe could
  only ever alert falsely (it did, every 15 minutes).
* **Health:** enable Coolify's own health check on `/ready` for the application
  (start period ~90s; startup waits on Postgres, Telegram and the price feeds).
  That, plus a container restart policy of `unless-stopped`, is the mechanism
  that brings a wedged trading engine back.

### Environment variables only exist in Coolify

`.env` is not deployed — `dockerignore` excludes it. Anything not set in the
Coolify application is missing at runtime, and the defaults are the safe ones:

* `LIVE_TRADING` — defaults to `false`. **The single most common cause of a
  silent bot.** With it unset the bot evaluates, logs signals, and sends no
  order, forever.
* `MAX_PORTFOLIO_EXPOSURE` — defaults to `0.15`. A user setting of
  `/set maxexposure 30` is silently clamped to 15%, so a ₦1,600 account can
  hold only ₦240 of filled exposure (two ₦100 orders).
* `MAKER_MAX_BID` — defaults to `0.58`, the most a MAKER quote will pay (a
  fill pays at least +72% on a win). MAKER prices against the live book and
  skips — `exec:maker_quote_behind_book` — when the book bids above this, so if
  the chosen side trades at 0.65+ MAKER will not trade. Raising it is a
  risk/reward decision; must be within `0.50`–`0.75` or startup fails.
* `MAX_TRADE_RISK` and `MAX_PORTFOLIO_EXPOSURE` are **fractions**: `0.02` and
  `0.30`. Writing `5` or `30` raises at startup inside `config.validate()`,
  which runs before the health server binds — Docker then restart-loops the
  container.

### Reading "quiet" correctly

`/why` now reports `N orders placed (P as passive quotes) | M confirmed fills |
R resting now` — `R` is read from the live risk book; the earlier "still resting
unfilled" figure was a lifetime placement count and overstated it. A MAKER quote
that rests and expires is *not* a trade: it is an order that produced no
position, capital came back, and the user is notified (`⏳` still resting, `⚪`
unfilled, `🚫` rejected, `❓` unconfirmed). When `NO_CONFIRMED_FILL` is the
verdict, the thing to inspect is the quoting price relative to the live book
and `MAKER_ORDER_TIMEOUT`, not a risk gate. Configuration exclusions
(`scope:blocked_by_policy`, `*:engine_not_clob`, `*_not_in_allowed_scope`) are
listed on their own "Excluded by configuration" line, not as gates.
