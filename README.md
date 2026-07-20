# beers-crawler

Server-side Untappd crawler (CLI first, SQLite **history** storage). Built to back apps like Toronado Viscosity with reliable beer page URLs and rating scores.

## Interfaces

| Interface | Input | Output |
|-----------|--------|--------|
| **1. Resolve** | beer name `str` | Untappd page URL (`BeerPageRef`) |
| **2. Metadata** | Untappd page URL | beer metadata, especially **`rating_score`** (`BeerMetadata`) |

```text
"Russian River Pliny the Elder"
        │
        ▼  resolve_page()          (live first)
https://untappd.com/b/.../4499
        │
        ▼  lookup_metadata()       (live first → append history)
{ name, brewery, rating_score, rating_count, scraped_at, ... }
```

### History + freshness policy

Ratings drift over time. Every successful metadata crawl **appends** a timestamped row — nothing is overwritten.

| Situation | Behavior |
|-----------|----------|
| Snapshot younger than `min_refresh` (default **6h**) | Return history; **skip** live crawl |
| Live crawl OK | Return fresh data; **append** history snapshot |
| Live crawl fails | Return **latest** history snapshot if one exists (`from_history=true`) |
| `--history-only` | Skip network; read latest history only |
| `--force` | Ignore freshness; always attempt live crawl |

Env: `BEERS_CRAWLER_MIN_REFRESH_SECONDS` (default `21600`), `BEERS_CRAWLER_DB`, `BEERS_CRAWLER_HEADED=1`.

## Setup

```bash
cd /path/to/beers-crawler
uv sync
uv run playwright install chromium
```

## CLI

```bash
uv run beers-crawler init-db

# 1) beer name → Untappd URL (live first, history fallback)
uv run beers-crawler resolve "Russian River Pliny the Elder"
uv run beers-crawler resolve "Moonlight Reality Czech" --json

# 2) Untappd URL → metadata (appends history on success)
uv run beers-crawler metadata "https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4499"

# Combined
uv run beers-crawler crawl "Russian River Pliny the Elder" -v

# Past snapshots for one beer
uv run beers-crawler history "https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4499"

# Export rating time series
uv run beers-crawler export --format csv -o history.csv
uv run beers-crawler export --format json --url "https://untappd.com/b/.../4499"

# Offline read of last known score
uv run beers-crawler crawl "Russian River Pliny the Elder" --history-only

# Force live re-crawl (ignore 6h freshness)
uv run beers-crawler crawl "Russian River Pliny the Elder" --force

# Batch (each success appends a history row)
uv run beers-crawler batch beers.example.txt --delay 2

# Ranked search candidates / cache stats / latest per beer
uv run beers-crawler candidates "Russian River Pliny the Elder"
uv run beers-crawler stats
uv run beers-crawler list

# HTTP API for Toronado / clients
uv run beers-crawler serve --port 8741
# docs: http://127.0.0.1:8741/docs
```

Flags:

- `--db PATH` — SQLite file (default `./data/beers.db`)
- `--history-only` — skip live crawl; read history only
- `--force` — ignore freshness window; always live crawl
- `--no-history` — do not read/write SQLite history
- `--headed` — show the browser
- `--json` — machine-readable output
- `-v` — debug logs

## HTTP API

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/health` | liveness + DB stats + min_refresh |
| `GET` | `/v1/resolve?q=` | name → URL |
| `GET` | `/v1/resolve/candidates?q=` | ranked candidates |
| `GET` | `/v1/metadata?url=` | URL → metadata (fresh / live + append) |
| `GET` | `/v1/metadata/history?url=` | all snapshots |
| `GET` | `/v1/export?format=csv\|json` | full history export |
| `POST` | `/v1/crawl` | `{ "name": "…", "force": false }` |
| `GET` | `/v1/list` | latest snapshot per beer |

Optional query/body: `history_only=true`, `force=true`.

### Web UI (Vite + Vue)

Minimal SPA under [`web/`](./web) exposing **resolve** + **metadata** with a readable result card.

```bash
# terminal 1
uv run beers-crawler serve --port 8741

# terminal 2
cd web && npm install && npm run dev
# → http://127.0.0.1:5173
```

### Toronado Viscosity

App uses `BeersCrawlerRatingLookup` → `POST /v1/crawl`. Run the service locally (Simulator → host):

```bash
uv run beers-crawler serve --host 0.0.0.0 --port 8741
# optional: BEERS_CRAWLER_URL=http://<mac-lan-ip>:8741
```

## Project layout

```text
src/beers_crawler/
  cli.py                 # click CLI
  api.py                 # FastAPI
  db.py                  # SQLite (append-only metadata history)
  models.py              # BeerPageRef, BeerMetadata
  service.py             # live-first + history fallback
  untappd/
    interfaces.py
    client.py            # Playwright (+ optional httpx)
    parsers.py           # HTML → models
tests/
```

## SQLite

- **`beer_pages`** — latest search candidates per query (upsert)
- **`beer_metadata`** — **append-only** crawl history (`page_url` + `scraped_at`); many rows per beer over time

## Tests

```bash
uv run pytest -q
```

Offline only (no live Untappd in CI).

## Notes

- Untappd may rate-limit or change DOM; parsers use primary beer-list + JSON-LD + CSS + regex fallbacks.
- Be polite: batch mode defaults to a delay between beers.
- See [`PLAN.md`](./PLAN.md) for roadmap.

## License

See [LICENSE](LICENSE).
