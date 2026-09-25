"""CI must not cry wolf about deploys.

Two workflows sent the operator a failure notice on a schedule, for reasons
that had nothing to do with whether the bot was trading (verified 2026-09-25
against the last dozen runs of each):

``Deploy to VPS`` (deploy.yml)
    Triggered on every push to main. The host behind ``VPS_HOST`` completes an
    SSH handshake and then drops the session, so the ``Deploy & restart bot``
    step failed on every run — while ``Check deploy configuration`` stayed
    green, meaning the secrets were set and the failure was the host, not the
    config. The ``Notify Telegram on failure`` step then ran and sent
    "❌ Bayse Bot deploy FAILED" for merges Coolify had already shipped.

``Bot watchdog`` (bot-watchdog.yml)
    Triggered every 15 minutes. Its alert asserted "a restart did not recover
    it" and told the operator to run ``systemctl status bayse-bot`` on the VPS —
    but the SSH recovery step is skipped whenever ``APP_URL`` is set, because a
    Coolify container has no systemd unit. No restart was ever attempted, and
    the instructions pointed at a machine production does not run on.

A deploy alarm that is always firing is worse than no alarm: it is the reason a
real outage gets dismissed as noise. These assertions pin the difference
between "the deploy failed" and "this workflow could not have deployed
anything".

Text-parsed rather than YAML-parsed on purpose: PyYAML is not a runtime
dependency of this project and should not become one for a test.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
DEPLOY = (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")
WATCHDOG = (WORKFLOWS / "bot-watchdog.yml").read_text(encoding="utf-8")


def _on_block(text: str) -> str:
    """The `on:` section, up to the next top-level key."""
    match = re.search(r"^on:\s*\n(.*?)(?=^\w)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "workflow has no `on:` block"
    return match.group(1)


def _step(text: str, name: str) -> str:
    """The body of the step with this name, up to the next step."""
    pattern = rf"- name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)"
    match = re.search(pattern, text, flags=re.DOTALL)
    assert match, f"no step named {name!r}"
    return match.group(1)


# ── deploy.yml: the VPS is not production, so it must not announce failure ────


def test_the_dead_vps_pipeline_does_not_run_on_every_merge():
    """Every push used to produce a red run and a "deploy FAILED" alert.

    Production deploys through Coolify. A workflow that SSHes to a host that no
    longer runs the bot can only fail, and firing it on every merge guarantees
    the operator learns to ignore deploy alerts.
    """
    block = _on_block(DEPLOY)
    assert "push" not in block, (
        "deploy.yml must not trigger on push: the VPS it targets is gone, so "
        "every run fails and sends a false 'deploy FAILED' alert"
    )
    assert "workflow_dispatch" in block, (
        "keep the manual trigger so the SSH path stays usable if the VPS returns"
    )


def test_the_vps_failure_alert_only_fires_when_a_deploy_was_attempted():
    """"deploy FAILED" is only true if the SSH step ran and failed."""
    alert = _step(DEPLOY, "Notify Telegram on failure")
    assert "steps.ssh_deploy.outcome == 'failure'" in alert, (
        "the alert must be gated on the SSH step actually failing; otherwise a "
        "missing-secrets run sends a 'deploy FAILED' notice when nothing was "
        "attempted"
    )


def test_the_vps_pipeline_still_explains_its_own_history():
    """The header must keep saying why this workflow exists and fires rarely."""
    assert "THIS WORKFLOW DOES NOT DEPLOY THE PRODUCTION BOT" in DEPLOY
    assert "Coolify" in DEPLOY


# ── bot-watchdog.yml: the alert must describe what actually happened ──────────


def test_watchdog_steps_the_alert_depends_on_are_identifiable():
    """The alert branches on `steps.ssh_recover.outcome`, so the id must exist."""
    match = re.search(
        r"- name: Verify the service is running \(SSH\)\n\s*id: (\w+)", WATCHDOG
    )
    assert match, "the SSH recovery step needs an `id:` for the alert to branch on"
    assert match.group(1) == "ssh_recover"
    assert "id: http" in WATCHDOG


def test_watchdog_alert_does_not_claim_a_restart_that_never_happened():
    """For a container deployment the SSH recovery step is skipped entirely."""
    alert = _step(WATCHDOG, "Alert on failure")
    assert "steps.ssh_recover.outcome == 'skipped'" in alert, (
        "the alert must distinguish 'no restart was attempted (container)'"
        " from 'a restart was attempted and failed (systemd)'"
    )
    # The systemd instructions may still be offered — but only in the branch
    # where a systemd unit exists, never unconditionally.
    assert alert.count("systemctl") >= 1, (
        "keep the systemd guidance for a genuine VPS deployment"
    )
    assert "Coolify" in alert, (
        "the container branch must tell the operator where production actually is"
    )
