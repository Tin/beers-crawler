from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from beers_crawler.db import BeerDatabase
from beers_crawler.models import BeerMetadata, BeerPageRef, utc_now
from beers_crawler.untappd.client import UntappdClient

logger = logging.getLogger(__name__)

# Default 6h — scores don't move that fast; avoids hammering Untappd / bloating history.
DEFAULT_MIN_REFRESH_SECONDS = float(
    os.environ.get("BEERS_CRAWLER_MIN_REFRESH_SECONDS", "21600")
)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_fresh(scraped_at: datetime | None, min_refresh_seconds: float) -> bool:
    """True when a snapshot is new enough to skip a live re-crawl."""
    if scraped_at is None or min_refresh_seconds <= 0:
        return False
    age = utc_now() - _as_utc(scraped_at)
    return age <= timedelta(seconds=min_refresh_seconds)


class CrawlerService:
    """Orchestrates live crawl + append-only SQLite history.

    Default policy for scores/metadata:
      1. If a fresh history snapshot exists (within ``min_refresh_seconds``), return it
      2. Else try a live crawl
      3. On success → append a timestamped history row and return it
      4. On failure → return the latest history snapshot when one exists
    """

    def __init__(
        self,
        db: BeerDatabase,
        client: UntappdClient,
        *,
        use_history: bool = True,
        min_refresh_seconds: float = DEFAULT_MIN_REFRESH_SECONDS,
    ) -> None:
        self.db = db
        self.client = client
        self.use_history = use_history
        self.min_refresh_seconds = min_refresh_seconds

    def _fresh_page_ref(self, beer_name: str) -> Optional[BeerPageRef]:
        if not self.use_history or self.min_refresh_seconds <= 0:
            return None
        ref = self.db.get_page_ref(beer_name)
        if ref is None or ref.resolved_at is None:
            return None
        if is_fresh(ref.resolved_at, self.min_refresh_seconds):
            return ref.model_copy(update={"from_history": True})
        return None

    def _fresh_metadata(self, page_url: str) -> Optional[BeerMetadata]:
        if not self.use_history or self.min_refresh_seconds <= 0:
            return None
        cached = self.db.get_latest_metadata(page_url)
        if cached is None:
            return None
        if is_fresh(cached.scraped_at, self.min_refresh_seconds):
            logger.info(
                "fresh history metadata for %s @ %s score=%s (min_refresh=%ss)",
                page_url,
                cached.scraped_at,
                cached.rating_score,
                self.min_refresh_seconds,
            )
            return cached
        return None

    async def beer_name_to_url(
        self,
        beer_name: str,
        *,
        history_only: bool = False,
        use_history: bool | None = None,
        force: bool = False,
    ) -> Optional[BeerPageRef]:
        """Live resolve first (unless fresh history); fall back to best historical candidate."""
        hist = self.use_history if use_history is None else use_history

        if history_only:
            cached = self.db.get_page_ref(beer_name)
            if cached is not None:
                logger.info("history-only page_ref for %r", beer_name)
            return cached

        if hist and not force:
            fresh = self._fresh_page_ref(beer_name)
            if fresh is not None:
                logger.info(
                    "fresh page_ref for %r → %s (skip live)",
                    beer_name,
                    fresh.page_url,
                )
                return fresh

        try:
            ref = await self.client.resolve_page(beer_name)
        except Exception:
            logger.exception("live resolve failed for %r", beer_name)
            ref = None

        candidates = self.client.last_candidates
        # Only persist candidates that clear the match floor (avoid sidebar junk)
        min_score = getattr(self.client, "min_match_score", 0.25)
        good = [c for c in candidates if c.match_score >= min_score]
        if hist and good:
            self.db.save_page_refs(good)
        elif hist and ref is not None and ref.match_score >= min_score:
            self.db.save_page_ref(ref)

        if ref is not None:
            return ref.model_copy(update={"from_history": False})

        if hist:
            cached = self.db.get_page_ref(beer_name)
            if cached is not None and cached.match_score >= min_score:
                logger.info(
                    "history fallback page_ref for %r → %s",
                    beer_name,
                    cached.page_url,
                )
                return cached
            if cached is not None:
                logger.info(
                    "ignoring weak history page_ref for %r score=%.2f",
                    beer_name,
                    cached.match_score,
                )
        return None

    async def beer_name_to_candidates(
        self,
        beer_name: str,
        *,
        history_only: bool = False,
        use_history: bool | None = None,
        force: bool = False,
        limit: int = 20,
    ) -> list[BeerPageRef]:
        """Ranked candidates from live search, else cached list."""
        hist = self.use_history if use_history is None else use_history

        if history_only:
            return self.db.list_page_refs(beer_name, limit=limit)

        if hist and not force:
            cached = self.db.list_page_refs(beer_name, limit=limit)
            if cached and cached[0].resolved_at and is_fresh(
                cached[0].resolved_at, self.min_refresh_seconds
            ):
                logger.info(
                    "fresh %d candidates for %r (skip live)", len(cached), beer_name
                )
                return cached

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
        force: bool = False,
    ) -> Optional[BeerMetadata]:
        """Live metadata crawl first (unless fresh); append history; fall back to last snapshot."""
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

        if hist and not force:
            fresh = self._fresh_metadata(page_url)
            if fresh is not None:
                return fresh

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
        force: bool = False,
    ) -> tuple[Optional[BeerPageRef], Optional[BeerMetadata]]:
        """Name → URL → metadata with freshness / live-first / history-fallback policy."""
        hist = self.use_history if use_history is None else use_history

        # Fast path: both resolve + metadata still fresh for this query
        if hist and not force and not history_only and self.min_refresh_seconds > 0:
            ref = self.db.get_page_ref(beer_name)
            if ref is not None:
                meta = self._fresh_metadata(ref.page_url)
                if meta is not None:
                    logger.info(
                        "fresh crawl hit for %r → %s score=%s",
                        beer_name,
                        ref.page_url,
                        meta.rating_score,
                    )
                    return ref.model_copy(update={"from_history": True}), meta

        ref = await self.beer_name_to_url(
            beer_name,
            history_only=history_only,
            use_history=use_history,
            force=force,
        )
        if ref is None:
            return None, None
        meta = await self.url_to_metadata(
            ref.page_url,
            history_only=history_only,
            use_history=use_history,
            force=force,
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
    min_refresh_seconds: float = DEFAULT_MIN_REFRESH_SECONDS,
) -> tuple[CrawlerService, UntappdClient, BeerDatabase]:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(
        db,
        client,
        use_history=use_history,
        min_refresh_seconds=min_refresh_seconds,
    )
    return service, client, db
