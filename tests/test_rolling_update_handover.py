"""A deploy must hand the bot over, not fight it for the singleton lease.

These tests reproduce the 2026-09-25 Coolify failure, where every deploy rolled
back and the new container crash-looped:

1. Coolify's rolling update starts the NEW container while the OLD one is still
   running and still renewing the lease.
2. The new container waited 12 × 5s for the lease and then exited, so it never
   bound port 8080 and never answered the platform's health probe.
3. Coolify only stops the old container after the new one is healthy — so the
   lease never freed, the new container restarted into the same wall, and after
   five failed probes the deploy was rolled back.

Separately, the probe itself could never have passed: the runtime image shipped
``curl`` but Coolify's injected healthcheck runs ``wget``
("/bin/sh: 1: wget: not found").

The asserted properties are the ones that break the deadlock:

* the image ships the clients the platform's probe can use;
* ``/live`` answers while this instance is a standby — a live standby is not a
  broken container and restarting it never wins the lease;
* standby waits through a whole rolling update instead of giving up at 60s;
* SIGTERM stops polling and releases the lease, so the handover takes seconds
  instead of the full lease period.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
import signal
import threading
from pathlib import Path

import pytest

import bot
import database
import health
import server

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")


def _runtime_stage() -> str:
    """The final stage of the Dockerfile — the one that actually ships."""
    stages = re.split(r"^FROM\s+", DOCKERFILE, flags=re.MULTILINE)
    return stages[-1]


@pytest.fixture(autouse=True)
def _clean_handover_state():
    bot._shutdown_event.clear()
    bot._owns_singleton = False
    server.instance_state["role"] = "starting"
    health.remove("singleton_lock")
    health.set_ready(False)
    yield
    bot._shutdown_event.clear()
    bot._owns_singleton = False
    bot._tg_app = None
    server.instance_state["role"] = "starting"
    health.remove("singleton_lock")
    health.set_ready(False)


class _FakeClock:
    """Deterministic time: standby loops are measured in minutes, tests are not."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _record(order: list[str], label: str):
    """Stand-in for `database.release_singleton_lock` that notes the call."""

    def _release() -> bool:
        order.append(label)
        return True

    return _release


@contextlib.contextmanager
def _fake_release(replacement):
    """Swap the real lease release for `replacement`, then put it back."""
    original = getattr(database, "release_singleton_lock", None)
    database.release_singleton_lock = replacement
    try:
        yield
    finally:
        if original is None:
            del database.release_singleton_lock
        else:
            database.release_singleton_lock = original


# ── The probe itself ──────────────────────────────────────────────────────────


def test_runtime_image_ships_the_clients_a_platform_probe_can_use():
    """Coolify's injected healthcheck runs `wget`; a slim Python image has none.

    The deploy log ended with Coolify's own hint — "the healthcheck needs a curl
    or wget command … make sure that it is available in the image" — after five
    probes died on `wget: not found`. Both clients are installed so the image
    survives whichever one the platform picks.
    """
    stage = _runtime_stage()
    installs = re.findall(r"apt-get install[^&|]+", stage, flags=re.DOTALL)
    assert installs, "runtime stage installs no system packages"
    packages = " ".join(installs)
    for client in ("wget", "curl"):
        assert re.search(rf"(^|\s){client}(\s|$)", packages), (
            f"{client} is missing from the runtime image; the platform "
            f"healthcheck cannot run"
        )


def test_docker_healthcheck_probes_liveness_not_readiness():
    """A readiness-based container healthcheck would restart a valid standby.

    During a rolling update the spare instance is alive and correct but not
    ready — it does not own the lease yet. Probing /ready would have Docker kill
    and restart it in a loop, which is the failure this file exists to prevent.
    """
    match = re.search(r"^HEALTHCHECK\s+(.*?)(?=^\w|\Z)", DOCKERFILE,
                      flags=re.MULTILINE | re.DOTALL)
    assert match, "Dockerfile has no HEALTHCHECK for non-Coolify runtimes"
    command = match.group(1)
    assert "/live" in command
    assert "/ready" not in command


