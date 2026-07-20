# Session handoff — 2026-07-19 (evening)

**Next agent: start here, then read [`PLAN.md`](./PLAN.md).**

---

## What happened this session

Built on green P0 live crawl:

### History storage (user request)

Ratings drift → **append-only** crawl history, not overwrite cache.

| Policy | Behavior |
|--------|----------|
| Live OK | Return fresh data; **append** `beer_metadata` row with `scraped_at` |
| Live fails | Fall back to **latest** history (`from_history=true`) |
| `--history-only` / `history_only=true` | No network |

Verified live: two Pliny crawls → history ids 1 and 2; `--history-only` returns latest.

### P1 harden resolution

- Prefer `div.beer-item` / `div.results-container` over sidebar featured brands
- Stronger brewery + beer token scoring (toronado-style)
- Mega-brand penalty expanded
- Store **all** search candidates in `beer_pages`
- Compact fixture: `tests/fixtures/search_pliny_compact.html`

### P3 FastAPI stub

```text
uv run beers-crawler serve --port 8741
GET  /v1/resolve?q=
GET  /v1/resolve/candidates?q=
GET  /v1/metadata?url=
GET  /v1/metadata/history?url=
POST /v1/crawl  { "name": "…" }
GET  /v1/list
GET  /health
```

### Tests / CI

- `uv run pytest -q` → **18 passed**
- `.github/workflows/ci.yml` (offline pytest only)

---

## Where to continue

1. **Toronado HTTP client** — point app at `beers-crawler serve` instead of in-app scrape
2. **P2 ops** — export CSV/JSON of history, retry polish, config file/env docs
3. Optional: rate-limit / min-interval so re-crawls don’t hammer Untappd every request
4. Optional: chart rating over time from `history` rows

---

## Bootstrap

```bash
cd /path/to/beers-crawler
uv sync
uv run playwright install chromium   # if needed
uv run pytest -q
uv run beers-crawler crawl "Russian River Pliny the Elder" -v
uv run beers-crawler history "https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4499"
uv run beers-crawler serve
```

**Contracts (stable):**

1. `beer name: str` → Untappd page URL
2. `untappd page URL` → metadata / **`rating_score`** (+ history)

**New model flags:** `from_history`, `history_id`, `scraped_at` on snapshots.

---

## Architecture

```text
CLI / FastAPI
       │
CrawlerService   live-first → append history → fallback
       │
UntappdClient (Playwright; optional httpx)
       │
parsers.py
       │
SQLite  beer_pages (candidates upsert)
        beer_metadata (append-only history)
```

---

## Session checklist

```text
[x] P1 harden parsers (list vs sidebar)
[x] Append-only rating history + live-first fallback
[x] FastAPI stub + serve CLI
[x] pytest 18 green + CI workflow
[ ] Commit + push this slice
[ ] Toronado HTTP client wiring
```
