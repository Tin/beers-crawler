"""Failed lookup queue + self-learning hooks."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from beers_crawler.auth import UserStore, reset_auth_cache
from beers_crawler.db import BeerDatabase
from beers_crawler.untappd.parsers import (
    search_query_variants,
    split_query_hints,
    strip_style_suffixes,
)


def test_strip_style_suffix_wandering_don():
    assert (
        strip_style_suffixes("Firestone Walker Wandering Don IPA")
        == "Firestone Walker Wandering Don"
    )
    variants = search_query_variants("Firestone Walker Wandering Don IPA")
    assert "Firestone Walker Wandering Don IPA" in variants
    assert "Firestone Walker Wandering Don" in variants


def test_split_firestone_walker():
    brewery, beer = split_query_hints("Firestone Walker Wandering Don IPA")
    assert "firestone" in brewery and "walker" in brewery
    assert "wandering" in beer and "don" in beer
    assert "walker" not in beer


def test_match_rejects_brewery_only_fieldwork():
    from beers_crawler.untappd.parsers import match_score

    wrong = match_score(
        "Fieldwork Of Ivander",
        "fieldwork-brewing-company-portrait-of-bruin",
        "Portrait of Bruin Fieldwork Brewing Company",
    )
    right = match_score(
        "Fieldwork Of Ivander",
        "fieldwork-brewing-company-ol-ivander",
        "Ol' Ivander Fieldwork Brewing Company",
    )
    assert right > wrong
    assert right >= 0.8
    assert wrong <= 0.5


def test_variants_prefer_beer_name_first():
    from beers_crawler.untappd.parsers import beer_name_search_string

    # Untappd-app style: search beer name, not brewery+name blob
    assert beer_name_search_string("Firestone Walker Wandering Don IPA") == "wandering don"
    assert beer_name_search_string("Moonlight Bombay by Boat") == "bombay boat"
    vs = search_query_variants("Firestone Walker Wandering Don IPA")
    assert vs[0].lower() == "wandering don"
    # full brewery+name should not be the first Algolia query
    assert "firestone" not in vs[0].lower()


def test_variants_drop_of_for_ivander():
    vs = search_query_variants("Fieldwork Of Ivander")
    # beer-name first after split: ivander (fieldwork is brewery)
    assert vs[0].lower() == "ivander"
    assert any("fieldwork" in v.lower() for v in vs)  # full query still a fallback


def test_record_and_list_failures(tmp_path):
    db = BeerDatabase(tmp_path / "f.db")
    a = db.record_failed_lookup("Moonlight X", error="no_match")
    assert a.fail_count == 1
    assert a.status == "open"
    b = db.record_failed_lookup("moonlight  x", error="no_match")
    assert b.id == a.id
    assert b.fail_count == 2

    rows = db.list_failed_lookups(status="open")
    assert len(rows) == 1
    assert rows[0].fail_count == 2

    done = db.mark_failed_lookup_resolved(
        a.id or 0,
        page_url="https://untappd.com/b/example/1",
        resolved_by="test",
    )
    assert done is not None
    assert done.status == "resolved"
    assert db.list_failed_lookups(status="open") == []
    assert db.failed_lookup_stats().get("resolved") == 1


def test_failures_api(monkeypatch, tmp_path):
    db_path = tmp_path / "f.db"
    monkeypatch.delenv("BEERS_CRAWLER_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("BEERS_CRAWLER_API_PASSWORD", raising=False)
    monkeypatch.setenv("BEERS_CRAWLER_DB", str(db_path))
    monkeypatch.setenv("BEERS_CRAWLER_ALLOW_PLAYWRIGHT", "0")
    monkeypatch.setenv("BEERS_CRAWLER_PREFER_HTTPX", "1")
    reset_auth_cache()
    UserStore(db_path).add_user("alice", "alice-password-1")

    from beers_crawler.api import app

    token = base64.b64encode(b"alice:alice-password-1").decode()
    headers = {"Authorization": f"Basic {token}"}

    with TestClient(app) as client:
        assert client.get("/v1/failures", headers=headers).status_code == 200
        assert client.get("/v1/failures").status_code == 401

        db = BeerDatabase(db_path)
        row = db.record_failed_lookup("Test Beer Fail", error="no_match")
        listed = client.get("/v1/failures?status=open", headers=headers).json()
        assert any(x["query"] == "Test Beer Fail" for x in listed)

        r = client.post(
            f"/v1/failures/{row.id}/resolve",
            headers=headers,
            json={
                "page_url": "https://untappd.com/b/test-beer/99",
                "resolved_by": "unit-test",
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "resolved"
        assert r.json()["resolved_page_url"].endswith("/99")

        stats = client.get("/v1/failures/stats", headers=headers).json()
        assert stats.get("resolved", 0) >= 1
