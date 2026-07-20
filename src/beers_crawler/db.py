from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from beers_crawler.models import BeerMetadata, BeerPageRef, utc_now

# beer_metadata is append-only history (many rows per page_url over time).
# beer_pages keeps latest candidates per (query, page_url) for resolve debug.
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
    page_url TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_beer_metadata_url_time
    ON beer_metadata(page_url, scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_beer_metadata_name ON beer_metadata(name);
CREATE INDEX IF NOT EXISTS idx_beer_metadata_score ON beer_metadata(rating_score);
CREATE INDEX IF NOT EXISTS idx_beer_metadata_scraped ON beer_metadata(scraped_at DESC);
"""


def default_db_path() -> Path:
    root = Path.cwd() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "beers.db"


def _parse_dt(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return utc_now()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return utc_now()


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
            self._migrate_metadata_unique(conn)

    def _migrate_metadata_unique(self, conn: sqlite3.Connection) -> None:
        """Drop legacy UNIQUE(page_url) so each crawl can append a history row."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='beer_metadata'"
        ).fetchone()
        if row is None or not row["sql"]:
            return
        ddl = row["sql"]
        if "page_url TEXT NOT NULL UNIQUE" not in ddl and "page_url TEXT UNIQUE" not in ddl:
            return
        conn.executescript(
            """
            ALTER TABLE beer_metadata RENAME TO beer_metadata_legacy_unique;
            CREATE TABLE beer_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_url TEXT NOT NULL,
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
            INSERT INTO beer_metadata (
                page_url, name, brewery, style, abv, ibu,
                rating_score, rating_count, description, beer_id, scraped_at, raw_json
            )
            SELECT
                page_url, name, brewery, style, abv, ibu,
                rating_score, rating_count, description, beer_id, scraped_at, raw_json
            FROM beer_metadata_legacy_unique;
            DROP TABLE beer_metadata_legacy_unique;
            CREATE INDEX IF NOT EXISTS idx_beer_metadata_url_time
                ON beer_metadata(page_url, scraped_at DESC);
            CREATE INDEX IF NOT EXISTS idx_beer_metadata_name ON beer_metadata(name);
            CREATE INDEX IF NOT EXISTS idx_beer_metadata_score ON beer_metadata(rating_score);
            CREATE INDEX IF NOT EXISTS idx_beer_metadata_scraped ON beer_metadata(scraped_at DESC);
            """
        )

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
        self.save_page_refs([ref])

    def save_page_refs(self, refs: list[BeerPageRef]) -> None:
        """Upsert latest search candidates for a query (debug / re-rank)."""
        if not refs:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            for ref in refs:
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
        refs = self.list_page_refs(query, limit=1)
        return refs[0] if refs else None

    def list_page_refs(self, query: str, limit: int = 20) -> list[BeerPageRef]:
        """Cached candidates for a query, best match_score first."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT query, page_url, slug, beer_id, match_score, source, created_at
                FROM beer_pages
                WHERE lower(query) = lower(?)
                ORDER BY match_score DESC, created_at DESC
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        return [
            BeerPageRef(
                query=row["query"],
                page_url=row["page_url"],
                slug=row["slug"],
                beer_id=row["beer_id"],
                match_score=row["match_score"],
                source=row["source"],
                resolved_at=_parse_dt(row["created_at"]),
                from_history=True,
            )
            for row in rows
        ]

    def append_metadata(self, meta: BeerMetadata) -> int:
        """Append a crawl snapshot; never overwrites prior history rows."""
        scraped = meta.scraped_at or utc_now()
        # Persist without ephemeral flags in raw_json
        payload = meta.model_copy(update={"from_history": False, "history_id": None})
        with self.connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO beer_metadata (
                    page_url, name, brewery, style, abv, ibu,
                    rating_score, rating_count, description, beer_id, scraped_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.page_url,
                    payload.name,
                    payload.brewery,
                    payload.style,
                    payload.abv,
                    payload.ibu,
                    payload.rating_score,
                    payload.rating_count,
                    payload.description,
                    payload.beer_id,
                    scraped.isoformat(),
                    payload.model_dump_json(),
                ),
            )
            return int(cur.lastrowid)

    def save_metadata(self, meta: BeerMetadata) -> int:
        """Alias for append_metadata (history-preserving)."""
        return self.append_metadata(meta)

    def _row_to_metadata(self, row: sqlite3.Row, *, from_history: bool) -> BeerMetadata:
        if row["raw_json"]:
            meta = BeerMetadata.model_validate_json(row["raw_json"])
        else:
            meta = BeerMetadata(
                page_url=row["page_url"],
                name=row["name"],
                brewery=row["brewery"],
                style=row["style"],
                abv=row["abv"],
                ibu=row["ibu"],
                rating_score=row["rating_score"],
                rating_count=row["rating_count"],
                description=row["description"],
                beer_id=row["beer_id"],
                scraped_at=_parse_dt(row["scraped_at"]),
            )
        return meta.model_copy(
            update={
                "from_history": from_history,
                "history_id": int(row["id"]),
                "scraped_at": _parse_dt(row["scraped_at"]),
            }
        )

    def get_metadata(self, page_url: str) -> Optional[BeerMetadata]:
        """Latest historical snapshot for a page URL."""
        return self.get_latest_metadata(page_url)

    def get_latest_metadata(self, page_url: str) -> Optional[BeerMetadata]:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, page_url, name, brewery, style, abv, ibu,
                       rating_score, rating_count, description, beer_id,
                       scraped_at, raw_json
                FROM beer_metadata
                WHERE page_url = ?
                ORDER BY scraped_at DESC, id DESC
                LIMIT 1
                """,
                (page_url,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row, from_history=True)

    def list_metadata_history(
        self, page_url: str, *, limit: int = 50
    ) -> list[BeerMetadata]:
        """All snapshots for one beer page, newest first."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, page_url, name, brewery, style, abv, ibu,
                       rating_score, rating_count, description, beer_id,
                       scraped_at, raw_json
                FROM beer_metadata
                WHERE page_url = ?
                ORDER BY scraped_at DESC, id DESC
                LIMIT ?
                """,
                (page_url, limit),
            ).fetchall()
        return [self._row_to_metadata(r, from_history=True) for r in rows]

    def list_metadata(self, limit: int = 50) -> list[BeerMetadata]:
        """Latest snapshot per page_url, most recently scraped first."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.page_url, m.name, m.brewery, m.style, m.abv, m.ibu,
                       m.rating_score, m.rating_count, m.description, m.beer_id,
                       m.scraped_at, m.raw_json
                FROM beer_metadata m
                INNER JOIN (
                    SELECT page_url, MAX(id) AS max_id
                    FROM beer_metadata
                    GROUP BY page_url
                ) latest ON m.id = latest.max_id
                ORDER BY m.scraped_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_metadata(r, from_history=True) for r in rows]

    def stats(self) -> dict[str, int]:
        with self.connection() as conn:
            pages = conn.execute("SELECT COUNT(*) AS n FROM beer_pages").fetchone()["n"]
            history_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM beer_metadata"
            ).fetchone()["n"]
            distinct_beers = conn.execute(
                "SELECT COUNT(DISTINCT page_url) AS n FROM beer_metadata"
            ).fetchone()["n"]
            with_score = conn.execute(
                """
                SELECT COUNT(*) AS n FROM (
                    SELECT page_url
                    FROM beer_metadata
                    WHERE rating_score IS NOT NULL
                    GROUP BY page_url
                )
                """
            ).fetchone()["n"]
        return {
            "page_refs": pages,
            "history_rows": history_rows,
            "metadata_rows": history_rows,  # backward-compatible alias
            "distinct_beers": distinct_beers,
            "with_rating_score": with_score,
        }
