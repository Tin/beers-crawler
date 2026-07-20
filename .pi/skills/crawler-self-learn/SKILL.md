---
name: crawler-self-learn
description: Pull failed beer lookups from beers-crawler, research correct Untappd URLs, fix resolve bugs, and mark failures resolved.
---

# Crawler self-learning

Use this skill when beer name → Untappd URL resolve fails in production or tests, or when asked to improve crawler match rate from the failure queue.

## Goal

1. Fetch open failures from the API  
2. Research the correct Untappd `/b/{slug}/{id}` page (external search OK)  
3. Reproduce locally; fix `parsers.py` / `client.py` if systemic  
4. Mark the failure resolved via API and seed `beer_pages`  
5. Deploy only after tests pass; never commit secrets or host identity

## Auth / API base

Do **not** hardcode hosts, usernames, or passwords in repo files.

Prefer environment (already set in the operator shell or gitignored local config):

- `BEERS_CRAWLER_API_BASE` — e.g. `https://example.com/beers/rating/api` (no trailing slash)
- `BEERS_CRAWLER_API_USER` / `BEERS_CRAWLER_API_PASSWORD` — HTTP Basic

Helper:

```bash
# from repo root
export BEERS_CRAWLER_API_BASE="${BEERS_CRAWLER_API_BASE:?set API base}"
AUTH=(-u "${BEERS_CRAWLER_API_USER:?}:${BEERS_CRAWLER_API_PASSWORD:?}")

curl -fsS "${AUTH[@]}" "$BEERS_CRAWLER_API_BASE/v1/failures?status=open&limit=50"
curl -fsS "${AUTH[@]}" "$BEERS_CRAWLER_API_BASE/v1/failures/stats"
```

CLI (local DB):

```bash
uv run beers-crawler failures --status open
uv run beers-crawler failures --status all --json
```

## Research a miss

For each open failure `query`:

1. Search the web for: `site:untappd.com/b {query}` (Brave / DDG / Google).  
2. Confirm the beer page matches brewery + beer name (not a random hit).  
3. Note canonical URL: `https://untappd.com/b/{slug}/{id}`.  
4. Locally reproduce:

```bash
uv run python - <<'PY'
import asyncio, os
os.environ.setdefault("BEERS_CRAWLER_AUTH_DISABLED", "1")
from beers_crawler.untappd.client import UntappdClient
async def main():
    q = "PASTE QUERY"
    c = UntappdClient(prefer_httpx=True, allow_playwright=False)
    await c.start()
    try:
        ref = await c.resolve_page(q)
        print(ref)
        for x in c.last_candidates[:10]:
            print(round(x.match_score,3), x.source, x.page_url)
    finally:
        await c.close()
asyncio.run(main())
PY
```

## Fix patterns (common)

| Symptom | Likely fix |
|---------|------------|
| Algolia 0 hits with trailing `IPA`/`Lager`/… | `strip_style_suffixes` / `search_query_variants` in `parsers.py` |
| Two-word brewery split wrong (`Firestone Walker`) | `KNOWN_BREWERY_PREFIXES` / `split_query_hints` |
| Sidebar mega-brands win | list/Algolia preference in `pick_best_candidate` |
| External search 429 on VPS | rely on Algolia path; don't require Brave/DDG |
| Known URL works, discover fails | add variant query or brewery alias; optional manual resolve API |

Primary code:

- `src/beers_crawler/untappd/client.py` — Algolia + resolve pipeline  
- `src/beers_crawler/untappd/parsers.py` — scoring, style strip, brewery hints  
- `src/beers_crawler/service.py` — records failures  
- `src/beers_crawler/db.py` — `failed_lookups` table  

## Mark resolved (teaches history)

```bash
curl -fsS "${AUTH[@]}" -X POST \
  -H 'content-type: application/json' \
  -d '{"page_url":"https://untappd.com/b/SLUG/ID","resolved_by":"self-learn","notes":"why it failed"}' \
  "$BEERS_CRAWLER_API_BASE/v1/failures/ID/resolve"
```

Or set status only:

```bash
curl -fsS "${AUTH[@]}" -X PATCH \
  -H 'content-type: application/json' \
  -d '{"status":"researching","notes":"..."}' \
  "$BEERS_CRAWLER_API_BASE/v1/failures/ID"
```

## Before finishing

```bash
uv run pytest -q
# identity leak check on tracked files
git grep -iE 'diamondtin|zztin|iamtin|public_html|/home/tin' $(git ls-files) && echo LEAK || echo clean
```

Deploy with `./scripts/deploy.sh` only if `deploy/deploy.env` exists locally (gitignored). Never commit credentials, hostnames, or absolute server paths.

## Do not

- Commit real passwords, API users, or production hostnames  
- Force-push history unless explicitly asked  
- Mark resolved without a verified Untappd `/b/…/id` URL  
