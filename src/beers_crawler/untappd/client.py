from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import quote_plus

import httpx

from beers_crawler.models import BeerMetadata, BeerPageRef
from beers_crawler.untappd.parsers import (
    normalize_beer_url,
    parse_beer_page,
    parse_search_results,
    split_query_hints,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def pick_best_candidate(
    candidates: list[BeerPageRef], query: str, min_score: float = 0.25
) -> Optional[BeerPageRef]:
    """Select best candidate from a scored list (same rules as ``best_search_result``)."""
    if not candidates:
        return None
    top = candidates[0]
    if top.match_score < min_score:
        return None

    brewery_tokens, beer_tokens = split_query_hints(query)
    distinctive_beer = {t for t in beer_tokens if len(t) >= 3}
    if brewery_tokens and distinctive_beer:
        hay = f"{top.slug or ''} {top.page_url}".lower()
        if not any(t in hay for t in distinctive_beer):
            for cand in candidates[1:]:
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
        min_match_score: float = 0.25,
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
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
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

    async def _get_html(self, url: str, *, wait_selector: Optional[str] = None) -> str:
        last_err: Optional[Exception] = None
        attempts = max(1, self.max_retries + 1)
        for attempt in range(attempts):
            try:
                if self.prefer_httpx or not self.allow_playwright:
                    html = await self._get_html_httpx(url)
                    if html:
                        return html
                    if not self.allow_playwright or self._playwright_failed:
                        raise RuntimeError(
                            f"httpx returned no beer HTML for {url} and Playwright is unavailable"
                        )
                    logger.info("httpx miss for %s; falling back to Playwright", url)
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

    async def resolve_candidates(self, beer_name: str) -> list[BeerPageRef]:
        """Return all scored search candidates (best first). Updates ``last_candidates``."""
        query = beer_name.strip()
        self._last_candidates = []
        if not query:
            return []
        search_url = f"https://untappd.com/search?q={quote_plus(query)}&type=beer"
        logger.info("searching Untappd for %r → %s", query, search_url)
        html = await self._get_html(search_url, wait_selector="a[href*='/b/']")
        results = parse_search_results(html, query)
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
