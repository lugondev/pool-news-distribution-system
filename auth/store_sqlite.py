"""SQLite implementation of AuthStore — uses data/stats.db (same file as analytics)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from auth.store import AuthStore, Pat, User

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('superadmin','manager','creator')),
    permissions   TEXT NOT NULL DEFAULT '{}',
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_login_at TIMESTAMP,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at    TIMESTAMP NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    prefix        TEXT NOT NULL,
    last_used_at  TIMESTAMP,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS pat_hash_idx ON personal_access_tokens(token_hash);

CREATE TABLE IF NOT EXISTS auth_setup (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    setup_token TEXT,
    consumed_at TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _db_path() -> str:
    p = os.getenv("SQLITE_PATH", "./data/stats.db")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return p


def _row_to_user(row) -> User:
    perms_raw = row["permissions"] if row["permissions"] else "{}"
    try:
        perms = json.loads(perms_raw)
    except (TypeError, ValueError):
        perms = {}
    return User(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        permissions=perms,
        is_active=bool(row["is_active"]),
        last_login_at=row["last_login_at"],
        created_at=row["created_at"],
    )


class SqliteAuthStore(AuthStore):
    async def _conn(self):
        conn = await aiosqlite.connect(_db_path())
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def init_schema(self) -> None:
        conn = await self._conn()
        try:
            await conn.executescript(_SCHEMA)
            await conn.commit()
            await self._migrate_pat_multi(conn)
        finally:
            await conn.close()

    async def _migrate_pat_multi(self, conn) -> None:
        """Phase-1 migration: PATs become multi-row per user.

        Detects pre-migration shape by checking for the `name` column. SQLite
        cannot DROP CONSTRAINT, so we recreate the table and copy rows.
        """
        cur = await conn.execute("PRAGMA table_info(personal_access_tokens)")
        cols = {row[1] for row in await cur.fetchall()}
        if "name" in cols:
            return
        await conn.executescript("""
            CREATE TABLE personal_access_tokens_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash    TEXT NOT NULL UNIQUE,
                name          TEXT NOT NULL DEFAULT 'default',
                prefix        TEXT NOT NULL,
                expires_at    TIMESTAMP,
                last_used_at  TIMESTAMP,
                created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO personal_access_tokens_new
                (id, user_id, token_hash, name, prefix, last_used_at, created_at)
            SELECT id, user_id, token_hash, 'default', prefix, last_used_at, created_at
              FROM personal_access_tokens;
            DROP TABLE personal_access_tokens;
            ALTER TABLE personal_access_tokens_new RENAME TO personal_access_tokens;
            CREATE INDEX IF NOT EXISTS pat_hash_idx ON personal_access_tokens(token_hash);
            CREATE INDEX IF NOT EXISTS pat_user_idx ON personal_access_tokens(user_id);
        """)
        await conn.commit()

    # ── users ───────────────────────────────────────────────────────────────
    async def count_users(self) -> int:
        conn = await self._conn()
        try:
            cur = await conn.execute("SELECT COUNT(*) FROM users")
            (n,) = await cur.fetchone()
            return int(n)
        finally:
            await conn.close()

    async def get_user_by_username(self, username: str) -> User | None:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            )
            row = await cur.fetchone()
            return _row_to_user(row) if row else None
        finally:
            await conn.close()

    async def get_user_by_id(self, user_id: int) -> User | None:
        conn = await self._conn()
        try:
            cur = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = await cur.fetchone()
            return _row_to_user(row) if row else None
        finally:
            await conn.close()

    async def get_password_hash(self, user_id: int) -> str | None:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            )
            row = await cur.fetchone()
            return row["password_hash"] if row else None
        finally:
            await conn.close()

    async def create_user(
        self, username: str, password_hash: str, role: str, permissions: dict
    ) -> int:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                """
                INSERT INTO users (username, password_hash, role, permissions)
                VALUES (?, ?, ?, ?)
                """,
                (username, password_hash, role, json.dumps(permissions or {})),
            )
            await conn.commit()
            return cur.lastrowid
        finally:
            await conn.close()

    async def update_user(
        self,
        user_id: int,
        *,
        password_hash: str | None = None,
        role: str | None = None,
        permissions: dict | None = None,
        is_active: bool | None = None,
    ) -> None:
        sets, vals = [], []
        if password_hash is not None:
            sets.append("password_hash = ?"); vals.append(password_hash)
        if role is not None:
            sets.append("role = ?"); vals.append(role)
        if permissions is not None:
            sets.append("permissions = ?"); vals.append(json.dumps(permissions))
        if is_active is not None:
            sets.append("is_active = ?"); vals.append(1 if is_active else 0)
        if not sets:
            return
        sets.append("updated_at = CURRENT_TIMESTAMP")
        vals.append(user_id)
        conn = await self._conn()
        try:
            await conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(vals)
            )
            await conn.commit()
        finally:
            await conn.close()

    async def delete_user(self, user_id: int) -> None:
        conn = await self._conn()
        try:
            await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await conn.commit()
        finally:
            await conn.close()

    async def list_users(self) -> list[User]:
        conn = await self._conn()
        try:
            cur = await conn.execute("SELECT * FROM users ORDER BY id")
            return [_row_to_user(r) for r in await cur.fetchall()]
        finally:
            await conn.close()

    async def touch_login(self, user_id: int) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user_id,),
            )
            await conn.commit()
        finally:
            await conn.close()

    # ── sessions ────────────────────────────────────────────────────────────
    async def create_session(
        self, session_id: str, user_id: int, expires_at: datetime
    ) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
                (session_id, user_id, expires_at.isoformat()),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_session(self, session_id: str) -> tuple[int, datetime] | None:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "SELECT user_id, expires_at FROM sessions WHERE id = ?", (session_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            try:
                exp = datetime.fromisoformat(row["expires_at"])
            except ValueError:
                return None
            return int(row["user_id"]), exp
        finally:
            await conn.close()

    async def bump_session(self, session_id: str) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def delete_session(self, session_id: str) -> None:
        conn = await self._conn()
        try:
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await conn.commit()
        finally:
            await conn.close()

    async def delete_expired_sessions(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (now,)
            )
            await conn.commit()
            return cur.rowcount or 0
        finally:
            await conn.close()

    # ── PAT ─────────────────────────────────────────────────────────────────
    async def list_user_pats(self, user_id: int) -> list[Pat]:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                """SELECT id, user_id, name, prefix, expires_at, last_used_at, created_at
                   FROM personal_access_tokens WHERE user_id = ? ORDER BY id""",
                (user_id,),
            )
            return [
                Pat(
                    id=r["id"], user_id=r["user_id"], name=r["name"],
                    prefix=r["prefix"], expires_at=r["expires_at"],
                    last_used_at=r["last_used_at"], created_at=r["created_at"],
                )
                for r in await cur.fetchall()
            ]
        finally:
            await conn.close()

    async def create_pat(
        self,
        user_id: int,
        name: str,
        token_hash: str,
        prefix: str,
        expires_at: datetime | None,
    ) -> int:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                """INSERT INTO personal_access_tokens
                       (user_id, name, token_hash, prefix, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, name, token_hash, prefix,
                 expires_at.isoformat() if expires_at else None),
            )
            await conn.commit()
            return cur.lastrowid
        finally:
            await conn.close()

    async def delete_pat_by_id(self, pat_id: int, user_id: int) -> bool:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "DELETE FROM personal_access_tokens WHERE id = ? AND user_id = ?",
                (pat_id, user_id),
            )
            await conn.commit()
            return (cur.rowcount or 0) > 0
        finally:
            await conn.close()

    async def delete_expired_pats(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "DELETE FROM personal_access_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            await conn.commit()
            return cur.rowcount or 0
        finally:
            await conn.close()

    async def get_user_by_pat_hash(self, token_hash: str) -> User | None:
        now = datetime.now(timezone.utc).isoformat()
        conn = await self._conn()
        try:
            cur = await conn.execute(
                """SELECT u.* FROM users u
                   JOIN personal_access_tokens p ON p.user_id = u.id
                   WHERE p.token_hash = ?
                     AND (p.expires_at IS NULL OR p.expires_at > ?)""",
                (token_hash, now),
            )
            row = await cur.fetchone()
            return _row_to_user(row) if row else None
        finally:
            await conn.close()

    async def bump_pat(self, token_hash: str) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                "UPDATE personal_access_tokens SET last_used_at = CURRENT_TIMESTAMP WHERE token_hash = ?",
                (token_hash,),
            )
            await conn.commit()
        finally:
            await conn.close()

    # ── back-compat single-PAT helpers ──────────────────────────────────────
    async def upsert_pat(self, user_id: int, token_hash: str, prefix: str) -> None:
        """Replace all PATs for user with one named 'default'. Used by legacy endpoint."""
        conn = await self._conn()
        try:
            await conn.execute(
                "DELETE FROM personal_access_tokens WHERE user_id = ?", (user_id,)
            )
            await conn.execute(
                """INSERT INTO personal_access_tokens
                       (user_id, name, token_hash, prefix)
                   VALUES (?, 'default', ?, ?)""",
                (user_id, token_hash, prefix),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def delete_pat(self, user_id: int) -> None:
        """Delete all PATs for user. Used by legacy revoke-all endpoint."""
        conn = await self._conn()
        try:
            await conn.execute(
                "DELETE FROM personal_access_tokens WHERE user_id = ?", (user_id,)
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_pat_meta(self, user_id: int) -> dict | None:
        """Returns most recent PAT meta. Used by legacy meta endpoint."""
        conn = await self._conn()
        try:
            cur = await conn.execute(
                """SELECT prefix, last_used_at, created_at
                   FROM personal_access_tokens WHERE user_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "prefix": row["prefix"],
                "last_used_at": row["last_used_at"],
                "created_at": row["created_at"],
            }
        finally:
            await conn.close()

    # ── setup token ─────────────────────────────────────────────────────────
    async def get_setup_token(self) -> str | None:
        conn = await self._conn()
        try:
            cur = await conn.execute(
                "SELECT setup_token FROM auth_setup WHERE id = 1"
            )
            row = await cur.fetchone()
            return row["setup_token"] if row and row["setup_token"] else None
        finally:
            await conn.close()

    async def set_setup_token(self, token: str) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                """INSERT INTO auth_setup (id, setup_token) VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET setup_token = excluded.setup_token,
                   consumed_at = NULL""",
                (token,),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def consume_setup_token(self) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                """UPDATE auth_setup SET setup_token = NULL,
                   consumed_at = CURRENT_TIMESTAMP WHERE id = 1"""
            )
            await conn.commit()
        finally:
            await conn.close()
