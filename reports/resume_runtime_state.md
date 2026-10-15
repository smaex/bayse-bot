# Resume reverts to paused: runtime-state diagnosis

## Confirmed code defect

Docker starts `python bot.py`. Python registers that module as `__main__`.
Telegram's `_apply_resume()` imports `bot` to call `reset_session_restrictions()`.
Without an alias, that import executes the file again under a different name.
The two modules have separate `_user_risks`, `_user_daily`, and active-user caches.
Telegram's injected risk dictionary belongs to the running `__main__` instance,
but the reset function looks up the empty dictionary in the imported instance.

Consequently, a successful database reset and a positive equity confirmation
could leave the actual live risk manager paused. The housekeeping loop's
`_risk_pause_active()` then persists that surviving pause again. This explains
how a genuine “Trading resumed” confirmation can be followed by paused status
without a new trade or loss. Normal import-based tests did not cover this path.

Fix: register the script instance as `bot` before importing application modules.
A subprocess test uses `runpy` with `run_name="__main__"`, intercepts the main
coroutine before network startup, invokes the actual Telegram resume handler,
and verifies the live risk, session baseline and cache invalidation.

## Additional overwrite risk

The learner previously saved a whole settings snapshot after awaiting ledger
queries. If Resume ran during that work, learning could restore the old pause.
It now atomically merges only `learned` into the current PostgreSQL settings.
Other whole-settings writers are unchanged; this is not a general versioning
solution for every possible settings race.

## Independent sizing blocker at the reported equity

At ₦741.74, a 2% ceiling permits at most ₦14.83 per order; the platform minimum
in this repository is ₦100. With the default 1% account setting the ceiling is
only ₦7.42. Resetting session stops cannot change this arithmetic. The minimum
possible equity for a ₦100 order is ₦5,000 at 2%, or ₦10,000 at 1%, before any
further Kelly, performance, cash, multi-leg or market-specific constraints.
These thresholds do not guarantee a qualifying trade.

Resume now warns about this deterministic budget blocker without increasing
risk or rounding the order up. Status also shows a surviving in-memory pause
and its reason, rather than relying only on the saved pause flag.

## Deployment / verification

No production logs, exchange account or database were accessed. The runtime
bug is reproduced locally, not confirmed against this account's live logs.
Redeploy the changed code, then use Resume and Status; `/why` reports other
entry blockers. Existing positions and risk ceilings are not relaxed by this
patch. No deployment was performed as part of this change.
