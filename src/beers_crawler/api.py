"""Thin FastAPI surface over ``CrawlerService`` for Toronado / clients.

Run:
    uv run beers-crawler serve
    # or: uv run uvicorn beers_crawler.api:app --reload --port 8741

Auth (HTTP Basic):
    BEERS_CRAWLER_API_USER / BEERS_CRAWLER_API_PASSWORD required unless
    BEERS_CRAWLER_AUTH_DISABLED=1 (local dev only).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from beers_crawler import __version__
from beers_crawler.auth import get_auth_config, init_auth, require_auth, reset_auth_cache
from beers_crawler.db import BeerDatabase, default_db_path
from beers_crawler.models import BeerMetadata, BeerPageRef
from beers_crawler.service import DEFAULT_MIN_REFRESH_SECONDS, CrawlerService
from beers_crawler.untappd.client import UntappdClient

logger = logging.getLogger(__name__)

AuthUser = Annotated[str, Depends(require_auth)]


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
    auth_required: bool
    db: str = ""
    min_refresh_seconds: float = 0
    stats: dict[str, int] = Field(default_factory=dict)


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
    # Fail fast if no users configured (unless auth explicitly disabled)
    reset_auth_cache()
    init_auth(db_path=_db_path_from_env())

    headless = os.environ.get("BEERS_CRAWLER_HEADED", "").lower() not in {
        "1",
        "true",
        "yes",
    }
    prefer_httpx = os.environ.get("BEERS_CRAWLER_PREFER_HTTPX", "").lower() in {
        "1",
        "true",
        "yes",
    }
    allow_pw_raw = os.environ.get("BEERS_CRAWLER_ALLOW_PLAYWRIGHT", "1").lower()
    allow_playwright = allow_pw_raw not in {"0", "false", "no"}
    min_refresh = _min_refresh_from_env()
    db = BeerDatabase(_db_path_from_env())
    client = UntappdClient(
        headless=headless,
        prefer_httpx=prefer_httpx,
        allow_playwright=allow_playwright,
    )
    await client.start()
    state.db = db
    state.client = client
    state.min_refresh_seconds = min_refresh
    state.service = CrawlerService(
        db, client, use_history=True, min_refresh_seconds=min_refresh
    )
    cfg = get_auth_config()
    logger.info(
        "API ready db=%s auth=%s headless=%s prefer_httpx=%s allow_playwright=%s min_refresh=%ss",
        db.path,
        "on" if cfg.enabled else "OFF",
        headless,
        prefer_httpx,
        allow_playwright,
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
        "Protected with HTTP Basic auth unless BEERS_CRAWLER_AUTH_DISABLED=1."
    ),
    lifespan=lifespan,
)

_cors = os.environ.get(
    "BEERS_CRAWLER_CORS",
    "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:4173,http://localhost:4173",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors.split(",") if o.strip()] or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "Authorization"],
    expose_headers=["WWW-Authenticate"],
)


def _service() -> CrawlerService:
    if state.service is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return state.service


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Public liveness probe — no DB stats, no auth required."""
    cfg = get_auth_config()
    return HealthResponse(
        status="ok" if state.service else "starting",
        version=__version__,
        auth_required=cfg.enabled,
    )


@app.get("/health/detail", response_model=HealthResponse)
async def health_detail(_: AuthUser) -> HealthResponse:
    """Authenticated health with DB path and cache stats."""
    db = state.db
    stats = db.stats() if db else {}
    cfg = get_auth_config()
    return HealthResponse(
        status="ok" if state.service else "starting",
        version=__version__,
        auth_required=cfg.enabled,
        db=str(db.path) if db else "",
        min_refresh_seconds=state.min_refresh_seconds,
        stats=stats,
    )


@app.get("/v1/resolve", response_model=BeerPageRef)
async def resolve(
    _: AuthUser,
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
    _: AuthUser,
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
    _: AuthUser,
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
    _: AuthUser,
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
    _: AuthUser,
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
async def crawl(_: AuthUser, body: CrawlRequest) -> CrawlResponse:
    """Combined: name → URL → metadata (freshness / live / history fallback)."""
    ref, meta = await _service().crawl_beer(
        body.name, history_only=body.history_only, force=body.force
    )
    if ref is None:
        raise HTTPException(status_code=404, detail=f"no Untappd page for {body.name!r}")
    return CrawlResponse(page=ref, metadata=meta)


@app.get("/v1/list", response_model=list[BeerMetadata])
async def list_cached(
    _: AuthUser, limit: int = Query(50, ge=1, le=500)
) -> list[BeerMetadata]:
    """Latest snapshot per beer page."""
    db = state.db
    if db is None:
        raise HTTPException(status_code=503, detail="db not ready")
    return db.list_metadata(limit=limit)
