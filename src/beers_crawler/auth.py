"""HTTP Basic authentication with multi-user support.

Users are stored as **password hashes** (never plaintext) in SQLite
(``api_users`` table). Manage via CLI:

    uv run beers-crawler user add <username>
    uv run beers-crawler user passwd <username>
    uv run beers-crawler user list
    uv run beers-crawler user delete <username>

Env (optional bootstrap / override — do not commit real values):
  BEERS_CRAWLER_API_USER / BEERS_CRAWLER_API_PASSWORD
      Single env-based user if no DB users exist yet (bootstrap only).
  BEERS_CRAWLER_AUTH_DISABLED=1
      Local dev only — disables auth entirely.
  BEERS_CRAWLER_DB
      SQLite path (same DB as crawl history; users live in ``api_users``).

No usernames or passwords belong in the git repository.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Iterator, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from beers_crawler.db import default_db_path

logger = logging.getLogger(__name__)

security = HTTPBasic(auto_error=False)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
PBKDF2_ROUNDS = 390_000
MIN_PASSWORD_LEN = 12

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_users (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_username(username: str) -> str:
    name = (username or "").strip()
    if not USERNAME_RE.match(name):
        raise ValueError(
            "username must be 1–64 chars: letters, digits, underscore, dot, hyphen"
        )
    return name


def hash_password(password: str) -> str:
    """Return a storable hash: pbkdf2_sha256$rounds$salt_hex$dk_hex."""
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS
    )
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password_hash(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt_hex, dk_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, rounds
    )
    return hmac.compare_digest(actual, expected)


class UserStore:
    """SQLite-backed API user directory (hashes only)."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.path = Path(db_path) if db_path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(USERS_SCHEMA)

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

    def count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM api_users").fetchone()
        return int(row["n"]) if row else 0

    def list_usernames(self) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT username FROM api_users ORDER BY lower(username)"
            ).fetchall()
        return [r["username"] for r in rows]

    def get_hash(self, username: str) -> Optional[str]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT password_hash FROM api_users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        return row["password_hash"] if row else None

    def add_user(self, username: str, password: str) -> None:
        name = validate_username(username)
        pw_hash = hash_password(password)
        now = _utc_now_iso()
        with self.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM api_users WHERE username = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if exists:
                raise ValueError(f"user already exists: {name}")
            conn.execute(
                """
                INSERT INTO api_users (username, password_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, pw_hash, now, now),
            )

    def set_password(self, username: str, password: str) -> None:
        name = validate_username(username)
        pw_hash = hash_password(password)
        now = _utc_now_iso()
        with self.connection() as conn:
            cur = conn.execute(
                """
                UPDATE api_users
                SET password_hash = ?, updated_at = ?
                WHERE username = ? COLLATE NOCASE
                """,
                (pw_hash, now, name),
            )
            if cur.rowcount == 0:
                raise ValueError(f"user not found: {name}")

    def delete_user(self, username: str) -> None:
        name = validate_username(username)
        with self.connection() as conn:
            cur = conn.execute(
                "DELETE FROM api_users WHERE username = ? COLLATE NOCASE",
                (name,),
            )
            if cur.rowcount == 0:
                raise ValueError(f"user not found: {name}")

    def verify(self, username: str, password: str) -> bool:
        stored = self.get_hash(username)
        if stored is None:
            # Dummy work to reduce user-enumeration timing gap slightly
            hash_password("x" * MIN_PASSWORD_LEN) if False else None
            return False
        return verify_password_hash(password, stored)


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    store: Optional[UserStore]
    # Optional single bootstrap user from env (plaintext only in process env)
    env_username: Optional[str]
    env_password: Optional[str]

    @classmethod
    def from_env(cls, db_path: Path | str | None = None) -> "AuthConfig":
        disabled = os.environ.get("BEERS_CRAWLER_AUTH_DISABLED", "").lower() in {
            "1",
            "true",
            "yes",
        }
        path = Path(db_path) if db_path else (
            Path(os.environ["BEERS_CRAWLER_DB"])
            if os.environ.get("BEERS_CRAWLER_DB")
            else default_db_path()
        )
        store = UserStore(path)
        env_user = (os.environ.get("BEERS_CRAWLER_API_USER") or "").strip() or None
        env_pass = os.environ.get("BEERS_CRAWLER_API_PASSWORD") or None
        if env_pass == "":
            env_pass = None

        if disabled:
            logger.warning(
                "API auth DISABLED via BEERS_CRAWLER_AUTH_DISABLED — do not use in production"
            )
            return cls(
                enabled=False,
                store=store,
                env_username=env_user,
                env_password=env_pass,
            )

        has_db_users = store.count() > 0
        has_env = bool(env_user and env_pass)
        if not has_db_users and not has_env:
            raise RuntimeError(
                "API auth is enabled but no users are configured. "
                "Create one with:  beers-crawler user add <username>  "
                "or set BEERS_CRAWLER_API_USER + BEERS_CRAWLER_API_PASSWORD "
                "for bootstrap, or BEERS_CRAWLER_AUTH_DISABLED=1 for local dev only."
            )
        if env_pass and len(env_pass) < MIN_PASSWORD_LEN:
            logger.warning(
                "BEERS_CRAWLER_API_PASSWORD is shorter than %s characters",
                MIN_PASSWORD_LEN,
            )
        logger.info(
            "API auth enabled (db_users=%s env_bootstrap=%s)",
            store.count(),
            has_env,
        )
        return cls(
            enabled=True,
            store=store,
            env_username=env_user,
            env_password=env_pass,
        )

    def authenticate(self, username: str, password: str) -> bool:
        if not self.enabled:
            return True
        # DB users first
        if self.store is not None and self.store.get_hash(username) is not None:
            return self.store.verify(username, password)
        # Env bootstrap user
        if self.env_username and self.env_password:
            user_ok = hmac.compare_digest(
                username.encode("utf-8"), self.env_username.encode("utf-8")
            )
            pass_ok = hmac.compare_digest(
                password.encode("utf-8"), self.env_password.encode("utf-8")
            )
            return user_ok and pass_ok
        return False


_config: Optional[AuthConfig] = None


def init_auth(db_path: Path | str | None = None) -> AuthConfig:
    """Load and cache auth config (call once at startup)."""
    global _config
    _config = AuthConfig.from_env(db_path=db_path)
    return _config


def get_auth_config() -> AuthConfig:
    if _config is None:
        return init_auth()
    return _config


def reset_auth_cache() -> None:
    """Test helper — clear cached config."""
    global _config
    _config = None


async def require_auth(
    credentials: Annotated[Optional[HTTPBasicCredentials], Depends(security)],
) -> str:
    """FastAPI dependency: require valid Basic auth when enabled. Returns username."""
    cfg = get_auth_config()
    if not cfg.enabled:
        return "anonymous"

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="beers-crawler"'},
        )

    if not cfg.authenticate(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": 'Basic realm="beers-crawler"'},
        )
    return credentials.username
