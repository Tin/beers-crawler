from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from beers_crawler.db import BeerDatabase
from beers_crawler.models import BeerMetadata, BeerPageRef
from beers_crawler.untappd.client import UntappdClient

logger = logging.getLogger(__name__)


class CrawlerService:
    """Orchestrates interfaces + SQLite cache."""

    def __init__(
        self,
        db: BeerDatabase,
        client: UntappdClient,
        *,
        use_cache: bool = True,
    ) -> None:
        self.db = db
        self.client = client
        self.use_cache = use_cache

    async def beer_name_to_url(
        self, beer_name: str, *, force: bool = False
    ) -> Optional[BeerPageRef]:
        if self.use_cache and not force:
            cached = self.db.get_page_ref(beer_name)
            if cached is not None:
                logger.info("cache hit page_ref for %r", beer_name)
                return cached
        ref = await self.client.resolve_page(beer_name)
        if ref is not None:
            self.db.save_page_ref(ref)
        return ref

    async def url_to_metadata(
        self, page_url: str, *, force: bool = False
    ) -> Optional[BeerMetadata]:
        if self.use_cache and not force:
            cached = self.db.get_metadata(page_url)
            if cached is not None:
                logger.info("cache hit metadata for %s", page_url)
                return cached
        meta = await self.client.lookup_metadata(page_url)
        if meta is not None:
            self.db.save_metadata(meta)
        return meta

    async def crawl_beer(
        self, beer_name: str, *, force: bool = False
    ) -> tuple[Optional[BeerPageRef], Optional[BeerMetadata]]:
        ref = await self.beer_name_to_url(beer_name, force=force)
        if ref is None:
            return None, None
        meta = await self.url_to_metadata(ref.page_url, force=force)
        return ref, meta


def build_service(
    db_path: Path | str | None = None,
    *,
    headless: bool = True,
    use_cache: bool = True,
) -> tuple[CrawlerService, UntappdClient, BeerDatabase]:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_cache=use_cache)
    return service, client, db
