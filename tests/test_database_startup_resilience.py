"""A database that is briefly unreachable must not take the container down.

Reproduced against the real startup path on 2026-09-25:

    DATABASE_URL=postgresql://…@127.0.0.1:5432/… python bot.py

    server: Health-check and dashboard server listening on port 8080
    psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432
        failed: Connection refused
    (process exits)

The health port was bound first, exactly as reports/coolify_rolling_update_lease.md
says it should be — and ``/live`` still never answered a single probe, because
``init_db`` raised out of ``main()`` and the process was gone in under a second.
A closed port is a closed port no matter when it was opened. From the platform's
side that is indistinguishable from a broken image: the health check fails, the
deploy rolls back, the restart policy fires, and the same thing happens again.

Binding early is necessary but not sufficient. The properties pinned here are:

* a failing ``init_db`` is retried inside a bounded window instead of killing
  the process;
* ``/live`` answers 200 over HTTP for that whole window while ``/ready`` stays
  503 — the container is alive and correctly not ready to trade;
* the retry never runs on the event loop, so the health server keeps answering;
* a database that never comes back still fails loudly and non-zero;
* a shutdown during the wait is honoured immediately rather than after a sleep.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import bot
import health
import server

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_database_state():
    bot._shutdown_event.clear()
    health.remove("database")
    server.instance_state["role"] = "starting"
    yield
    bot._shutdown_event.clear()
    health.remove("database")
    server.instance_state["role"] = "starting"


class _FakeClock:
    """Deterministic time: a two-minute outage must not cost two minutes here."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _flaky(fail_times: int, threads: list[threading.Thread] | None = None):
    """Stand-in for `database.init_db`: raise `fail_times`, then succeed."""
    state = {"n": 0}

    def _init() -> None:
        if threads is not None:
            threads.append(threading.current_thread())
        state["n"] += 1
        if state["n"] <= fail_times:
            raise RuntimeError("connection refused")

    return _init, state


# ── The retry itself ──────────────────────────────────────────────────────────


def test_a_transient_database_outage_is_retried_not_fatal():
    """The old behaviour raised out of main() on the very first failure."""
    clock = _FakeClock()
    init, state = _flaky(fail_times=3)

    ok = asyncio.run(
        bot._init_database_with_retry(
            init, retry_sec=5.0, timeout_sec=120.0, sleep=clock.sleep, clock=clock,
        )
    )

    assert ok is True
    assert state["n"] == 4, "should have retried through the three failures"
    assert clock.sleeps == [5.0, 5.0, 5.0]
    assert health.snapshot()["components"]["database"]["age_sec"] is not None


def test_database_retry_never_blocks_the_event_loop():
    """init_db is blocking psycopg2; on the loop it would freeze /live."""
    clock = _FakeClock()
    threads: list[threading.Thread] = []
    init, _ = _flaky(fail_times=2, threads=threads)

    asyncio.run(
        bot._init_database_with_retry(
            init, retry_sec=1.0, timeout_sec=60.0, sleep=clock.sleep, clock=clock,
        )
    )

    assert threads, "init_db was never called"
    assert all(t is not threading.main_thread() for t in threads), (
        "init_db ran on the event loop's thread — the health server cannot "
        "answer while it blocks"
    )


def test_database_outage_outlasts_the_platform_probe_window():
    """Coolify allows 5 probes; a Supabase blip should be waited out, not fatal.

    12 failures × 5s is a 60-second outage. Before this fix the container was
    dead at t≈0s, so it could not have survived a single probe.
    """
    clock = _FakeClock()
    init, state = _flaky(fail_times=12)

    ok = asyncio.run(
        bot._init_database_with_retry(
            init, retry_sec=5.0, timeout_sec=120.0, sleep=clock.sleep, clock=clock,
        )
    )

    assert ok is True
    assert clock.now >= 60.0, "gave up before a routine blip had cleared"
    assert state["n"] == 13


