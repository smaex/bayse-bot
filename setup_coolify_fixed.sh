#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Coolify deployment – CORRECT architecture
#
# Coolify needs its OWN local PostgreSQL (coolify-db).
# Supabase is for YOUR APP (bayse-bot), not for Coolify itself.
# ============================================================

# ------------------------------------------------------------
# 1️⃣ Backup existing Coolify data (if any)
# ------------------------------------------------------------
if [ -d "$HOME/coolify/data" ]; then
  echo "🔒 Backing up existing Coolify data..."
  BACKUP_DIR="$HOME/coolify/backup"
  mkdir -p "$BACKUP_DIR"
  BACKUP_FILE="$BACKUP_DIR/coolify_data_$(date +%Y%m%d%H%M%S).tar.gz"
  tar czf "$BACKUP_FILE" -C "$HOME/coolify" data
  echo "✅ Backup created at $BACKUP_FILE"
else
  echo "⚠️  No existing Coolify data directory – creating fresh one."
  mkdir -p "$HOME/coolify/data"
fi

mkdir -p "$HOME/coolify/pgdata"

# ------------------------------------------------------------
# 2️⃣ Ensure Docker network exists (idempotent)
# ------------------------------------------------------------
if ! docker network inspect coolify-network >/dev/null 2>&1; then
  docker network create coolify-network
  echo "✅ Docker network 'coolify-network' created."
else
  echo "✅ Docker network 'coolify-network' already exists."
fi

# ------------------------------------------------------------
# 3️⃣ Remove old Coolify containers (keep data volumes)
# ------------------------------------------------------------
docker rm -f coolify 2>/dev/null || true
docker rm -f coolify-redis 2>/dev/null || true
docker rm -f coolify-db 2>/dev/null || true

# ------------------------------------------------------------
# 4️⃣ Start local PostgreSQL for Coolify itself
# ------------------------------------------------------------
COOLIFY_DB_USER="coolify"
COOLIFY_DB_NAME="coolify"
COOLIFY_CREDS_FILE="$HOME/coolify/.db_credentials"

# Reuse existing password if pgdata volume already exists, so re-runs don't break the DB.
if [ -f "$COOLIFY_CREDS_FILE" ]; then
  # shellcheck source=/dev/null
  source "$COOLIFY_CREDS_FILE"
  echo "🔑 Reusing existing DB password from $COOLIFY_CREDS_FILE"
else
  COOLIFY_DB_PASSWORD="coolify_db_secret_$(openssl rand -hex 8)"
  echo "COOLIFY_DB_PASSWORD='${COOLIFY_DB_PASSWORD}'" > "$COOLIFY_CREDS_FILE"
  chmod 600 "$COOLIFY_CREDS_FILE"
  echo "🔑 Generated and saved new DB password to $COOLIFY_CREDS_FILE"
fi

echo "🐘 Starting local PostgreSQL (coolify-db)..."
docker run -d \
  --name coolify-db \
  --network coolify-network \
  --network-alias coolify-db \
  --restart unless-stopped \
  -v "$HOME/coolify/pgdata:/var/lib/postgresql/data" \
  -e POSTGRES_USER="${COOLIFY_DB_USER}" \
  -e POSTGRES_PASSWORD="${COOLIFY_DB_PASSWORD}" \
  -e POSTGRES_DB="${COOLIFY_DB_NAME}" \
  postgres:15-alpine

# Wait for Postgres to be ready
echo "⏳ Waiting for PostgreSQL to be ready..."
for i in {1..20}; do
  if docker exec coolify-db pg_isready -U "${COOLIFY_DB_USER}" >/dev/null 2>&1; then
    echo "✅ PostgreSQL is ready."
    break
  fi
  echo "⏳ Waiting for PostgreSQL... ($i)"
  sleep 2
done

# ------------------------------------------------------------
# 5️⃣ Start Redis side-car
# ------------------------------------------------------------
echo "🐳 Starting Redis container..."
docker run -d \
  --name coolify-redis \
  --network coolify-network \
  --network-alias coolify-redis \
  --restart unless-stopped \
  redis:7-alpine

# Wait for Redis
for i in {1..10}; do
  if docker exec coolify-redis redis-cli ping 2>/dev/null | grep -q PONG; then
    echo "✅ Redis responded to ping."
    break
  fi
  echo "⏳ Waiting for Redis... ($i)"
  sleep 2
done

# ------------------------------------------------------------
# 6️⃣ Persist APP_KEY (reuse existing to avoid MAC errors on re-runs)
# ------------------------------------------------------------
COOLIFY_APPKEY_FILE="$HOME/coolify/.app_key"
if [ -f "$COOLIFY_APPKEY_FILE" ]; then
  APP_KEY="$(cat "$COOLIFY_APPKEY_FILE")"
  echo "🔑 Reusing existing APP_KEY from $COOLIFY_APPKEY_FILE"
else
  APP_KEY="base64:$(openssl rand -base64 32)"
  echo "$APP_KEY" > "$COOLIFY_APPKEY_FILE"
  chmod 600 "$COOLIFY_APPKEY_FILE"
  echo "🔑 Generated and saved new APP_KEY to $COOLIFY_APPKEY_FILE"
fi

# ------------------------------------------------------------
# 7️⃣ Remove any stale .env files inside the data volume
# ------------------------------------------------------------
for ENV_FILE in \
    "$HOME/coolify/data/.env" \
    "$HOME/coolify/data/coolify.env" \
    "$HOME/coolify/data/storage/.env"; do
  if [ -f "$ENV_FILE" ]; then
    echo "🗑️  Removing stale env file: $ENV_FILE"
    rm -f "$ENV_FILE"
  fi
done

# ------------------------------------------------------------
# 8️⃣ Launch Coolify connected to local PostgreSQL + Redis
# ------------------------------------------------------------
echo "🚀 Starting Coolify container..."
docker run -d \
  --name coolify \
  --network coolify-network \
  --restart unless-stopped \
  -p 3000:8080 \
  -p 8000:8000 \
  -v "$HOME/coolify/data:/app/storage" \
  -e APP_KEY="${APP_KEY}" \
  -e APP_URL="http://69.164.244.180:3000" \
  -e APP_ENV="production" \
  -e APP_DEBUG="false" \
  -e DB_CONNECTION="pgsql" \
  -e DB_HOST="coolify-db" \
  -e DB_PORT="5432" \
  -e DB_DATABASE="${COOLIFY_DB_NAME}" \
  -e DB_USERNAME="${COOLIFY_DB_USER}" \
  -e DB_PASSWORD="${COOLIFY_DB_PASSWORD}" \
  -e REDIS_HOST="coolify-redis" \
  -e REDIS_PORT="6379" \
  coollabsio/coolify:latest

# ------------------------------------------------------------
# 9️⃣ Show recent logs for verification
# ------------------------------------------------------------
echo "⏳ Waiting 15 s for Coolify to initialise..."
sleep 15
echo ""
echo "📜 Last 30 Coolify logs:"
docker logs --tail 30 coolify
echo ""
echo "✅ Deployment complete."
echo "   Open: http://69.164.244.180:3000"
