# beers-crawler — plan & handoff

Last updated: 2026-07-19  
Repo: `/path/to/beers-crawler` (`git@github.com:example/beers-crawler.git`)  
Related app: `/path/to/toronado-viscosity` (iOS; in-app Untappd scrape is weak — this service is the intended backend)

---

## Goal

Build a **server-side** Untappd crawler that can reliably:

1. **Beer name → Untappd page URL**
2. **Untappd page URL → beer metadata** (especially **rating score**)

Storage: **SQLite**. Interface priority: **CLI first**, web UI later. Optional: HTTP API for Toronado Viscosity.

---

## Two interfaces (stable contract)

Defined in `src/beers_crawler/untappd/interfaces.py` and models in `models.py`.

### 1. `BeerNameToPageResolver`

| | |
|--|--|
| **Input** | `beer_name: str` (free text, ideally `"Brewery Beer Name"`) |
| **Output** | `BeerPageRef \| None` |
| **Fields** | `query`, `page_url`, `slug`, `beer_id`, `match_score`, `source` |
| **CLI** | `uv run beers-crawler resolve "Russian River Pliny the Elder"` |
| **Code** | `UntappdClient.resolve_page` → `CrawlerService.beer_name_to_url` |

### 2. `BeerMetadataLookup`

| | |
|--|--|
| **Input** | `page_url: str` (Untappd `/b/{slug}/{id}`) |
| **Output** | `BeerMetadata \| None` |
| **Critical field** | `rating_score` (0–5 float) |
| **Also** | `name`, `brewery`, `style`, `abv`, `ibu`, `rating_count`, `description`, `beer_id`, `scraped_at` |
| **CLI** | `uv run beers-crawler metadata "https://untappd.com/b/..."` |
| **Code** | `UntappdClient.lookup_metadata` → `CrawlerService.url_to_metadata` |

### Combined

```text
crawl / crawl_beer: name → resolve → metadata → SQLite
```

CLI: `uv run beers-crawler crawl "…"`

---

## Why this exists (context from Toronado)

In-app `WebBeerRatingLookup` (URLSession + HTML) often:

- Picks the **wrong** beer URL (first `/b/` link = featured Guinness, etc.)
- Gets empty/blocked Untappd HTML without a real browser
- Times out or falls back to DuckDuckGo search links with **no score**

This crawler uses **Playwright (Chromium)** + **scored search matching** + **SQLite cache** so resolution can be fixed server-side and reused.

---

## What’s already scaffolded (v0.1)

```text
beers-crawler/
  PLAN.md                 ← this file
  README.md               ← user-facing usage
  pyproject.toml          ← uv / hatchling, script beers-crawler
  beers.example.txt
  src/beers_crawler/
    cli.py                ← click: resolve | metadata | crawl | batch | list | init-db
    db.py                 ← SQLite schema + cache
    models.py             ← Pydantic BeerPageRef, BeerMetadata
    service.py            ← cache orchestration
    untappd/
      interfaces.py       ← Protocols for the two APIs
      client.py           ← Playwright UntappdClient
      parsers.py          ← search HTML → candidates; beer HTML → metadata
  tests/
    test_parsers.py       ← offline (Guinness vs Pliny matching, JSON-LD rating)
    test_db.py
```

### Design choices already made

| Choice | Decision |
|--------|----------|
| Runtime | Python 3.12+, **uv** |
| Browser | **Playwright** Chromium (JS-rendered Untappd) |
| HTML parse | BeautifulSoup + lxml; JSON-LD + CSS + regex fallbacks |
| Match quality | Token overlap on slug/link text; **penalize Guinness/Corona** when not in query; min_score default `0.25` |
| DB | SQLite at `./data/beers.db` (WAL); tables `beer_pages`, `beer_metadata` |
| Cache | Default on; `--force` refresh; `--no-cache` skip |
| Batch politeness | `--delay` seconds between beers (default 1.5) |

### Offline tests cover

- Prefer Pliny over Guinness in mixed search HTML
- Parse `ratingValue` / brewery / ABV / IBU from fixture beer page
- SQLite save/load for both tables

---

## Bootstrap (new session)

