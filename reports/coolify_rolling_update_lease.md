# Coolify deploy rolled back — the health probe and the singleton lease deadlocked each other

Date: 2026-09-25
Scope: `Dockerfile`, `bot.py` (startup ordering, standby, shutdown), `server.py` (probes).
Deployment: `smaex/bayse-bot:main` @ `02e03690a321a2403c90f0d3450c38c0b8e7abed`, Coolify on `localhost`, Docker 29.5.2 with BuildKit/Buildx.
Outcome of the incident: **no outage.** Coolify kept the previous container running and rolled the new one back. No order, position, risk rail, or user setting was touched by any of this.

## What the deploy log actually shows

The build succeeded (05:26:20 → 05:27:49). Everything after that failed for two
*independent* reasons, and both had to be fixed — either one alone was enough to
roll the deploy back.

```
05:27:52  New container started.
05:27:52  Waiting for healthcheck to pass on the new container.
05:27:52  Healthcheck URL (inside the container): GET: http://localhost:8080/live
05:29:23  Attempt 1 of 5 | Healthcheck status: "starting"
05:29:23  Healthcheck logs: /bin/sh: 1: wget: not found      ← failure #1
…
05:28:02  Singleton lease is held by another live instance: ('1-78b4b400…', 1, 05:27:56)
05:28:02  Singleton lease held by another instance — waiting for expiry (attempt 1/12)
…
05:29:07  [CRITICAL] Could not acquire singleton lock after 60s. Exiting.   ← failure #2
05:29:14  (container restarts, same 12 attempts, same CRITICAL)
05:30:20  (again)
05:31:32  (again)
05:31:57  WARNING: … The healthcheck needs a curl or wget command …
05:31:57  New container is not healthy, rolling back to the old container.
```

### Failure #1 — the platform's probe could not run at all

Coolify injects its own container healthcheck for Dockerfile-based deployments
and that command uses `wget`. The runtime stage installed `libpq5 curl` on
`python:3.11-slim`, which ships neither. Every probe died with
`/bin/sh: 1: wget: not found` and return code 1 — it never made a single HTTP
request, so nothing the bot did could have made it pass.

### Failure #2 — a circular wait between the two containers

Coolify's rolling update is: **start the new container → wait until it is
healthy → stop the old one.** The bot's singleton lease was: **wait until the
old container is gone → then start the health server.**

Those two rules deadlock:

| | old container | new container |
|---|---|---|
| holds the lease | yes, renewing every 12s | no |
| will stop when | the new one is healthy | — |
| becomes healthy when | — | it owns the lease *and* answers `:8080` |

The proof is in the timestamps: the holder's `updated_at` advances
05:27:56 → 05:28:09 → 05:28:21 → 05:28:35 → … while the new container logs its
12 attempts. That is a live, healthy process renewing its lease — the old
container — not a stale row. The lease could not expire while Coolify was
waiting for a healthcheck that could not pass until the lease expired.

The 60s deadline (`for attempt in range(12)`, 12 × 5s) then turned the deadlock
into a crash-loop: `main()` returned, the process exited 0, the restart policy
started it again, and it hit the same wall four times until Coolify gave up.

A secondary defect made it worse: the HTTP server was started *after* the lease
was won, so even with `wget` installed and even after Coolify eventually stopped
the old container, the new one had nothing listening on 8080 during the entire
window the platform was probing.

## What changed

| Fix | Where | Why |
|---|---|---|
| Install `wget` alongside `curl` in the runtime stage | `Dockerfile` | Coolify's injected probe is a `wget` command; the image must be able to run it. Both clients are present so either probe style works. |
| Add a liveness `HEALTHCHECK` on `/live` (wget, curl fallback) | `Dockerfile` | Non-Coolify runtimes (plain `docker run`, compose) get the same semantics. Deliberately `/live`, never `/ready`: a standby is legitimately not ready, and a readiness-based container healthcheck would restart it in a loop. |
| Bind the health port **before** contesting the lease, and before `init_db` | `bot.py` `main()` | A rolling update probes the new container while the old one still owns the lease. *(This entry originally also claimed an unreachable Supabase "must not keep `/live` dark either". That was **wrong** — binding the port does not survive the process exiting, and `init_db` raising killed it in under a second. See `reports/database_startup_crash.md`.)* |
| Replace the 60s give-up with a **standby loop** | `bot.py` `_acquire_singleton_lease()` | "Lease held by another live instance" is the normal state of a new container during a deploy, not an error. Stand by: alive, answering `/live`, never `/ready`. |
| Move the lease call off the event loop (`asyncio.to_thread`) | `bot.py` | A blocking psycopg2 call inside the loop would freeze `/live` — the exact symptom the platform interprets as a dead container. |
| Handle `SIGTERM`/`SIGINT` and unwind in order: cancel trading loops → stop Telegram polling → **release the lease** → close clients | `bot.py` `_request_shutdown`, `_graceful_stop`, `_release_lease_if_owned` | Python's default `SIGTERM` action kills the process without running `finally`, so the lease stayed held until it expired (`LOCK_LEASE_SEC`, 45s of dead air) on every deploy and restart. Releasing *after* polling stops is what prevents the standby from eating `409 Conflict` on the shared bot token. |
| Report the instance role (`starting`/`standby`/`active`/`stopping`) on `/live` and `/ready` | `server.py` | During a deploy two containers answer. An operator reading a probe response must be able to tell which one is trading. |
| Standby cap: `LOCK_ACQUIRE_TIMEOUT_SEC` (default 900s, `0` = unlimited) + an ERROR at 5 minutes | `bot.py` | Standing by forever is correct during a deploy, but two live deployments sharing one database must surface as a visible restart and a loud log line, not as a healthy-looking container that never trades. |

