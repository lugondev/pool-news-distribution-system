"""
Log cleanup job — deletes logs older than 5h if table has ≥200 rows.
Runs every 5h to prevent unbounded SQLite growth.
"""

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from storage.sqlite_stats import _db, log_system_event

logger = logging.getLogger(__name__)

# Tables to clean with their timestamp columns
LOG_TABLES = {
    "crawl_logs": "started_at",
    "webhook_logs": "sent_at",
    "ai_logs": "created_at",
    "telegram_logs": "sent_at",
    "system_logs": "started_at",
    "api_logs": "requested_at",
    "channel_logs": "requested_at",
}

MIN_ROWS_THRESHOLD = 200


async def cleanup_logs_job(redis: aioredis.Redis) -> None:
    """Delete logs older than 5h from all log tables if they have ≥200 rows."""
    started = datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()

    total_deleted = 0
    results = {}

    try:
        async with _db() as db:
            for table, ts_col in LOG_TABLES.items():
                # Count total rows
                count_result = await db.execute_fetchall(
                    f"SELECT COUNT(*) as cnt FROM {table}"
                )
                total_rows = count_result[0]["cnt"] if count_result else 0

                if total_rows < MIN_ROWS_THRESHOLD:
                    logger.debug(
                        f"[log_cleanup] {table}: {total_rows} rows < {MIN_ROWS_THRESHOLD}, skipping"
                    )
                    results[table] = {"skipped": True, "total_rows": total_rows}
                    continue

                # Delete old logs
                cursor = await db.execute(
                    f"DELETE FROM {table} WHERE {ts_col} < ?", [cutoff]
                )
                deleted = cursor.rowcount
                total_deleted += deleted

                logger.info(
                    f"[log_cleanup] {table}: deleted {deleted} rows (total: {total_rows})"
                )
                results[table] = {
                    "deleted": deleted,
                    "total_rows": total_rows,
                    "remaining": total_rows - deleted,
                }

            await db.commit()

        # Sweep expired Personal Access Tokens (separate transaction).
        try:
            from auth.store import get_auth_store
            expired_pats = await get_auth_store().delete_expired_pats()
            results["personal_access_tokens"] = {"expired_deleted": expired_pats}
            if expired_pats:
                logger.info(f"[log_cleanup] expired PATs deleted: {expired_pats}")
        except Exception as ex:
            logger.warning(f"[log_cleanup] PAT sweep failed: {ex}", exc_info=True)
            results["personal_access_tokens"] = {"error": str(ex)}

        await log_system_event(
            "log_cleanup_job",
            started,
            status="ok",
            metadata={
                "total_deleted": total_deleted,
                "cutoff": cutoff,
                "results": results,
            },
        )

        logger.info(
            f"Log cleanup job: deleted {total_deleted} rows across {len(results)} tables"
        )

    except Exception as exc:
        logger.error(f"Log cleanup job failed: {exc}", exc_info=True)
        await log_system_event(
            "log_cleanup_job", started, status="error", error_msg=str(exc)
        )
        raise
