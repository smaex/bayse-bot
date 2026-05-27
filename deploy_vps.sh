#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Bayse Bot — VPS Deploy Script
# Run this ONCE on a fresh Ubuntu 22.04 / Debian 12 VPS.
# Usage: chmod +x deploy_vps.sh && sudo ./deploy_vps.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

BOT_DIR="/opt/bayse-bot"
BOT_USER="bayse"
SERVICE_NAME="bayse-bot"
PYTHON="python3.11"

# ── Set your GitHub repo URL here ────────────────────────────────────────────
GITHUB_REPO="https://github.com/YOUR_USERNAME/bayse-bot.git"
# e.g. GITHUB_REPO="https://github.com/johndoe/bayse-bot.git"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Bayse Bot — VPS Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. System dependencies ────────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3.11 python3.11-venv python3-pip \
    libpq-dev build-essential \
    git curl ufw

# ── 2. Create dedicated system user ───────────────────────────────────────────
echo "[2/7] Creating system user '$BOT_USER'..."
id "$BOT_USER" &>/dev/null || useradd --system --create-home --shell /bin/bash "$BOT_USER"

# ── 3. Clone / update code from GitHub ───────────────────────────────────────
echo "[3/7] Deploying code to $BOT_DIR ..."
if [ -d "$BOT_DIR/.git" ]; then
    echo "  → Repo exists, pulling latest..."
    git -C "$BOT_DIR" fetch --all
    git -C "$BOT_DIR" reset --hard origin/main 2>/dev/null || \
    git -C "$BOT_DIR" reset --hard origin/master
else
    echo "  → Fresh clone from GitHub..."
    if [ -z "$GITHUB_REPO" ] || [ "$GITHUB_REPO" = "https://github.com/YOUR_USERNAME/bayse-bot.git" ]; then
        echo "  ❌  ERROR: Edit GITHUB_REPO at the top of this script before running!"
        exit 1
    fi
    git clone "$GITHUB_REPO" "$BOT_DIR"
fi
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"

# ── 4. Python virtual environment ─────────────────────────────────────────────
echo "[4/7] Setting up Python venv..."
sudo -u "$BOT_USER" $PYTHON -m venv "$BOT_DIR/.venv"
sudo -u "$BOT_USER" "$BOT_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$BOT_USER" "$BOT_DIR/.venv/bin/pip" install --quiet -r "$BOT_DIR/requirements.txt"

# ── 5. Environment file ───────────────────────────────────────────────────────
echo "[5/7] Checking .env file..."
if [ ! -f "$BOT_DIR/.env" ]; then
    echo ""
    echo "  ⚠️  No .env file found. Creating from example..."
    cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
    chown "$BOT_USER:$BOT_USER" "$BOT_DIR/.env"
    chmod 600 "$BOT_DIR/.env"
    echo ""
    echo "  ➡  Edit $BOT_DIR/.env and fill in your secrets:"
    echo "      TELEGRAM_TOKEN=..."
    echo "      ENCRYPTION_KEY=..."
    echo "      DATABASE_URL=..."
    echo "      NEWSAPI_KEY=...   (optional)"
    echo "      DEPLOYMENT_ENV=vps"
    echo ""
    echo "  Then re-run this script or: sudo systemctl start $SERVICE_NAME"
    echo ""
fi

# Ensure DEPLOYMENT_ENV=vps is set
grep -q "DEPLOYMENT_ENV" "$BOT_DIR/.env" || echo "DEPLOYMENT_ENV=vps" >> "$BOT_DIR/.env"

# ── 6. Systemd service ────────────────────────────────────────────────────────
echo "[6/7] Installing systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Bayse Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$BOT_DIR
EnvironmentFile=$BOT_DIR/.env
ExecStart=$BOT_DIR/.venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bayse-bot

# Resource limits — tune to your VPS plan
# 1 vCPU / 1GB RAM minimum recommended
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── Grant GitHub Actions passwordless restart permission ─────────────────────
# The deploy workflow SSHes in and runs: sudo systemctl restart bayse-bot
# This sudoers rule allows that without a password prompt blocking Actions.
SUDOERS_FILE="/etc/sudoers.d/bayse-bot"
if [ ! -f "$SUDOERS_FILE" ]; then
    echo "  → Adding sudoers rule for systemctl restart..."
    echo "$BOT_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart $SERVICE_NAME, /bin/systemctl status $SERVICE_NAME, /bin/systemctl is-active $SERVICE_NAME" > "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    echo "  ✅  Sudoers rule added: $SUDOERS_FILE"
fi

# If the VPS_USER in GitHub Actions is root, also allow root to restart:
echo "root ALL=(ALL) NOPASSWD: /bin/systemctl restart $SERVICE_NAME, /bin/systemctl status $SERVICE_NAME" >> "$SUDOERS_FILE" 2>/dev/null || true

# ── 7. Firewall ───────────────────────────────────────────────────────────────
echo "[7/7] Configuring firewall (UFW)..."
ufw allow OpenSSH
ufw --force enable

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅  Setup complete!"
echo ""
echo "  Bot Commands:"
echo "    Start:   sudo systemctl start $SERVICE_NAME"
echo "    Stop:    sudo systemctl stop $SERVICE_NAME"
echo "    Restart: sudo systemctl restart $SERVICE_NAME"
echo "    Logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "    Status:  sudo systemctl status $SERVICE_NAME"
echo ""
echo "  GitHub Actions — add these secrets to your repo:"
echo "    VPS_HOST  = your server IP or hostname"
echo "    VPS_USER  = the SSH user (root or $BOT_USER)"
echo "    VPS_SSH_KEY = your private SSH key (paste the full key)"
echo "    VPS_PORT  = 22 (or your custom SSH port)"
echo "    TELEGRAM_NOTIFY_CHAT_ID = your Telegram chat_id for deploy alerts"
echo ""
echo "  To generate an SSH key pair for GitHub Actions:"
echo "    ssh-keygen -t ed25519 -C 'github-actions-deploy' -f ~/.ssh/bayse_deploy"
echo "    cat ~/.ssh/bayse_deploy.pub >> ~/.ssh/authorized_keys  # on the VPS"
echo "    cat ~/.ssh/bayse_deploy                                # paste into GitHub secret VPS_SSH_KEY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
