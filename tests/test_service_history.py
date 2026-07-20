"""Service live-first / freshness / history-fallback policy (mocked client)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from beers_crawler.db import BeerDatabase
from beers_crawler.models import BeerMetadata, BeerPageRef, utc_now
from beers_crawler.service import CrawlerService, is_fresh


class FakeClient:
    def __init__(
        self,
        *,
        ref: Optional[BeerPageRef] = None,
        meta: Optional[BeerMetadata] = None,
        fail_resolve: bool = False,
        fail_meta: bool = False,
        candidates: Optional[list[BeerPageRef]] = None,
    ) -> None:
        self._ref = ref
        self._meta = meta
        self._fail_resolve = fail_resolve
        self._fail_meta = fail_meta
        self._candidates = candidates or ([] if ref is None else [ref])
        self.last_candidates: list[BeerPageRef] = []
        self.resolve_calls = 0
        self.meta_calls = 0

    async def resolve_page(self, beer_name: str) -> Optional[BeerPageRef]:
        self.resolve_calls += 1
        if self._fail_resolve:
            raise RuntimeError("network down")
        self.last_candidates = list(self._candidates)
        return self._ref

    async def resolve_candidates(self, beer_name: str) -> list[BeerPageRef]:
        self.resolve_calls += 1
        if self._fail_resolve:
            raise RuntimeError("network down")
        self.last_candidates = list(self._candidates)
        return list(self._candidates)

    async def lookup_metadata(self, page_url: str) -> Optional[BeerMetadata]:
        self.meta_calls += 1
        if self._fail_meta:
            raise RuntimeError("network down")
        return self._meta


URL = "https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4499"


def test_is_fresh():
    now = utc_now()
    assert is_fresh(now - timedelta(hours=1), 21600)
    assert not is_fresh(now - timedelta(hours=10), 21600)
    assert not is_fresh(None, 21600)
    assert not is_fresh(now, 0)


@pytest.mark.asyncio
async def test_live_success_appends_history(tmp_path):
    db = BeerDatabase(tmp_path / "h.db")
    ref = BeerPageRef(
        query="Pliny", page_url=URL, match_score=1.0, slug="pliny", beer_id="4499"
    )
    meta = BeerMetadata(
        page_url=URL,
        name="Pliny the Elder",
        rating_score=4.5,
        scraped_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    service = CrawlerService(
        db, FakeClient(ref=ref, meta=meta), min_refresh_seconds=0  # type: ignore[arg-type]
    )
    out_ref, out_meta = await service.crawl_beer("Pliny")
    assert out_ref is not None and out_ref.from_history is False
    assert out_meta is not None and out_meta.from_history is False
    assert out_meta.rating_score == 4.5
    assert out_meta.history_id is not None
    assert db.stats()["history_rows"] == 1


@pytest.mark.asyncio
async def test_live_fail_falls_back_to_history(tmp_path):
    db = BeerDatabase(tmp_path / "h.db")
    db.append_metadata(
        BeerMetadata(
            page_url=URL,
            name="Pliny the Elder",
            rating_score=4.4,
            scraped_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
    )
    db.save_page_ref(
        BeerPageRef(
            query="Pliny", page_url=URL, match_score=0.9, slug="pliny", beer_id="4499"
        )
    )
    service = CrawlerService(
        db,
        FakeClient(fail_resolve=True, fail_meta=True),  # type: ignore[arg-type]
        min_refresh_seconds=0,
    )
    out_ref, out_meta = await service.crawl_beer("Pliny")
    assert out_ref is not None and out_ref.from_history is True
    assert out_meta is not None and out_meta.from_history is True
    assert out_meta.rating_score == 4.4


@pytest.mark.asyncio
async def test_second_live_crawl_adds_second_history_row(tmp_path):
    db = BeerDatabase(tmp_path / "h.db")
    ref = BeerPageRef(query="Pliny", page_url=URL, match_score=1.0)
    m1 = BeerMetadata(
        page_url=URL,
        name="Pliny",
        rating_score=4.40,
        scraped_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    m2 = BeerMetadata(
        page_url=URL,
        name="Pliny",
        rating_score=4.48,
        scraped_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )

    s1 = CrawlerService(
        db, FakeClient(ref=ref, meta=m1), min_refresh_seconds=0  # type: ignore[arg-type]
    )
    await s1.crawl_beer("Pliny")
    s2 = CrawlerService(
        db, FakeClient(ref=ref, meta=m2), min_refresh_seconds=0  # type: ignore[arg-type]
    )
    await s2.crawl_beer("Pliny")

    hist = db.list_metadata_history(URL)
    assert len(hist) == 2
    assert hist[0].rating_score == 4.48
    assert hist[1].rating_score == 4.40


@pytest.mark.asyncio
async def test_history_only_skips_live(tmp_path):
    db = BeerDatabase(tmp_path / "h.db")
    db.append_metadata(
        BeerMetadata(
            page_url=URL,
            name="Pliny",
            rating_score=4.1,
            scraped_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
    )
    db.save_page_ref(BeerPageRef(query="Pliny", page_url=URL, match_score=1.0))
    client = FakeClient(
        ref=BeerPageRef(query="Pliny", page_url=URL, match_score=1.0),
        meta=BeerMetadata(page_url=URL, name="Pliny", rating_score=9.9),
    )
    service = CrawlerService(db, client, min_refresh_seconds=0)  # type: ignore[arg-type]
    _, meta = await service.crawl_beer("Pliny", history_only=True)
    assert meta is not None
    assert meta.rating_score == 4.1
    assert meta.from_history is True
    assert client.resolve_calls == 0
    assert client.meta_calls == 0


@pytest.mark.asyncio
async def test_fresh_snapshot_skips_live(tmp_path):
    db = BeerDatabase(tmp_path / "h.db")
    now = utc_now()
    db.append_metadata(
        BeerMetadata(
            page_url=URL, name="Pliny", rating_score=4.2, scraped_at=now
        )
    )
    db.save_page_ref(BeerPageRef(query="Pliny", page_url=URL, match_score=1.0))
    client = FakeClient(
        ref=BeerPageRef(query="Pliny", page_url=URL, match_score=1.0),
        meta=BeerMetadata(page_url=URL, name="Pliny", rating_score=9.9),
    )
    service = CrawlerService(
        db, client, min_refresh_seconds=3600  # type: ignore[arg-type]
    )
    ref, meta = await service.crawl_beer("Pliny")
    assert ref is not None and ref.from_history is True
    assert meta is not None and meta.rating_score == 4.2
    assert client.resolve_calls == 0
    assert client.meta_calls == 0
    assert db.stats()["history_rows"] == 1  # no append


@pytest.mark.asyncio
async def test_force_bypasses_freshness(tmp_path):
    db = BeerDatabase(tmp_path / "h.db")
    now = utc_now()
    db.append_metadata(
        BeerMetadata(page_url=URL, name="Pliny", rating_score=4.2, scraped_at=now)
    )
    db.save_page_ref(BeerPageRef(query="Pliny", page_url=URL, match_score=1.0))
    client = FakeClient(
        ref=BeerPageRef(query="Pliny", page_url=URL, match_score=1.0),
        meta=BeerMetadata(
            page_url=URL, name="Pliny", rating_score=4.9, scraped_at=now
        ),
    )
    service = CrawlerService(
        db, client, min_refresh_seconds=3600  # type: ignore[arg-type]
    )
    _, meta = await service.crawl_beer("Pliny", force=True)
    assert meta is not None
    assert meta.rating_score == 4.9
    assert meta.from_history is False
    assert client.meta_calls == 1
    assert db.stats()["history_rows"] == 2


def test_export_csv_and_json(tmp_path):
    db = BeerDatabase(tmp_path / "h.db")
    db.append_metadata(
        BeerMetadata(
            page_url=URL,
            name="Pliny",
            brewery="RR",
            rating_score=4.5,
            scraped_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
    )
    csv_text = db.export_history_csv()
    assert "rating_score" in csv_text
    assert "4.5" in csv_text
    json_text = db.export_history_json()
    assert "Pliny" in json_text


@pytest.mark.asyncio
async def test_prefer_cache_uses_ios_ttl(tmp_path):
    """iOS prefer_cache returns DB score within 3 days even if default min_refresh expired."""
    from datetime import timedelta

    from beers_crawler.models import utc_now

    db = BeerDatabase(tmp_path / "c.db")
    url = "https://untappd.com/b/x/1"
    # 2 days old — older than default 6h min_refresh, younger than 3d ios cache
    scraped = utc_now() - timedelta(days=2)
    db.append_metadata(
        BeerMetadata(page_url=url, name="X", rating_score=3.5, scraped_at=scraped)
    )
    db.save_page_ref(BeerPageRef(query="X Beer", page_url=url, match_score=1.0))

    # prefer_cache first — must hit 3.5 without network
    client_ios = FakeClient(
        ref=BeerPageRef(query="X Beer", page_url=url, match_score=1.0),
        meta=BeerMetadata(page_url=url, name="X", rating_score=9.9),
    )
    service_ios = CrawlerService(
        db,
        client_ios,  # type: ignore[arg-type]
        min_refresh_seconds=6 * 3600,
        ios_cache_seconds=3 * 24 * 3600,
    )
    _, meta_ios = await service_ios.crawl_beer("X Beer", prefer_cache=True)
    assert meta_ios is not None
    assert meta_ios.rating_score == 3.5
    assert meta_ios.from_history is True
    assert client_ios.resolve_calls == 0
    assert client_ios.meta_calls == 0

    # without prefer_cache → live-fetch (score 9.9) because 6h window expired
    client_live = FakeClient(
        ref=BeerPageRef(query="X Beer", page_url=url, match_score=1.0),
        meta=BeerMetadata(page_url=url, name="X", rating_score=9.9),
    )
    service_live = CrawlerService(
        db,
        client_live,  # type: ignore[arg-type]
        min_refresh_seconds=6 * 3600,
        ios_cache_seconds=3 * 24 * 3600,
    )
    _, meta_live = await service_live.crawl_beer("X Beer", prefer_cache=False)
    assert meta_live is not None
    assert meta_live.rating_score == 9.9
    assert client_live.meta_calls == 1
