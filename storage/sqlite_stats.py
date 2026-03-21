"""
SQLite statistics — crawl logs, webhook logs, AI logs, system events, API requests.
Dùng aiosqlite để không block event loop.
"""
import json
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
                finished_at TEXT,
                duration_ms INTEGER DEFAULT 0,
                http_status INTEGER,
                domain      TEXT,
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

            CREATE TABLE IF NOT EXISTS telegram_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  TEXT NOT NULL,
                channel_id  TEXT NOT NULL,
                chat_id     TEXT NOT NULL,
                sent_at     TEXT NOT NULL,
                status_code INTEGER,
                success     INTEGER DEFAULT 0,
                error_msg   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tg_sent ON telegram_logs(sent_at);
            CREATE INDEX IF NOT EXISTS idx_tg_channel ON telegram_logs(channel_id);

            CREATE TABLE IF NOT EXISTS system_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'ok',
                metadata    TEXT,
                error_msg   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sys_event ON system_logs(event_type);
            CREATE INDEX IF NOT EXISTS idx_sys_started ON system_logs(started_at);
            CREATE INDEX IF NOT EXISTS idx_sys_status ON system_logs(status);

            CREATE TABLE IF NOT EXISTS api_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                method       TEXT NOT NULL,
                path         TEXT NOT NULL,
                status_code  INTEGER,
                duration_ms  INTEGER DEFAULT 0,
                requested_at TEXT NOT NULL,
                error_msg    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_api_path ON api_logs(path);
            CREATE INDEX IF NOT EXISTS idx_api_requested ON api_logs(requested_at);
            CREATE INDEX IF NOT EXISTS idx_api_status ON api_logs(status_code);
        """)

        for col, typedef in [
            ("finished_at", "TEXT"),
            ("duration_ms", "INTEGER DEFAULT 0"),
            ("http_status", "INTEGER"),
            ("domain", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE crawl_logs ADD COLUMN {col} {typedef}")
            except Exception:
                pass

        for idx, col in [
            ("idx_crawl_domain", "domain"),
            ("idx_crawl_errors", "errors"),
            ("idx_crawl_http", "http_status"),
        ]:
            try:
                await db.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON crawl_logs({col})")
            except Exception:
                pass

        await db.commit()


async def log_crawl_result(source_id: str, stats: dict, started_at: datetime) -> None:
    finished_at = datetime.now(timezone.utc)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO crawl_logs
               (source_id, started_at, finished_at, duration_ms, http_status, domain,
                found, saved, duplicates, errors, error_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                started_at.isoformat(),
                finished_at.isoformat(),
                duration_ms,
                stats.get("http_status"),
                stats.get("domain"),
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


async def log_telegram(
    article_id: str,
    channel_id: str,
    chat_id: str,
    status_code: int,
    success: bool,
    error_msg: str | None = None,
) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO telegram_logs
               (article_id, channel_id, chat_id, sent_at, status_code, success, error_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                article_id,
                channel_id,
                chat_id,
                datetime.now(timezone.utc).isoformat(),
                status_code,
                int(success),
                error_msg,
            ),
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

        # Telegram hôm nay
        row = await db.execute_fetchall(
            "SELECT COUNT(*) as total, SUM(success) as ok FROM telegram_logs WHERE sent_at >= date('now')"
        )
        tg = dict(row[0]) if row else {}

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
            "telegram": tg,
            "ai": ai,
            "top_sources": [dict(r) for r in top_sources],
            "hourly": [dict(r) for r in hourly],
        }


async def get_recent_webhook_logs(limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        total_row = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM webhook_logs")
        total = total_row[0]["cnt"] if total_row else 0
        rows = await db.execute_fetchall(
            "SELECT article_id, webhook_url, sent_at, status_code, success, error_msg "
            "FROM webhook_logs ORDER BY sent_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows], total


async def get_recent_ai_logs(limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        total_row = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM ai_logs")
        total = total_row[0]["cnt"] if total_row else 0
        rows = await db.execute_fetchall(
            "SELECT article_id, model, tokens_used, created_at "
            "FROM ai_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows], total


async def get_recent_telegram_logs(
    limit: int = 20,
    offset: int = 0,
    channel_id: str | None = None,
) -> tuple[list[dict], int]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        where, params = [], []
        if channel_id:
            where.append("channel_id = ?")
            params.append(channel_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        total_row = await db.execute_fetchall(
            f"SELECT COUNT(*) as cnt FROM telegram_logs {clause}", params
        )
        total = total_row[0]["cnt"] if total_row else 0
        rows = await db.execute_fetchall(
            f"SELECT article_id, channel_id, chat_id, sent_at, status_code, success, error_msg "
            f"FROM telegram_logs {clause} ORDER BY sent_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [dict(r) for r in rows], total


async def get_telegram_stats() -> dict:
    """Telegram delivery stats for dashboard."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute_fetchall(
            "SELECT COUNT(*) as total, SUM(success) as ok "
            "FROM telegram_logs WHERE sent_at >= date('now')"
        )
        return dict(row[0]) if row else {"total": 0, "ok": 0}


