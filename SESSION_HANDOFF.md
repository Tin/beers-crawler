# Session handoff — 2026-07-19 (late)

**Next agent: start here, then read [`PLAN.md`](./PLAN.md).**

---

## What landed

### beers-crawler (`main`)

| Feature | Status |
|---------|--------|
| Live Untappd crawl (P0) | done |
| List-vs-sidebar ranking (P1) | done |
| Append-only rating history | done |
| **Min refresh window (default 6h)** | done — `BEERS_CRAWLER_MIN_REFRESH_SECONDS` |
| **`--force` / API `force`** | done |
| **`export` CSV/JSON** | done CLI + `GET /v1/export` |
| FastAPI `serve` | done |
| Offline tests | **22 passed** + CI |

### Toronado Viscosity (`main` — commit this session)

| Feature | Status |
|---------|--------|
| `BeersCrawlerClient` + `BeersCrawlerRatingLookup` | done |
| App default lookup → crawler HTTP (fallback search links) | done |
| Specs | `DescribeBeersCrawlerClientSpec` green |
| Timeout | `TimedBeerRatingLookup` 45s for server crawl |

---

## How it fits together

```text
iOS MenuScanViewModel
  TimedBeerRatingLookup(45s)
    BeersCrawlerRatingLookup  ──POST /v1/crawl──►  beers-crawler serve :8741
      fallback: SearchLinkBeerRatingLookup              │
                                                   CrawlerService
                                                   fresh history? → return
                                                   else live Playwright
                                                   append history / fallback
```

Local Simulator: crawler on Mac `127.0.0.1:8741` works if app runs in Simulator (shares host net).  
Physical device: `BEERS_CRAWLER_URL=http://<mac-lan-ip>:8741` + `serve --host 0.0.0.0`.

---

## Bootstrap

```bash
# crawler
cd /path/to/beers-crawler
uv sync && uv run playwright install chromium
uv run pytest -q
uv run beers-crawler serve --host 0.0.0.0 --port 8741

# app core specs
cd /path/to/toronado-viscosity
swift run ToronadoCoreSpecs
```

---

## Web UI + production deploy

**Live:** https://www.example.com/beers/rating/

```bash
# local
uv run beers-crawler serve --port 8741
cd web && npm run dev

# production (preserves remote data/ + env)
./scripts/deploy.sh
```

Docs: [`deploy/DEPLOY.md`](./deploy/DEPLOY.md)

Server: `you@your-server.example` → `/var/www/beers-crawler/`  
VPS is 1GB RAM — Playwright disabled; resolve uses **DuckDuckGo HTML** → Untappd detail via httpx.

## Next ideas

1. E2E Simulator against production API URL
2. History chart in web UI
3. Larger crawl worker host if Playwright needed again
4. Free more disk on example-host (root ~96% full)

---

## Key files

| Area | Path |
|------|------|
| Freshness + crawl policy | `beers-crawler/src/beers_crawler/service.py` |
| History export | `beers-crawler/src/beers_crawler/db.py` (`export_history_*`) |
| API | `beers-crawler/src/beers_crawler/api.py` |
| iOS client | `toronado-viscosity/Sources/ToronadoCore/BeersCrawlerClient.swift` |
| App wiring | `toronado-viscosity/App/ToronadoViscosity/MenuScanViewModel.swift` |
