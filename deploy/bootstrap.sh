#!/usr/bin/env bash
# One-time server setup. Run ON the server as root:  bash bootstrap.sh
# Debian 12. Installs uv, clones the repo to /opt/cheiron-ct-viz, installs the systemd unit.
set -euo pipefail
REPO_URL="${REPO_URL:-https://github.com/lareinaLY/cheiron-ct-viz.git}"
APP_DIR="${APP_DIR:-/opt/cheiron-ct-viz}"
BRANCH="${BRANCH:-main}"

apt-get update -qq
apt-get install -y -qq git curl >/dev/null
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
uv sync --frozen --no-dev

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i -E '/^[A-Z_]+=$/d' .env
  echo ">>> Edit $APP_DIR/.env now (API keys, LLM_FALLBACK_CHAIN, DEMO_TOKEN), then: systemctl restart ctviz"
fi

install -m 644 deploy/ctviz.service /etc/systemd/system/ctviz.service
systemctl daemon-reload
systemctl enable --now ctviz
sleep 2
systemctl --no-pager --lines=5 status ctviz || true
curl -fsS 127.0.0.1:8000/health && echo
