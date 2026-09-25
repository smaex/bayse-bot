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

## 4. The probe could not tell a dead bot from the wrong URL *(added after operator follow-up)*

### What the operator supplied

* Container status: **Running**, with **2 restarts**.
* The application's public address:
  `http://lxipev6rdu9rbvr5d0uxj8cz.69.164.244.180.sslip.io`
* Earlier, the port in play: **3000**.

That address is a **Coolify-generated application domain** (sslip.io, `<uuid>.<ip>`),
not a bare host:port. It resolves to `69.164.244.180` and carries no port, so it
is served by Coolify's reverse proxy on 80, which forwards to whatever the
application's **Ports Exposes** is set to.

**Correction to what was claimed a step earlier.** This report previously said
`APP_URL` probably pointed at Coolify's dashboard on `:3000`. Given the actual
domain, that was wrong: the URL is the application's own domain. `3000` is much
more likely the application's **Ports Exposes** value — which would be the
actual fault, because the bot binds **8080**:

| Where | Value |
|---|---|
| `bot.py` | `server.start_server(port=int(os.getenv("PORT", "8080")))` |
| `Dockerfile` | `EXPOSE 8080`; healthcheck probes `${PORT:-8080}/live` |
| Coolify Ports Exposes | **3000** (per the operator) |

If those disagree, the proxy forwards to a port nothing is listening on and
answers `502 Bad Gateway` for every path — including `/live`.

### The defect in the probe

Whatever the cause, the old probe could not report it usefully:

```bash
live=$(curl -fsS --max-time 10 "$base/live" 2>/dev/null || echo "")
if [ -z "$live" ]; then … exit 1
```

`-f` makes curl fail on any 4xx/5xx and discards the body, so a `502` from the
proxy, a `404` from the wrong service, and a genuinely dead bot all produced the
same message. The probe now verifies the answer came from *this* bot (the health
server identifies itself as `{"status": "live", …}`) and names the failure mode:
nothing answered, proxy cannot reach the container, wrong service, or
alive-but-not-ready.

Two bugs in the first version of that rewrite were caught by its own tests
before it shipped, and are pinned there:

* `curl -w '%{http_code}'` already prints `000` when it cannot connect, so
  `… || echo 000` produced `000000` and the `= "000"` test never matched —
  an unreachable host was reported as "wrong server";
* `curl -o` does not truncate the output file on failure, so a failed probe
  reported the *previous* probe's response body as its own.

### What the operator has to do

Set the application's **Ports Exposes to 8080** (Coolify → the bayse-bot
application → Configuration), or set a `PORT` environment variable equal to
whatever is exposed — `bot.py` honours it. Then redeploy.

To confirm the diagnosis before changing anything, open the domain in a browser
and look at `/live`:

* `{"status": "live", …}` → routing is fine; the problem was elsewhere.
* `502`/`503`/`504` → port mismatch, as described above.
* `404` → the domain is not the bot's.

The container showing **Running with 2 restarts** is consistent with cause #1 —
the crash-on-unreachable-database loop — but it is not proof: the deploy that
rolled back left the *previous* image running, so the running container may
predate both fixes.

## Not verified from here

Stated plainly, because these are the open questions:

* **The application's public URL could not be probed from here, for a reason
  that has nothing to do with the bot.** This sandbox has no plain-HTTP egress:
  `http://example.com` and `http://neverssl.com` both fail with the same
  `curl: (52) Empty reply from server` that the bot's domain returned, while
  allowlisted hosts such as `api.github.com` return 200. So the "empty reply"
  observed against
  `http://lxipev6rdu9rbvr5d0uxj8cz.69.164.244.180.sslip.io/live` is this
  sandbox's network policy and is **not** evidence about the deployment. The
  port-mismatch reading above is an inference from the domain's shape plus the
  reported `3000`, not an observation. The operator's browser settles it in one
  request.
* **The value of the `APP_URL` repository variable and the Ports Exposes
  setting.** Neither is readable with the available credentials
  (`gh api …/actions/variables` → `403 Resource not accessible by integration`).
* **Whether the running container predates these fixes.** A rolled-back deploy
  leaves the *previous* image running, so "Running" does not prove the new code
  is live. Confirm by checking the image/commit Coolify reports for the running
  container.
* **No Docker build was run.** Docker is not installed in this sandbox, so the
  image was never rebuilt or smoke-tested. The Dockerfile was not modified by
  this change.