```bash
cd /path/to/beers-crawler
uv sync
uv run playwright install chromium
uv run pytest -q
uv run beers-crawler init-db
uv run beers-crawler crawl "Russian River Pliny the Elder" -v
uv run beers-crawler list
```

If live crawl fails, debug with `--headed -v` and inspect search/detail HTML.

---

## Near-term work (do next)

### P0 — Prove live crawl works

1. ~~Run real `resolve` + `metadata` + `crawl` against Untappd.~~ **Done 2026-07-19**
2. If search HTML has no `/b/` links: Untappd may require login, bot wall, or different markup — capture HTML fixture under `tests/fixtures/` and adapt selectors in `parsers.py`. *(not needed yet)*
3. ~~Confirm `rating_score` is non-null for at least: Pliny the Elder, Reality Czech, Sierra Nevada Pale Ale.~~ **Done** (4.49 / 3.78 / 3.62)
4. Commit scaffold; keep `data/*.db` gitignored. (`uv.lock` currently gitignored)

### P1 — Harden resolution

1. Prefer results inside the main beer search list container (not nav/footer/featured).
2. Require brewery **or** beer-name token in slug when both present in query (mirror toronado `UntappdBeerURLResolver`).
3. Optional: httpx fallback for static HTML when Playwright is overkill.
4. Store **all** search candidates (not only best) for debugging / re-rank.

### P2 — CLI / ops polish

1. `beers-crawler stats` richer output; export CSV/JSON.
2. Structured logging to file; retry/backoff on 429/timeout.
3. Config file (YAML/env): DB path, delay, headless, user-agent.
4. GitHub Actions: `pytest` only (no live Untappd in CI).

### P3 — Service API (for iOS later)

1. Thin FastAPI (or similar):  
   - `GET /v1/resolve?q=`  
   - `GET /v1/metadata?url=`  
   - `POST /v1/crawl` `{ "name": "…" }`
2. Same `CrawlerService` underneath; no scrape logic in the app.
3. Toronado Viscosity: replace or wrap `WebBeerRatingLookup` with HTTP client to this service.

### P4 — Web UI (explicitly later)

1. Simple internal UI: search box → table of cached beers + scores.
2. Trigger crawl jobs; show progress.
3. Do **not** block CLI/API on UI.

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
  UNIQUE(query, page_url)
)

beer_metadata(
  page_url UNIQUE,
  name, brewery, style, abv, ibu,
  rating_score, rating_count, description, beer_id,
  scraped_at, raw_json
)
```

---

## Code map for implementers

| Task | Start here |
|------|------------|
| Change match ranking | `untappd/parsers.py` → `match_score`, `best_search_result` |
| Change rating extraction | `untappd/parsers.py` → `parse_beer_page` |
| Browser fetch behavior | `untappd/client.py` → `_get_html`, timeouts, wait selectors |
| Cache policy | `service.py` + `db.py` |
| New CLI command | `cli.py` |
| Contract / types | `models.py`, `untappd/interfaces.py` |

---

## Success criteria (v0.2)

- [x] `uv run pytest -q` green (8 passed)
- [x] Live: `crawl "Russian River Pliny the Elder"` → `.../pliny-the-elder/4499`, rating **4.49**
- [x] Live: Reality Czech + Sierra Nevada Pale Ale distinct URLs + scores; Pliny re-run = cache hit (no network)
- [x] `batch beers.example.txt` → **3/3** with scores
- [x] README matches actual CLI
- [x] This PLAN updated when architecture changes

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

---

## Session checklist (copy for next agent)

```text
[x] cd /path/to/beers-crawler && uv sync && uv run playwright install chromium
[x] uv run pytest -q
[x] Live crawl known beers with -v (Pliny, Reality Czech, SNPA)
[x] Commit working slice; update PLAN.md
[ ] P1: harden resolution (search-list container, brewery token rules, store all candidates)
[ ] P2: stats/export, retry/backoff, config file
[ ] P3: FastAPI + Toronado HTTP client
```

---

## Git status note

Scaffold committed after live crawl verification (v0.2 success criteria). Next work is P1 harden resolution / P3 FastAPI for iOS.