Nothing about lease *safety* was loosened: the lease is still only taken when it
is free, already ours, or stale; the heartbeat still fails closed
(`os._exit(1)` on lost ownership); and a standby never polls Telegram, never
connects a user, and never evaluates a market.

## What to expect on the next deploy

**This one deploy hands over slowly, then every deploy after it is fast.**

The container running right now is the *old* code: it has no `SIGTERM` handler,
so when Coolify stops it, the lease is not released — it expires on its own
after `LOCK_LEASE_SEC` (45s). The new container stands by through that (its
limit is 900s), then takes over. From the deploy after this one, both sides run
the new code and the handover is about a second.

Sequence to expect in the Coolify log:

1. New container starts, binds 8080, logs `Singleton lease held by another instance — standing by`.
2. Probe passes on the first attempt (wget is present, `/live` answers 200 with `"role": "standby"`).
3. Coolify stops the old container → `Received SIGTERM …` → `Singleton lease released` (or, this once, lease expiry ≤45s).
4. New container logs `Singleton lease acquired after Ns in standby`, then `Bot startup complete; readiness enabled`, and `/ready` returns 200 with `"role": "active"`.

## Verification

`tests/test_rolling_update_handover.py` (11 tests) pins the properties, using a
fake clock so a 200-second standby costs no wall time:

* the runtime stage installs `wget` and `curl`; the Docker `HEALTHCHECK` probes `/live` and not `/ready`;
* `main()` binds the health port before it contests the lease and before `init_db`;
* standby survives 200 simulated seconds (the old deadline was 60) and still takes over;
* the lease call never runs on the event loop's thread;
* a standby is `/live` 200 + `/ready` 503 with `singleton_lock` named as the reason;
* a shutdown request ends standby immediately without taking the lease;
* the standby cap exits with the event unset, so the caller can exit non-zero;
* a real `SIGTERM` to a real process sets the shutdown event instead of killing it;
* `_graceful_stop` releases the lease only after polling stops, cancels trading loops first, and is idempotent.

The full suite was also run against a two-process simulation of the incident
(real `server.start_server`, real `_acquire_singleton_lease`, real `SIGTERM`,
file-backed stand-in for the `bot_lock` row, Coolify's probe command verbatim):

```
PASS — Coolify probe (wget --spider /live) passes on the STANDBY — 200 OK
PASS — standby /live is 200 with role=standby
PASS — standby /ready is 503 (never looks ready to trade)
PASS — still alive and answering after 70s of standby
PASS — old instance released the lease on SIGTERM — 0.72s
PASS — new instance took over the lease
PASS — handover finished in seconds, not lease-periods — 1.11s
PASS — after handover /live reports role=active
PASS — lease row is free at the end (no lock left behind)
```

## Operator checklist

* **Healthcheck path stays `/live`.** Pointing Coolify's probe at `/ready`
  reintroduces the crash-loop: a standby is correctly not ready, and Coolify
  would roll back a deploy that was working as designed.
* **Exactly one deployment of this bot may exist.** The lease is the only thing
  preventing double trading. A second live deployment (an old VPS systemd unit,
  a Render service, a container started by hand) will sit in standby forever,
  log `Still in standby after 300s …` and never trade. Stop it.
* **Do not shorten the stop grace period below ~10s.** The release is the last
  thing the next deployment waits on, and the unwind is bounded to fit inside
  Docker's default.
* If the base image or the package list ever changes, re-check that a probe
  client is still installed — that single missing package is what made five
  health probes fail without ever sending a request.

## Knobs

| Variable | Default | Meaning |
|---|---|---|
| `LOCK_LEASE_SEC` | `45` | How long a lease stays valid without a heartbeat. Also the worst-case handover time when an instance is SIGKILLed. |
| `LOCK_ACQUIRE_RETRY_SEC` | `5` | Standby retry interval. |
| `LOCK_ACQUIRE_TIMEOUT_SEC` | `900` | Standby cap; `0` waits forever. Exceeding it exits non-zero so the platform restarts and retries. |
| `BOT_INSTANCE_ID` | `pid-uuid` | Lease owner token. Leave unset: a PID is not unique across containers. |
