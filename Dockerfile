# ── Bayse Bot — Dockerfile for Coolify/VPS deployment ──────────────────────
# Multi-stage build: keeps the final image lean (~150MB vs ~800MB)

# ── Stage 1: Build ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build deps (needed for psycopg2-binary, cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into a separate directory (for clean copy to final stage)
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Runtime system deps only (no build tools).
#
# curl AND wget are both required, not optional: Coolify injects its own
# container healthcheck for Dockerfile-based deployments and that command is
# `wget --spider <healthcheck url>`. A slim Python image ships neither, so a
# deploy fails its health probe with "/bin/sh: 1: wget: not found" and rolls
# back even though the bot itself is running fine (2026-09-25 incident — see
# reports/coolify_rolling_update_lease.md). Keeping both clients also means the
# image works with either Coolify's probe or the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl wget \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Copy source code
COPY . .

# Make sure pip-installed binaries are on PATH
ENV PATH=/root/.local/bin:$PATH

# Coolify/Render health check port
EXPOSE 8080

# Tell Coolify this is not a sleeping process — it's a persistent bot
ENV PYTHONUNBUFFERED=1

# Liveness only (/live). A readiness probe here would kill the container during
# a rolling update: the new instance answers /live while it waits for the old
# one to hand over the singleton lease, and must not be restarted for it.
# Coolify overrides this with its own wget probe; it matters for plain
# `docker run` / docker-compose / other orchestrators.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD wget -q --spider "http://127.0.0.1:${PORT:-8080}/live" \
        || curl -fsS "http://127.0.0.1:${PORT:-8080}/live" >/dev/null \
        || exit 1

CMD ["python", "bot.py"]
