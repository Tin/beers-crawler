from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from beers_crawler.models import BeerMetadata, BeerPageRef, FailedLookup, utc_now

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

-- API users: password hashes only (never plaintext). Managed via CLI ``user`` commands.
CREATE TABLE IF NOT EXISTS api_users (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Failed name→URL resolves for self-learning / research queue.
CREATE TABLE IF NOT EXISTS failed_lookups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    normalized_query TEXT NOT NULL UNIQUE,
    fail_count INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    candidate_summary TEXT,
    first_failed_at TEXT NOT NULL,
    last_failed_at TEXT NOT NULL,
    resolved_page_url TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE INDEX IF NOT EXISTS idx_failed_lookups_status_time
    ON failed_lookups(status, last_failed_at DESC);
CREATE INDEX IF NOT EXISTS idx_failed_lookups_count
    ON failed_lookups(fail_count DESC);
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
            # Ensure tables exist even on DBs created before auth / failed-lookup features
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_users (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS failed_lookups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    normalized_query TEXT NOT NULL UNIQUE,
                    fail_count INTEGER NOT NULL DEFAULT 1,
                    last_error TEXT,
                    candidate_summary TEXT,
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    resolved_page_url TEXT,
                    resolved_at TEXT,
                    resolved_by TEXT,
                    notes TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                );
                CREATE INDEX IF NOT EXISTS idx_failed_lookups_status_time
                    ON failed_lookups(status, last_failed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_failed_lookups_count
                    ON failed_lookups(fail_count DESC);
                """
            )

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

    def iter_history_rows(
        self, *, page_url: str | None = None, limit: int | None = None
    ) -> list[BeerMetadata]:
        """Export helper: history rows newest-first, optional filter by page_url."""
        sql = """
            SELECT id, page_url, name, brewery, style, abv, ibu,
                   rating_score, rating_count, description, beer_id,
                   scraped_at, raw_json
            FROM beer_metadata
        """
        params: list[object] = []
        if page_url:
            sql += " WHERE page_url = ?"
            params.append(page_url)
        sql += " ORDER BY scraped_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_metadata(r, from_history=True) for r in rows]

    def export_history_json(
        self, *, page_url: str | None = None, limit: int | None = None
    ) -> str:
        import json

        rows = self.iter_history_rows(page_url=page_url, limit=limit)
        return json.dumps([r.model_dump(mode="json") for r in rows], indent=2)

    def export_history_csv(
        self, *, page_url: str | None = None, limit: int | None = None
    ) -> str:
        import csv
        import io

        rows = self.iter_history_rows(page_url=page_url, limit=limit)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "history_id",
                "scraped_at",
                "page_url",
                "name",
                "brewery",
                "style",
                "rating_score",
                "rating_count",
                "abv",
                "ibu",
                "beer_id",
            ]
        )
        for m in rows:
            writer.writerow(
                [
                    m.history_id,
                    m.scraped_at.isoformat() if m.scraped_at else "",
                    m.page_url,
                    m.name or "",
                    m.brewery or "",
                    m.style or "",
                    m.rating_score if m.rating_score is not None else "",
                    m.rating_count if m.rating_count is not None else "",
                    m.abv if m.abv is not None else "",
                    m.ibu if m.ibu is not None else "",
                    m.beer_id or "",
                ]
            )
        return buf.getvalue()

    @staticmethod
    def normalize_lookup_query(query: str) -> str:
        return " ".join(query.strip().lower().split())

    def _row_to_failed_lookup(self, row: sqlite3.Row) -> FailedLookup:
        return FailedLookup(
            id=int(row["id"]),
            query=row["query"],
            normalized_query=row["normalized_query"],
            fail_count=int(row["fail_count"] or 1),
            last_error=row["last_error"],
            candidate_summary=row["candidate_summary"],
            first_failed_at=_parse_dt(row["first_failed_at"]),
            last_failed_at=_parse_dt(row["last_failed_at"]),
            resolved_page_url=row["resolved_page_url"],
            resolved_at=_parse_dt(row["resolved_at"]) if row["resolved_at"] else None,
            resolved_by=row["resolved_by"],
            notes=row["notes"],
            status=row["status"] or "open",
        )

    def record_failed_lookup(
        self,
        query: str,
        *,
        error: str | None = None,
        candidate_summary: str | None = None,
    ) -> FailedLookup:
        """Upsert a failed resolve; increments fail_count for open items."""
        now = utc_now().isoformat()
        norm = self.normalize_lookup_query(query)
        original = query.strip()
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM failed_lookups WHERE normalized_query = ?",
                (norm,),
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    """
                    INSERT INTO failed_lookups (
                        query, normalized_query, fail_count, last_error,
                        candidate_summary, first_failed_at, last_failed_at, status
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, 'open')
                    """,
                    (original, norm, error, candidate_summary, now, now),
                )
                row_id = int(cur.lastrowid)
            else:
                # Re-open if previously resolved/ignored and it failed again
                status = existing["status"] or "open"
                if status in {"resolved", "ignored"}:
                    status = "open"
                conn.execute(
                    """
                    UPDATE failed_lookups SET
                        query = ?,
                        fail_count = fail_count + 1,
                        last_error = ?,
                        candidate_summary = COALESCE(?, candidate_summary),
                        last_failed_at = ?,
                        status = ?,
                        resolved_page_url = CASE WHEN ? = 'open' THEN NULL ELSE resolved_page_url END,
                        resolved_at = CASE WHEN ? = 'open' THEN NULL ELSE resolved_at END
                    WHERE normalized_query = ?
                    """,
                    (
                        original,
                        error,
                        candidate_summary,
                        now,
                        status,
                        status,
                        status,
                        norm,
                    ),
                )
                row_id = int(existing["id"])
            row = conn.execute(
                "SELECT * FROM failed_lookups WHERE id = ?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_failed_lookup(row)

    def list_failed_lookups(
        self,
        *,
        status: str | None = "open",
        limit: int = 50,
        min_fail_count: int = 1,
    ) -> list[FailedLookup]:
        sql = "SELECT * FROM failed_lookups WHERE fail_count >= ?"
        params: list[object] = [min_fail_count]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY fail_count DESC, last_failed_at DESC LIMIT ?"
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_failed_lookup(r) for r in rows]

    def get_failed_lookup(self, lookup_id: int) -> Optional[FailedLookup]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM failed_lookups WHERE id = ?", (lookup_id,)
            ).fetchone()
        return self._row_to_failed_lookup(row) if row else None

    def mark_failed_lookup_resolved(
        self,
        lookup_id: int,
        *,
        page_url: str,
        resolved_by: str = "manual",
        notes: str | None = None,
    ) -> Optional[FailedLookup]:
        now = utc_now().isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE failed_lookups SET
                    status = 'resolved',
                    resolved_page_url = ?,
                    resolved_at = ?,
                    resolved_by = ?,
                    notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (page_url, now, resolved_by, notes, lookup_id),
            )
        return self.get_failed_lookup(lookup_id)

    def mark_failed_lookup_status(
        self,
        lookup_id: int,
        status: str,
        *,
        notes: str | None = None,
    ) -> Optional[FailedLookup]:
        if status not in {"open", "researching", "resolved", "ignored"}:
            raise ValueError(f"invalid status: {status}")
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE failed_lookups SET
                    status = ?,
                    notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (status, notes, lookup_id),
            )
        return self.get_failed_lookup(lookup_id)

    def failed_lookup_stats(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n FROM failed_lookups GROUP BY status
                """
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM failed_lookups"
            ).fetchone()["n"]
        out = {str(r["status"]): int(r["n"]) for r in rows}
        out["total"] = int(total)
        return out
