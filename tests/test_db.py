from datetime import datetime, timedelta, timezone

from beers_crawler.db import BeerDatabase
from beers_crawler.models import BeerMetadata, BeerPageRef


def test_save_and_get_page_ref(tmp_path):
    db = BeerDatabase(tmp_path / "t.db")
    ref = BeerPageRef(
        query="Pliny the Elder",
        page_url="https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4691",
        slug="russian-river-brewing-company-pliny-the-elder",
        beer_id="4691",
        match_score=0.9,
    )
    db.save_page_ref(ref)
    got = db.get_page_ref("pliny the elder")
    assert got is not None
    assert got.page_url == ref.page_url
    assert got.match_score == 0.9
    assert got.from_history is True


def test_append_metadata_keeps_history(tmp_path):
    db = BeerDatabase(tmp_path / "t.db")
    url = "https://untappd.com/b/x/1"
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=7)

    id0 = db.append_metadata(
        BeerMetadata(
            page_url=url,
            name="Test Beer",
            brewery="Test Brewery",
            rating_score=4.2,
            rating_count=10,
            scraped_at=t0,
        )
    )
    id1 = db.append_metadata(
        BeerMetadata(
            page_url=url,
            name="Test Beer",
            brewery="Test Brewery",
            rating_score=4.35,
            rating_count=12,
            scraped_at=t1,
        )
    )
    assert id1 != id0

    latest = db.get_latest_metadata(url)
    assert latest is not None
    assert latest.rating_score == 4.35
    assert latest.from_history is True
    assert latest.history_id == id1

    hist = db.list_metadata_history(url)
    assert len(hist) == 2
    assert hist[0].rating_score == 4.35
    assert hist[1].rating_score == 4.2

    stats = db.stats()
    assert stats["history_rows"] == 2
    assert stats["distinct_beers"] == 1
    assert stats["with_rating_score"] == 1


def test_list_metadata_latest_per_url(tmp_path):
    db = BeerDatabase(tmp_path / "t.db")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.append_metadata(
        BeerMetadata(page_url="https://untappd.com/b/a/1", name="A", rating_score=3.0, scraped_at=t0)
    )
    db.append_metadata(
        BeerMetadata(
            page_url="https://untappd.com/b/a/1",
            name="A",
            rating_score=3.5,
            scraped_at=t0 + timedelta(days=1),
        )
    )
    db.append_metadata(
        BeerMetadata(
            page_url="https://untappd.com/b/b/2",
            name="B",
            rating_score=4.0,
            scraped_at=t0 + timedelta(days=2),
        )
    )
    rows = db.list_metadata(limit=10)
    assert len(rows) == 2
    by_name = {r.name: r.rating_score for r in rows}
    assert by_name["A"] == 3.5
    assert by_name["B"] == 4.0


def test_migrate_legacy_unique_page_url(tmp_path):
    """Existing DBs with UNIQUE(page_url) should migrate to append-only history."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE beer_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_url TEXT NOT NULL UNIQUE,
            name TEXT,
            brewery TEXT,
            style TEXT,
            abv REAL,
            ibu REAL,
            rating_score REAL,
            rating_count INTEGER,
            description TEXT,
            beer_id TEXT,
            scraped_at TEXT NOT NULL,
            raw_json TEXT
        );
        INSERT INTO beer_metadata (page_url, name, rating_score, scraped_at)
        VALUES ('https://untappd.com/b/x/1', 'Old', 4.0, '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = BeerDatabase(path)
    db.append_metadata(
        BeerMetadata(
            page_url="https://untappd.com/b/x/1",
            name="New",
            rating_score=4.1,
            scraped_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
    )
    hist = db.list_metadata_history("https://untappd.com/b/x/1")
    assert len(hist) == 2
    assert hist[0].rating_score == 4.1
