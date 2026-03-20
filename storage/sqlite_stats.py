"""
SQLite statistics — crawl logs, webhook logs, AI logs.
Dùng aiosqlite để không block event loop.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

DB_PATH = os.getenv("SQLITE_PATH", "./data/stats.db")


def _db_path() -> str:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


async def init_db() -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id          TEXT PRIMARY KEY,
                name        TEXT,
                url         TEXT,
                lang        TEXT,
                category    TEXT,
                enabled     INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS crawl_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id   TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                found       INTEGER DEFAULT 0,
                saved       INTEGER DEFAULT 0,
                duplicates  INTEGER DEFAULT 0,
                errors      INTEGER DEFAULT 0,
                error_msg   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_crawl_source ON crawl_logs(source_id);
            CREATE INDEX IF NOT EXISTS idx_crawl_started ON crawl_logs(started_at);

            CREATE TABLE IF NOT EXISTS webhook_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  TEXT NOT NULL,
                webhook_url TEXT NOT NULL,
                sent_at     TEXT NOT NULL,
                status_code INTEGER,
                success     INTEGER DEFAULT 0,
                error_msg   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_webhook_sent ON webhook_logs(sent_at);

            CREATE TABLE IF NOT EXISTS ai_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  TEXT NOT NULL,
                model       TEXT,
                tokens_used INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            );
        """)
        await db.commit()


async def log_crawl_result(source_id: str, stats: dict, started_at: datetime) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO crawl_logs (source_id, started_at, found, saved, duplicates, errors, error_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                started_at.isoformat(),
                stats.get("found", 0),
                stats.get("saved", 0),
                stats.get("duplicates", 0),
                stats.get("errors", 0),
                stats.get("error_msg"),
            ),
        )
        await db.commit()


async def log_webhook(article_id: str, webhook_url: str, status_code: int, success: bool, error_msg: str = None) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO webhook_logs (article_id, webhook_url, sent_at, status_code, success, error_msg)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (article_id, webhook_url, datetime.now(timezone.utc).isoformat(), status_code, int(success), error_msg),
        )
        await db.commit()


async def log_ai_usage(article_id: str, model: str, tokens_used: int) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO ai_logs (article_id, model, tokens_used) VALUES (?, ?, ?)",
            (article_id, model, tokens_used),
        )
        await db.commit()


async def get_dashboard_stats() -> dict:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        # Tổng crawl hôm nay
        row = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt, SUM(found) as found, SUM(saved) as saved, SUM(duplicates) as dupes "
            "FROM crawl_logs WHERE started_at >= date('now')"
        )
        crawl = dict(row[0]) if row else {}

        # Webhook hôm nay
        row = await db.execute_fetchall(
            "SELECT COUNT(*) as total, SUM(success) as ok FROM webhook_logs WHERE sent_at >= date('now')"
        )
        hook = dict(row[0]) if row else {}

        # AI hôm nay
        row = await db.execute_fetchall(
            "SELECT COUNT(*) as total, SUM(tokens_used) as tokens FROM ai_logs WHERE created_at >= date('now')"
        )
        ai = dict(row[0]) if row else {}

        # Top sources hôm nay
        top_sources = await db.execute_fetchall(
            "SELECT source_id, SUM(saved) as saved FROM crawl_logs "
            "WHERE started_at >= date('now') GROUP BY source_id ORDER BY saved DESC LIMIT 10"
        )

        # Crawl theo giờ (24h gần nhất)
        hourly = await db.execute_fetchall(
            "SELECT strftime('%H', started_at) as hour, SUM(saved) as saved "
            "FROM crawl_logs WHERE started_at >= datetime('now', '-24 hours') "
            "GROUP BY hour ORDER BY hour"
        )

        return {
            "crawl": crawl,
            "webhook": hook,
            "ai": ai,
            "top_sources": [dict(r) for r in top_sources],
            "hourly": [dict(r) for r in hourly],
        }


async def get_recent_webhook_logs(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT article_id, webhook_url, sent_at, status_code, success, error_msg "
            "FROM webhook_logs ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


async def get_recent_ai_logs(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT article_id, model, tokens_used, created_at "
            "FROM ai_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
