# "Server is not functional / deployment failed" — three separate causes, one recurring symptom

Date: 2026-09-25
Scope: `bot.py` (database startup), `.github/workflows/deploy.yml`, `.github/workflows/bot-watchdog.yml`
Predecessor: `reports/coolify_rolling_update_lease.md`

The operator's report was that the bot "keeps saying server is not functional and
deployment failed". That turned out to be three unrelated things producing one
indistinguishable stream of red notifications. All three are fixed here; the
first is a real bug, the other two are alarms that were never true.

## 1. An unreachable database killed the process before `/live` ever answered

**This is the real defect.** Reproduced against the shipped entrypoint:

```console
$ DATABASE_URL=postgresql://…@127.0.0.1:5432/… python bot.py
2026-09-25 06:25:46 [INFO] server: Health-check and dashboard server listening on port 8080
Traceback (most recent call last):
  File "bot.py", line 2278, in main
    await asyncio.to_thread(database.init_db)
  …
psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432 failed:
    Connection refused
```

Probing that process once per second:

```
t=1s   /live=000noconn   process=DEAD
t=2s   /live=000noconn   process=DEAD
…
t=10s  /live=000noconn   process=DEAD
```

`/live` never returned a single response. `init_db` opens the connection pool
and runs migrations; on an unreachable Postgres it raises, the exception
propagates out of `main()`, `asyncio.run` tears the loop down, and the health
server goes with it.

This falsified a claim in the previous report, which listed "bind the health
port before `init_db`" as the fix for database problems and asserted that "a
slow or unreachable Supabase must not keep `/live` dark either". Binding the
port is necessary but not sufficient: **a closed port is closed no matter when
it was opened.** From the platform's side, a container that dies in one second
is indistinguishable from a broken image — the health check fails, the deploy
rolls back, the restart policy fires, and it happens again.

Anything that briefly refuses Postgres connections would do this: a Supabase
project pausing, pool exhaustion, a DNS hiccup, a failover, a maintenance
restart.

### The fix

`_init_database_with_retry()` runs `init_db` in a worker thread and retries it
inside a bounded window instead of letting the first error escape:

* each failure is logged and recorded via `health.fail("database", …)`, so
  `/ready` returns 503 and explains itself;
* `/live` keeps answering 200 the whole time;
* a shutdown request ends the wait immediately;
* if the database never returns, the wait ends at the cap and `main()` raises
  `SystemExit(1)` — a loud, non-zero failure rather than a silent crash-loop.

The cap defaults to 120s, which covers the probe grace the Dockerfile already
advertises (`HEALTHCHECK … --start-period=90s`), so a database that is briefly
unreachable is outlasted rather than treated as fatal.

| Knob | Default | Meaning |
|---|---|---|
| `DB_INIT_RETRY_SEC` | `5` | Delay between attempts. |
| `DB_INIT_TIMEOUT_SEC` | `120` | Give-up point; `0` retries forever. |

After the fix, the same command:

```
t=2s   /live=200  process=ALIVE  body={"status": "live", "role": "starting"}
t=4s   /live=200  process=ALIVE  body={"status": "live", "role": "starting"}
…
t=28s  /live=200  process=ALIVE
=== EXIT CODE: 1 ===        (cap reached — loud, correct failure)
```

and `/ready` over the same window:

```json
{"status":"starting","role":"starting","components":{"http_server":8.74,"database":null},"issues":4}
HTTP 503
```

## 2. `Deploy to VPS` announced a failed deploy on every merge

`deploy.yml` triggered on `push` to `main`. The host behind `VPS_HOST` is
documented in that file's own header as gone, so the SSH step failed every run —
and `Notify Telegram on failure` then sent **"❌ Bayse Bot deploy FAILED"** for
merges Coolify had already shipped successfully.

From the retained run history (`gh run list`), the last 12 runs of this workflow
all failed at the same step:

```
4 Check deploy configuration -> success     ← secrets ARE set
5 Encode deploy script       -> success
6 Deploy & restart bot       -> failure     ← the host, not the config
8 Notify Telegram on failure -> success     ← the false alarm was sent
```

