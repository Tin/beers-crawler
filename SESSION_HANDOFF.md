# Session handoff — 2026-03-22

**Next agent: start here, then read [`PLAN.md`](./PLAN.md).**

---

## What happened this session

**beers-crawler P0 is done.** Live Untappd crawl works; scaffold committed/pushed.

| Check | Result |
|-------|--------|
| `uv run pytest -q` | 8 passed |
| Pliny the Elder | `.../pliny-the-elder/4499` · **4.49** · match 1.00 |
| Moonlight Reality Czech | `.../reality-czeck/46125` · **3.78** · match 0.82 |
| Sierra Nevada Pale Ale | `.../pale-ale/6284` · **3.62** · match 1.00 |
| Cache re-run | hit page_ref + metadata (no network) |
| `batch beers.example.txt` | **3/3** with scores |

No parser/selector fixes were required — existing Playwright + scored search + JSON-LD path worked against live Untappd.

---

## Where to continue (priority order)

### 1. beers-crawler P1 — harden resolution

See `PLAN.md` P1:

- Prefer results inside main beer search list (not nav/footer/featured)
- Stronger brewery **or** beer-name token requirements when both in query
- Store **all** search candidates for debug / re-rank
- Optional httpx static fallback

### 2. beers-crawler P2 — ops polish

- Richer `stats`, CSV/JSON export
- Retry/backoff on 429/timeout
- Config file / env for DB path, delay, headless
- GitHub Actions: `pytest` only (no live Untappd)

### 3. beers-crawler P3 — FastAPI for iOS

```text
GET  /v1/resolve?q=
GET  /v1/metadata?url=
POST /v1/crawl  { "name": "…" }
```

Same `CrawlerService`; then Toronado replaces in-app scrape with HTTP client.

### 4. Toronado Viscosity (still paused)

**Do not prioritize iOS rating scrape** until crawler API exists, unless user asks.

**Repo:** `git@github.com:example/toronado-viscosity.git` @ `3ae26a7`  
In-app lookup still weak; long-term client of this service.

---

## Bootstrap

```bash
cd /path/to/beers-crawler
uv sync
uv run playwright install chromium   # if needed
uv run pytest -q
uv run beers-crawler crawl "Russian River Pliny the Elder" -v
uv run beers-crawler list
```

**Contracts (stable):**

1. `beer name: str` → Untappd page URL (`BeerNameToPageResolver` / `resolve`)
2. `untappd page URL` → metadata, especially **`rating_score`** (`BeerMetadataLookup` / `metadata`)

---

## Architecture (unchanged)

```text
CLI (click)
  resolve | metadata | crawl | batch | list | init-db
       │
CrawlerService  ← SQLite cache (data/beers.db)
       │
UntappdClient (Playwright Chromium)
       │
parsers.py  — search HTML → BeerPageRef; beer HTML → BeerMetadata
```

| Concern | File |
|---------|------|
| Interfaces | `src/beers_crawler/untappd/interfaces.py` |
| Models | `src/beers_crawler/models.py` |
| Playwright | `src/beers_crawler/untappd/client.py` |
| Match + score parse | `src/beers_crawler/untappd/parsers.py` |
| SQLite | `src/beers_crawler/db.py` |
| CLI | `src/beers_crawler/cli.py` |
| Full plan | `PLAN.md` |

---

## Explicit non-goals right now

- Web UI for crawler (P4)
- Shipping Playwright inside iOS
- Committing `data/*.db` / `.venv/`

---

## Session checklist for next agent

```text
[x] Read PLAN.md + this file
[x] Live crawl proven; v0.2 success criteria ticked
[x] git commit + push scaffold
[ ] P1 harden parsers/ranking
[ ] P3 FastAPI stub when ready for Toronado
```
