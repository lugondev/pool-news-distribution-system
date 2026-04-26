#!/usr/bin/env python3
"""
Sync config YAML files <-> Postgres tables (4-table relational schema).

Schema lives in config/schema.sql. One YAML file ↔ one table:
    sources.yaml         ↔ sources           (370+ rows)
    social_agents.yaml   ↔ social_agents     (~7 rows)
    sim_personas.yaml    ↔ sim_personas      (composite PK type+name)
    settings.yaml        ↔ settings          (one row per section)

Commands (manual, both directions):
    python config/sync.py init           # apply schema.sql (creates 4 tables)
    python config/sync.py status         # row counts diff (yaml vs db)
    python config/sync.py yaml-to-db     # local YAMLs → upsert into tables
    python config/sync.py db-to-yaml     # tables → write local YAMLs (atomic)

Round-trip guarantee: yaml-to-db then db-to-yaml is semantically identical
(same items, same field values). Comments and exact formatting are NOT
preserved — PyYAML drops them.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).parent
SCHEMA_PATH = CONFIG_DIR / "schema.sql"


# ─── Lazy imports (so --help works without deps installed) ───────────────────

def _psycopg():
    try:
        import psycopg
        from psycopg.types.json import Jsonb
        return psycopg, Jsonb
    except ImportError:
        sys.exit("psycopg not installed — run: pip install 'psycopg[binary]'")


def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        sys.exit("pyyaml not installed — run: pip install pyyaml")


def connect():
    psycopg, _ = _psycopg()
    url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set — see deploy/COOLIFY.md.")
    # prepare_threshold=None disables psycopg's automatic prepared statements.
    # Required because Supabase's Transaction Pooler (port 6543) reuses
    # connections across transactions — prepared statements created in one
    # transaction will collide ("DuplicatePreparedStatement") in the next.
    return psycopg.connect(url, autocommit=False, prepare_threshold=None)


def _load_yaml(name: str) -> Any:
    p = CONFIG_DIR / name
    if not p.exists():
        return None
    with p.open() as f:
        return _yaml().safe_load(f)


# ─── Entity definitions ──────────────────────────────────────────────────────
# Each entity owns the YAML <-> table mapping for one config file. Adding a
# new file later = subclass Entity and append to ENTITIES.

class Entity:
    yaml_filename: str
    table: str

    def count_yaml(self, data: Any) -> int: ...
    def push(self, conn, data: Any) -> int: ...
    def pull(self, conn) -> Any: ...

    def count_db(self, conn) -> int:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {self.table}")
            return cur.fetchone()[0]


class SourcesEntity(Entity):
    yaml_filename = "sources.yaml"
    table = "sources"
    # Anything not in this set spills into `extra JSONB`.
    KNOWN = {"id", "name", "url", "type", "lang", "country", "category", "enabled"}

    def count_yaml(self, data):
        return len((data or {}).get("sources", []))

    def push(self, conn, data):
        _, Jsonb = _psycopg()
        raw = data.get("sources", [])
        # Dedup keeping FIRST occurrence so display_order matches the visual
        # position operators see in the YAML file. UPSERT alone would let
        # later duplicates overwrite earlier display_order, cascading every
        # subsequent row's position.
        seen = set()
        rows = []
        skipped = 0
        for s in raw:
            if s["id"] in seen:
                skipped += 1
                continue
            seen.add(s["id"])
            rows.append(s)
        if skipped:
            print(f"  ! sources.yaml: skipped {skipped} duplicate id(s)")
        with conn.cursor() as cur:
            for idx, s in enumerate(rows):
                extra = {k: v for k, v in s.items() if k not in self.KNOWN}
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
                    s.get("enabled", True), Jsonb(extra), idx,
                ))
        return len(rows)

    def pull(self, conn):
        with conn.cursor() as cur:
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
                if r[8]:                 # merge extras (custom fields)
                    d.update(r[8])
                sources.append(d)
        return {"sources": sources}


class SocialAgentsEntity(Entity):
    yaml_filename = "social_agents.yaml"
    table = "social_agents"

    def count_yaml(self, data):
        return len((data or {}).get("agents", []))

    def push(self, conn, data):
        _, Jsonb = _psycopg()
        rows = data.get("agents", [])
        with conn.cursor() as cur:
            for idx, a in enumerate(rows):
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
                    Jsonb(a.get("persona"))       if a.get("persona")       else None,
                    Jsonb(a.get("platforms"))     if a.get("platforms")     else None,
                    Jsonb(a.get("source_filter")) if a.get("source_filter") else None,
                    idx,
                ))
        return len(rows)

    def pull(self, conn):
        with conn.cursor() as cur:
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
        return {"agents": agents}


class SimPersonasEntity(Entity):
    yaml_filename = "sim_personas.yaml"
    table = "sim_personas"

    def count_yaml(self, data):
        if not isinstance(data, dict):
            return 0
        return sum(len(v) for v in data.values() if isinstance(v, dict))

    def push(self, conn, data):
        _, Jsonb = _psycopg()
        n = 0
        with conn.cursor() as cur:
            for type_name, items in (data or {}).items():
                if not isinstance(items, dict):
                    continue
                for name, payload in items.items():
                    cur.execute("""
                        INSERT INTO sim_personas (type, name, data, updated_at)
                        VALUES (%s,%s,%s, now())
                        ON CONFLICT (type, name) DO UPDATE SET
                            data=EXCLUDED.data, updated_at=now()
                    """, (type_name, name, Jsonb(payload)))
                    n += 1
        return n

    def pull(self, conn):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT type, name, data FROM sim_personas
                ORDER BY type, created_at, name
            """)
            result: dict = {}
            for type_name, name, data in cur.fetchall():
                result.setdefault(type_name, {})[name] = data
        return result


