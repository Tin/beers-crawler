# Deploy beers-crawler to example.com

Public URLs:

| What | URL |
|------|-----|
| **Web UI** | https://www.example.com/beers/rating/ |
| **API health** | https://www.example.com/beers/rating/api/health |
| **Resolve** | `GET /beers/rating/api/v1/resolve?q=` |
| **Metadata** | `GET /beers/rating/api/v1/metadata?url=` |
| **Crawl** | `POST /beers/rating/api/v1/crawl` |

Host: `you@your-server.example` (same box as example.com).  
SSH as user **tin** (sudo available).

---

## Layout on server

```text
/var/www/beers-crawler/
  app/                 # Python package (rsync --delete OK)
  web/dist/            # Vue build (rsync --delete OK)
  deploy/              # nginx unit helpers
  data/                # ★ SQLite history — NEVER wiped by deploy
    beers.db
  logs/api.log
  env                  # ★ created once; never overwritten by deploy
  .local/bin/uv
```

**Data safety**

- Deploy rsync **excludes** `data/`, `*.db`, and does not touch `env` if present.
- systemd `BEERS_CRAWLER_DB` points at `data/beers.db` outside `app/`.
- Only append-only history writes hit that DB at runtime.

---

## One-command deploy (laptop)

From the repo root (needs SSH key access to `you@your-server.example`):

```bash
./scripts/deploy.sh
```

What it does:

1. `npm run build` with `base=/beers/rating/` and `VITE_API_BASE=/beers/rating/api`
2. Creates remote dirs; **seeds `env` only if missing**
3. rsync code → `app/` (deletes stale code files, not data)
4. rsync `web/dist/`
5. `uv sync` on server
6. Installs/restarts `beers-crawler.service` (uvicorn `:8741`)
7. Installs nginx snippet + reloads nginx
8. Smoke-tests local + public health

Useful flags:

```bash
./scripts/deploy.sh --dry-run
./scripts/deploy.sh --skip-build          # reuse existing web/dist
./scripts/deploy.sh --host you@your-server.example
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
tail -f /var/www/beers-crawler/logs/api.log
sudo systemctl restart beers-crawler
```

### nginx

Snippet: `/etc/nginx/snippets/beers-rating.conf`  
Included from `sites-enabled/example.com.conf` (www server block).

```bash
sudo nginx -t && sudo systemctl reload nginx
```

WordPress owns `/` on www.example.com. Locations use `^~ /beers/rating/` so static regex for `.js` under the WP root does not steal assets.

### env

`/var/www/beers-crawler/env` (not in git on server):

```bash
BEERS_CRAWLER_DB=/var/www/beers-crawler/data/beers.db
BEERS_CRAWLER_PREFER_HTTPX=1
BEERS_CRAWLER_MIN_REFRESH_SECONDS=21600
BEERS_CRAWLER_CORS=https://www.example.com,https://example.com
```

On this **1 GB VPS**, production uses:

- `PREFER_HTTPX=1` + `ALLOW_PLAYWRIGHT=0` (Chromium OOMs / missing headless_shell)
- **DuckDuckGo HTML** fallback to resolve Untappd `/b/` URLs when Untappd search is JS-only
- Beer **detail** pages usually work with plain httpx (JSON-LD ratings)

Optional later: move crawl workers to a larger host and set `ALLOW_PLAYWRIGHT=1`.

---

## Backup history DB

```bash
ssh you@your-server.example \
  'cp -a /var/www/beers-crawler/data/beers.db \
        /var/www/beers-crawler/data/beers.db.bak-$(date +%Y%m%d)'
```

---

## Rollback code (not data)

```bash
# redeploy previous git revision from laptop
git checkout <good-sha>
./scripts/deploy.sh
git checkout main
```

Data directory is left alone either way.

---

## Local UI against production API (optional)

```bash
cd web
VITE_API_BASE=https://www.example.com/beers/rating/api npm run dev
```

---

## Disk note

VPS root is tight (~1 GB free). Deploy skips Playwright browsers by default. Avoid copying `node_modules` or full Chromium to the server.
