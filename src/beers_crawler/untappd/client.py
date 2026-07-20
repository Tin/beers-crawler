from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx

from beers_crawler.models import BeerMetadata, BeerPageRef
from beers_crawler.llm import guess_search_keywords, llm_enabled
from beers_crawler.untappd.parsers import (
    match_score,
    normalize_beer_url,
    parse_beer_page,
    parse_search_results,
    search_query_variants,
    split_query_hints,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def pick_best_candidate(
    candidates: list[BeerPageRef], query: str, min_score: float = 0.4
) -> Optional[BeerPageRef]:
    """Select best candidate from a scored list (same rules as ``best_search_result``)."""
    if not candidates:
        return None

    list_hits = [c for c in candidates if c.source == "untappd_search_list"]
    pool = list_hits if list_hits else candidates
    top = pool[0]
    if top.match_score < min_score:
        return None

    # Prefer Algolia / primary list over sidebar junk when mixed
    algolia_hits = [c for c in candidates if c.source == "untappd_algolia"]
    if algolia_hits and not list_hits:
        pool = algolia_hits
        top = pool[0]
        if top.match_score < min_score:
            return None

    if not list_hits and not algolia_hits and top.source in {
        "untappd_search",
        "untappd_search_sidebar",
        "untappd_search_raw",
    }:
        slug_l = (top.slug or "").lower()
        mega = (
            "guinness",
            "corona",
            "heineken",
            "stella",
            "modelo",
            "budweiser",
            "michelob",
            "coors",
            "miller",
        )
        if any(b in slug_l for b in mega) and top.match_score < 0.6:
            return None

    brewery_tokens, beer_tokens = split_query_hints(query)
    distinctive_beer = {t for t in beer_tokens if len(t) >= 3}
    if brewery_tokens and distinctive_beer:
        hay = f"{top.slug or ''} {top.page_url}".lower()
        if not any(t in hay for t in distinctive_beer):
            for cand in pool[1:]:
                if cand.match_score < min_score:
                    break
                hay_c = f"{cand.slug or ''} {cand.page_url}".lower()
                if any(t in hay_c for t in distinctive_beer):
                    return cand
            return None
    return top


class UntappdClient:
    """Playwright-backed Untappd client implementing both crawler interfaces.

    1. ``resolve_page(beer_name)`` → BeerPageRef (URL)
    2. ``lookup_metadata(page_url)`` → BeerMetadata (rating_score, …)

    When ``prefer_httpx`` is True, try a static HTTP fetch first and fall back
    to Playwright if the HTML lacks beer signals.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        min_match_score: float = 0.4,
        prefer_httpx: bool = False,
        max_retries: int = 2,
        allow_playwright: bool = True,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.min_match_score = min_match_score
        self.prefer_httpx = prefer_httpx
        # When False, never launch Chromium (low-memory hosts / httpx-only).
        self.allow_playwright = allow_playwright
        self.max_retries = max_retries
        self._playwright = None
        self._browser = None
        self._context = None
        self._last_candidates: list[BeerPageRef] = []
        self._playwright_failed = False
        self._algolia_cfg: Optional[dict[str, str]] = None
        self._algolia_cfg_at: float = 0.0

    @property
    def last_candidates(self) -> list[BeerPageRef]:
        """Candidates from the most recent resolve call."""
        return list(self._last_candidates)

    async def __aenter__(self) -> "UntappdClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Eager Playwright start (optional). Safe no-op when Playwright is disabled."""
        if not self.allow_playwright:
            logger.info("Playwright disabled (httpx-only mode)")
            return
        if self.prefer_httpx:
            # Lazy-start browser only if httpx misses — saves RAM on small VPS.
            logger.info("Playwright deferred until httpx miss")
            return
        await self._ensure_playwright()

    async def _ensure_playwright(self) -> None:
        if self._browser is not None:
            return
        if not self.allow_playwright or self._playwright_failed:
            raise RuntimeError("Playwright unavailable")
        from playwright.async_api import async_playwright

        try:
            import os
            from pathlib import Path

            # On small VPS we may only have full chromium, not headless_shell.
            os.environ.setdefault("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_SHELL", "0")

            self._playwright = await async_playwright().start()
            launch_kwargs: dict = {"headless": self.headless}
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
            if not exe:
                # Common cache layout after `playwright install chromium`
                cache = Path(
                    os.environ.get(
                        "PLAYWRIGHT_BROWSERS_PATH",
                        Path.home() / ".cache" / "ms-playwright",
                    )
                )
                for candidate in sorted(cache.glob("chromium-*/chrome-linux*/chrome")):
                    if candidate.is_file():
                        exe = str(candidate)
                        break
            if exe:
                launch_kwargs["executable_path"] = exe
                logger.info("Playwright using executable_path=%s", exe)

            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
            self._context.set_default_timeout(self.timeout_ms)
        except Exception:
            self._playwright_failed = True
            await self.close()
            logger.exception("Playwright start failed")
            raise

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _get_html_httpx(self, url: str) -> Optional[str]:
        timeout = self.timeout_ms / 1000.0
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                follow_redirects=True,
                timeout=timeout,
            ) as client:
                resp = await client.get(url)
                if resp.status_code == 429:
                    logger.warning("httpx 429 for %s", url)
                    return None
                resp.raise_for_status()
                text = resp.text
                if "/b/" in text or "application/ld+json" in text:
                    return text
                logger.debug("httpx HTML lacked beer signals for %s", url)
                return None
        except Exception as exc:
            logger.debug("httpx fetch failed for %s: %s", url, exc)
            return None

    async def _get_html_playwright(
        self, url: str, *, wait_selector: Optional[str] = None
    ) -> str:
        await self._ensure_playwright()
        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=8_000)
                except Exception:
                    logger.debug("wait_selector %s not found for %s", wait_selector, url)
            await page.wait_for_timeout(800)
            return await page.content()
        finally:
            await page.close()

    @staticmethod
    def _static_html_is_usable(url: str, html: str) -> bool:
        """Return True if static HTML is good enough (skip Playwright).

        Untappd search without JS usually only has sidebar featured brands
        (no ``div.beer-item``). Beer detail pages often have JSON-LD.
        """
        if not html:
            return False
        low = html.lower()
        is_search = "/search" in url.lower()
        if is_search:
            # Real beer search results render as beer-item cards.
            # Empty shells often still include result-list / results-container markup.
            return "beer-item" in low
        # Detail page: JSON-LD rating or h1 name is enough
        if "application/ld+json" in low and "ratingvalue" in low:
            return True
        if "/b/" in url and ("rating_score" in low or 'class="num"' in low or "<h1" in low):
            return True
        return "/b/" in low

    async def _get_html(self, url: str, *, wait_selector: Optional[str] = None) -> str:
        last_err: Optional[Exception] = None
        attempts = max(1, self.max_retries + 1)
        for attempt in range(attempts):
            try:
                if self.prefer_httpx or not self.allow_playwright:
                    html = await self._get_html_httpx(url)
                    if html and self._static_html_is_usable(url, html):
                        return html
                    if html:
                        logger.info(
                            "httpx HTML incomplete for %s (len=%d); Playwright=%s",
                            url,
                            len(html),
                            self.allow_playwright and not self._playwright_failed,
                        )
                    if not self.allow_playwright or self._playwright_failed:
                        if html:
                            return html
                        raise RuntimeError(
                            f"httpx returned no beer HTML for {url} and Playwright is unavailable"
                        )
                    logger.info("falling back to Playwright for %s", url)
                return await self._get_html_playwright(url, wait_selector=wait_selector)
            except Exception as exc:
                last_err = exc
                backoff = 0.75 * (2**attempt)
                logger.warning(
                    "fetch attempt %s/%s failed for %s: %s; sleep %.1fs",
                    attempt + 1,
                    attempts,
                    url,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
        assert last_err is not None
        raise last_err

    async def _fetch_untappd_search_config(self) -> Optional[dict[str, str]]:
        """Public Algolia search keys embedded in Untappd search page HTML."""
        now = time.time()
        if self._algolia_cfg and now - self._algolia_cfg_at < 3600:
            return self._algolia_cfg
        html = await self._get_html_httpx(
            "https://untappd.com/search?q=beer&type=beer"
        )
        if not html:
            return self._algolia_cfg
        m = re.search(
            r"window\.UNTAPPD_SEARCH_CONFIG\s*=\s*(\{.*?\})\s*;",
            html,
            re.S,
        )
        if not m:
            logger.warning("UNTAPPD_SEARCH_CONFIG not found in search HTML")
            return self._algolia_cfg
        try:
            cfg = json.loads(m.group(1))
        except json.JSONDecodeError:
            logger.warning("failed to parse UNTAPPD_SEARCH_CONFIG")
            return self._algolia_cfg
        app_id = cfg.get("appId") or cfg.get("autocompleteAppId")
        api_key = cfg.get("searchKey") or cfg.get("autocompleteSearchKey")
        indexes = cfg.get("indexes") or {}
        beer_idx = "beer"
        if isinstance(indexes.get("beer"), dict):
            beer_idx = indexes["beer"].get("all") or beer_idx
        elif isinstance(indexes.get("beer"), str):
            beer_idx = indexes["beer"]
        if not app_id or not api_key:
            return self._algolia_cfg
        self._algolia_cfg = {
            "app_id": str(app_id),
            "api_key": str(api_key),
            "index": str(beer_idx),
        }
        self._algolia_cfg_at = now
        logger.info(
            "Untappd Algolia config loaded app_id=%s index=%s",
            app_id,
            beer_idx,
        )
        return self._algolia_cfg

    async def _algolia_query_once(
        self,
        *,
        cfg: dict[str, str],
        query: str,
        original_query: str,
    ) -> list[BeerPageRef]:
        app_id = cfg["app_id"]
        api_key = cfg["api_key"]
        index = cfg["index"]
        url = f"https://{app_id}-dsn.algolia.net/1/indexes/{index}/query"
        headers = {
            "X-Algolia-Application-Id": app_id,
            "X-Algolia-API-Key": api_key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        payload: dict[str, Any] = {
            "query": query,
            "hitsPerPage": 20,
            # Typo tolerance helps OCR noise; still rank with our scorer.
            # Do NOT use removeWordsIfNoResults — it returns unrelated same-brewery
            # beers when a distinctive token is missing (e.g. Ivander → random Fieldwork).
            "typoTolerance": True,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_ms / 1000.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 429:
                    logger.warning("Algolia 429 for query %r", query)
                    return []
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Algolia search failed for %r: %s", query, exc)
            return []

        results: list[BeerPageRef] = []
        seen: set[str] = set()
        for hit in data.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            bid = hit.get("bid") or hit.get("objectID")
            slug = hit.get("beer_slug")
            if not bid:
                continue
            if not slug:
                idx = str(hit.get("beer_index") or hit.get("beer_name") or "")
                slug = re.sub(r"[^a-z0-9]+", "-", idx.lower()).strip("-") or "beer"
            page_url = f"https://untappd.com/b/{slug}/{bid}"
            if page_url in seen:
                continue
            seen.add(page_url)
            # Lead with beer_name so exact-title ranking can see it clearly.
            beer_name = str(hit.get("beer_name") or "")
            brewery_name = str(hit.get("brewery_name") or "")
            link_text = " ".join(
                x
                for x in (
                    beer_name,
                    brewery_name,
                    str(hit.get("beer_index") or ""),
                    str(hit.get("type_name") or ""),
                )
                if x
            )
            # Score against the original user query (with styles), not only variant.
            score = match_score(
                original_query, str(slug), link_text, in_primary_list=True
            )
            # Slight preference for in-production base beers over aged variants when tied
            if hit.get("in_production") in (0, "0", False):
                score = max(0.0, score - 0.02)
            # Prefer shorter beer titles when scores are close (base vs Vanilla/Double)
            name_len = len(beer_name)
            results.append(
                (
                    score,
                    name_len,
                    BeerPageRef(
                        query=original_query,
                        page_url=page_url,
                        slug=str(slug),
                        beer_id=str(bid),
                        match_score=score,
                        source="untappd_algolia",
                    ),
                )
            )
        results.sort(key=lambda t: (-t[0], t[1]))
        return [t[2] for t in results]

    async def _resolve_via_algolia(self, query: str) -> list[BeerPageRef]:
        """Query Untappd's public Algolia beer index (same backend as site search UI).

        Strategy (mirrors Untappd app behavior):
          1. Search primarily by **beer name** (not "Brewery + Beer" as one string).
          2. Rank hits with our scorer so the correct **brewery** wins among namesakes.
          3. If still weak and LLM is configured, ask the model for better keywords
             and search those (DeepSeek OpenAI-compatible API).
        """
        cfg = await self._fetch_untappd_search_config()
        if not cfg:
            return []

        async def _run_variants(variants: list[str]) -> list[BeerPageRef]:
            by_url: dict[str, BeerPageRef] = {}
            for variant in variants:
                hits = await self._algolia_query_once(
                    cfg=cfg, query=variant, original_query=query
                )
                logger.info(
                    "Algolia beer-search %r → %d hits (top=%s)",
                    variant,
                    len(hits),
                    f"{hits[0].match_score:.2f} {hits[0].page_url}"
                    if hits
                    else "n/a",
                )
                for h in hits:
                    prev = by_url.get(h.page_url)
                    if prev is None or h.match_score > prev.match_score:
                        by_url[h.page_url] = h
                if hits and hits[0].match_score >= 0.9:
                    break
            # Higher score first; on ties prefer shorter slug (base beer over variants)
            return sorted(
                by_url.values(),
                key=lambda r: (-r.match_score, len(r.slug or r.page_url)),
            )

        # 1) Cheap heuristics only (no LLM tokens)
        best = await _run_variants(search_query_variants(query))

        # 2) LLM only if heuristics failed or scored below accept threshold.
        #    Good heuristic hits never spend tokens.
        heuristic_ok = bool(best) and best[0].match_score >= self.min_match_score
        if llm_enabled() and not heuristic_ok:
            logger.info(
                "LLM keyword fallback for %r (heuristic top=%s)",
                query,
                f"{best[0].match_score:.2f}" if best else "none",
            )
            guess = await guess_search_keywords(query)
            if guess and guess.search_queries:
                llm_hits = await _run_variants(guess.search_queries)
                if llm_hits and (
                    not best or llm_hits[0].match_score > best[0].match_score
                ):
                    best = [
                        h.model_copy(update={"source": "untappd_algolia_llm"})
                        for h in llm_hits
                    ]

        logger.info(
            "Algolia merged %d candidates for %r (top=%s)",
            len(best),
            query,
            f"{best[0].match_score:.2f}" if best else "n/a",
        )
        return best

    async def _resolve_via_external_search(self, query: str) -> list[BeerPageRef]:
        """Third-party HTML search when Algolia is unavailable."""
        q = quote_plus(f"site:untappd.com/b {query}")
        engines = (
            ("brave", f"https://search.brave.com/search?q={q}"),
            ("duckduckgo", f"https://html.duckduckgo.com/html/?q={q}"),
            ("duckduckgo_lite", f"https://lite.duckduckgo.com/lite/?q={q}"),
        )
        for name, url in engines:
            logger.info("%s fallback search for %r", name, query)
            html = await self._get_html_httpx(url)
            if not html or "/b/" not in html:
                logger.info("%s returned no Untappd beer links", name)
                continue
            results = parse_search_results(html, query)
            out = [
                r.model_copy(update={"source": f"search_{name}"}) for r in results
            ]
            out.sort(key=lambda x: x.match_score, reverse=True)
            if out:
                logger.info(
                    "%s found %d candidates for %r (top=%.2f)",
                    name,
                    len(out),
                    query,
                    out[0].match_score,
                )
                return out
        return []

    async def resolve_candidates(self, beer_name: str) -> list[BeerPageRef]:
        """Return all scored search candidates (best first). Updates ``last_candidates``."""
        query = beer_name.strip()
        self._last_candidates = []
        if not query:
            return []

        # 1) Untappd Algolia (real search backend; works on small VPS)
        results = await self._resolve_via_algolia(query)
        best = pick_best_candidate(results, query, self.min_match_score)

        # 2) Static Untappd HTML / Playwright if Algolia missed
        if best is None:
            search_url = f"https://untappd.com/search?q={quote_plus(query)}&type=beer"
            logger.info("searching Untappd HTML for %r → %s", query, search_url)
            html = await self._get_html(search_url, wait_selector="a[href*='/b/']")
            html_results = parse_search_results(html, query)
            if html_results:
                results = html_results
                best = pick_best_candidate(results, query, self.min_match_score)

        # 3) Third-party search engines last
        if best is None:
            external = await self._resolve_via_external_search(query)
            if external:
                results = external

        self._last_candidates = results
        logger.info("found %d candidates for %r", len(results), query)
        return results

    async def resolve_page(self, beer_name: str) -> Optional[BeerPageRef]:
        """Interface 1: beer name string → Untappd page URL (best match)."""
        query = beer_name.strip()
        if not query:
            return None
        candidates = await self.resolve_candidates(query)
        ref = pick_best_candidate(candidates, query, self.min_match_score)
        if ref is None:
            logger.warning("no Untappd beer page matched for %r", query)
        else:
            logger.info(
                "resolved %r → %s (score=%.2f, candidates=%d)",
                query,
                ref.page_url,
                ref.match_score,
                len(candidates),
            )
        return ref

    async def lookup_metadata(self, page_url: str) -> Optional[BeerMetadata]:
        """Interface 2: Untappd page URL → beer metadata (rating_score primary)."""
        url = normalize_beer_url(page_url) or page_url.strip()
        if "/b/" not in url:
            logger.warning("not an Untappd beer URL: %s", page_url)
            return None
        logger.info("fetching beer page %s", url)
        html = await self._get_html(url, wait_selector="h1")
        meta = parse_beer_page(html, url)
        if meta.rating_score is None and meta.name is None:
            logger.warning("failed to parse useful metadata from %s", url)
            return None
        logger.info(
            "parsed %s | score=%s name=%r brewery=%r",
            url,
            meta.rating_score,
            meta.name,
            meta.brewery,
        )
        return meta

    async def resolve_and_lookup(
        self, beer_name: str
    ) -> tuple[Optional[BeerPageRef], Optional[BeerMetadata]]:
        """Convenience: name → URL → metadata in one call."""
        ref = await self.resolve_page(beer_name)
        if ref is None:
            return None, None
        meta = await self.lookup_metadata(ref.page_url)
        return ref, meta


def run_async(coro):
    return asyncio.run(coro)
