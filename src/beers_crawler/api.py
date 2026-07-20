"""Thin FastAPI surface over ``CrawlerService`` for Toronado / clients.

Run:
    uv run beers-crawler serve
    # or: uv run uvicorn beers_crawler.api:app --reload --port 8741
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from beers_crawler import __version__
from beers_crawler.db import BeerDatabase, default_db_path
from beers_crawler.models import BeerMetadata, BeerPageRef
from beers_crawler.service import DEFAULT_MIN_REFRESH_SECONDS, CrawlerService
from beers_crawler.untappd.client import UntappdClient

logger = logging.getLogger(__name__)


class CrawlRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Beer name, ideally 'Brewery Beer'")
    history_only: bool = Field(
        False, description="Skip live crawl; return latest history only"
    )
    force: bool = Field(
        False, description="Ignore freshness window; always attempt live crawl"
    )


class CrawlResponse(BaseModel):
    page: Optional[BeerPageRef] = None
    metadata: Optional[BeerMetadata] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    db: str
    min_refresh_seconds: float
    stats: dict[str, int]


class AppState:
    def __init__(self) -> None:
        self.db: Optional[BeerDatabase] = None
        self.client: Optional[UntappdClient] = None
        self.service: Optional[CrawlerService] = None
        self.min_refresh_seconds: float = DEFAULT_MIN_REFRESH_SECONDS


state = AppState()


def _db_path_from_env() -> Path:
    raw = os.environ.get("BEERS_CRAWLER_DB")
    return Path(raw) if raw else default_db_path()


def _min_refresh_from_env() -> float:
    raw = os.environ.get("BEERS_CRAWLER_MIN_REFRESH_SECONDS")
    if raw is None or raw == "":
        return DEFAULT_MIN_REFRESH_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_REFRESH_SECONDS


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    headless = os.environ.get("BEERS_CRAWLER_HEADED", "").lower() not in {
        "1",
        "true",
        "yes",
    }
    min_refresh = _min_refresh_from_env()
    db = BeerDatabase(_db_path_from_env())
    client = UntappdClient(headless=headless)
    await client.start()
    state.db = db
    state.client = client
    state.min_refresh_seconds = min_refresh
    state.service = CrawlerService(
        db, client, use_history=True, min_refresh_seconds=min_refresh
    )
    logger.info(
        "API ready db=%s headless=%s min_refresh=%ss",
        db.path,
        headless,
        min_refresh,
    )
    try:
        yield
    finally:
        await client.close()
        state.service = None
        state.client = None
        state.db = None


app = FastAPI(
    title="beers-crawler",
    version=__version__,
    description=(
        "Untappd beer resolve + metadata API. "
        "Fresh history within min_refresh window; else live crawl (append); "
        "fall back to history on failure."
    ),
    lifespan=lifespan,
)

# Vite dev server (web/) and common local origins
_cors = os.environ.get(
    "BEERS_CRAWLER_CORS",
    "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:4173,http://localhost:4173",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors.split(",") if o.strip()] or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _service() -> CrawlerService:
    if state.service is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return state.service


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    db = state.db
    stats = db.stats() if db else {}
    return HealthResponse(
        status="ok" if state.service else "starting",
        version=__version__,
        db=str(db.path) if db else "",
        min_refresh_seconds=state.min_refresh_seconds,
        stats=stats,
    )


@app.get("/v1/resolve", response_model=BeerPageRef)
async def resolve(
    q: str = Query(..., min_length=1, description="Beer name query"),
    history_only: bool = Query(False, description="Skip live; use history only"),
    force: bool = Query(False, description="Ignore freshness; live crawl"),
) -> BeerPageRef:
    """Interface 1: beer name → best Untappd page URL."""
    ref = await _service().beer_name_to_url(
        q, history_only=history_only, force=force
    )
    if ref is None:
        raise HTTPException(status_code=404, detail=f"no Untappd page for {q!r}")
    return ref


@app.get("/v1/resolve/candidates", response_model=list[BeerPageRef])
async def resolve_candidates(
    q: str = Query(..., min_length=1),
    history_only: bool = Query(False),
    force: bool = Query(False),
    limit: int = Query(10, ge=1, le=50),
) -> list[BeerPageRef]:
    """Ranked search candidates for debugging / re-rank."""
    return await _service().beer_name_to_candidates(
        q, history_only=history_only, force=force, limit=limit
    )


@app.get("/v1/metadata", response_model=BeerMetadata)
async def metadata(
    url: str = Query(..., min_length=8, description="Untappd /b/… page URL"),
    history_only: bool = Query(False, description="Skip live; use latest history"),
    force: bool = Query(False, description="Ignore freshness; live crawl"),
) -> BeerMetadata:
    """Interface 2: page URL → metadata."""
    meta = await _service().url_to_metadata(
        url, history_only=history_only, force=force
    )
    if meta is None:
        raise HTTPException(status_code=404, detail=f"failed to fetch metadata for {url}")
    return meta


@app.get("/v1/metadata/history", response_model=list[BeerMetadata])
async def metadata_history(
    url: str = Query(..., min_length=8, description="Untappd /b/… page URL"),
    limit: int = Query(20, ge=1, le=200),
) -> list[BeerMetadata]:
    """All timestamped crawl snapshots for a beer page (newest first)."""
    rows = _service().metadata_history(url, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no history for {url}")
    return rows


@app.get("/v1/export")
async def export_history(
    format: str = Query("json", pattern="^(json|csv)$"),
    url: Optional[str] = Query(None, description="Optional Untappd beer page filter"),
    limit: Optional[int] = Query(None, ge=1, le=10_000),
) -> Response:
    """Export crawl history as JSON or CSV."""
    db = state.db
    if db is None:
        raise HTTPException(status_code=503, detail="db not ready")
    if format == "csv":
        body = db.export_history_csv(page_url=url, limit=limit)
        return Response(
            content=body,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=beer_history.csv"},
        )
    body = db.export_history_json(page_url=url, limit=limit)
    return Response(content=body, media_type="application/json")


@app.post("/v1/crawl", response_model=CrawlResponse)
async def crawl(body: CrawlRequest) -> CrawlResponse:
    """Combined: name → URL → metadata (freshness / live / history fallback)."""
    ref, meta = await _service().crawl_beer(
        body.name, history_only=body.history_only, force=body.force
    )
    if ref is None:
        raise HTTPException(status_code=404, detail=f"no Untappd page for {body.name!r}")
    return CrawlResponse(page=ref, metadata=meta)


@app.get("/v1/list", response_model=list[BeerMetadata])
async def list_cached(limit: int = Query(50, ge=1, le=500)) -> list[BeerMetadata]:
    """Latest snapshot per beer page."""
    db = state.db
    if db is None:
        raise HTTPException(status_code=503, detail="db not ready")
    return db.list_metadata(limit=limit)