def _code_only(source: str) -> str:
    """Drop whole-line comments.

    Ordering assertions read ``main()`` as text, so a prose comment that happens
    to name a function would otherwise satisfy or defeat them. Assert on the
    call sites instead.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


def test_health_port_is_bound_before_the_lease_is_contested():
    """Ordering is the whole fix: no socket, no healthcheck pass, no handover."""
    source = _code_only(inspect.getsource(bot.main))
    bind = source.index("server.start_server(")
    contest = source.index("await _acquire_singleton_lease(")
    assert bind < contest, (
        "main() must start the health server before waiting for the singleton "
        "lease, or a rolling update can never complete"
    )
    # Same argument for the database: a slow or unreachable Supabase must not
    # keep /live dark while the platform is deciding whether we are healthy.
    assert bind < source.index("_init_database_with_retry(")


# ── Standing by ───────────────────────────────────────────────────────────────


def test_standby_outlasts_a_whole_rolling_update():
    """The old deadline was 60s; a real handover can take longer than that.

    The old instance renews every 12s and is only stopped once this one is
    healthy, so the lease is legitimately held for the entire swap. Waiting
    through 200 simulated seconds and still taking over is the property that
    turns a crash-loop into a deploy.
    """
    clock = _FakeClock()
    calls = {"n": 0}
    threads = []

    def acquire() -> bool:
        # The lease call is blocking I/O; it must never run on the event loop or
        # /live stops answering while we wait for the database.
        threads.append(threading.current_thread())
        calls["n"] += 1
        return calls["n"] > 40  # held for the first 40 × 5s = 200s

    owned = asyncio.run(
        bot._acquire_singleton_lease(
            acquire,
            retry_sec=5.0,
            standby_limit_sec=900.0,
            sleep=clock.sleep,
            clock=clock,
        )
    )

    assert owned is True
    assert clock.now >= 200.0, "gave up before a rolling update could finish"
    assert clock.now > 60.0, "still bounded by the old 60s deadline"
    assert all(t is not threading.main_thread() for t in threads)
    assert server.instance_state["role"] == "active"
    assert health.snapshot()["components"]["singleton_lock"]["age_sec"] is not None


def test_standby_stays_live_and_reports_why_it_is_not_ready():
    """/live is 200 (restart helps nothing); /ready explains the wait."""
    clock = _FakeClock()
    seen: dict[str, object] = {}

    async def scenario() -> None:
        waiter = asyncio.create_task(
            bot._acquire_singleton_lease(
                lambda: clock.now > 30.0,  # old instance lets go at t=30s
                retry_sec=5.0,
                standby_limit_sec=900.0,
                sleep=clock.sleep,
                clock=clock,
            )
        )
        # Step the loop until the standby has recorded its first failed attempt.
        # The fake clock makes the whole wait instantaneous, so the observation
        # has to be taken from inside the window rather than after a real sleep.
        for _ in range(1000):
            if "singleton_lock" in health.snapshot()["components"]:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("standby never recorded its wait in the health registry")

        seen["live_role"] = server.instance_state["role"]
        seen["ready"], seen["reasons"], _ = health.readiness()
        live = await server.handle_live(None)
        seen["live_status"] = live.status
        seen["owned"] = await waiter

    asyncio.run(scenario())

    assert seen["live_status"] == 200, "a standby must still answer the probe"
    assert seen["live_role"] == "standby"
    assert seen["ready"] is False, "a standby must never look ready to trade"
    assert any("singleton_lock" in reason for reason in seen["reasons"])
    assert seen["owned"] is True


def test_shutdown_request_ends_standby_without_taking_the_lease():
    clock = _FakeClock()
    bot._shutdown_event.set()

    owned = asyncio.run(
        bot._acquire_singleton_lease(
            lambda: False,
            retry_sec=5.0,
            standby_limit_sec=900.0,
            sleep=clock.sleep,
            clock=clock,
        )
    )

    assert owned is False
    assert clock.sleeps == [], "stood by after being told to stop"
    assert server.instance_state["role"] == "standby"


def test_standby_limit_is_a_safety_valve_not_a_deadline():
    """Two live deployments sharing one database must surface, not hide.

    Standing by forever is correct during a deploy, but if a stale second
    deployment holds the lease permanently the spare would sit there looking
    healthy and never trade. The cap (default 900s, `LOCK_ACQUIRE_TIMEOUT_SEC`,
    0 = unlimited) makes that a visible restart instead of silence.
    """
    clock = _FakeClock()

    owned = asyncio.run(
        bot._acquire_singleton_lease(
            lambda: False,
            retry_sec=5.0,
            standby_limit_sec=60.0,
            sleep=clock.sleep,
            clock=clock,
        )
    )

    assert owned is False
    assert bot._shutdown_event.is_set() is False, (
        "a standby timeout is a failure, not a clean stop — the caller must be "
        "able to tell them apart and exit non-zero"
    )
    assert clock.now >= 60.0


def test_standalone_deployment_without_a_lock_is_not_left_in_standby():
    """No lease support (older database module) must still mean 'active'."""
    source = inspect.getsource(bot.main)
    assert re.search(r"else:\s*\n\s*server\.instance_state\[.role.\]\s*=\s*.active.",
                     source), "main() leaves the role at 'starting' when there is no lease"


# ── Handing the lease back ────────────────────────────────────────────────────


def test_sigterm_becomes_a_shutdown_instead_of_a_hard_kill():
    """Python's default SIGTERM action skips `finally`, so the lease stayed held.

    Docker stops a container with SIGTERM. Without a handler the process died
    immediately and the lease lingered until it expired — dead air on every
    deploy and every restart.
    """
    previous = signal.getsignal(signal.SIGTERM)
    # Fallback so a regression cannot kill the test runner itself.
    signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        async def scenario() -> bool:
            bot._install_signal_handlers(asyncio.get_running_loop())
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.05)  # let the loop deliver it
            return bot._shutdown_event.is_set()

        assert asyncio.run(scenario()) is True, "SIGTERM did not request a shutdown"
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert server.instance_state["role"] == "stopping"
    assert health.readiness()[0] is False, "still advertising readiness while stopping"


def test_graceful_stop_releases_the_lease_only_after_polling_stops():
    """Releasing first would hand the token over while we are still polling it.

    The standby then starts its own poller and Telegram answers 409 Conflict, so
    a cheap handover turns into a restart. Order matters: trading loops, then
    polling, then the lease.
    """
    order: list[str] = []

    class _FakeUpdater:
        running = True

        async def stop(self) -> None:
            order.append("polling_stopped")
            self.running = False

    class _FakeApp:
        def __init__(self) -> None:
            self.updater = _FakeUpdater()

        async def stop(self) -> None:
            order.append("app_stopped")

        async def shutdown(self) -> None:
            order.append("app_shutdown")

    async def _never_ending() -> None:
        await asyncio.sleep(3600)

    bot._tg_app = _FakeApp()
    bot._owns_singleton = True

    async def scenario() -> None:
        # A supervised trading loop: it must be cancelled before the lease is
        # handed over, or it can still place an order mid-handover.
        task = asyncio.create_task(_never_ending(), name="supervisor:scanner_loop")
        bot._background_tasks.add(task)

        with _fake_release(_record(order, "lease_released")):
            await bot._graceful_stop()

        assert task.cancelled(), "a trading loop outlived the shutdown"

    asyncio.run(scenario())

    assert "lease_released" in order, "the lease was not released during shutdown"
    assert order.index("polling_stopped") < order.index("lease_released"), (
        "lease released while Telegram polling was still running"
    )
    assert order.index("lease_released") < order.index("app_shutdown")
    assert bot._owns_singleton is False
    assert bot._background_tasks == set(), "background tasks outlived the shutdown"


def test_lease_release_is_idempotent_so_the_backstop_cannot_steal_a_new_owner():
    """`__main__` releases again after `_graceful_stop`; the second call is a no-op."""
    calls: list[str] = []

    with _fake_release(_record(calls, "release")):
        bot._owns_singleton = True
        bot._release_lease_if_owned()
        bot._release_lease_if_owned()

    assert calls == ["release"], "released a lease this process no longer owns"
