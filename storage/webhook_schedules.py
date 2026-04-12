"""
Webhook schedule storage and queries.
Allows scheduling periodic webhook triggers via cron expressions.
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from storage.sqlite_stats import _db_path


@asynccontextmanager
async def _db():
    """Open SQLite connection with optimized settings."""
    async with aiosqlite.connect(_db_path()) as conn:
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA temp_store=memory")
        yield conn


async def init_schedules_db() -> None:
    """Initialize webhook_schedules table."""
    async with _db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS webhook_schedules (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                cron_expression TEXT NOT NULL,
                enabled         INTEGER DEFAULT 1,
                webhook_endpoint_id TEXT,
                telegram_channel_id TEXT,
                twitter_account_id TEXT,
                query_params    TEXT,
                max_articles    INTEGER DEFAULT 1,
                last_run_at     TEXT,
                next_run_at     TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            );
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedule_next_run ON webhook_schedules(next_run_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedule_enabled ON webhook_schedules(enabled)"
        )
        await db.commit()


async def create_schedule(
    name: str,
    cron_expression: str,
    webhook_endpoint_id: str | None = None,
    telegram_channel_id: str | None = None,
    twitter_account_id: str | None = None,
    query_params: dict | None = None,
    max_articles: int = 1,
    enabled: bool = True,
) -> str:
    """Create a new webhook schedule. Returns schedule ID."""
    schedule_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    async with _db() as db:
        await db.execute(
            """INSERT INTO webhook_schedules
               (id, name, cron_expression, enabled, webhook_endpoint_id,
                telegram_channel_id, twitter_account_id, query_params, max_articles,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                schedule_id,
                name,
                cron_expression,
                int(enabled),
                webhook_endpoint_id,
                telegram_channel_id,
                twitter_account_id,
                json.dumps(query_params) if query_params else None,
                max_articles,
                now,
                now,
            ),
        )
        await db.commit()
    return schedule_id


async def update_schedule(
    schedule_id: str,
    name: str | None = None,
    cron_expression: str | None = None,
    enabled: bool | None = None,
    webhook_endpoint_id: str | None = None,
    telegram_channel_id: str | None = None,
    twitter_account_id: str | None = None,
    query_params: dict | None = None,
    max_articles: int | None = None,
) -> bool:
    """Update an existing schedule. Returns True if updated."""
    updates = []
    params = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if cron_expression is not None:
        updates.append("cron_expression = ?")
        params.append(cron_expression)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(int(enabled))
    if webhook_endpoint_id is not None:
        updates.append("webhook_endpoint_id = ?")
        params.append(webhook_endpoint_id)
    if telegram_channel_id is not None:
        updates.append("telegram_channel_id = ?")
        params.append(telegram_channel_id)
    if twitter_account_id is not None:
        updates.append("twitter_account_id = ?")
        params.append(twitter_account_id)
    if query_params is not None:
        updates.append("query_params = ?")
        params.append(json.dumps(query_params) if query_params else None)
    if max_articles is not None:
        updates.append("max_articles = ?")
        params.append(max_articles)

    if not updates:
        return False

    updates.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(schedule_id)

    async with _db() as db:
        await db.execute(
            f"UPDATE webhook_schedules SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await db.commit()
    return True


async def delete_schedule(schedule_id: str) -> bool:
    """Delete a schedule. Returns True if deleted."""
    async with _db() as db:
        cursor = await db.execute(
            "DELETE FROM webhook_schedules WHERE id = ?", (schedule_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_schedule(schedule_id: str) -> dict | None:
    """Get a single schedule by ID."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM webhook_schedules WHERE id = ?", (schedule_id,)
        )
        if not rows:
            return None
        row = dict(rows[0])
        if row.get("query_params"):
            try:
                row["query_params"] = json.loads(row["query_params"])
            except Exception:
                row["query_params"] = {}
        row["enabled"] = bool(row.get("enabled"))
        return row


async def get_all_schedules() -> list[dict]:
    """Get all schedules."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM webhook_schedules ORDER BY created_at DESC"
        )
        result = []
        for row in rows:
            r = dict(row)
            if r.get("query_params"):
                try:
                    r["query_params"] = json.loads(r["query_params"])
                except Exception:
                    r["query_params"] = {}
            r["enabled"] = bool(r.get("enabled"))
            result.append(r)
        return result


async def get_due_schedules(now: datetime) -> list[dict]:
    """Get all enabled schedules whose next_run_at is due or null."""
    now_iso = now.isoformat()
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """SELECT * FROM webhook_schedules
               WHERE enabled = 1
               AND (next_run_at IS NULL OR next_run_at <= ?)
               ORDER BY next_run_at""",
            (now_iso,),
        )
        result = []
        for row in rows:
            r = dict(row)
            if r.get("query_params"):
                try:
                    r["query_params"] = json.loads(r["query_params"])
                except Exception:
                    r["query_params"] = {}
            r["enabled"] = bool(r.get("enabled"))
            result.append(r)
        return result


async def update_schedule_run_time(
    schedule_id: str, last_run_at: datetime, next_run_at: datetime
) -> None:
    """Update last_run_at and next_run_at after execution."""
    async with _db() as db:
        await db.execute(
            """UPDATE webhook_schedules
               SET last_run_at = ?, next_run_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                last_run_at.isoformat(),
                next_run_at.isoformat(),
                datetime.now(timezone.utc).isoformat(),
                schedule_id,
            ),
        )
        await db.commit()


async def log_schedule_execution(
    schedule_id: str,
    status: str,
    article_count: int = 0,
    error_msg: str | None = None,
) -> None:
    """Log schedule execution result."""
    async with _db() as db:
        await db.execute(
            """INSERT INTO system_logs
               (event_type, started_at, finished_at, duration_ms, status, metadata, error_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "webhook_schedule",
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                0,
                status,
                json.dumps(
                    {"schedule_id": schedule_id, "article_count": article_count}
                ),
                error_msg,
            ),
        )
        await db.commit()
