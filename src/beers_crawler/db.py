from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from beers_crawler.models import BeerMetadata, BeerPageRef

SCHEMA = """
CREATE TABLE IF NOT EXISTS beer_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    page_url TEXT NOT NULL,
    slug TEXT,
    beer_id TEXT,
    match_score REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'untappd_search',
    created_at TEXT NOT NULL,
    UNIQUE(query, page_url)
);

CREATE INDEX IF NOT EXISTS idx_beer_pages_query ON beer_pages(query);
CREATE INDEX IF NOT EXISTS idx_beer_pages_url ON beer_pages(page_url);

CREATE TABLE IF NOT EXISTS beer_metadata (
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

CREATE INDEX IF NOT EXISTS idx_beer_metadata_name ON beer_metadata(name);
CREATE INDEX IF NOT EXISTS idx_beer_metadata_score ON beer_metadata(rating_score);
"""


def default_db_path() -> Path:
    root = Path.cwd() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "beers.db"


class BeerDatabase:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_page_ref(self, ref: BeerPageRef) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO beer_pages (query, page_url, slug, beer_id, match_score, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query, page_url) DO UPDATE SET
                    slug=excluded.slug,
                    beer_id=excluded.beer_id,
                    match_score=excluded.match_score,
                    source=excluded.source,
                    created_at=excluded.created_at
                """,
                (
                    ref.query,
                    ref.page_url,
                    ref.slug,
                    ref.beer_id,
                    ref.match_score,
                    ref.source,
                    now,
                ),
            )

    def get_page_ref(self, query: str) -> Optional[BeerPageRef]:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT query, page_url, slug, beer_id, match_score, source
                FROM beer_pages
                WHERE lower(query) = lower(?)
                ORDER BY match_score DESC, created_at DESC
                LIMIT 1
                """,
                (query,),
            ).fetchone()
        if row is None:
            return None
        return BeerPageRef(
            query=row["query"],
            page_url=row["page_url"],
            slug=row["slug"],
            beer_id=row["beer_id"],
            match_score=row["match_score"],
            source=row["source"],
        )

    def save_metadata(self, meta: BeerMetadata) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO beer_metadata (
                    page_url, name, brewery, style, abv, ibu,
                    rating_score, rating_count, description, beer_id, scraped_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(page_url) DO UPDATE SET
                    name=excluded.name,
                    brewery=excluded.brewery,
                    style=excluded.style,
                    abv=excluded.abv,
                    ibu=excluded.ibu,
                    rating_score=excluded.rating_score,
                    rating_count=excluded.rating_count,
                    description=excluded.description,
                    beer_id=excluded.beer_id,
                    scraped_at=excluded.scraped_at,
                    raw_json=excluded.raw_json
                """,
                (
                    meta.page_url,
                    meta.name,
                    meta.brewery,
                    meta.style,
                    meta.abv,
                    meta.ibu,
                    meta.rating_score,
                    meta.rating_count,
                    meta.description,
                    meta.beer_id,
                    meta.scraped_at.isoformat(),
                    meta.model_dump_json(),
                ),
            )

    def get_metadata(self, page_url: str) -> Optional[BeerMetadata]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT raw_json FROM beer_metadata WHERE page_url = ?",
                (page_url,),
            ).fetchone()
        if row is None or not row["raw_json"]:
            return None
        return BeerMetadata.model_validate_json(row["raw_json"])

    def list_metadata(self, limit: int = 50) -> list[BeerMetadata]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT raw_json FROM beer_metadata
                ORDER BY scraped_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            BeerMetadata.model_validate_json(r["raw_json"])
            for r in rows
            if r["raw_json"]
        ]

    def stats(self) -> dict[str, int]:
        with self.connection() as conn:
            pages = conn.execute("SELECT COUNT(*) AS n FROM beer_pages").fetchone()["n"]
            metas = conn.execute("SELECT COUNT(*) AS n FROM beer_metadata").fetchone()[
                "n"
            ]
            with_score = conn.execute(
                "SELECT COUNT(*) AS n FROM beer_metadata WHERE rating_score IS NOT NULL"
            ).fetchone()["n"]
        return {
            "page_refs": pages,
            "metadata_rows": metas,
            "with_rating_score": with_score,
        }
