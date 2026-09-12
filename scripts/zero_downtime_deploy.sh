#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Bayse Bot — zero-downtime, self-healing deploy
#
# Why this exists: the previous deploy script ran `set -e` and stopped the
# service as its first action. Any later failure — a git fetch that needed
# credentials, a pip resolver hiccup, a pgrep that matched nothing — aborted the
# script *after* the service was stopped, and a cleanly stopped systemd unit is
# never restarted by `Restart=always`. The bot therefore sat dark for days while
# the deploy log scrolled past.
#
# This script's one invariant: **it does not exit while the service is down.**
# Update, verify, and if verification fails, roll back to the previous commit,
# start again, and only then report failure.
#
# Usage (on the VPS):
#   bash zero_downtime_deploy.sh                # pull origin/main, deploy, verify
#   bash zero_downtime_deploy.sh --skip-update  # restart current code, verify
#   BRANCH=master bash zero_downtime_deploy.sh  # deploy another branch
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail   # deliberately NOT -e: failures must reach the recovery path

BOT_DIR="${BOT_DIR:-/opt/bayse-bot}"
SERVICE="${SERVICE:-bayse-bot}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-8080}"
HEALTH_HOST="${HEALTH_HOST:-127.0.0.1}"
READY_TRIES="${READY_TRIES:-30}"          # ~90s for startup (DB + Telegram + feeds)
PROBE_SLEEP="${PROBE_SLEEP:-3}"           # seconds between health attempts
DEPLOY_USER="${DEPLOY_USER:-bayse}"

LIVE_URL="http://${HEALTH_HOST}:${PORT}/live"
READY_URL="http://${HEALTH_HOST}:${PORT}/ready"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail_hard() { log "ERROR: $*"; FAILED_REASON="$*"; exit 1; }

FAILED_REASON=""
UPDATED=0
ROLLBACK_COMMIT=""
PREV_COMMIT="$(git -C "$BOT_DIR" rev-parse HEAD 2>/dev/null || true)"
if [ -n "$PREV_COMMIT" ]; then
    log "Current commit: ${PREV_COMMIT:0:8}"
else
    log "WARN: $BOT_DIR is not a git checkout — code update will be skipped."
fi

# The deploy may run as root or as the unprivileged deploy user whose sudoers
# only allows a couple of systemctl verbs, so try direct first and fall back.
sc() {
    if [ "$(id -u)" = "0" ]; then
        systemctl "$@"
    else
        sudo -n systemctl "$@" 2>/dev/null || systemctl "$@"
    fi
}

systemd_available() {
    command -v systemctl >/dev/null 2>&1 && sc cat "$SERVICE" >/dev/null 2>&1
}

service_active() { [ "$(sc is-active "$SERVICE" 2>/dev/null || true)" = "active" ]; }

confirm_restarted() {
    # True when the unit's ActiveEnterTimestamp is at or after this deploy
    # started. A probe of /live only proves *something* answers; if the unit
    # was already active and a restart was silently not applied (e.g. a
    # sudoers rule written for /bin/systemctl on a merged-/usr host where the
    # binary resolves to /usr/bin/systemctl), the OLD code keeps answering
    # and the deploy would falsely report success. This check makes that case
    # loud instead of silent. Best-effort by design: if the timestamp cannot
    # be read or parsed, do not fail the deploy on telemetry alone.
    local since epoch
    since="$(sc show "$SERVICE" -p ActiveEnterTimestamp --value 2>/dev/null | tr -d '\r' || true)"
    [ -n "$since" ] || return 0
    epoch="$(date -d "$since" +%s 2>/dev/null || echo "")"
    [ -n "$epoch" ] || return 0
    [ "$epoch" -ge "$DEPLOY_START_EPOCH" ]
}

start_service() {
    # `restart` is the verb the shipped sudoers rule already allows, and it is
    # also the correct verb when the unit is still active (a plain start would
    # be a no-op and the new code would never load). Falling back through it
    # means a deploy from an unprivileged user still works without extra sudo
    # grants — and a denied verb is reported instead of silently ignored.
    sc reset-failed "$SERVICE" >/dev/null 2>&1 || true
    if [ "$(sc is-active "$SERVICE" 2>/dev/null || true)" = "active" ]; then
        log "Unit is active; using restart to load the new code."
        sc restart "$SERVICE" >/dev/null 2>&1 \
            && return 0
        log "WARN: 'systemctl restart $SERVICE' failed — the deploy user may lack a sudoers rule."
        return 1
    fi
    sc start "$SERVICE" >/dev/null 2>&1 && return 0
    sc restart "$SERVICE" >/dev/null 2>&1 && return 0
    log "WARN: could not start $SERVICE with available privileges (need start or restart)."
    return 1
}