# ── Crawl log queries for tracing & optimization ─────────────────────────────

async def get_crawl_logs(
    limit: int = 50,
    offset: int = 0,
    source_id: str | None = None,
    domain: str | None = None,
    errors_only: bool = False,
    http_status: int | None = None,
    since: str | None = None,
) -> tuple[list[dict], int]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        where, params = [], []

        if source_id:
            where.append("source_id = ?")
            params.append(source_id)
        if domain:
            where.append("domain = ?")
            params.append(domain)
        if errors_only:
            where.append("errors > 0")
        if http_status:
            where.append("http_status = ?")
            params.append(http_status)
        if since:
            where.append("started_at >= ?")
            params.append(since)

        clause = f"WHERE {' AND '.join(where)}" if where else ""

        total_row = await db.execute_fetchall(
            f"SELECT COUNT(*) as cnt FROM crawl_logs {clause}", params
        )
        total = total_row[0]["cnt"] if total_row else 0

        rows = await db.execute_fetchall(
            f"SELECT id, source_id, started_at, finished_at, duration_ms, http_status, "
            f"domain, found, saved, duplicates, errors, error_msg "
            f"FROM crawl_logs {clause} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [dict(r) for r in rows], total


async def get_crawl_source_summary(since: str | None = None) -> list[dict]:
    """Per-source aggregated stats: total runs, success rate, avg duration, etc."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        time_filter = "WHERE started_at >= ?" if since else ""
        params = [since] if since else []
        rows = await db.execute_fetchall(
            f"""SELECT
                source_id,
                domain,
                COUNT(*) as total_runs,
                SUM(CASE WHEN errors = 0 THEN 1 ELSE 0 END) as success_runs,
                SUM(CASE WHEN errors > 0 THEN 1 ELSE 0 END) as failed_runs,
                ROUND(100.0 * SUM(CASE WHEN errors = 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate,
                ROUND(AVG(duration_ms)) as avg_duration_ms,
                MAX(duration_ms) as max_duration_ms,
                SUM(found) as total_found,
                SUM(saved) as total_saved,
                SUM(duplicates) as total_duplicates,
                MAX(started_at) as last_run
            FROM crawl_logs {time_filter}
            GROUP BY source_id
            ORDER BY failed_runs DESC, total_runs DESC""",
            params,
        )
        return [dict(r) for r in rows]


async def get_crawl_domain_summary(since: str | None = None) -> list[dict]:
    """Per-domain aggregated stats to detect rate limiting patterns."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        time_filter = "WHERE started_at >= ?" if since else ""
        params = [since] if since else []
        rows = await db.execute_fetchall(
            f"""SELECT
                domain,
                COUNT(*) as total_requests,
                SUM(CASE WHEN errors > 0 THEN 1 ELSE 0 END) as failed_requests,
                SUM(CASE WHEN http_status = 429 THEN 1 ELSE 0 END) as rate_limited,
                SUM(CASE WHEN http_status = 403 THEN 1 ELSE 0 END) as forbidden,
                ROUND(AVG(duration_ms)) as avg_duration_ms,
                MAX(duration_ms) as max_duration_ms,
                ROUND(100.0 * SUM(CASE WHEN errors = 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate,
                COUNT(DISTINCT source_id) as source_count,
                MAX(started_at) as last_request
            FROM crawl_logs {time_filter}
            GROUP BY domain
            ORDER BY rate_limited DESC, failed_requests DESC""",
            params,
        )
        return [dict(r) for r in rows]


async def get_crawl_error_breakdown(since: str | None = None) -> list[dict]:
    """Group errors by type for quick diagnosis."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        time_filter = "AND started_at >= ?" if since else ""
        params = [since] if since else []
        rows = await db.execute_fetchall(
            f"""SELECT
                CASE
                    WHEN http_status = 429 THEN '429 Rate Limited'
                    WHEN http_status = 403 THEN '403 Forbidden'
                    WHEN http_status = 404 THEN '404 Not Found'
                    WHEN http_status >= 500 THEN '5xx Server Error'
                    WHEN error_msg LIKE '%timeout%' OR error_msg LIKE '%Timeout%' THEN 'Timeout'
                    WHEN error_msg LIKE '%connect%' OR error_msg LIKE '%Connect%' THEN 'Connection Error'
                    WHEN error_msg IS NOT NULL THEN 'Other Error'
                    ELSE 'Unknown'
                END as error_type,
                COUNT(*) as count,
                GROUP_CONCAT(DISTINCT source_id) as affected_sources
            FROM crawl_logs
            WHERE errors > 0 {time_filter}
            GROUP BY error_type
            ORDER BY count DESC""",
            params,
        )
        return [dict(r) for r in rows]


async def log_system_event(
    event_type: str,
    started_at: datetime,
    status: str = "ok",
    metadata: dict | None = None,
    error_msg: str | None = None,
) -> None:
    finished_at = datetime.now(timezone.utc)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO system_logs
               (event_type, started_at, finished_at, duration_ms, status, metadata, error_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event_type,
                started_at.isoformat(),
                finished_at.isoformat(),
                duration_ms,
                status,
                json.dumps(metadata) if metadata else None,
                error_msg,
            ),
        )
        await db.commit()


async def log_api_request(
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
    requested_at: datetime,
    error_msg: str | None = None,
) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO api_logs (method, path, status_code, duration_ms, requested_at, error_msg)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (method, path, status_code, duration_ms, requested_at.isoformat(), error_msg),
        )
        await db.commit()


async def get_system_logs(
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
    status: str | None = None,
    since: str | None = None,
) -> tuple[list[dict], int]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        where, params = [], []
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if status:
            where.append("status = ?")
            params.append(status)
        if since:
            where.append("started_at >= ?")
            params.append(since)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total_row = await db.execute_fetchall(
            f"SELECT COUNT(*) as cnt FROM system_logs {clause}", params
        )
        total = total_row[0]["cnt"] if total_row else 0
        rows = await db.execute_fetchall(
            f"SELECT id, event_type, started_at, finished_at, duration_ms, status, metadata, error_msg "
            f"FROM system_logs {clause} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        result = []
        for r in rows:
            row = dict(r)
            if row.get("metadata"):
                try:
                    row["metadata"] = json.loads(row["metadata"])
                except Exception:
                    pass
            result.append(row)
        return result, total


async def get_system_summary(since: str | None = None) -> list[dict]:
    """Per event_type aggregated stats."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        time_filter = "WHERE started_at >= ?" if since else ""
        params = [since] if since else []
        rows = await db.execute_fetchall(
            f"""SELECT
                event_type,
                COUNT(*) as total_runs,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as success_runs,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_runs,
                ROUND(AVG(duration_ms)) as avg_duration_ms,
                MAX(duration_ms) as max_duration_ms,
                MAX(started_at) as last_run
            FROM system_logs {time_filter}
            GROUP BY event_type
            ORDER BY total_runs DESC""",
            params,
        )
        return [dict(r) for r in rows]


async def get_api_logs(
    limit: int = 50,
    offset: int = 0,
    method: str | None = None,
    path: str | None = None,
    status_code: int | None = None,
    errors_only: bool = False,
    since: str | None = None,
) -> tuple[list[dict], int]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        where, params = [], []
        if method:
            where.append("method = ?")
            params.append(method.upper())
        if path:
            where.append("path LIKE ?")
            params.append(f"%{path}%")
        if status_code:
            where.append("status_code = ?")
            params.append(status_code)
        if errors_only:
            where.append("status_code >= 400")
        if since:
            where.append("requested_at >= ?")
            params.append(since)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total_row = await db.execute_fetchall(
            f"SELECT COUNT(*) as cnt FROM api_logs {clause}", params
        )
        total = total_row[0]["cnt"] if total_row else 0
        rows = await db.execute_fetchall(
            f"SELECT id, method, path, status_code, duration_ms, requested_at, error_msg "
            f"FROM api_logs {clause} ORDER BY requested_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [dict(r) for r in rows], total


async def get_api_summary(since: str | None = None) -> list[dict]:
    """Per-path aggregated stats: count, avg latency, error rate."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        time_filter = "WHERE requested_at >= ?" if since else ""
        params = [since] if since else []
        rows = await db.execute_fetchall(
            f"""SELECT
                method,
                path,
                COUNT(*) as total_requests,
                SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) as error_count,
                ROUND(100.0 * SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) / COUNT(*), 1) as error_rate,
                ROUND(AVG(duration_ms)) as avg_duration_ms,
                MAX(duration_ms) as max_duration_ms,
                MAX(requested_at) as last_request
            FROM api_logs {time_filter}
            GROUP BY method, path
            ORDER BY total_requests DESC""",
            params,
        )
        return [dict(r) for r in rows]


async def get_crawl_timeline(hours: int = 24) -> list[dict]:
    """Hourly crawl performance for the last N hours."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """SELECT
                strftime('%Y-%m-%d %H:00', started_at) as hour,
                COUNT(*) as runs,
                SUM(found) as found,
                SUM(saved) as saved,
                SUM(duplicates) as duplicates,
                SUM(CASE WHEN errors > 0 THEN 1 ELSE 0 END) as errors,
                ROUND(AVG(duration_ms)) as avg_duration_ms
            FROM crawl_logs
            WHERE started_at >= datetime('now', ?)
            GROUP BY hour ORDER BY hour""",
            (f"-{hours} hours",),
        )
        return [dict(r) for r in rows]
