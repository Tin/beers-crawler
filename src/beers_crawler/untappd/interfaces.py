"""Two primary crawler interfaces.

1. BeerNameToPageResolver: beer name (string) → Untappd page URL
2. BeerMetadataLookup: Untappd page URL → beer metadata (rating score)
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from beers_crawler.models import BeerMetadata, BeerPageRef


@runtime_checkable
class BeerNameToPageResolver(Protocol):
    """Resolve a free-text beer name (optionally with brewery) to an Untappd beer page."""

    async def resolve_page(self, beer_name: str) -> Optional[BeerPageRef]:
        """Return best Untappd beer page for ``beer_name``, or None if not found."""
        ...


@runtime_checkable
class BeerMetadataLookup(Protocol):
    """Fetch structured metadata for an Untappd beer page URL."""

    async def lookup_metadata(self, page_url: str) -> Optional[BeerMetadata]:
        """Return beer metadata including ``rating_score``, or None on failure."""
        ...