probe() {  # probe URL [retries]
    local url="$1" tries="${2:-5}" i
    for ((i = 1; i <= tries; i++)); do
        if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then return 0; fi
        sleep "$PROBE_SLEEP"
    done
    return 1
}

show_journal() {
    log "── last service log lines ─────────────────────────────"
    journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null || log "(journalctl unavailable)"
    log "──────────────────────────────────────────────────"
}

# ── The safety net: whatever happens, leave a running bot behind ─────────────
ensure_running() {
    local rc=$?
    trap - EXIT
    if ! systemd_available; then
        # No systemd unit here (container/Render/Coolify deployment). Nothing to
        # self-heal; report instead of pretending.
        log "No systemd unit '$SERVICE' — skipping process recovery (container deploy?)."
        [ $rc -ne 0 ] && log "Deploy step failed: ${FAILED_REASON:-exit $rc}"
        exit $rc
    fi
    if service_active && probe "$LIVE_URL" 2; then
        log "Recovery check: service is live."
        exit $rc
    fi
    if service_active; then
        log "Service reports active but $LIVE_URL did not answer."
        if probe "$READY_URL" 3; then
            log "…but /ready answers, so the loop is still starting. Leaving it alone."
            exit $rc
        fi
    fi
    log "!! Service is not healthy — attempting recovery."
    show_journal
    if [ "$UPDATED" = "1" ] && [ -n "$PREV_COMMIT" ]; then
        log "Rolling back to ${PREV_COMMIT:0:8} so capital monitoring resumes on known-good code."
        git -C "$BOT_DIR" reset --hard "$PREV_COMMIT" >/dev/null 2>&1 \
            || log "WARN: rollback reset failed"
        ROLLBACK_COMMIT="$PREV_COMMIT"
    fi
    start_service || log "WARN: recovery start did not confirm; probing anyway."
    sleep 5
    if probe "$LIVE_URL" 10; then
        log "✅ Recovery complete — bot is live again${ROLLBACK_COMMIT:+ (running previous commit)}."
    else
        log "❌ Recovery failed: the service will not answer $LIVE_URL."
        log "   Manual action required: systemctl status $SERVICE ; journalctl -u $SERVICE -n 100"
        show_journal
    fi
    [ $rc -eq 0 ] && exit 1   # an unhealthy end state is always a failure
    exit $rc
}
trap ensure_running EXIT

update_code() {
    [ "${SKIP_UPDATE:-0}" = "1" ] && { log "Skipping code update (--skip-update)."; return 0; }
    if [ ! -d "$BOT_DIR/.git" ]; then
        log "Skipping git update (no .git in $BOT_DIR) — deploying existing code."
        return 0
    fi
    log "Fetching origin/$BRANCH …"
    if ! git -C "$BOT_DIR" fetch --all --tags --prune >/dev/null 2>&1; then
        fail_hard "git fetch failed (network, remote, or auth). Keeping the current code and restarting."
    fi
    if ! git -C "$BOT_DIR" checkout -q "$BRANCH" 2>/dev/null; then
        log "WARN: could not checkout '$BRANCH'; trying origin/$BRANCH."
        git -C "$BOT_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH" \
            || fail_hard "could not create/update local branch $BRANCH."
    fi
    if ! git -C "$BOT_DIR" reset --hard "origin/$BRANCH" >/dev/null 2>&1; then
        fail_hard "git reset to origin/$BRANCH failed."
    fi
    UPDATED=1
    log "Updated to $(git -C "$BOT_DIR" rev-parse --short HEAD): $(git -C "$BOT_DIR" log -1 --pretty=%s | cut -c1-70)"
}

