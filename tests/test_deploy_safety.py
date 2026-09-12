"""Tests for the deploy script's one invariant: never leave the bot stopped.

These run `scripts/zero_downtime_deploy.sh` against fake `systemctl`, `curl`,
and `git` binaries on PATH, so the failure that took production down — an
update step that aborts after the service was stopped — is reproduced exactly,
and the asserted property is the one that matters: capital monitoring is back
up whether or not the deploy itself succeeded.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "zero_downtime_deploy.sh"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
STATE="$STATE_DIR/service_state"
case "$1" in
  start)
      if [ -f "$STATE_DIR/deny_start" ]; then exit 1; fi
      echo active > "$STATE"; echo "started" ;;
  restart)
      if [ -f "$STATE_DIR/deny_start" ] && [ ! -f "$STATE_DIR/restart_once" ]; then
        touch "$STATE_DIR/restart_once"
      fi
      echo active > "$STATE"; echo "restarted" ;;
  stop)          echo inactive > "$STATE"; echo "stopped" ;;
  is-active)     cat "$STATE" 2>/dev/null || echo inactive ;;
  cat)           exit 0 ;;
  reset-failed)  exit 0 ;;
  *)             exit 0 ;;
esac
"""

FAKE_CURL = """#!/usr/bin/env bash
# -fsS with a non-2xx must fail; the fake decides purely from a mode file.
mode="$(cat "$STATE_DIR/curl_mode" 2>/dev/null || echo all_ok)"
case "$mode" in
  all_ok) echo '{"status":"ready"}'; exit 0 ;;
  dead)   exit 22 ;;
esac
exit 0
"""

FAKE_GIT = """#!/usr/bin/env bash
echo "git $*" >> "$STATE_DIR/git_calls"
# The deploy script always calls `git -C <dir> …`; normalise so the subcommand
# is $1 for the assertions below.
if [ "$1" = "-C" ]; then shift 2; fi
case "$1" in
  rev-parse) echo "abcdef1234567890" ;;
  fetch)
      exit "$(cat "$STATE_DIR/git_fetch_rc" 2>/dev/null || echo 0)" ;;
  checkout)
      exit "$(cat "$STATE_DIR/git_checkout_rc" 2>/dev/null || echo 0)" ;;
  reset)
      # An origin/<branch> reset is the update; any other target is a rollback.
      case "$3" in
        origin/*|main|master) exit 0 ;;
        *) echo "$3" > "$STATE_DIR/rolled_back_to"; exit 0 ;;
      esac ;;
  log) echo "fake commit subject" ;;
  *) exit 0 ;;
esac
"""

FAKE_JOURNALCTL = """#!/usr/bin/env bash
echo "fake journal line"
"""

FAKE_PYTHON = """#!/usr/bin/env bash
# Used both for the venv python and for pip: succeed unless told otherwise.
if [ "$1" = "-r" ] || [ "$1" = "install" ]; then exit 0; fi
rc="$(cat "$STATE_DIR/sanity_rc" 2>/dev/null || echo 0)"
exit "$rc"
"""

FAKE_SUDO = """#!/usr/bin/env bash
# Enough sudo to exercise the "not root" branch: drop the flags, run the command.
args=()
for a in "$@"; do
  case "$a" in -n|-E|-m) ;; *) args+=("$a") ;; esac
done
exec "${args[@]}"
"""


def _write_exec(dir_path: Path, name: str, body: str) -> None:
    target = dir_path / name
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def env(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("systemctl", FAKE_SYSTEMCTL),
        ("curl", FAKE_CURL),
        ("git", FAKE_GIT),
        ("journalctl", FAKE_JOURNALCTL),
        ("sudo", FAKE_SUDO),
        ("python", FAKE_PYTHON),
    ):
        _write_exec(bin_dir, name, body)
    os.symlink(str(bin_dir / "python"), str(bin_dir / "python3"))

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "service_state").write_text("active")
    (state_dir / "curl_mode").write_text("all_ok")
    (state_dir / "git_fetch_rc").write_text("0")
    (state_dir / "sanity_rc").write_text("0")

    bot_dir = tmp_path / "bayse-bot"
    (bot_dir / ".git").mkdir(parents=True)
    (bot_dir / ".venv" / "bin").mkdir(parents=True)
    os.symlink(str(bin_dir / "python"), str(bot_dir / ".venv" / "bin" / "python"))
    os.symlink(str(bin_dir / "python"), str(bot_dir / ".venv" / "bin" / "pip"))
    (bot_dir / "requirements.txt").write_text("")

    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    return {
        "env": {**os.environ, "PATH": path, "STATE_DIR": str(state_dir),
                "BOT_DIR": str(bot_dir), "SERVICE": "bayse-bot", "READY_TRIES": "2",
                "PROBE_SLEEP": "0"},
        "state": state_dir,
        "bot_dir": bot_dir,
    }


def _run(env, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=120, env=env["env"],
    )


def _service_state(env) -> str:
    return (env["state"] / "service_state").read_text().strip()


def test_happy_path_leaves_service_running(env):
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _service_state(env) == "active"
    assert "Deploy complete" in result.stdout


def test_failed_git_fetch_never_leaves_the_bot_down(env):
    """The exact production failure: fetch aborts mid-deploy, after the stop."""
    (env["state"] / "git_fetch_rc").write_text("1")
    result = _run(env)
    # The deploy must report failure — silence is not acceptable either way.
    assert result.returncode != 0
    # But it must never be reported by leaving capital monitoring stopped.
    assert _service_state(env) == "active", (
        "deploy failed while the service was stopped; the recovery trap did not run"
    )
    assert "Recovery" in result.stdout or "live" in result.stdout.lower()


def test_unhealthy_new_code_rolls_back_and_restarts(env):
    """New code that cannot answer /live: roll back, start again, still fail loudly."""
    (env["state"] / "curl_mode").write_text("dead")
    result = _run(env)
    assert result.returncode != 0
    assert _service_state(env) == "active", "no restart was attempted after a bad deploy"
    rolled = env["state"] / "rolled_back_to"
    assert rolled.exists(), "rollback to the previous commit was skipped"
    assert rolled.read_text().strip() == "abcdef1234567890"
    calls = (env["state"] / "git_calls").read_text()
    assert "reset --hard" in calls
    assert "Recovery" in result.stdout


def test_broken_config_is_caught_before_orders_could_be_sent(env):
    """Code that cannot even import must fail the deploy instead of trading badly."""
    (env["state"] / "sanity_rc").write_text("1")
    result = _run(env)
    assert result.returncode != 0
    # The bot is still restored, and nothing was left half-deployed and silent.
    assert _service_state(env) == "active"


def test_unprivileged_deploy_still_leaves_bot_running(env):
    """A deploy user whose sudoers only allows `restart` must still work."""
    (env["state"] / "deny_start").write_text("1")
    result = _run(env)
    assert _service_state(env) == "active", (
        "the script gave up when `systemctl start` was denied instead of restarting"
    )
    calls = (env["state"] / "git_calls").read_text() if (env["state"] / "git_calls").exists() else ""
    assert "rev-parse" in calls  # reached the end of the flow rather than aborting
    assert result.returncode == 0, result.stdout + result.stderr


def test_skip_update_only_restarts_current_code(env):
    result = _run(env, "--skip-update")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (env["state"] / "git_calls").read_text() if (env["state"] / "git_calls").exists() else ""
    assert "fetch --all" not in calls
    assert _service_state(env) == "active"
