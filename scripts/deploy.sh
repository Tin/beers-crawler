#!/usr/bin/env bash
# Deploy beers-crawler API + web UI without wiping SQLite history.
#
# Setup (once):
#   cp deploy/deploy.env.example deploy/deploy.env
#   $EDITOR deploy/deploy.env   # set host, paths, nginx site
#
# Usage (from repo root):
#   ./scripts/deploy.sh
#   ./scripts/deploy.sh --skip-build
#   ./scripts/deploy.sh --dry-run
#   DEPLOY_HOST=user@host DEPLOY_ROOT=/var/www/beers-crawler ./scripts/deploy.sh
#
# Safe by design:
#   - NEVER rsyncs/deletes remote data/ (beers.db history)
#   - NEVER overwrites remote env if it already exists
#   - Code + web/dist are updated; service is restarted
#   - Host/path details live in gitignored deploy/deploy.env

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Load local deploy config if present (gitignored)
if [[ -f "$ROOT/deploy/deploy.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/deploy/deploy.env"
  set +a
fi

HOST="${DEPLOY_HOST:-}"
REMOTE_ROOT="${DEPLOY_ROOT:-}"
PUBLIC_ORIGINS="${DEPLOY_PUBLIC_ORIGINS:-}"
PUBLIC_BASE="${DEPLOY_PUBLIC_BASE:-}"
NGINX_SITE_CONF="${DEPLOY_NGINX_SITE_CONF:-}"
NGINX_SERVER_NAME="${DEPLOY_NGINX_SERVER_NAME:-}"
SERVICE_USER="${DEPLOY_SERVICE_USER:-}"
SERVICE_GROUP="${DEPLOY_SERVICE_GROUP:-$SERVICE_USER}"

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
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$HOST" || -z "$REMOTE_ROOT" ]]; then
  cat >&2 <<'ERR'
Missing DEPLOY_HOST and/or DEPLOY_ROOT.

Create a local (gitignored) config:
  cp deploy/deploy.env.example deploy/deploy.env
  edit deploy/deploy.env

Or pass env vars:
  DEPLOY_HOST=user@host DEPLOY_ROOT=/var/www/beers-crawler ./scripts/deploy.sh
ERR
  exit 1
fi

if [[ "$INSTALL_SYSTEMD" -eq 1 && -z "$SERVICE_USER" ]]; then
  echo "DEPLOY_SERVICE_USER is required when installing systemd (set in deploy/deploy.env)" >&2
  exit 1
fi

if [[ "$INSTALL_NGINX" -eq 1 ]]; then
  if [[ -z "$NGINX_SITE_CONF" || -z "$NGINX_SERVER_NAME" ]]; then
    echo "DEPLOY_NGINX_SITE_CONF and DEPLOY_NGINX_SERVER_NAME required for nginx install (or pass --no-nginx)" >&2
    exit 1
  fi
fi

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
ORIGINS="$PUBLIC_ORIGINS"
mkdir -p "\$ROOT"/{app,data,logs,web/dist,deploy,.local/bin}
if [[ ! -f "\$ROOT/env" ]]; then
  {
    echo "BEERS_CRAWLER_DB=\$ROOT/data/beers.db"
    echo "BEERS_CRAWLER_PREFER_HTTPX=1"
    echo "BEERS_CRAWLER_ALLOW_PLAYWRIGHT=0"
    echo "BEERS_CRAWLER_MIN_REFRESH_SECONDS=21600"
    if [[ -n "\$ORIGINS" ]]; then
      echo "BEERS_CRAWLER_CORS=\$ORIGINS"
    fi
  } > "\$ROOT/env"
  echo "Created \$ROOT/env"
else
  echo "Keeping existing \$ROOT/env"
  if ! grep -q '^BEERS_CRAWLER_ALLOW_PLAYWRIGHT=' "\$ROOT/env"; then
    echo 'BEERS_CRAWLER_ALLOW_PLAYWRIGHT=0' >> "\$ROOT/env"
    echo "Appended BEERS_CRAWLER_ALLOW_PLAYWRIGHT=0"
  fi
fi
ls -la "\$ROOT/data" || true
df -h / | tail -1
EOF

# --- 3. Rsync code (exclude data, venv, caches, local deploy secrets) ---
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
  --exclude 'deploy/deploy.env' \
  --exclude '.env' \
  --exclude '.env.*' \
  -e "$RSYNC_SSH" \
  "$ROOT/" "$HOST:$REMOTE_ROOT/app/"

echo "==> Rsync web/dist → ${REMOTE_ROOT}/web/dist/"
rsync -az --delete \
  -e "$RSYNC_SSH" \
  "$ROOT/web/dist/" "$HOST:$REMOTE_ROOT/web/dist/"

echo "==> Rsync deploy helpers → ${REMOTE_ROOT}/deploy/"
rsync -az \
  --exclude 'deploy.env' \
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

if grep -q 'BEERS_CRAWLER_PREFER_HTTPX=0' "\$ROOT/env" 2>/dev/null \
   || grep -q 'BEERS_CRAWLER_ALLOW_PLAYWRIGHT=1' "\$ROOT/env" 2>/dev/null; then
  echo "Playwright enabled in env — installing chromium if needed…"
  "\$ROOT/.local/bin/uv" run playwright install-deps chromium 2>/dev/null || true
  "\$ROOT/.local/bin/uv" run playwright install chromium || true
else
  echo "Skipping Playwright browser install (httpx / external-search mode)"
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
USER_NAME="$SERVICE_USER"
GROUP_NAME="$SERVICE_GROUP"
sudo tee /etc/systemd/system/beers-crawler.service >/dev/null <<UNIT
[Unit]
Description=beers-crawler Untappd API (uvicorn)
After=network.target

[Service]
Type=simple
User=\${USER_NAME}
Group=\${GROUP_NAME}
WorkingDirectory=\${ROOT}/app
EnvironmentFile=-\${ROOT}/env
Environment=BEERS_CRAWLER_DB=\${ROOT}/data/beers.db
Environment=PATH=\${ROOT}/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=\${ROOT}/.local/bin/uv run uvicorn beers_crawler.api:app --host 127.0.0.1 --port 8741 --workers 1
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
MemoryMax=512M
StandardOutput=append:\${ROOT}/logs/api.log
StandardError=append:\${ROOT}/logs/api.log

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
  ssh_run "bash -s" <<EOF
set -euo pipefail
ROOT="$REMOTE_ROOT"
SNIP="/etc/nginx/snippets/beers-rating.conf"
SITE_CONF="$NGINX_SITE_CONF"
SERVER_NAME="$NGINX_SERVER_NAME"
sudo mkdir -p /etc/nginx/snippets
# Render alias path into snippet
sudo sed "s|DEPLOY_ROOT|\${ROOT}|g" "\$ROOT/deploy/nginx-beers-rating.conf" | sudo tee "\$SNIP" >/dev/null

if [[ ! -f "\$SITE_CONF" ]]; then
  echo "nginx site conf not found: \$SITE_CONF" >&2
  exit 1
fi

if ! grep -q 'snippets/beers-rating.conf' "\$SITE_CONF"; then
  sudo cp "\$SITE_CONF" "\${SITE_CONF}.bak.beers-\$(date +%Y%m%d%H%M%S)"
  sudo python3 - "\$SITE_CONF" "\$SERVER_NAME" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
server_name = sys.argv[2]
text = path.read_text()
needle = f"server_name {server_name};"
include_line = "\n    # beers-crawler SPA + API\n    include /etc/nginx/snippets/beers-rating.conf;\n"
if "snippets/beers-rating.conf" in text:
    print("nginx already includes beers-rating snippet")
elif needle not in text:
    raise SystemExit(f"Could not find {needle!r} in {path}")
else:
    path.write_text(text.replace(needle, needle + include_line, 1))
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

if [[ -n "$PUBLIC_BASE" ]]; then
  echo "==> Public URL checks"
  set +e
  curl -fsSI "${PUBLIC_BASE}/" | head -20
  echo "---"
  curl -fsS "${PUBLIC_BASE}/api/health"
  echo
  set -e
  echo "  UI:  ${PUBLIC_BASE}/"
  echo "  API: ${PUBLIC_BASE}/api/health"
else
  echo "==> Skipping public URL checks (DEPLOY_PUBLIC_BASE unset)"
fi

echo
echo "Done."
echo "  DB:  ${REMOTE_ROOT}/data/beers.db  (preserved across deploys)"
echo "  Logs:${REMOTE_ROOT}/logs/api.log"
echo "  Redeploy: ./scripts/deploy.sh"
