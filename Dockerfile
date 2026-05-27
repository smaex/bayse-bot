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

# Runtime system deps only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
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

CMD ["python", "bot.py"]
