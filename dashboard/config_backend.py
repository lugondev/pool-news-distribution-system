"""
Pluggable backend for config storage — selected via CONFIG_BACKEND env var.

    CONFIG_BACKEND=yaml      → read/write config/*.yaml in volume (default)
    CONFIG_BACKEND=db        → read/write directly to Postgres (Supabase)

The chosen backend is a process-wide singleton built on first call to
`get_backend()`. Both backends implement the same `Backend` protocol so
`dashboard.config_io` can delegate without caring which is active.

Postgres mode caches reads (TTL 30s by default). Cache invalidates on
every write so UI saves reflect immediately.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ─── Backend protocol ────────────────────────────────────────────────────────

class Backend(ABC):
    @abstractmethod
    def read_sources(self) -> list[dict]: ...
    @abstractmethod
    def write_sources(self, sources: list[dict]) -> None: ...
    @abstractmethod
    def read_settings(self) -> dict: ...
    @abstractmethod
    def write_settings(self, cfg: dict) -> None: ...
    @abstractmethod
    def read_social_agents(self) -> list[dict]: ...
    @abstractmethod
    def write_social_agents(self, agents: list[dict]) -> None: ...
    @abstractmethod
    def read_sim_personas(self) -> dict: ...
    @abstractmethod
    def write_sim_personas(self, personas: dict) -> None: ...


# ─── YAML backend (file-based) ───────────────────────────────────────────────

class YamlBackend(Backend):
    """Reads/writes config/*.yaml in the volume — preserves current behavior."""

    def __init__(self):
        from storage.config_cache import cached_yaml, invalidate
        self._cached = cached_yaml
        self._invalidate = invalidate
        base = Path(__file__).parent.parent / "config"
        self.SOURCES_PATH        = str(base / "sources.yaml")
        self.SETTINGS_PATH       = str(base / "settings.yaml")
        self.SOCIAL_AGENTS_PATH  = str(base / "social_agents.yaml")
        self.SIM_PERSONAS_PATH   = str(base / "sim_personas.yaml")

    @staticmethod
    def _dump(path: str, data: Any) -> None:
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def read_sources(self):
        return self._cached(self.SOURCES_PATH).get("sources", [])

    def write_sources(self, sources):
        self._dump(self.SOURCES_PATH, {"sources": sources})
        self._invalidate(self.SOURCES_PATH)

    def read_settings(self):
        return self._cached(self.SETTINGS_PATH)

    def write_settings(self, cfg):
        self._dump(self.SETTINGS_PATH, cfg)
        self._invalidate(self.SETTINGS_PATH)

    def read_social_agents(self):
        return self._cached(self.SOCIAL_AGENTS_PATH).get("agents", [])

    def write_social_agents(self, agents):
        self._dump(self.SOCIAL_AGENTS_PATH, {"agents": agents})
        self._invalidate(self.SOCIAL_AGENTS_PATH)

    def read_sim_personas(self):
        return self._cached(self.SIM_PERSONAS_PATH)

    def write_sim_personas(self, personas):
        self._dump(self.SIM_PERSONAS_PATH, personas)
        self._invalidate(self.SIM_PERSONAS_PATH)


# ─── TTL cache for Postgres backend ──────────────────────────────────────────

class _TTLCache:
    """Per-key TTL cache. Reads cheap, writes evict the key."""

    def __init__(self, ttl_seconds: float = 30.0):
        self.ttl = ttl_seconds
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._d.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.time() - ts > self.ttl:
            self._d.pop(key, None)
            return None
        return val

    def set(self, key: str, value: Any) -> None:
        self._d[key] = (time.time(), value)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._d.clear()
        else:
            self._d.pop(key, None)


# ─── Postgres backend (Supabase) ─────────────────────────────────────────────

class PostgresBackend(Backend):
    """
    Reads/writes 4 relational tables defined in config/schema.sql.

    Reads cached for `cache_ttl` seconds. Writes always invalidate cache.
    Each call uses a fresh connection — Supabase's Transaction Pooler handles
    actual pooling, so we don't double-pool here.
    """

    KNOWN_SOURCE_KEYS = {"id", "name", "url", "type", "lang", "country", "category", "enabled"}

    def __init__(self, dsn: str | None = None, cache_ttl: float = 30.0):
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except ImportError as ex:
            raise RuntimeError("PostgresBackend requires psycopg — pip install 'psycopg[binary]'") from ex
        self._psycopg = psycopg
        self._Jsonb = Jsonb
        self.dsn = dsn or os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
        if not self.dsn:
            raise RuntimeError("PostgresBackend requires SUPABASE_DB_URL env var")
        self._cache = _TTLCache(cache_ttl)

    def _conn(self):
        # prepare_threshold=None: required for Supabase's PgBouncer-based
        # Transaction Pooler. Connections get reused across transactions, so
        # auto-prepared statements collide with "DuplicatePreparedStatement".
        return self._psycopg.connect(self.dsn, autocommit=False, prepare_threshold=None)

    # ── sources ─────────────────────────────────────────────────────────────

    def read_sources(self):
        cached = self._cache.get("sources")
        if cached is not None:
            return cached
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, url, type, lang, country, category, enabled, extra
                FROM sources ORDER BY display_order, id
            """)
            sources = []
            for r in cur.fetchall():
                d = {
                    "id": r[0], "name": r[1], "url": r[2], "type": r[3],
                    "lang": r[4], "country": r[5], "category": r[6], "enabled": r[7],
                }
                d = {k: v for k, v in d.items() if v is not None}
                if r[8]:
                    d.update(r[8])
                sources.append(d)
        self._cache.set("sources", sources)
        return sources

    def write_sources(self, sources):
        # First-wins dedup so display_order matches what users see in YAML.
        seen, deduped = set(), []
        for s in sources:
            if s["id"] in seen:
                logger.warning("write_sources: skipped duplicate id %s", s["id"])
                continue
            seen.add(s["id"])
            deduped.append(s)
        ids_in_payload = [s["id"] for s in deduped]
        with self._conn() as conn, conn.cursor() as cur:
            for idx, s in enumerate(deduped):
                extra = {k: v for k, v in s.items() if k not in self.KNOWN_SOURCE_KEYS}
                cur.execute("""
                    INSERT INTO sources (id, name, url, type, lang, country,
                                         category, enabled, extra, display_order, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                    ON CONFLICT (id) DO UPDATE SET
                        name=EXCLUDED.name, url=EXCLUDED.url, type=EXCLUDED.type,
                        lang=EXCLUDED.lang, country=EXCLUDED.country,
                        category=EXCLUDED.category, enabled=EXCLUDED.enabled,
                        extra=EXCLUDED.extra, display_order=EXCLUDED.display_order,
                        updated_at=now()
                """, (
                    s["id"], s["name"], s["url"], s.get("type", "rss"),
                    s.get("lang"), s.get("country"), s.get("category"),
                    s.get("enabled", True), self._Jsonb(extra), idx,
                ))
            # Whole-list-replace semantics: drop rows not in payload.
            cur.execute(
                "DELETE FROM sources WHERE id <> ALL(%s)",
                (ids_in_payload,),
            )
            conn.commit()
        self._cache.invalidate("sources")

    # ── settings ────────────────────────────────────────────────────────────

    def read_settings(self):
        cached = self._cache.get("settings")
        if cached is not None:
            return cached
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT section, data FROM settings ORDER BY created_at, section")
            cfg = {section: data for section, data in cur.fetchall()}
        self._cache.set("settings", cfg)
        return cfg

    def write_settings(self, cfg):
        sections_in_payload = list(cfg.keys())
        with self._conn() as conn, conn.cursor() as cur:
            for section, data in cfg.items():
                cur.execute("""
                    INSERT INTO settings (section, data, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (section) DO UPDATE SET
                        data=EXCLUDED.data, updated_at=now()
                """, (section, self._Jsonb(data)))
            cur.execute(
                "DELETE FROM settings WHERE section <> ALL(%s)",
                (sections_in_payload,),
            )
            conn.commit()
        self._cache.invalidate("settings")

    # ── social_agents ───────────────────────────────────────────────────────

    def read_social_agents(self):
        cached = self._cache.get("social_agents")
        if cached is not None:
            return cached
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, enabled, persona, platforms, source_filter
                FROM social_agents ORDER BY display_order, id
            """)
            agents = []
            for r in cur.fetchall():
                d = {"id": r[0], "name": r[1], "enabled": r[2]}
                if r[3]: d["persona"] = r[3]
                if r[4]: d["platforms"] = r[4]
                if r[5]: d["source_filter"] = r[5]
                agents.append(d)
        self._cache.set("social_agents", agents)
        return agents

    def write_social_agents(self, agents):
        ids_in_payload = [a["id"] for a in agents]
        with self._conn() as conn, conn.cursor() as cur:
            for idx, a in enumerate(agents):
                cur.execute("""
                    INSERT INTO social_agents
                        (id, name, enabled, persona, platforms, source_filter,
                         display_order, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                    ON CONFLICT (id) DO UPDATE SET
                        name=EXCLUDED.name, enabled=EXCLUDED.enabled,
                        persona=EXCLUDED.persona, platforms=EXCLUDED.platforms,
                        source_filter=EXCLUDED.source_filter,
                        display_order=EXCLUDED.display_order, updated_at=now()
                """, (
                    a["id"], a["name"], a.get("enabled", True),
                    self._Jsonb(a.get("persona"))       if a.get("persona")       else None,
                    self._Jsonb(a.get("platforms"))     if a.get("platforms")     else None,
                    self._Jsonb(a.get("source_filter")) if a.get("source_filter") else None,
                    idx,
                ))
            cur.execute(
                "DELETE FROM social_agents WHERE id <> ALL(%s)",
                (ids_in_payload,),
            )
            conn.commit()
        self._cache.invalidate("social_agents")

    # ── sim_personas ────────────────────────────────────────────────────────

    def read_sim_personas(self):
        cached = self._cache.get("sim_personas")
        if cached is not None:
            return cached
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT type, name, data FROM sim_personas
                ORDER BY type, created_at, name
            """)
            result: dict = {}
            for type_name, name, data in cur.fetchall():
                result.setdefault(type_name, {})[name] = data
        self._cache.set("sim_personas", result)
        return result

    def write_sim_personas(self, personas):
        keys_in_payload = []
        with self._conn() as conn, conn.cursor() as cur:
            for type_name, items in personas.items():
                if not isinstance(items, dict):
                    continue
                for name, data in items.items():
                    cur.execute("""
                        INSERT INTO sim_personas (type, name, data, updated_at)
                        VALUES (%s,%s,%s, now())
                        ON CONFLICT (type, name) DO UPDATE SET
                            data=EXCLUDED.data, updated_at=now()
                    """, (type_name, name, self._Jsonb(data)))
                    keys_in_payload.append((type_name, name))
            # Composite-key delete: build a (type, name) tuple list.
            if keys_in_payload:
                types = [k[0] for k in keys_in_payload]
                names = [k[1] for k in keys_in_payload]
                cur.execute("""
                    DELETE FROM sim_personas
                    WHERE (type, name) NOT IN (
                        SELECT t, n FROM unnest(%s::text[], %s::text[]) AS x(t, n)
                    )
                """, (types, names))
            else:
                cur.execute("DELETE FROM sim_personas")
            conn.commit()
        self._cache.invalidate("sim_personas")


# ─── Backend factory (process-wide singleton) ────────────────────────────────

_backend: Backend | None = None


def get_backend() -> Backend:
    """Return the active backend, instantiated on first call."""
    global _backend
    if _backend is None:
        choice = os.environ.get("CONFIG_BACKEND", "yaml").strip().lower()
        if choice in ("db", "postgres", "pg"):
            _backend = PostgresBackend()
            logger.info("config backend: postgres (CONFIG_BACKEND=%s)", choice)
        else:
            _backend = YamlBackend()
            logger.info("config backend: yaml (CONFIG_BACKEND=%s)", choice or "yaml [default]")
    return _backend


def reset_backend() -> None:
    """For tests / config switch at runtime — drops the cached singleton."""
    global _backend
    _backend = None
