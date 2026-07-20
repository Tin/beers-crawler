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


def test_save_and_get_metadata(tmp_path):
    db = BeerDatabase(tmp_path / "t.db")
    meta = BeerMetadata(
        page_url="https://untappd.com/b/x/1",
        name="Test Beer",
        brewery="Test Brewery",
        rating_score=4.2,
        rating_count=10,
    )
    db.save_metadata(meta)
    got = db.get_metadata(meta.page_url)
    assert got is not None
    assert got.rating_score == 4.2
    assert got.name == "Test Beer"
    stats = db.stats()
    assert stats["metadata_rows"] == 1
    assert stats["with_rating_score"] == 1
