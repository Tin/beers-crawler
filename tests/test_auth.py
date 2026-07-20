"""HTTP Basic auth + user store."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from beers_crawler.auth import (
    MIN_PASSWORD_LEN,
    UserStore,
    hash_password,
    reset_auth_cache,
    verify_password_hash,
)


@pytest.fixture(autouse=True)
def _clear_auth():
    reset_auth_cache()
    yield
    reset_auth_cache()


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_hash_roundtrip():
    h = hash_password("a-long-enough-secret")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password_hash("a-long-enough-secret", h)
    assert not verify_password_hash("wrong-password-xx", h)


def test_hash_rejects_short_password():
    with pytest.raises(ValueError, match="at least"):
        hash_password("short")


def test_user_store_crud(tmp_path):
    store = UserStore(tmp_path / "u.db")
    assert store.count() == 0
    store.add_user("alice", "alice-password-1")
    assert store.count() == 1
    assert store.list_usernames() == ["alice"]
    assert store.verify("alice", "alice-password-1")
    assert store.verify("Alice", "alice-password-1")  # case-insensitive name
    assert not store.verify("alice", "nope-nope-nope")

    store.set_password("alice", "alice-password-2")
    assert store.verify("alice", "alice-password-2")
    assert not store.verify("alice", "alice-password-1")

    with pytest.raises(ValueError, match="already exists"):
        store.add_user("alice", "another-password1")

    store.delete_user("alice")
    assert store.count() == 0
    with pytest.raises(ValueError, match="not found"):
        store.delete_user("alice")


def test_api_requires_user(monkeypatch, tmp_path):
    monkeypatch.delenv("BEERS_CRAWLER_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("BEERS_CRAWLER_API_PASSWORD", raising=False)
    monkeypatch.delenv("BEERS_CRAWLER_API_USER", raising=False)
    monkeypatch.setenv("BEERS_CRAWLER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("BEERS_CRAWLER_ALLOW_PLAYWRIGHT", "0")
    monkeypatch.setenv("BEERS_CRAWLER_PREFER_HTTPX", "1")

    from beers_crawler.api import app

    with pytest.raises(RuntimeError, match="no users"):
        with TestClient(app):
            pass


def test_api_db_user_auth(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    monkeypatch.delenv("BEERS_CRAWLER_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("BEERS_CRAWLER_API_PASSWORD", raising=False)
    monkeypatch.delenv("BEERS_CRAWLER_API_USER", raising=False)
    monkeypatch.setenv("BEERS_CRAWLER_DB", str(db))
    monkeypatch.setenv("BEERS_CRAWLER_ALLOW_PLAYWRIGHT", "0")
    monkeypatch.setenv("BEERS_CRAWLER_PREFER_HTTPX", "1")

    store = UserStore(db)
    store.add_user("alice", "alice-password-1")

    from beers_crawler.api import app

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["auth_required"] is True

        assert client.get("/v1/list").status_code == 401
        assert client.get("/v1/list", headers=_basic("alice", "wrong-password-x")).status_code == 401

        ok = client.get("/v1/list", headers=_basic("alice", "alice-password-1"))
        assert ok.status_code == 200
        assert ok.json() == []

        detail = client.get(
            "/health/detail", headers=_basic("alice", "alice-password-1")
        )
        assert detail.status_code == 200
        assert "stats" in detail.json()


def test_api_env_bootstrap_user(monkeypatch, tmp_path):
    monkeypatch.delenv("BEERS_CRAWLER_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("BEERS_CRAWLER_API_USER", "envuser")
    monkeypatch.setenv("BEERS_CRAWLER_API_PASSWORD", "env-bootstrap-pass")
    monkeypatch.setenv("BEERS_CRAWLER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("BEERS_CRAWLER_ALLOW_PLAYWRIGHT", "0")
    monkeypatch.setenv("BEERS_CRAWLER_PREFER_HTTPX", "1")

    from beers_crawler.api import app

    with TestClient(app) as client:
        assert client.get("/v1/list").status_code == 401
        ok = client.get(
            "/v1/list", headers=_basic("envuser", "env-bootstrap-pass")
        )
        assert ok.status_code == 200


def test_auth_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("BEERS_CRAWLER_AUTH_DISABLED", "1")
    monkeypatch.delenv("BEERS_CRAWLER_API_PASSWORD", raising=False)
    monkeypatch.setenv("BEERS_CRAWLER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("BEERS_CRAWLER_ALLOW_PLAYWRIGHT", "0")

    from beers_crawler.api import app

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.json()["auth_required"] is False
        assert client.get("/v1/list").status_code == 200


def test_min_password_constant():
    assert MIN_PASSWORD_LEN >= 12
