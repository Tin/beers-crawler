from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BeerPageRef(BaseModel):
    """Result of beer-name → Untappd page URL resolution."""

    query: str
    page_url: str
    slug: Optional[str] = None
    beer_id: Optional[str] = None
    match_score: float = 0.0
    source: str = "untappd_search"


class BeerMetadata(BaseModel):
    """Result of Untappd page URL → beer metadata (rating is primary)."""

    page_url: str
    name: Optional[str] = None
    brewery: Optional[str] = None
    style: Optional[str] = None
    abv: Optional[float] = None
    ibu: Optional[float] = None
    rating_score: Optional[float] = Field(
        default=None, description="Untappd community rating 0–5"
    )
    rating_count: Optional[int] = None
    description: Optional[str] = None
    beer_id: Optional[str] = None
    scraped_at: datetime = Field(default_factory=utc_now)
