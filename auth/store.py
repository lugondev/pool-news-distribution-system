"""AuthStore — backend-agnostic interface for users / sessions / PAT / setup.

Selects implementation based on CONFIG_BACKEND env var (matches dashboard.config_backend):
    CONFIG_BACKEND=yaml | unset → SQLiteAuthStore (data/stats.db)
    CONFIG_BACKEND=db|postgres  → PostgresAuthStore (SUPABASE_DB_URL)

All methods are async. Postgres impl wraps sync psycopg in asyncio.to_thread.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

ROLES = ("superadmin", "manager", "creator")

# Per-user creator toggles. Adding a key here = new toggle, no migration needed
# (missing keys default to False at check time).
PERMISSIONS = (
    "can_create_social_article",
    "can_create_newsletter",
    "can_create_debate",
    "can_create_sim",
    "can_run_social_agent",
)

ROLE_HIERARCHY = {"superadmin": 3, "manager": 2, "creator": 1}


# ── User DTO ─────────────────────────────────────────────────────────────────

@dataclass
class User:
    id: int
    username: str
    role: str
    permissions: dict[str, bool] = field(default_factory=dict)
    is_active: bool = True
    last_login_at: str | None = None
    created_at: str | None = None

    def has_role(self, required: str) -> bool:
        return ROLE_HIERARCHY.get(self.role, 0) >= ROLE_HIERARCHY.get(required, 99)

    def has_perm(self, name: str) -> bool:
        # superadmin/manager always pass perm checks (they have full access).
        if self.has_role("manager"):
            return True
        return bool(self.permissions.get(name, False))


# ── Interface ────────────────────────────────────────────────────────────────

class AuthStore(ABC):
    @abstractmethod
    async def init_schema(self) -> None: ...

    # users
    @abstractmethod
    async def count_users(self) -> int: ...
    @abstractmethod
    async def get_user_by_username(self, username: str) -> User | None: ...
    @abstractmethod
    async def get_user_by_id(self, user_id: int) -> User | None: ...
    @abstractmethod
    async def get_password_hash(self, user_id: int) -> str | None: ...
    @abstractmethod
    async def create_user(
        self, username: str, password_hash: str, role: str, permissions: dict
    ) -> int: ...
    @abstractmethod
    async def update_user(
        self,
        user_id: int,
        *,
        password_hash: str | None = None,
        role: str | None = None,
        permissions: dict | None = None,
        is_active: bool | None = None,
    ) -> None: ...
    @abstractmethod
    async def delete_user(self, user_id: int) -> None: ...
    @abstractmethod
    async def list_users(self) -> list[User]: ...
    @abstractmethod
    async def touch_login(self, user_id: int) -> None: ...

    # sessions
    @abstractmethod
    async def create_session(
        self, session_id: str, user_id: int, expires_at: datetime
    ) -> None: ...
    @abstractmethod
    async def get_session(self, session_id: str) -> tuple[int, datetime] | None: ...
    @abstractmethod
    async def bump_session(self, session_id: str) -> None: ...
    @abstractmethod
    async def delete_session(self, session_id: str) -> None: ...
    @abstractmethod
    async def delete_expired_sessions(self) -> int: ...

    # personal access tokens (1 per user — replaces existing on regen)
    @abstractmethod
    async def upsert_pat(
        self, user_id: int, token_hash: str, prefix: str
    ) -> None: ...
    @abstractmethod
    async def get_user_by_pat_hash(self, token_hash: str) -> User | None: ...
    @abstractmethod
    async def bump_pat(self, token_hash: str) -> None: ...
    @abstractmethod
    async def delete_pat(self, user_id: int) -> None: ...
    @abstractmethod
    async def get_pat_meta(self, user_id: int) -> dict | None: ...

    # first-run setup token
    @abstractmethod
    async def get_setup_token(self) -> str | None: ...
    @abstractmethod
    async def set_setup_token(self, token: str) -> None: ...
    @abstractmethod
    async def consume_setup_token(self) -> None: ...


# ── Factory ──────────────────────────────────────────────────────────────────

_store: AuthStore | None = None


def get_auth_store() -> AuthStore:
    """Return the active AuthStore, instantiated on first call."""
    global _store
    if _store is None:
        choice = os.environ.get("CONFIG_BACKEND", "yaml").strip().lower()
        if choice in ("db", "postgres", "pg"):
            from auth.store_postgres import PostgresAuthStore
            _store = PostgresAuthStore()
            logger.info("auth backend: postgres")
        else:
            from auth.store_sqlite import SqliteAuthStore
            _store = SqliteAuthStore()
            logger.info("auth backend: sqlite")
    return _store


def reset_auth_store() -> None:
    """For tests — drop the cached singleton so env changes take effect."""
    global _store
    _store = None


async def init_auth_db() -> None:
    """Create tables on the active backend (idempotent)."""
    await get_auth_store().init_schema()
