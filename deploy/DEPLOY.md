# Deploy beers-crawler (API + web UI)

Public path (default):

| What | Path |
|------|------|
| **Web UI** | `/beers/rating/` |
| **API health** | `/beers/rating/api/health` |
| **Resolve** | `GET /beers/rating/api/v1/resolve?q=` |
| **Metadata** | `GET /beers/rating/api/v1/metadata?url=` |
| **Crawl** | `POST /beers/rating/api/v1/crawl` |

Host-specific values (**SSH target, filesystem paths, nginx site name, public domain**) are **not** stored in git. Put them in a local file:

```bash
cp deploy/deploy.env.example deploy/deploy.env
$EDITOR deploy/deploy.env
```

`deploy/deploy.env` is gitignored.

---

## Layout on server

```text
$DEPLOY_ROOT/
  app/                 # Python package (rsync --delete OK)
  web/dist/            # Vue build (rsync --delete OK)
  deploy/              # nginx / unit helpers (templates)
  data/                # ★ SQLite history — NEVER wiped by deploy
    beers.db
  logs/api.log
  env                  # ★ created once; never overwritten by deploy
  .local/bin/uv
```

**Data safety**

- Deploy rsync **excludes** `data/`, `*.db`, and does not touch `env` if present.
- systemd `BEERS_CRAWLER_DB` points at `data/beers.db` outside `app/`.
- Local `deploy/deploy.env` is never rsynced to the server.

---

## One-command deploy (laptop)

```bash
./scripts/deploy.sh
```

Requires `deploy/deploy.env` (or the same variables exported in the environment).

What it does:

1. `npm run build` with `base=/beers/rating/` and `VITE_API_BASE=/beers/rating/api`
2. Creates remote dirs; **seeds `env` only if missing** (never overwrites users/DB)
3. rsync code → `app/` (deletes stale code files, not data)
4. rsync `web/dist/`
5. `uv sync` on server
6. Installs/restarts `beers-crawler.service` (uvicorn `127.0.0.1:8741`)
7. Installs nginx snippet + reloads nginx (unless `--no-nginx`)
8. Smoke-tests local `/health` (+ optional public URL if `DEPLOY_PUBLIC_BASE` set)

After first deploy, create at least one API user (see below) or the API will refuse to start.

Useful flags:

```bash
./scripts/deploy.sh --dry-run
./scripts/deploy.sh --skip-build          # reuse existing web/dist
./scripts/deploy.sh --no-nginx            # code/service only
./scripts/deploy.sh --no-systemd
```

---

## Manual pieces (first time / recovery)

### systemd

```bash
sudo systemctl status beers-crawler
sudo journalctl -u beers-crawler -n 50 --no-pager
# or:
tail -f $DEPLOY_ROOT/logs/api.log
sudo systemctl restart beers-crawler
```

### nginx

Snippet: `/etc/nginx/snippets/beers-rating.conf`  
Included from the site conf named in `DEPLOY_NGINX_SITE_CONF` (matched `server_name` from `DEPLOY_NGINX_SERVER_NAME`).

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Use `^~ /beers/rating/` so other location regexes (CMS static rules, etc.) do not steal assets.

### env (on server)

`$DEPLOY_ROOT/env` (created once by deploy; not in git):

```bash
BEERS_CRAWLER_DB=$DEPLOY_ROOT/data/beers.db
BEERS_CRAWLER_PREFER_HTTPX=1
BEERS_CRAWLER_ALLOW_PLAYWRIGHT=0
BEERS_CRAWLER_MIN_REFRESH_SECONDS=21600
# BEERS_CRAWLER_CORS=https://www.example.com
```

### API users (required)

HTTP Basic auth is **on** by default. Users live in SQLite (`api_users`) as **password hashes only**.

```bash
# on server, from $DEPLOY_ROOT/app with the service env loaded:
cd $DEPLOY_ROOT/app
set -a && source $DEPLOY_ROOT/env && set +a
uv run beers-crawler user add <username>          # prompts for password
uv run beers-crawler user passwd <username>       # change password
uv run beers-crawler user list
uv run beers-crawler user delete <username>
sudo systemctl restart beers-crawler
```

Never put real usernames/passwords in git. Optional env bootstrap:
`BEERS_CRAWLER_API_USER` + `BEERS_CRAWLER_API_PASSWORD` (prefer CLI users).
Local open mode only: `BEERS_CRAWLER_AUTH_DISABLED=1`.

On small VPS hosts, production typically uses:

- `PREFER_HTTPX=1` + `ALLOW_PLAYWRIGHT=0` (Chromium is heavy)
- External search fallback to resolve Untappd `/b/` URLs when Untappd search is JS-only:
  **Brave → DuckDuckGo HTML → DDG lite**
- Beer **detail** pages usually work with plain httpx (JSON-LD ratings)
- Fresh history (6h) avoids re-hitting search engines

**Caveat:** free search endpoints may return 202/429 from some IPs. Cached history still serves prior successful crawls. For always-on live resolve, use a larger host with Playwright (`ALLOW_PLAYWRIGHT=1` + `playwright install chromium`).

---

## Backup history DB

```bash
ssh "$DEPLOY_HOST" \
  "cp -a $DEPLOY_ROOT/data/beers.db $DEPLOY_ROOT/data/beers.db.bak-\$(date +%Y%m%d)"
```

---

## Rollback code (not data)

```bash
git checkout <good-sha>
./scripts/deploy.sh
git checkout main
```

Data directory is left alone either way.

---

## Local UI against a remote API (optional)

```bash
cd web
VITE_API_BASE=https://www.example.com/beers/rating/api npm run dev
```

---

## Notes

- Prefer not committing real hostnames, usernames, or absolute server paths.
- Keep disk free of `node_modules` / full Chromium on small VPS installs unless Playwright is required.
