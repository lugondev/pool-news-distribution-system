"""
Scheduled webhook execution job.
Runs every minute, checks for due schedules, fetches articles, and dispatches to webhooks.
"""

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from croniter import croniter

from storage.webhook_schedules import (
    get_due_schedules,
    update_schedule_run_time,
    log_schedule_execution,
)
from storage.redis_store import get_latest_articles
from webhook.dispatcher import enqueue_dispatch
from storage.config_cache import cached_yaml

logger = logging.getLogger(__name__)


async def scheduled_webhook_job(redis: aioredis.Redis) -> None:
    """
    Check for due webhook schedules and execute them.
    Each schedule:
    1. Fetches top N articles from Redis based on query_params (category/source filters)
    2. Dispatches to the configured webhook/telegram/twitter endpoint
    3. Updates next_run_at based on cron expression
    """
    now = datetime.now(timezone.utc)

    # Get all schedules that are due
    due = await get_due_schedules(now)
    if not due:
        return

    logger.info(f"=== Scheduled webhook job: {len(due)} schedules due ===")

    # Load config to resolve webhook/telegram/twitter endpoints
    cfg = cached_yaml("config/settings.yaml")
    webhook_map = {e["id"]: e for e in cfg.get("webhook", {}).get("endpoints", [])}
    telegram_map = {c["id"]: c for c in cfg.get("telegram", {}).get("channels", [])}
    twitter_map = {a["id"]: a for a in cfg.get("twitter", {}).get("accounts", [])}

    for schedule in due:
        schedule_id = schedule["id"]
        schedule_name = schedule["name"]
        cron_expr = schedule["cron_expression"]

        try:
            # Resolve endpoint
            webhook_endpoint = None
            telegram_channel = None
            twitter_account = None

            if schedule.get("webhook_endpoint_id"):
                webhook_endpoint = webhook_map.get(schedule["webhook_endpoint_id"])
            if schedule.get("telegram_channel_id"):
                telegram_channel = telegram_map.get(schedule["telegram_channel_id"])
            if schedule.get("twitter_account_id"):
                twitter_account = twitter_map.get(schedule["twitter_account_id"])

            if not webhook_endpoint and not telegram_channel and not twitter_account:
                logger.warning(
                    f"Schedule {schedule_name} ({schedule_id}): no valid endpoint configured, skipping"
                )
                await log_schedule_execution(
                    schedule_id, "skipped", error_msg="no valid endpoint"
                )
                continue

            # Parse query params
            query_params = schedule.get("query_params") or {}
            category = query_params.get("category")
            source_id = query_params.get("source_id")
            limit = schedule.get("max_articles", 1)

            # Fetch articles from Redis
            articles, _ = await get_latest_articles(
                redis,
                limit=limit,
                category=category,
                source_id=source_id,
            )

            if not articles:
                logger.debug(
                    f"Schedule {schedule_name} ({schedule_id}): no articles found, skipping"
                )
                await log_schedule_execution(
                    schedule_id, "skipped", error_msg="no articles"
                )
                # Still update next_run_at
                iter = croniter(cron_expr, now)
                next_run = iter.get_next(datetime)
                await update_schedule_run_time(schedule_id, now, next_run)
                continue

            # Dispatch each article
            for article in articles:
                endpoints = [webhook_endpoint] if webhook_endpoint else []
                channels = [telegram_channel] if telegram_channel else []
                accounts = [twitter_account] if twitter_account else []

                await enqueue_dispatch(
                    article=article,
                    endpoints=endpoints,
                    telegram_channels=channels,
                    twitter_accounts=accounts,
                )

            logger.info(
                f"Schedule {schedule_name} ({schedule_id}): dispatched {len(articles)} articles"
            )

            # Update next_run_at
            iter = croniter(cron_expr, now)
            next_run = iter.get_next(datetime)
            await update_schedule_run_time(schedule_id, now, next_run)
            await log_schedule_execution(schedule_id, "ok", article_count=len(articles))

        except Exception as exc:
            logger.error(
                f"Schedule {schedule_name} ({schedule_id}) failed: {exc}", exc_info=True
            )
            await log_schedule_execution(schedule_id, "error", error_msg=str(exc))
            # On error, still try to compute next_run_at if possible
            try:
                iter = croniter(cron_expr, now)
                next_run = iter.get_next(datetime)
                await update_schedule_run_time(schedule_id, now, next_run)
            except Exception:
                pass  # If cron parsing fails, leave next_run_at as-is
