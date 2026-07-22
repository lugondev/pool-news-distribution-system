"""
Log cleanup job — deletes logs older than configured age if table has >= threshold rows.
Runs on configured interval to prevent unbounded SQLite growth.

Default: Delete logs >5h if table has ≥200 rows, runs every 5h.
Configurable via settings.yaml:
  log_retention:
    enabled: true
    max_age_hours: 5
    cleanup_interval_hours: 5
    min_rows_threshold: 200
"""

import logging
from datetime import datetime, timedelta, timezone

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

# Default values (overridden by settings.yaml)
DEFAULT_MAX_AGE_HOURS = 5
DEFAULT_MIN_ROWS_THRESHOLD = 200


def _load_config() -> dict:
    """Load retention config from settings.yaml."""
    from dashboard.config_io import read_settings
    settings = read_settings()
    return settings.get("log_retention", {})


async def cleanup_logs_job(redis: aioredis.Redis) -> None:
    """Delete logs older than configured age from all log tables if they have >= threshold rows."""
    started = datetime.now(timezone.utc)

    # Load config
    config = _load_config()
    if not config.get("enabled", True):
        logger.info("[log_cleanup] Disabled via config, skipping")
        return

    max_age_hours = config.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)
    min_rows_threshold = config.get("min_rows_threshold", DEFAULT_MIN_ROWS_THRESHOLD)

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()

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

                if total_rows < min_rows_threshold:
                    logger.debug(
                        f"[log_cleanup] {table}: {total_rows} rows < {min_rows_threshold}, skipping"
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

            # WAL checkpoint to let auto_vacuum=INCREMENTAL actually shrink
            # the file back to the OS. Without checkpoint, freed pages stay
            # internal and file doesn't physically shrink on disk.
            # RESTART = checkpoint + reset WAL. Safe, doesn't block readers.
            await db.execute("PRAGMA wal_checkpoint(RESTART)")
            await db.commit()

            # One-time VACUUM recovery for bloated legacy DBs (auto_vacuum wasn't
            # set due to past startup failures). This is optional, expensive (~100GB
            # free space for 100GB DB), and skipped if disk is full or disabled.
            try:
                if config.get("enable_vacuum_recovery", False):
                    logger.info("[log_cleanup] Attempting VACUUM recovery (expensive)...")
                    await db.execute("VACUUM")
                    await db.commit()
                    logger.info("[log_cleanup] VACUUM succeeded")
                    results["vacuum"] = {"status": "success"}
            except Exception as e:
                # Disk full, DB locked, etc. — skip and rely on incremental cleanup.
                logger.info(
                    f"[log_cleanup] VACUUM skipped (expected on disk-full): {e}"
                )
                results["vacuum"] = {"status": "skipped", "reason": str(e)}

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
                "max_age_hours": max_age_hours,
                "min_rows_threshold": min_rows_threshold,
                "results": results,
            },
        )

        logger.info(
            f"[log_cleanup] Deleted {total_deleted} rows (age >{max_age_hours}h, threshold >={min_rows_threshold})"
        )

    except Exception as exc:
        logger.error(f"Log cleanup job failed: {exc}", exc_info=True)
        await log_system_event(
            "log_cleanup_job", started, status="error", error_msg=str(exc)
        )
        raise