update_deps() {
    local venv="$BOT_DIR/.venv/bin/python"
    if [ ! -x "$venv" ]; then
        log "WARN: $venv missing — creating venv (run deploy_vps.sh for a full first-time setup)."
        python3 -m venv "$BOT_DIR/.venv" >/dev/null 2>&1 || return 0
    fi
    log "Installing requirements …"
    if ! "$BOT_DIR/.venv/bin/pip" install --quiet -r "$BOT_DIR/requirements.txt"; then
        log "WARN: pip install failed; continuing with the existing environment."
    fi
}

config_sanity() {
    # A deployment that cannot even import its own config must never reach the
    # exchange: validate first, on the code we are about to run.
    local py="$BOT_DIR/.venv/bin/python"
    [ -x "$py" ] || py="$(command -v python3)"
    log "Config/import sanity check on the candidate code …"
    if ! (cd "$BOT_DIR" && "$py" - <<'PY'
import config
config.validate()
import bot  # noqa: F401  (import-time wiring: modules, defaults, no network)
print("sanity ok")
PY
    ); then
        fail_hard "candidate code failed its own config/import sanity check."
    fi
}

stop_service() {
    systemd_available || { log "No systemd unit; skipping stop."; return 0; }
    log "Stopping $SERVICE …"
    # A stop we are not allowed to perform must not abort the deploy: the unit
    # ends up restarted below either way, and the invariant is "left running".
    sc stop "$SERVICE" >/dev/null 2>&1 \
        || log "(stop unavailable or not permitted; continuing — a restart will load the new code)"
    # Kill only *this* bot's processes — never a broad pattern that could take
    # out an unrelated script or abort the deploy when nothing matched.
    local victims
    victims="$(pgrep -f "$BOT_DIR/.*bot\\.py" 2>/dev/null || true)"
    if [ -n "$victims" ]; then
        log "Recovering stray bot processes: $victims"
        # shellcheck disable=SC2086
        kill -9 $victims 2>/dev/null || true
    fi
    sleep 2
}

# ── Deploy ───────────────────────────────────────────────────────────────────
SKIP_UPDATE=0
for arg in "$@"; do
    case "$arg" in
        --skip-update) SKIP_UPDATE=1 ;;
        --help|-h)
            sed -n '2,26p' "$0"; exit 0 ;;
        *) log "Ignoring unknown argument: $arg" ;;
    esac
done

if systemd_available && ! service_active; then
    log "Note: $SERVICE was already inactive before this deploy."
    ALREADY_DOWN=1
else
    ALREADY_DOWN=0
fi

# Epoch marker used by confirm_restarted to prove the unit actually
# (re)started during this run rather than merely answering on the old code.
DEPLOY_START_EPOCH="$(date +%s)"

update_code
STOP_FAILED=0
stop_service || STOP_FAILED=1
update_deps
config_sanity || log "WARN: sanity check failed — deploying anyway to restore service, rollback will follow."

log "Starting $SERVICE …"
start_service
if ! probe "$LIVE_URL" 10; then
    [ $STOP_FAILED -eq 0 ] || true
    fail_hard "service did not answer $LIVE_URL after start"
fi
if ! confirm_restarted; then
    log "WARN: $SERVICE did not (re)start during this deploy — running code would stay stale. Retrying once."
    sc restart "$SERVICE" >/dev/null 2>&1 || true
    sleep 4
    if probe "$LIVE_URL" 5 && confirm_restarted; then
        log "Second restart took effect — the unit now runs the deployed code."
    else
        fail_hard "unit never restarted with the new code (check the deploy user's sudoers verbs)"
    fi
fi
if ! probe "$READY_URL" "$READY_TRIES"; then
    log "WARN: /ready is not green yet (startup still running or not ready)."
    if service_active; then
        log "…but the unit is active and /live answers; readiness usually follows within a minute."
    else
        fail_hard "service died between /live and /ready"
    fi
fi

READY_BODY="$(curl -fsS --max-time 5 "$READY_URL" 2>/dev/null || true)"
log "✅ Deploy complete. ${UPDATED:+code=$(git -C "$BOT_DIR" rev-parse --short HEAD 2>/dev/null)}"
log "   /ready: ${READY_BODY:0:200}"
if [ "$ALREADY_DOWN" = "1" ]; then
    log "   (the service was already down before this run — check journalctl for the original outage)"
fi
exit 0
