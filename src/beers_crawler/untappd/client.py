from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import quote_plus

from beers_crawler.models import BeerMetadata, BeerPageRef
from beers_crawler.untappd.parsers import best_search_result, normalize_beer_url, parse_beer_page

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class UntappdClient:
    """Playwright-backed Untappd client implementing both crawler interfaces.

    1. ``resolve_page(beer_name)`` → BeerPageRef (URL)
    2. ``lookup_metadata(page_url)`` → BeerMetadata (rating_score, …)
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        min_match_score: float = 0.25,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.min_match_score = min_match_score
        self._playwright = None
        self._browser = None
        self._context = None

    async def __aenter__(self) -> "UntappdClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        self._context.set_default_timeout(self.timeout_ms)

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

    async def _get_html(self, url: str, *, wait_selector: Optional[str] = None) -> str:
        if self._context is None:
            await self.start()
        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=8_000)
                except Exception:
                    logger.debug("wait_selector %s not found for %s", wait_selector, url)
            # Allow late JS content
            await page.wait_for_timeout(800)
            return await page.content()
        finally:
            await page.close()

    async def resolve_page(self, beer_name: str) -> Optional[BeerPageRef]:
        """Interface 1: beer name string → Untappd page URL (best match)."""
        query = beer_name.strip()
        if not query:
            return None
        search_url = (
            f"https://untappd.com/search?q={quote_plus(query)}&type=beer"
        )
        logger.info("searching Untappd for %r → %s", query, search_url)
        html = await self._get_html(
            search_url,
            wait_selector="a[href*='/b/']",
        )
        ref = best_search_result(html, query, min_score=self.min_match_score)
        if ref is None:
            logger.warning("no Untappd beer page matched for %r", query)
        else:
            logger.info(
                "resolved %r → %s (score=%.2f)",
                query,
                ref.page_url,
                ref.match_score,
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

    async def resolve_and_lookup(self, beer_name: str) -> tuple[Optional[BeerPageRef], Optional[BeerMetadata]]:
        """Convenience: name → URL → metadata in one call."""
        ref = await self.resolve_page(beer_name)
        if ref is None:
            return None, None
        meta = await self.lookup_metadata(ref.page_url)
        return ref, meta


def run_async(coro):
    return asyncio.run(coro)
