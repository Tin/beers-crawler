# beers-crawler web UI

Minimal Vite + Vue SPA for the two crawler interfaces:

1. **Resolve** — beer name → Untappd page URL (`GET /v1/resolve`)
2. **Metadata** — page URL → rating & details (`GET /v1/metadata`)

“Look up beer” runs the combined crawl (`POST /v1/crawl`) and renders a human-readable card.

## Run

Terminal 1 — API:

```bash
cd /path/to/beers-crawler
uv run beers-crawler serve --port 8741
```

Terminal 2 — UI:

```bash
cd /path/to/beers-crawler/web
npm install
npm run dev
```

Open http://127.0.0.1:5173

Dev server proxies `/v1` and `/health` to `http://127.0.0.1:8741` (override with `BEERS_CRAWLER_URL`).

## Build

```bash
npm run build    # → web/dist
npm run preview  # preview production build
```

Optional: set `VITE_API_BASE=http://127.0.0.1:8741` if you serve the static build without a proxy.
