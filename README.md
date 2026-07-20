# beers-crawler

Server-side Untappd crawler (CLI first, SQLite storage). Built to back apps like Toronado Viscosity with reliable beer page URLs and rating scores.

## Interfaces

| Interface | Input | Output |
|-----------|--------|--------|
| **1. Resolve** | beer name `str` | Untappd page URL (`BeerPageRef`) |
| **2. Metadata** | Untappd page URL | beer metadata, especially **`rating_score`** (`BeerMetadata`) |

```text
"Russian River Pliny the Elder"
        │
        ▼  resolve_page()
https://untappd.com/b/.../4499
        │
        ▼  lookup_metadata()
{ name, brewery, rating_score, rating_count, abv, ... }
```

Implementation: Playwright (Chromium) + BeautifulSoup parsers + SQLite cache.

## Setup

```bash
cd /path/to/beers-crawler
uv sync
uv run playwright install chromium
```

## CLI

```bash
# Create DB (also auto-created on first write)
uv run beers-crawler init-db

# 1) beer name → Untappd URL
uv run beers-crawler resolve "Russian River Pliny the Elder"
uv run beers-crawler resolve "Moonlight Reality Czech" --json

# 2) Untappd URL → metadata (rating)
uv run beers-crawler metadata "https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4499"

# Combined (name → URL → metadata), cached in SQLite
uv run beers-crawler crawl "Russian River Pliny the Elder" -v

# Batch: one beer name per line
uv run beers-crawler batch beers.txt --delay 2

# Inspect cache
uv run beers-crawler list
```

Flags:

- `--db PATH` — SQLite file (default `./data/beers.db`)
- `--force` — ignore cache and re-fetch
- `--no-cache` — do not read/write SQLite
- `--headed` — show the browser
- `--json` — machine-readable output
- `-v` — debug logs

## Project layout

```text
src/beers_crawler/
  cli.py                 # click CLI
  db.py                  # SQLite
  models.py              # BeerPageRef, BeerMetadata
  service.py             # cache + orchestration
  untappd/
    interfaces.py        # Protocol definitions for the two APIs
    client.py            # Playwright UntappdClient
    parsers.py           # HTML → models (no network)
tests/
```

## SQLite schema (summary)

- **`beer_pages`** — query → page_url (+ match_score)
- **`beer_metadata`** — page_url → name, brewery, **rating_score**, counts, etc.

## Tests (offline parsers + DB)

```bash
uv run pytest -q
```

## Status

Live Untappd crawl verified (Pliny, Reality Czech, Sierra Nevada Pale Ale) with SQLite cache hits on re-run. See [`PLAN.md`](./PLAN.md).

## Notes / next steps

- Untappd may rate-limit or change DOM; parsers use JSON-LD + CSS + regex fallbacks.
- Be polite: batch mode defaults to a delay between beers.
- Next: harden match ranking; optional HTTP API for Toronado Viscosity; web UI later.

## License

See [LICENSE](LICENSE).
