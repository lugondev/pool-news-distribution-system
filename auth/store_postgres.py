"""Postgres implementation of AuthStore — wraps sync psycopg in asyncio.to_thread.

Uses SUPABASE_DB_URL or DATABASE_URL (matches dashboard.config_backend.PostgresBackend).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

from auth.store import AuthStore, Pat, User

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('superadmin','manager','creator')),
    permissions   JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    prefix        TEXT NOT NULL,
    last_used_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pat_hash_idx ON personal_access_tokens(token_hash);

CREATE TABLE IF NOT EXISTS auth_setup (
    id          INT PRIMARY KEY CHECK (id = 1),
    setup_token TEXT,
    consumed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _row_to_user(row, cols) -> User:
    d = dict(zip(cols, row))
    perms = d.get("permissions") or {}
    if isinstance(perms, str):
        import json
        try:
            perms = json.loads(perms)
        except ValueError:
            perms = {}
    return User(
        id=int(d["id"]),
        username=d["username"],
        role=d["role"],
        permissions=perms,
        is_active=bool(d["is_active"]),
        last_login_at=d["last_login_at"].isoformat() if d.get("last_login_at") else None,
        created_at=d["created_at"].isoformat() if d.get("created_at") else None,
    )


_USER_COLS = (
    "id", "username", "password_hash", "role", "permissions",
    "is_active", "last_login_at", "created_at", "updated_at",
)


class PostgresAuthStore(AuthStore):
    def __init__(self, dsn: str | None = None):
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except ImportError as ex:
            raise RuntimeError(
                "PostgresAuthStore requires psycopg — pip install 'psycopg[binary]'"
            ) from ex
        self._psycopg = psycopg
        self._Jsonb = Jsonb
        self.dsn = (
            dsn or os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
        )
        if not self.dsn:
            raise RuntimeError("PostgresAuthStore requires SUPABASE_DB_URL env var")

    def _conn(self):
        # prepare_threshold=None for Supabase Transaction Pooler (PgBouncer) compat.
        return self._psycopg.connect(self.dsn, autocommit=False, prepare_threshold=None)

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ── schema ──────────────────────────────────────────────────────────────
    _PAT_MIGRATION_SQL = """
        ALTER TABLE personal_access_tokens
            DROP CONSTRAINT IF EXISTS personal_access_tokens_user_id_key;
        ALTER TABLE personal_access_tokens
            ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT 'default';
        ALTER TABLE personal_access_tokens
            ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
        CREATE INDEX IF NOT EXISTS pat_user_idx ON personal_access_tokens(user_id);
    """

    def _init_schema_sync(self):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute(self._PAT_MIGRATION_SQL)
            conn.commit()

    async def init_schema(self) -> None:
        await self._run(self._init_schema_sync)

    # ── users ───────────────────────────────────────────────────────────────
    def _count_users_sync(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            (n,) = cur.fetchone()
            return int(n)

    async def count_users(self) -> int:
        return await self._run(self._count_users_sync)

    def _select_user_sync(self, where_sql: str, args: tuple) -> User | None:
        cols = ", ".join(_USER_COLS)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM users WHERE {where_sql}", args)
            row = cur.fetchone()
            return _row_to_user(row, _USER_COLS) if row else None

    async def get_user_by_username(self, username: str) -> User | None:
        return await self._run(self._select_user_sync, "username = %s", (username,))

    async def get_user_by_id(self, user_id: int) -> User | None:
        return await self._run(self._select_user_sync, "id = %s", (user_id,))

    def _get_pwd_sync(self, user_id: int) -> str | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None

    async def get_password_hash(self, user_id: int) -> str | None:
        return await self._run(self._get_pwd_sync, user_id)

    def _create_user_sync(self, username, password_hash, role, permissions) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (username, password_hash, role, permissions)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (username, password_hash, role, self._Jsonb(permissions or {})),
            )
            (uid,) = cur.fetchone()
            conn.commit()
            return int(uid)

    async def create_user(
        self, username: str, password_hash: str, role: str, permissions: dict
    ) -> int:
        return await self._run(
            self._create_user_sync, username, password_hash, role, permissions
        )

    def _update_user_sync(self, user_id, fields):
        sets, vals = [], []
        for col, val in fields.items():
            if col == "permissions":
                sets.append("permissions = %s"); vals.append(self._Jsonb(val))
            else:
                sets.append(f"{col} = %s"); vals.append(val)
        if not sets:
            return
        sets.append("updated_at = now()")
        vals.append(user_id)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = %s", tuple(vals)
            )
            conn.commit()

    async def update_user(
        self,
        user_id: int,
        *,
        password_hash: str | None = None,
        role: str | None = None,
        permissions: dict | None = None,
        is_active: bool | None = None,
    ) -> None:
        fields: dict[str, Any] = {}
        if password_hash is not None: fields["password_hash"] = password_hash
        if role is not None:          fields["role"] = role
        if permissions is not None:   fields["permissions"] = permissions
        if is_active is not None:     fields["is_active"] = is_active
        if fields:
            await self._run(self._update_user_sync, user_id, fields)

    def _delete_user_sync(self, user_id):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()

    async def delete_user(self, user_id: int) -> None:
        await self._run(self._delete_user_sync, user_id)

    def _list_users_sync(self) -> list[User]:
        cols = ", ".join(_USER_COLS)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM users ORDER BY id")
            return [_row_to_user(r, _USER_COLS) for r in cur.fetchall()]

    async def list_users(self) -> list[User]:
        return await self._run(self._list_users_sync)

    def _touch_login_sync(self, user_id):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
            conn.commit()

    async def touch_login(self, user_id: int) -> None:
        await self._run(self._touch_login_sync, user_id)

    # ── sessions ────────────────────────────────────────────────────────────
    def _create_session_sync(self, sid, uid, exp):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (id, user_id, expires_at) VALUES (%s, %s, %s)",
                (sid, uid, exp),
            )
            conn.commit()

    async def create_session(
        self, session_id: str, user_id: int, expires_at: datetime
    ) -> None:
        await self._run(self._create_session_sync, session_id, user_id, expires_at)

    def _get_session_sync(self, sid):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, expires_at FROM sessions WHERE id = %s", (sid,)
            )
            row = cur.fetchone()
            return (int(row[0]), row[1]) if row else None

    async def get_session(self, session_id: str) -> tuple[int, datetime] | None:
        return await self._run(self._get_session_sync, session_id)

    def _bump_session_sync(self, sid):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET last_seen_at = now() WHERE id = %s", (sid,)
            )
            conn.commit()

    async def bump_session(self, session_id: str) -> None:
        await self._run(self._bump_session_sync, session_id)

    def _delete_session_sync(self, sid):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE id = %s", (sid,))
            conn.commit()

    async def delete_session(self, session_id: str) -> None:
        await self._run(self._delete_session_sync, session_id)

    def _delete_expired_sessions_sync(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < now()")
            conn.commit()
            return cur.rowcount or 0

    async def delete_expired_sessions(self) -> int:
        return await self._run(self._delete_expired_sessions_sync)

    # ── PAT (multi-row) ─────────────────────────────────────────────────────
    def _list_user_pats_sync(self, user_id) -> list[Pat]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, user_id, name, prefix, expires_at, last_used_at, created_at
                   FROM personal_access_tokens WHERE user_id = %s ORDER BY id""",
                (user_id,),
            )
            out = []
            for r in cur.fetchall():
                out.append(Pat(
                    id=int(r[0]), user_id=int(r[1]), name=r[2], prefix=r[3],
                    expires_at=r[4].isoformat() if r[4] else None,
                    last_used_at=r[5].isoformat() if r[5] else None,
                    created_at=r[6].isoformat() if r[6] else None,
                ))
            return out

    async def list_user_pats(self, user_id: int) -> list[Pat]:
        return await self._run(self._list_user_pats_sync, user_id)

    def _create_pat_sync(self, user_id, name, token_hash, prefix, expires_at) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO personal_access_tokens
                       (user_id, name, token_hash, prefix, expires_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (user_id, name, token_hash, prefix, expires_at),
            )
            (pid,) = cur.fetchone()
            conn.commit()
            return int(pid)

    async def create_pat(
        self, user_id: int, name: str, token_hash: str, prefix: str,
        expires_at: datetime | None,
    ) -> int:
        return await self._run(
            self._create_pat_sync, user_id, name, token_hash, prefix, expires_at
        )

    def _delete_pat_by_id_sync(self, pat_id, user_id) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM personal_access_tokens WHERE id = %s AND user_id = %s",
                (pat_id, user_id),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0

    async def delete_pat_by_id(self, pat_id: int, user_id: int) -> bool:
        return await self._run(self._delete_pat_by_id_sync, pat_id, user_id)

    def _delete_expired_pats_sync(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM personal_access_tokens WHERE expires_at IS NOT NULL AND expires_at < now()"
            )
            conn.commit()
            return cur.rowcount or 0

    async def delete_expired_pats(self) -> int:
        return await self._run(self._delete_expired_pats_sync)

    def _get_user_by_pat_sync(self, token_hash) -> User | None:
        cols = ", ".join(f"u.{c}" for c in _USER_COLS)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT {cols} FROM users u
                    JOIN personal_access_tokens p ON p.user_id = u.id
                    WHERE p.token_hash = %s
                      AND (p.expires_at IS NULL OR p.expires_at > now())""",
                (token_hash,),
            )
            row = cur.fetchone()
            return _row_to_user(row, _USER_COLS) if row else None

    async def get_user_by_pat_hash(self, token_hash: str) -> User | None:
        return await self._run(self._get_user_by_pat_sync, token_hash)

    def _bump_pat_sync(self, token_hash):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE personal_access_tokens SET last_used_at = now() WHERE token_hash = %s",
                (token_hash,),
            )
            conn.commit()

    async def bump_pat(self, token_hash: str) -> None:
        await self._run(self._bump_pat_sync, token_hash)

    # ── back-compat single-PAT helpers ──────────────────────────────────────
    def _upsert_pat_sync(self, user_id, token_hash, prefix):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM personal_access_tokens WHERE user_id = %s", (user_id,))
            cur.execute(
                """INSERT INTO personal_access_tokens
                       (user_id, name, token_hash, prefix)
                   VALUES (%s, 'default', %s, %s)""",
                (user_id, token_hash, prefix),
            )
            conn.commit()

    async def upsert_pat(self, user_id: int, token_hash: str, prefix: str) -> None:
        await self._run(self._upsert_pat_sync, user_id, token_hash, prefix)

    def _delete_pat_sync(self, user_id):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM personal_access_tokens WHERE user_id = %s", (user_id,))
            conn.commit()

    async def delete_pat(self, user_id: int) -> None:
        await self._run(self._delete_pat_sync, user_id)

    def _get_pat_meta_sync(self, user_id):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT prefix, last_used_at, created_at
                   FROM personal_access_tokens WHERE user_id = %s
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "prefix": row[0],
                "last_used_at": row[1].isoformat() if row[1] else None,
                "created_at":   row[2].isoformat() if row[2] else None,
            }

    async def get_pat_meta(self, user_id: int) -> dict | None:
        return await self._run(self._get_pat_meta_sync, user_id)

    # ── setup token ─────────────────────────────────────────────────────────
    def _get_setup_token_sync(self):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT setup_token FROM auth_setup WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    async def get_setup_token(self) -> str | None:
        return await self._run(self._get_setup_token_sync)

    def _set_setup_token_sync(self, token):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO auth_setup (id, setup_token) VALUES (1, %s)
                   ON CONFLICT (id) DO UPDATE SET setup_token = EXCLUDED.setup_token,
                   consumed_at = NULL""",
                (token,),
            )
            conn.commit()

    async def set_setup_token(self, token: str) -> None:
        await self._run(self._set_setup_token_sync, token)

    def _consume_setup_token_sync(self):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE auth_setup SET setup_token = NULL, consumed_at = now() WHERE id = 1"
            )
            conn.commit()

    async def consume_setup_token(self) -> None:
        await self._run(self._consume_setup_token_sync)
