#!/usr/bin/env bash
# Deploy beers-crawler API + web UI to example.com without wiping SQLite history.
#
# Usage (from laptop, repo root):
#   ./scripts/deploy.sh
#   ./scripts/deploy.sh --host you@your-server.example
#   ./scripts/deploy.sh --skip-build
#   ./scripts/deploy.sh --dry-run
#
# Safe by design:
#   - NEVER rsyncs/deletes remote data/ (beers.db history)
#   - NEVER overwrites remote env if it already exists
#   - Code + web/dist are updated; service is restarted

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${DEPLOY_HOST:-you@your-server.example}"
REMOTE_ROOT="${DEPLOY_ROOT:-/var/www/beers-crawler}"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
SKIP_BUILD=0
DRY_RUN=0
INSTALL_SYSTEMD=1
INSTALL_NGINX=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --root) REMOTE_ROOT="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-systemd) INSTALL_SYSTEMD=0; shift ;;
    --no-nginx) INSTALL_NGINX=0; shift ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

ssh_run() {
  # shellcheck disable=SC2029
  ssh "${SSH_OPTS[@]}" "$HOST" "$@"
}

RSYNC_SSH="ssh ${SSH_OPTS[*]}"

echo "==> Target ${HOST}:${REMOTE_ROOT}"

# --- 1. Build SPA for /beers/rating/ ---
if [[ "$SKIP_BUILD" -eq 0 ]]; then
  echo "==> Building web UI (base=/beers/rating/)"
  (
    cd "$ROOT/web"
    if [[ ! -d node_modules ]]; then
      npm ci || npm install
    fi
    VITE_BASE=/beers/rating/ VITE_API_BASE=/beers/rating/api npm run build
  )
else
  echo "==> Skipping web build"
  [[ -d "$ROOT/web/dist" ]] || { echo "web/dist missing; run without --skip-build" >&2; exit 1; }
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> Dry run: would rsync app + web/dist, preserve data/, restart service"
  exit 0
fi

# --- 2. Remote directories (data/ created once, never wiped) ---
echo "==> Ensuring remote layout (data/ preserved)"
ssh_run "bash -s" <<EOF
set -euo pipefail
ROOT="$REMOTE_ROOT"
mkdir -p "\$ROOT"/{app,data,logs,web/dist,deploy,.local/bin}
if [[ ! -f "\$ROOT/env" ]]; then
  cat > "\$ROOT/env" <<ENV
BEERS_CRAWLER_DB=$REMOTE_ROOT/data/beers.db
BEERS_CRAWLER_PREFER_HTTPX=1
BEERS_CRAWLER_ALLOW_PLAYWRIGHT=0
BEERS_CRAWLER_MIN_REFRESH_SECONDS=21600
BEERS_CRAWLER_CORS=https://www.example.com,https://example.com
ENV
  echo "Created \$ROOT/env"
else
  echo "Keeping existing \$ROOT/env"
  # Non-destructive: add ALLOW_PLAYWRIGHT=0 only if key missing
  if ! grep -q '^BEERS_CRAWLER_ALLOW_PLAYWRIGHT=' "\$ROOT/env"; then
    echo 'BEERS_CRAWLER_ALLOW_PLAYWRIGHT=0' >> "\$ROOT/env"
    echo "Appended BEERS_CRAWLER_ALLOW_PLAYWRIGHT=0"
  fi
fi
ls -la "\$ROOT/data" || true
df -h / | tail -1
EOF

# --- 3. Rsync code (exclude data, venv, caches) ---
echo "==> Rsync app code → ${REMOTE_ROOT}/app/"
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'data/' \
  --exclude 'web/node_modules/' \
  --exclude 'web/dist/' \
  --exclude 'node_modules/' \
  --exclude '.DS_Store' \
  --exclude '*.db' \
  --exclude '*.db-wal' \
  --exclude '*.db-shm' \
  -e "$RSYNC_SSH" \
  "$ROOT/" "$HOST:$REMOTE_ROOT/app/"

echo "==> Rsync web/dist → ${REMOTE_ROOT}/web/dist/"
rsync -az --delete \
  -e "$RSYNC_SSH" \
  "$ROOT/web/dist/" "$HOST:$REMOTE_ROOT/web/dist/"

echo "==> Rsync deploy helpers → ${REMOTE_ROOT}/deploy/"
rsync -az \
  -e "$RSYNC_SSH" \
  "$ROOT/deploy/" "$HOST:$REMOTE_ROOT/deploy/"

# --- 4. Remote uv + deps ---
echo "==> Remote uv sync"
ssh_run "bash -s" <<EOF
set -euo pipefail
ROOT="$REMOTE_ROOT"
export PATH="\$ROOT/.local/bin:\$HOME/.local/bin:\$PATH"

