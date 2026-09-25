"""The watchdog probe must tell a dead bot from a wrong URL.

`curl -fsS` answers one question — "did anything respond?" — and treats a 404
from the wrong server exactly like the bot being down, because `-f` fails the
command on any 4xx and the body is thrown away either way.

This deployment has a concrete way to hit that: Coolify's own dashboard is
published on port 3000 (`setup_coolify_fixed.sh`: `-p 3000:8080`, with
`APP_URL="http://<host>:3000"` being Coolify's admin URL). Point the `APP_URL`
repository *variable* at that same address and the watchdog probes the admin UI,
receives a 404 on `/live`, and reports an outage every 15 minutes while the bot
trades normally. The operator is told the server is not functional; the server
was never asked.

So the probe now verifies the answer came from *this* bot — the health server
identifies itself as ``{"status": "live", ...}`` — and names the failure mode.

These tests execute the real `run:` script from the workflow against real
servers, not a re-implementation of it.
"""

from __future__ import annotations

import http.server
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WATCHDOG = REPO / ".github" / "workflows" / "bot-watchdog.yml"


def _probe_script() -> str:
    """Extract the HTTP probe step's shell script straight out of the workflow."""
    text = WATCHDOG.read_text(encoding="utf-8")
    match = re.search(
        r"- name: HTTP probe \(APP_URL\).*?\n(\s*)run: \|\n(.*?)(?=\n\s{0,6}- name: |\n\s{0,6}#|\Z)",
        text,
        flags=re.DOTALL,
    )
    assert match, "could not find the HTTP probe step"
    body = match.group(2).splitlines()
    indents = [len(line) - len(line.lstrip()) for line in body if line.strip()]
    common = min(indents)
    return "\n".join(line[common:] if line.strip() else "" for line in body)


def _run_probe(app_url: str) -> subprocess.CompletedProcess:
    """Execute the probe script the way Actions would."""
    script = _probe_script().replace("${{ vars.APP_URL }}", app_url)
    out_file = REPO / ".probe_github_output"
    out_file.unlink(missing_ok=True)
    try:
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "RUNNER_TEMP": str(REPO), "GITHUB_OUTPUT": str(out_file)},
        )
    finally:
        out_file.unlink(missing_ok=True)
        (REPO / ".probe_body").unlink(missing_ok=True)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _NotTheBot(http.server.BaseHTTPRequestHandler):
    """Stands in for Coolify's dashboard: answers, but 404s on /live."""

    def do_GET(self):  # noqa: N802 - stdlib naming
        body = b"<!DOCTYPE html><html><title>Coolify</title></html>"
        self.send_response(404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _HealthyBot(http.server.BaseHTTPRequestHandler):
    """Answers exactly like server.py does when startup has completed."""

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path == "/live":
            body, status = b'{"status": "live", "role": "active"}', 200
        elif self.path == "/ready":
            body, status = b'{"status": "ready", "role": "active", "issues": 0}', 200
        else:
            body, status = b"not found", 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler):
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port, server


@pytest.fixture()
def wrong_server():
    port, server = _serve(_NotTheBot)
    try:
        yield port
    finally:
        server.shutdown()


@pytest.fixture()
def healthy_bot():
    port, server = _serve(_HealthyBot)
    try:
        yield port
    finally:
        server.shutdown()


@pytest.fixture()
def real_bot():
    """The shipped entrypoint, healthy enough to answer /live."""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "bot.py"],
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "TELEGRAM_TOKEN": "123456:sanity-check-token",
            "ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            # Unreachable on purpose: the bot must still answer /live, which is
            # the property reports/database_startup_crash.md is about.
            "DATABASE_URL": "postgresql://sanity:sanity@127.0.0.1:1/sanity",
            "PORT": str(port),
            "DB_INIT_TIMEOUT_SEC": "60",
            "DB_INIT_RETRY_SEC": "2",
        },
    )
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/live", timeout=2):
                break
        except Exception:
            time.sleep(0.3)
    try:
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_probe_script_is_extracted_and_looks_like_shell():
    script = _probe_script()
    assert "curl" in script
    assert "/live" in script
    assert "is_bot" in script, "the probe must identify the bot, not just any answer"


def test_probe_passes_against_a_healthy_bot(healthy_bot):
    result = _run_probe(f"http://127.0.0.1:{healthy_bot}")
    assert result.returncode == 0, f"probe failed against a healthy bot:\n{result.stdout}"
    assert "healthy" in result.stdout


def test_probe_identifies_the_real_bot_and_reports_not_ready(real_bot):
    """The shipped entrypoint with its database down: alive, not trading.

    Reported as "alive but not ready" — a real, actionable state — and never as
    "wrong server". Conflating those two is exactly the bug being fixed.
    """
    result = _run_probe(f"http://127.0.0.1:{real_bot}")
    assert result.returncode == 1
    assert "NOT pointing at the bot" not in result.stdout, result.stdout
    assert "alive" in result.stdout and "not ready" in result.stdout, result.stdout


def test_probe_names_a_wrong_server_instead_of_crying_outage(wrong_server):
    """The exact failure this deployment is exposed to: Coolify's dashboard."""
    result = _run_probe(f"http://127.0.0.1:{wrong_server}")
    assert result.returncode == 1, "a 404 from the wrong server must not pass"
    assert "NOT pointing at the bot" in result.stdout, (
        f"probe could not tell the wrong server from an outage:\n{result.stdout}"
    )
    assert "never actually probed" in result.stdout
    assert "3000" in result.stdout, (
        "the error should name the port 3000 trap, since that is how this "
        "deployment gets misconfigured"
    )


def test_probe_names_an_unreachable_host():
    """Pins two real bugs: a doubled status code and a stale response body.

    `curl -w '%{http_code}'` already prints `000` when it cannot connect, so the
    original `|| echo 000` produced `000000` and the `= "000"` check missed —
    an unreachable host was reported as "wrong server". And `curl -o` does not
    truncate on failure, so the body would have been the previous probe's.
    """
    port = _free_port()  # nothing listening
    # Seed the scratch file the probe writes to. If the script does not clear it
    # before curl, a failed probe would report this text as the response body —
    # deterministically, rather than depending on which test ran first.
    (REPO / ".probe_body").write_text("STALE-BODY-FROM-A-PREVIOUS-PROBE")
    result = _run_probe(f"http://127.0.0.1:{port}")
    assert result.returncode == 1
    assert "Nothing answered" in result.stdout, result.stdout
    assert "HTTP 000 " in result.stdout, (
        f"status code was not exactly 000 (doubled?):\n{result.stdout}"
    )
    assert "body: <empty>" in result.stdout, (
        f"a failed probe must not reuse the previous body:\n{result.stdout}"
    )
    assert "STALE-BODY" not in result.stdout, (
        "the probe reported a previous response as this one's body"
    )
    assert "NOT pointing at the bot" not in result.stdout, (
        "an unreachable host is not the same diagnosis as a wrong server"
    )
