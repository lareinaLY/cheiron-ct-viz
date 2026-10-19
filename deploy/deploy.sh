#!/usr/bin/env bash
# Deploy the latest pushed main to the server and restart. Run FROM your laptop:
#   ./deploy/deploy.sh            # uses HOST below
#   HOST=root@1.2.3.4 ./deploy/deploy.sh
set -euo pipefail
HOST="${HOST:-root@45.77.187.77}"
APP_DIR="${APP_DIR:-/opt/cheiron-ct-viz}"
BRANCH="${BRANCH:-main}"

LOCAL_REV=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain)" ]; then
  echo "!! working tree not clean; deploying origin/$BRANCH, not your local changes" >&2
fi
if ! git diff --quiet "origin/$BRANCH" HEAD 2>/dev/null; then
  echo "!! local HEAD ($LOCAL_REV) differs from origin/$BRANCH; did you push?" >&2
fi

ssh "$HOST" "bash -s" <<EOF
set -euo pipefail
export PATH="\$HOME/.local/bin:\$PATH"
cd "$APP_DIR"
git fetch --quiet origin "$BRANCH"
git reset --hard --quiet "origin/$BRANCH"
uv sync --frozen --no-dev --quiet
systemctl restart ctviz
sleep 2
systemctl is-active ctviz
echo "deployed \$(git rev-parse --short HEAD)"
curl -fsS 127.0.0.1:8000/health && echo
EOF

echo "remote health:"
curl -fsS "http://${HOST#*@}:8000/health" && echo