class SettingsEntity(Entity):
    yaml_filename = "settings.yaml"
    table = "settings"

    def count_yaml(self, data):
        return len(data) if isinstance(data, dict) else 0

    def push(self, conn, data):
        _, Jsonb = _psycopg()
        n = 0
        with conn.cursor() as cur:
            for section, payload in (data or {}).items():
                cur.execute("""
                    INSERT INTO settings (section, data, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (section) DO UPDATE SET
                        data=EXCLUDED.data, updated_at=now()
                """, (section, Jsonb(payload)))
                n += 1
        return n

    def pull(self, conn):
        with conn.cursor() as cur:
            cur.execute("SELECT section, data FROM settings ORDER BY created_at, section")
            return {section: data for section, data in cur.fetchall()}


ENTITIES: list[Entity] = [
    SourcesEntity(),
    SocialAgentsEntity(),
    SimPersonasEntity(),
    SettingsEntity(),
]


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_init(conn) -> int:
    sql = SCHEMA_PATH.read_text()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print(f"schema applied from {SCHEMA_PATH.name}")
    return 0


def cmd_status(conn) -> int:
    print(f"{'ENTITY':<22} {'YAML':>6} {'DB':>6}   STATUS")
    print("-" * 70)
    for e in ENTITIES:
        local = _load_yaml(e.yaml_filename)
        y = e.count_yaml(local) if local is not None else 0
        try:
            d = e.count_db(conn)
        except Exception as ex:
            print(f"{e.yaml_filename:<22} {y:>6} {'?':>6}   ERR: {str(ex)[:40]}")
            continue
        if y == d == 0:
            status = "(both empty)"
        elif y == d:
            status = "row count matches"
        elif d == 0:
            status = "DB empty — run yaml-to-db"
        elif y == 0:
            status = "YAML empty — run db-to-yaml"
        else:
            status = f"differ by {abs(y - d)}"
        print(f"{e.yaml_filename:<22} {y:>6} {d:>6}   {status}")
    return 0


def cmd_yaml_to_db(conn) -> int:
    for e in ENTITIES:
        data = _load_yaml(e.yaml_filename)
        if data is None:
            print(f"  skip {e.yaml_filename}: file missing")
            continue
        n = e.push(conn, data)
        print(f"  {e.yaml_filename:<22} → {e.table:<16}: {n} rows upserted")
    conn.commit()
    print("\nyaml-to-db done")
    return 0


def cmd_db_to_yaml(conn) -> int:
    yaml = _yaml()
    for e in ENTITIES:
        data = e.pull(conn)
        target = CONFIG_DIR / e.yaml_filename
        # Atomic write: tmp + rename so a crash never leaves partial YAML.
        tmp = target.with_suffix(target.suffix + ".tmp")
        with tmp.open("w") as f:
            yaml.safe_dump(
                data, f,
                allow_unicode=True,        # Vietnamese / non-ASCII intact
                sort_keys=False,           # preserve dict insertion order
                default_flow_style=False,  # block style, human-readable
                width=120,
            )
        tmp.replace(target)
        n = e.count_yaml(data)
        print(f"  {e.table:<16} → {e.yaml_filename:<22}: {n} items written")
    print("\ndb-to-yaml done")
    return 0


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync config YAML files <-> Postgres tables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init",       help="apply config/schema.sql (creates 4 tables)")
    sub.add_parser("status",     help="show row count diff (yaml vs db)")
    sub.add_parser("yaml-to-db", help="parse YAMLs and upsert into tables")
    sub.add_parser("db-to-yaml", help="dump tables back into local YAMLs")
    args = parser.parse_args()

    with connect() as conn:
        if args.cmd == "init":       return cmd_init(conn)
        if args.cmd == "status":     return cmd_status(conn)
        if args.cmd == "yaml-to-db": return cmd_yaml_to_db(conn)
        if args.cmd == "db-to-yaml": return cmd_db_to_yaml(conn)
    return 1


if __name__ == "__main__":
    sys.exit(main())
