# beers-crawler — plan & handoff

Last updated: 2026-07-19 (evening)  
Repo: `/path/to/beers-crawler` (`git@github.com:example/beers-crawler.git`)  
Related app: `/path/to/toronado-viscosity` (iOS; in-app Untappd scrape is weak — this service is the intended backend)

---

## Goal

Build a **server-side** Untappd crawler that can reliably:

1. **Beer name → Untappd page URL**
2. **Untappd page URL → beer metadata** (especially **rating score**)

Storage: **SQLite append-only crawl history** (scores change over time). Interface priority: **CLI first**, HTTP API for Toronado, web UI later.

### History policy (v0.3)

| Event | Behavior |
|-------|----------|
| Live crawl succeeds | Return live data; **append** timestamped `beer_metadata` row |
| Live crawl fails | Return **latest** history row if present (`from_history=true`) |
| `history_only` | Skip network; read latest history only |

Never overwrite prior snapshots — ratings drift and history is the product.

---

## Two interfaces (stable contract)

Defined in `src/beers_crawler/untappd/interfaces.py` and models in `models.py`.

### 1. `BeerNameToPageResolver`

| | |
|--|--|
| **Input** | `beer_name: str` (free text, ideally `"Brewery Beer Name"`) |
| **Output** | `BeerPageRef \| None` |
| **Fields** | `query`, `page_url`, `slug`, `beer_id`, `match_score`, `source`, `from_history` |
| **CLI** | `uv run beers-crawler resolve "Russian River Pliny the Elder"` |
| **Code** | `UntappdClient.resolve_page` → `CrawlerService.beer_name_to_url` |

### 2. `BeerMetadataLookup`

| | |
|--|--|
| **Input** | `page_url: str` (Untappd `/b/{slug}/{id}`) |
| **Output** | `BeerMetadata \| None` |
| **Critical field** | `rating_score` (0–5 float) |
| **Also** | `name`, `brewery`, `style`, `abv`, `ibu`, `rating_count`, `description`, `beer_id`, `scraped_at`, `from_history`, `history_id` |
| **CLI** | `uv run beers-crawler metadata "https://untappd.com/b/..."` |
| **Code** | `UntappdClient.lookup_metadata` → `CrawlerService.url_to_metadata` |

### Combined

```text
crawl / crawl_beer: name → resolve → metadata → append history
```

CLI: `uv run beers-crawler crawl "…"`

---

## Why this exists (context from Toronado)

In-app `WebBeerRatingLookup` (URLSession + HTML) often:

- Picks the **wrong** beer URL (first `/b/` link = featured Guinness, etc.)
- Gets empty/blocked Untappd HTML without a real browser
- Times out or falls back to DuckDuckGo search links with **no score**

This crawler uses **Playwright (Chromium)** + **scored search matching** + **SQLite history** so resolution can be fixed server-side and reused.

---

## What’s built (v0.3)

```text
beers-crawler/
  PLAN.md
  README.md
  SESSION_HANDOFF.md
  pyproject.toml
  beers.example.txt
  .github/workflows/ci.yml
  src/beers_crawler/
    cli.py                # resolve | metadata | crawl | batch | list | history | candidates | stats | serve
    api.py                # FastAPI /v1/*
    db.py                 # SQLite schema + append-only history
    models.py
    service.py            # live-first + history fallback
    untappd/
      interfaces.py
      client.py           # Playwright (+ optional httpx)
      parsers.py          # list vs sidebar ranking
  tests/
    fixtures/search_pliny_compact.html
```

---

## Bootstrap (new session)

```bash
cd /path/to/beers-crawler
uv sync
uv run playwright install chromium
uv run pytest -q
uv run beers-crawler crawl "Russian River Pliny the Elder" -v
uv run beers-crawler history "https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4499"
uv run beers-crawler serve --port 8741
```

---

## Near-term work

### P0 — Prove live crawl works — **done**

### P1 — Harden resolution — **done**

1. Prefer `div.beer-item` / `results-container` over sidebar featured brands
2. Brewery + beer-name token scoring (`split_query_hints`)
3. Optional `prefer_httpx` static fetch
4. Store all search candidates (`save_page_refs`, `candidates` CLI/API)

### P1b — Append-only rating history — **done**

1. Many `beer_metadata` rows per `page_url` with `scraped_at`
2. Live-first; append on success; history fallback on failure
3. CLI `history` + API `GET /v1/metadata/history`
4. Migrate legacy UNIQUE(page_url) DBs on open