if [[ ! -x "\$ROOT/.local/bin/uv" ]]; then
  if [[ -x "\$HOME/.local/bin/uv" ]]; then
    ln -sfn "\$HOME/.local/bin/uv" "\$ROOT/.local/bin/uv"
  else
    echo "Installing uv into \$ROOT/.local"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="\$ROOT/.local" sh
  fi
fi

cd "\$ROOT/app"
export UV_PROJECT_ENVIRONMENT="\$ROOT/app/.venv"
"\$ROOT/.local/bin/uv" sync --no-dev

if grep -q 'BEERS_CRAWLER_PREFER_HTTPX=0' "\$ROOT/env" 2>/dev/null; then
  echo "Installing Playwright chromium…"
  "\$ROOT/.local/bin/uv" run playwright install-deps chromium 2>/dev/null || true
  "\$ROOT/.local/bin/uv" run playwright install chromium || true
else
  echo "Skipping Playwright browser install (BEERS_CRAWLER_PREFER_HTTPX=1)"
fi

touch "\$ROOT/data/.keep"
ls -la "\$ROOT/data"
df -h / | tail -1
EOF

# --- 5. systemd unit ---
if [[ "$INSTALL_SYSTEMD" -eq 1 ]]; then
  echo "==> Install/restart systemd unit beers-crawler"
  ssh_run "bash -s" <<EOF
set -euo pipefail
ROOT="$REMOTE_ROOT"
sudo tee /etc/systemd/system/beers-crawler.service >/dev/null <<UNIT
[Unit]
Description=beers-crawler Untappd API (uvicorn)
After=network.target

[Service]
Type=simple
User=tin
Group=tin
WorkingDirectory=$REMOTE_ROOT/app
EnvironmentFile=-$REMOTE_ROOT/env
Environment=BEERS_CRAWLER_DB=$REMOTE_ROOT/data/beers.db
Environment=PATH=$REMOTE_ROOT/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$REMOTE_ROOT/.local/bin/uv run uvicorn beers_crawler.api:app --host 127.0.0.1 --port 8741 --workers 1
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
MemoryMax=512M
StandardOutput=append:$REMOTE_ROOT/logs/api.log
StandardError=append:$REMOTE_ROOT/logs/api.log

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable beers-crawler.service
sudo systemctl restart beers-crawler.service
sleep 3
sudo systemctl --no-pager --full status beers-crawler.service | head -30 || true
EOF
fi

# --- 6. nginx snippet ---
if [[ "$INSTALL_NGINX" -eq 1 ]]; then
  echo "==> Install nginx locations for /beers/rating/"
  ssh_run "bash -s" <<'EOF'
set -euo pipefail
ROOT="/var/www/beers-crawler"
SNIP="/etc/nginx/snippets/beers-rating.conf"
sudo mkdir -p /etc/nginx/snippets
sudo cp "$ROOT/deploy/nginx-beers-rating.conf" "$SNIP"

CONF="/etc/nginx/sites-enabled/example.com.conf"
if ! grep -q 'snippets/beers-rating.conf' "$CONF"; then
  sudo cp "$CONF" "${CONF}.bak.beers-$(date +%Y%m%d%H%M%S)"
  sudo python3 - <<'PY'
from pathlib import Path
path = Path("/etc/nginx/sites-enabled/example.com.conf")
text = path.read_text()
needle = "server_name www.example.com;"
include_line = "\n    # beers-crawler SPA + API\n    include /etc/nginx/snippets/beers-rating.conf;\n"
if "snippets/beers-rating.conf" in text:
    print("nginx already includes beers-rating snippet")
elif needle not in text:
    raise SystemExit(f"Could not find {needle!r} in {path}")
else:
    text = text.replace(needle, needle + include_line, 1)
    path.write_text(text)
    print("Patched", path)
PY
else
  echo "nginx already includes beers-rating snippet"
fi

sudo nginx -t
sudo systemctl reload nginx
echo "nginx reloaded"
EOF
fi

# --- 7. Smoke tests ---
echo "==> Smoke tests (local on server)"
ssh_run "bash -s" <<EOF
set -euo pipefail
for i in 1 2 3 4 5; do
  if curl -fsS http://127.0.0.1:8741/health >/tmp/beers-health.json; then
    cat /tmp/beers-health.json
    echo
    break
  fi
  echo "wait for api… (\$i)"
  sleep 2
done
ls -la "$REMOTE_ROOT/data"
test -f "$REMOTE_ROOT/web/dist/index.html"
EOF

echo "==> Public URL checks"
set +e
curl -fsSI "https://www.example.com/beers/rating/" | head -20
echo "---"
curl -fsS "https://www.example.com/beers/rating/api/health"
echo
set -e

echo
echo "Done."
echo "  UI:  https://www.example.com/beers/rating/"
echo "  API: https://www.example.com/beers/rating/api/health"
echo "  DB:  ${REMOTE_ROOT}/data/beers.db  (preserved across deploys)"
echo "  Logs:${REMOTE_ROOT}/logs/api.log"
echo "  Redeploy: ./scripts/deploy.sh"