The configuration check being *green* matters: it rules out "missing secrets"
and confirms the failure is the unreachable host.

Fixed by removing the `push` trigger (the workflow stays available on
`workflow_dispatch` for anyone who brings the VPS back) and gating the alert on
`steps.ssh_deploy.outcome == 'failure'`, so a run that attempted nothing cannot
report a failed deploy.

## 3. The watchdog's outage alert described a machine that isn't there

`bot-watchdog.yml` runs every 15 minutes. Its `HTTP probe (APP_URL)` step fails
whenever `/live` does not answer, and `Alert on failure` then sent:

> `/ready` did not answer and a restart did not recover it.
> Run: `sudo systemctl status bayse-bot` … on the VPS.

Both halves were false for this deployment. The SSH recovery step is
`if: env.VPS_HOST != '' && vars.APP_URL == ''` — skipped whenever `APP_URL` is
set, because a Coolify container has no systemd unit. So **no restart was ever
attempted**, and the instructions pointed at a machine production does not run
on. Confirmed against the run history:

```
3 Note unconfigured deployment      -> skipped
4 HTTP probe (APP_URL)              -> failure
5 Verify the service is running     -> skipped   ← no restart happened
6 Alert on failure                  -> success   ← wrong advice sent
```

The alert now branches on `steps.ssh_recover.outcome`: the container branch says
plainly that no restart was attempted and points at Coolify's logs and
deployments, and the systemd guidance is kept only for a genuine VPS
deployment. The alert still fires — a probe that does not answer is worth
waking someone for. It just no longer sends them to the wrong host.

## Verification

```
$ python -m pytest -q
134 passed in 40.89s          (was 121 before this change; +13 new)
```

`tests/test_database_startup_resilience.py` (8 tests) pins the startup
behaviour. The important one runs the **real `bot.py` in a subprocess** against
a `DATABASE_URL` that refuses connections and probes it over a real socket —
deliberately not in-process, because the bug is that `main()` raising destroys
the loop, and an in-process harness keeps the server task alive and passes for
the wrong reason. (That false-passing harness was built and caught before the
test was written.)

All 8 were confirmed to fail against the pre-fix `bot.py`, and the subprocess
test fails for the right reason:

```
AssertionError: /live never answered while the database was down (process exit=1);
    the platform health check would fail and the deploy would roll back
assert None == 200
```

`tests/test_workflow_alerts.py` (5 tests) pins the two workflow changes; 4 of
them fail against the pre-fix workflows.

One existing assertion, `test_health_port_is_bound_before_the_lease_is_contested`,
was reading `main()` as text and matched a function name inside a *comment*.
It is now stripped of whole-line comments and asserts on call sites, so prose
cannot satisfy or defeat an ordering check.

## 4. The watchdog was pointed at the wrong address — resolved with hard evidence

Two earlier guesses in this section were wrong and are corrected here: that
`APP_URL` pointed at Coolify's dashboard on `:3000`, and then that the
application's Ports Exposes was mismatched. The operator opening
`http://lxipev6rdu9rbvr5d0uxj8cz.69.164.244.180.sslip.io/live` and getting
`status: live` disproved both — routing and the port mapping are fine.

What actually settled it was a **job annotation**, readable from
`api.github.com` even though the run logs are hosted on a domain this sandbox
cannot reach:

```
$ gh api repos/smaex/bayse-bot/check-runs/107967141285/annotations
[warning] /live did not answer on https://bayse-bot.***.sslip.io
```

Run `36102281530`, 2026-09-25T06:18:31Z. So `APP_URL` was
`https://bayse-bot.<host>.sslip.io` — **a different subdomain from the one that
actually serves the bot** (`lxipev6rdu9rbvr5d0uxj8cz.<host>.sslip.io`), and
`https` where the working address is `http`. The `***` is GitHub redaction of a
substring matching a repository secret, consistent with the host IP appearing in
one.

The failure step also changed partway through, which dates the mistake:

| Run (UTC) | Failed step | What that implies |
|---|---|---|
| 2026-09-24 20:06 | `Verify the service is running (SSH)` | `APP_URL` **empty** — that step is gated `vars.APP_URL == ''` |
| 2026-09-24 23:11 | `Verify the service is running (SSH)` | same |
| 2026-09-25 01:27 | `Verify the service is running (SSH)` | same |
| 2026-09-25 06:18 | `HTTP probe (APP_URL)` | `APP_URL` **set** — and set to the wrong domain |

So for months the watchdog SSHed to a host that no longer exists, and between
01:27 and 06:18 on 2026-09-25 `APP_URL` was set to a domain the bot does not
serve. In neither case was the bot down. **The bot was never the thing that was
broken in those alerts.**

### The fix

Set the `APP_URL` repository variable to the address that actually works:

```
http://lxipev6rdu9rbvr5d0uxj8cz.69.164.244.180.sslip.io
```

Use `http` unless a certificate is configured for that domain — `http` is what
was verified to return `status: live`.

### The defect in the probe

Independent of the wrong address, the old probe could not have explained any of
this:

```bash
live=$(curl -fsS --max-time 10 "$base/live" 2>/dev/null || echo "")
if [ -z "$live" ]; then … exit 1
```

`-f` makes curl fail on any 4xx/5xx and discards the body, so a `502` from the
proxy, a `404` from an unrouted domain, and a genuinely dead bot all produced
the same one-line message. The probe now verifies the answer came from *this*
bot (the health server identifies itself as `{"status": "live", …}`) and names
the failure mode: nothing answered, proxy cannot reach the container, wrong
service, or alive-but-not-ready. Had it been in place, the 06:18 run would have
said "404 — something serves this host but has no /live route, it is not the
bot" instead of "the bot is down".

Two bugs in the first version of that rewrite were caught by its own tests
before it shipped, and are pinned there:

* `curl -w '%{http_code}'` already prints `000` when it cannot connect, so
  `… || echo 000` produced `000000` and the `= "000"` test never matched —
  an unreachable host was reported as "wrong server";
* `curl -o` does not truncate the output file on failure, so a failed probe
  reported the *previous* probe's response body as its own.

The container showing **Running with 2 restarts** is consistent with cause #1 —
the crash-on-unreachable-database loop — but it is not proof: the deploy that
rolled back left the *previous* image running, so the running container may
predate both fixes.

## Not verified from here

* **Nothing on the deployment host was probed directly.** This sandbox has no
  plain-HTTP egress: `http://example.com` and `http://neverssl.com` both fail
  with the same `curl: (52) Empty reply from server` that the bot's domain
  returned, while allowlisted hosts such as `api.github.com` return 200. So the
  "empty reply" seen against the bot's `/live` was this sandbox's network
  policy, not evidence about the deployment. The operator's browser — which
  returned `status: live` — is the observation that matters.
* **The exact un-redacted value of `APP_URL`.** The annotation shows
  `https://bayse-bot.***.sslip.io`; the `***` is GitHub masking a substring that
  matches a repository secret, and the variable itself is not readable
  (`gh api …/actions/variables` → `403 Resource not accessible by integration`).
  The *subdomain* differing from the working one is visible and is the finding.
* **Whether the running container predates these fixes.** A rolled-back deploy
  leaves the *previous* image running, so "Running" does not prove the new code
  is live. Confirm by checking the image/commit Coolify reports for the running
  container.
* **No Docker build was run.** Docker is not installed in this sandbox, so the
  image was never rebuilt or smoke-tested. The Dockerfile was not modified by
  this change.

### Retracted along the way

Recorded so the wrong turns are not repeated:

1. "The watchdog failure is unexplained." — Resolved: wrong `APP_URL`.
2. "`APP_URL` probably points at Coolify's dashboard on `:3000`." — Wrong; the
   operator's URL is the application's own domain.
3. "Ports Exposes is probably 3000 and mismatches the bot's 8080." — Wrong;
   `/live` returning `status: live` proves the proxy reaches the container.

The lesson is that two of the three were reached by reasoning from config files
in the repo instead of reading the one piece of runtime evidence — the job
annotation — that was reachable all along.