### P2 — CLI / ops polish

1. ~~Export CSV/JSON of history time series~~ **done** (`export` + `/v1/export`)
2. ~~Min re-crawl interval~~ **done** (`min_refresh_seconds` default 6h, `--force`)
3. Config file (YAML/env): DB path, delay, headless, user-agent — partial (env vars exist)
4. ~~GitHub Actions offline pytest~~ **done**
5. Stronger retry/backoff on 429/timeout — basic retries exist; can harden

### P3 — Service API

1. ~~FastAPI stub + `beers-crawler serve`~~ **done**
2. Same `CrawlerService` underneath
3. ~~Toronado Viscosity HTTP client → this service~~ **done** (`BeersCrawlerRatingLookup`)

### P4 — Web UI (later)

1. Search box → table of latest scores + history sparkline
2. Trigger crawl jobs
3. Do not block CLI/API on UI

---

## Non-goals (for now)

- Full Untappd site mirror / bulk dump of all beers
- Official Untappd API keys (unless we later get access)
- BeerAdvocate parity (optional second source later)
- Shipping Playwright inside the iOS app

---

## SQLite schema (reference)

```sql
beer_pages(
  query, page_url, slug, beer_id, match_score, source, created_at
  UNIQUE(query, page_url)   -- latest candidates (upsert)
)

beer_metadata(
  id PK,
  page_url,                 -- NOT unique: append-only history
  name, brewery, style, abv, ibu,
  rating_score, rating_count, description, beer_id,
  scraped_at, raw_json
)
-- latest: ORDER BY scraped_at DESC, id DESC LIMIT 1 per page_url
```

---

## Code map for implementers

| Task | Start here |
|------|------------|
| Change match ranking | `untappd/parsers.py` → `match_score`, `best_search_result` |
| Change rating extraction | `untappd/parsers.py` → `parse_beer_page` |
| Browser fetch behavior | `untappd/client.py` → `_get_html`, timeouts, wait selectors |
| History / fallback policy | `service.py` + `db.py` |
| New CLI command | `cli.py` |
| HTTP routes | `api.py` |
| Contract / types | `models.py`, `untappd/interfaces.py` |

---

## Success criteria (v0.2) — done

- [x] Live Pliny / Reality Czech / SNPA with scores
- [x] README matches CLI

## Success criteria (v0.3) — history + API

- [x] Append-only history: two live crawls → two rows; `history` CLI lists both
- [x] Live-first default; `--history-only` skips network
- [x] Live failure falls back to latest history (unit-tested)
- [x] Primary-list vs sidebar ranking (fixture + unit tests)
- [x] FastAPI `/v1/*` + `beers-crawler serve`
- [x] `pytest` offline; CI workflow
- [x] Toronado client wired to API

## Success criteria (v0.4) — freshness + export + iOS

- [x] Min refresh window skips live when snapshot is fresh
- [x] `--force` / `force=true` bypasses freshness
- [x] `export` CSV/JSON + `/v1/export`
- [x] `BeersCrawlerRatingLookup` default in MenuScanViewModel
- [x] ToronadoCoreSpecs green including crawler client specs
- [ ] E2E Simulator scan against live `serve`

### Live results snapshot (2026-07-19)

| Query | URL slug/id | Score |
|-------|-------------|-------|
| Russian River Pliny the Elder | `russian-river-brewing-company-pliny-the-elder/4499` | 4.49 |
| Moonlight Reality Czech | `moonlight-brewing-company-reality-czeck/46125` | 3.78 |
| Sierra Nevada Pale Ale | `sierra-nevada-brewing-co-pale-ale/6284` | 3.62 |

---

## Open risks

1. **Untappd anti-bot / login wall** — Playwright may still get interstitial HTML; may need cookies, slower pacing, or residential proxy (last resort).
2. **DOM churn** — keep multiple extractors; snapshot fixtures when parsers break.
3. **Legal/ToS** — personal/research use; rate-limit; don’t hammer.
4. **Wrong-beer matches** — never take “first link”; always score; fail open to `None` rather than bad URL.
5. **History growth** — every crawl appends; consider min re-crawl interval / retention later.

---

## Session checklist (copy for next agent)

```text
[x] P0–P3 + history + freshness + export + Toronado client
[ ] Commit/push both repos
[ ] E2E: serve crawler + Simulator rating lookup
[ ] Optional: config file, history chart UI
```
