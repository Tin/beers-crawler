from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from beers_crawler.db import BeerDatabase
from beers_crawler.models import BeerMetadata, BeerPageRef
from beers_crawler.untappd.client import UntappdClient

logger = logging.getLogger(__name__)


class CrawlerService:
    """Orchestrates live crawl + append-only SQLite history.

    Default policy for scores/metadata:
      1. Try a live crawl
      2. On success → append a timestamped history row and return it
      3. On failure → return the latest history snapshot when one exists
    """

    def __init__(
        self,
        db: BeerDatabase,
        client: UntappdClient,
        *,
        use_history: bool = True,
    ) -> None:
        self.db = db
        self.client = client
        self.use_history = use_history

    async def beer_name_to_url(
        self,
        beer_name: str,
        *,
        history_only: bool = False,
        use_history: bool | None = None,
    ) -> Optional[BeerPageRef]:
        """Live resolve first; fall back to best historical candidate."""
        hist = self.use_history if use_history is None else use_history

        if history_only:
            cached = self.db.get_page_ref(beer_name)
            if cached is not None:
                logger.info("history-only page_ref for %r", beer_name)
            return cached

        try:
            ref = await self.client.resolve_page(beer_name)
        except Exception:
            logger.exception("live resolve failed for %r", beer_name)
            ref = None

        candidates = self.client.last_candidates
        if hist and candidates:
            self.db.save_page_refs(candidates)
        elif hist and ref is not None:
            self.db.save_page_ref(ref)

        if ref is not None:
            return ref.model_copy(update={"from_history": False})

        if hist:
            cached = self.db.get_page_ref(beer_name)
            if cached is not None:
                logger.info(
                    "history fallback page_ref for %r → %s",
                    beer_name,
                    cached.page_url,
                )
                return cached
        return None

    async def beer_name_to_candidates(
        self,
        beer_name: str,
        *,
        history_only: bool = False,
        use_history: bool | None = None,
        limit: int = 20,
    ) -> list[BeerPageRef]:
        """Ranked candidates from live search, else cached list."""
        hist = self.use_history if use_history is None else use_history

        if history_only:
            return self.db.list_page_refs(beer_name, limit=limit)

        try:
            candidates = await self.client.resolve_candidates(beer_name)
        except Exception:
            logger.exception("live candidates failed for %r", beer_name)
            candidates = []

        if hist and candidates:
            self.db.save_page_refs(candidates)

        if candidates:
            return [
                c.model_copy(update={"from_history": False}) for c in candidates[:limit]
            ]

        if hist:
            cached = self.db.list_page_refs(beer_name, limit=limit)
            if cached:
                logger.info(
                    "history fallback %d candidates for %r", len(cached), beer_name
                )
            return cached
        return []

    async def url_to_metadata(
        self,
        page_url: str,
        *,
        history_only: bool = False,
        use_history: bool | None = None,
    ) -> Optional[BeerMetadata]:
        """Live metadata crawl first; append history; fall back to last snapshot."""
        hist = self.use_history if use_history is None else use_history

        if history_only:
            cached = self.db.get_latest_metadata(page_url)
            if cached is not None:
                logger.info(
                    "history-only metadata for %s @ %s score=%s",
                    page_url,
                    cached.scraped_at,
                    cached.rating_score,
                )
            return cached

        meta: Optional[BeerMetadata] = None
        try:
            meta = await self.client.lookup_metadata(page_url)
        except Exception:
            logger.exception("live metadata failed for %s", page_url)
            meta = None

        if meta is not None:
            if hist:
                row_id = self.db.append_metadata(meta)
                meta = meta.model_copy(
                    update={"from_history": False, "history_id": row_id}
                )
                logger.info(
                    "appended history id=%s for %s score=%s",
                    row_id,
                    page_url,
                    meta.rating_score,
                )
            else:
                meta = meta.model_copy(update={"from_history": False})
            return meta

        if hist:
            cached = self.db.get_latest_metadata(page_url)
            if cached is not None:
                logger.info(
                    "history fallback metadata for %s @ %s score=%s",
                    page_url,
                    cached.scraped_at,
                    cached.rating_score,
                )
                return cached
        return None

    async def crawl_beer(
        self,
        beer_name: str,
        *,
        history_only: bool = False,
        use_history: bool | None = None,
    ) -> tuple[Optional[BeerPageRef], Optional[BeerMetadata]]:
        """Name → URL → metadata with live-first / history-fallback policy."""
        ref = await self.beer_name_to_url(
            beer_name, history_only=history_only, use_history=use_history
        )
        if ref is None:
            return None, None
        meta = await self.url_to_metadata(
            ref.page_url, history_only=history_only, use_history=use_history
        )
        return ref, meta

    def metadata_history(
        self, page_url: str, *, limit: int = 50
    ) -> list[BeerMetadata]:
        return self.db.list_metadata_history(page_url, limit=limit)


def build_service(
    db_path: Path | str | None = None,
    *,
    headless: bool = True,
    use_history: bool = True,
) -> tuple[CrawlerService, UntappdClient, BeerDatabase]:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_history=use_history)
    return service, client, db