def test_a_database_that_never_returns_still_fails_loudly_and_non_zero():
    """Bounded, not infinite: a permanent outage must surface as a failed start.

    Returning False with `_shutdown_event` unset is what lets main() raise
    SystemExit(1), so the platform reports a failure instead of a clean stop
    that quietly restarts forever.
    """
    clock = _FakeClock()

    def never() -> None:
        raise RuntimeError("connection refused")

    ok = asyncio.run(
        bot._init_database_with_retry(
            never, retry_sec=5.0, timeout_sec=30.0, sleep=clock.sleep, clock=clock,
        )
    )

    assert ok is False
    assert clock.now >= 30.0
    assert bot._shutdown_event.is_set() is False, (
        "the caller tells a timeout apart from a shutdown via this event; it "
        "must not be set on a timeout"
    )


def test_shutdown_during_the_wait_is_honoured_immediately():
    """SIGTERM while waiting for the database must not wait out the cap."""
    clock = _FakeClock()
    bot._shutdown_event.set()

    def never() -> None:
        raise RuntimeError("connection refused")

    ok = asyncio.run(
        bot._init_database_with_retry(
            never, retry_sec=5.0, timeout_sec=900.0, sleep=clock.sleep, clock=clock,
        )
    )

    assert ok is False
    assert clock.sleeps == [], "slept through a shutdown request"


# ── What the platform actually sees ───────────────────────────────────────────


def test_real_bot_process_keeps_live_up_with_an_unreachable_database():
    """End-to-end against the shipped entrypoint — the incident, reproduced.

    Run the real ``bot.py`` in a subprocess with a ``DATABASE_URL`` that refuses
    connections, then probe it over a real socket the way Coolify does. This is
    deliberately not an in-process test: the failure is that ``main()`` raising
    makes ``asyncio.run`` tear the whole loop down, so the health server must be
    exercised in a process that could genuinely die.

    Before the fix this returned no response at all — the process exited in
    under a second, so every probe failed and the deploy rolled back.
    """
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()

    env = {
        **os.environ,
        "TELEGRAM_TOKEN": "123456:sanity-check-token",
        "ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        # Port 1 is reserved and nothing listens on it: connection refused.
        "DATABASE_URL": "postgresql://sanity:sanity@127.0.0.1:1/sanity",
        "PORT": str(port),
        "DB_INIT_TIMEOUT_SEC": "12",
        "DB_INIT_RETRY_SEC": "2",
        "PYTHONUNBUFFERED": "1",
    }

    process = subprocess.Popen(
        [sys.executable, "bot.py"],
        cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        live_status, live_body = None, ""
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/live", timeout=2
                ) as response:
                    live_status = response.status
                    live_body = response.read().decode()
                if live_status == 200:
                    break
            except Exception:
                time.sleep(0.4)

        assert live_status == 200, (
            f"/live never answered while the database was down (process exit="
            f"{process.poll()}); the platform health check would fail and the "
            f"deploy would roll back"
        )
        assert '"role": "starting"' in live_body

        # It must still give up loudly rather than hang forever.
        returncode = process.wait(timeout=30)
        assert returncode != 0, (
            "a permanently unreachable database must exit non-zero so the "
            "platform reports a failed start"
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


# ── Wiring ────────────────────────────────────────────────────────────────────


def test_main_routes_database_startup_through_the_retry_helper():
    """A bare `await asyncio.to_thread(database.init_db)` is the bug itself."""
    raw = inspect.getsource(bot.main)
    source = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    assert "_init_database_with_retry(" in source, (
        "main() must start the database through the retry helper"
    )
    # The health port still has to come first, or there is nothing to probe
    # during the retry window.
    assert source.index("server.start_server(") < source.index(
        "_init_database_with_retry("
    )
    assert "await asyncio.to_thread(database.init_db)" not in source


def test_defaults_outlast_the_docker_healthcheck_start_period():
    """The cap must cover the probe grace the Dockerfile advertises.

    HEALTHCHECK has start-period=90s; a database cap shorter than that would
    let the container give up before the platform has finished probing.
    """
    assert bot.DB_INIT_TIMEOUT_SEC >= 90.0
    assert bot.DB_INIT_RETRY_SEC >= 1.0
