#!/usr/bin/env bash
# One-time installer for the Crypto Momentum Scanner on Ubuntu 24.04.
# Run as root:  bash install.sh [Your/Timezone]   e.g.  bash install.sh America/New_York
set -euo pipefail

REPO=https://github.com/henryGitHu/Fomo-.git
BRANCH=claude/bot-spec-phase-1-y38wxm
DIR=/opt/crypto-scanner
TZ_NAME="${1:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run this as root (or put 'sudo' in front)."; exit 1
fi

echo "==> Installing system packages (this takes a minute)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git nano >/dev/null

PYV=$(python3 -c 'import sys; print(sys.version_info >= (3, 11))')
if [ "$PYV" != "True" ]; then
  echo "This server's Python is too old (need 3.11+). Please create the server with Ubuntu 24.04."; exit 1
fi

if [ -n "$TZ_NAME" ]; then
  echo "==> Setting the clock to $TZ_NAME (used for the end-of-day summary time)..."
  timedatectl set-timezone "$TZ_NAME"
fi

echo "==> Creating a dedicated 'scanner' user (the program never runs as root)..."
id scanner >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin scanner

echo "==> Downloading the scanner..."
if [ -d "$DIR/.git" ]; then
  sudo -u scanner git -C "$DIR" fetch -q origin "$BRANCH"
  sudo -u scanner git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  mkdir -p "$DIR"; chown scanner:scanner "$DIR"
  sudo -u scanner git clone -q --branch "$BRANCH" "$REPO" "$DIR"
fi

echo "==> Installing Python packages (this takes a few minutes)..."
sudo -u scanner python3 -m venv "$DIR/.venv"
sudo -u scanner "$DIR/.venv/bin/python" -m pip install -q --upgrade pip
sudo -u scanner "$DIR/.venv/bin/python" -m pip install -q -r "$DIR/requirements.txt"

if [ ! -f "$DIR/.env" ]; then
  sudo -u scanner cp "$DIR/.env.example" "$DIR/.env"
fi
chmod 600 "$DIR/.env"

echo "==> Setting it up to run all the time and restart itself..."
install -m 644 "$DIR/deploy/crypto-scanner.service" /etc/systemd/system/crypto-scanner.service
install -m 755 "$DIR/deploy/scanner" /usr/local/bin/scanner
systemctl daemon-reload
systemctl enable -q crypto-scanner

sudo -u scanner "$DIR/.venv/bin/python" -c "from scanner.config import load_config; load_config(); print('config.yaml OK')"

echo
echo "=============================================================="
echo " Installed. Next steps:"
echo "   1. scanner edit-env      - paste your Telegram bot token + chat ID"
echo "   2. scanner test-alert    - check a message reaches your phone"
echo "   3. scanner start         - start scanning (runs 24/7, survives reboots)"
echo "   4. scanner logs          - watch it work (Ctrl+C to stop watching)"
echo " Type 'scanner' any time to see all commands."
echo "=============================================================="
