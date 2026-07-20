# Session handoff

**Next agent: start here, then read [`PLAN.md`](./PLAN.md).**

---

## What landed

### beers-crawler (`main`)

| Feature | Status |
|---------|--------|
| Live Untappd crawl (local / Playwright) | done |
| List-vs-sidebar ranking | done |
| Append-only rating history | done |
| Min refresh window (default 6h) + `--force` | done |
| Export CSV/JSON | done |
| FastAPI `serve` | done |
| Vite + Vue SPA (`web/`) | done |
| Deploy script (data-safe, host via gitignored `deploy/deploy.env`) | done |
| External search fallback when Untappd search is JS-only | done (Brave → DDG) |
| Offline tests | green + CI |

### Toronado Viscosity (separate repo)

| Feature | Status |
|---------|--------|
| `BeersCrawlerClient` + `BeersCrawlerRatingLookup` | done |
| App default lookup → crawler HTTP (fallback search links) | done |

---

## How it fits together

```text
iOS / web UI
  → POST /v1/crawl  (or /beers/rating/api/v1/crawl behind nginx)
       ↓
  CrawlerService
    fresh history? → return
    else resolve (Untappd / external search) + metadata (httpx)
    append history / fallback
```

---

## Bootstrap

```bash
cd /path/to/beers-crawler
uv sync && uv run playwright install chromium
uv run pytest -q
uv run beers-crawler serve --port 8741
cd web && npm install && npm run dev
```

Deploy (local config only):

```bash
cp deploy/deploy.env.example deploy/deploy.env
# edit deploy/deploy.env with your host/paths (gitignored)
./scripts/deploy.sh
```

See [`deploy/DEPLOY.md`](./deploy/DEPLOY.md).

---

## Security / privacy notes for agents

- **Do not commit** `deploy/deploy.env`, `.env`, keys, or real server hostnames/paths.
- Templates use placeholders (`you@your-server.example`, `/var/www/beers-crawler`).
- No API keys or passwords are required for the default crawler setup.

---

## Next ideas

1. E2E app scan against a running API
2. History chart in web UI
3. Larger crawl worker if Playwright is required in production
4. Keep deploy host disk free (avoid shipping Chromium unless needed)
