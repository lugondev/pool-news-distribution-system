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


def _should_dispatch_article(article: dict, ai_mode: str) -> bool:
    """
    Check if article should be dispatched based on endpoint's ai_mode.

    - "off" or None: dispatch all articles (raw)
    - "rewrite": only dispatch articles that have been AI-processed (ai_status="done")
    - "synthetic": only dispatch synthetic articles (type="synthetic")
    - "debate": only dispatch debate articles (type="debate")

    This ensures scheduled webhooks respect the same ai_mode logic as
    ai_job/synthesis_job, preventing confusion when a webhook is configured
    for synthetic but receives original articles.
    """
    if not ai_mode or ai_mode == "off":
        return True  # Dispatch all articles

    if ai_mode == "rewrite":
        # Only dispatch if AI has processed this article
        return article.get("ai_status") == "done"

    if ai_mode == "synthetic":
        # Only dispatch synthetic articles
        return article.get("type") == "synthetic"

    if ai_mode == "debate":
        # Only dispatch debate articles
        return article.get("type") == "debate"

    # Unknown ai_mode: default to allowing dispatch
    return True


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

            # Dispatch each article (respecting ai_mode filter)
            dispatched_count = 0
            for article in articles:
                # Check ai_mode compatibility for each endpoint type
                should_dispatch = True

                if webhook_endpoint:
                    ai_mode = webhook_endpoint.get("ai_mode", "off")
                    if not _should_dispatch_article(article, ai_mode):
                        logger.debug(
                            f"Schedule {schedule_name}: skipping article {article.get('id', '?')} "
                            f"(ai_mode={ai_mode}, article type={article.get('type', 'original')}, "
                            f"ai_status={article.get('ai_status', 'pending')})"
                        )
                        should_dispatch = False

                if telegram_channel and should_dispatch:
                    ai_mode = telegram_channel.get("ai_mode", "off")
                    if not _should_dispatch_article(article, ai_mode):
                        should_dispatch = False

                if twitter_account and should_dispatch:
                    ai_mode = twitter_account.get("ai_mode", "off")
                    if not _should_dispatch_article(article, ai_mode):
                        should_dispatch = False

                if not should_dispatch:
                    continue

                endpoints = [webhook_endpoint] if webhook_endpoint else []
                channels = [telegram_channel] if telegram_channel else []
                accounts = [twitter_account] if twitter_account else []

                await enqueue_dispatch(
                    article=article,
                    endpoints=endpoints,
                    telegram_channels=channels,
                    twitter_accounts=accounts,
                )
                dispatched_count += 1

            # Log result with clear status
            if dispatched_count == 0:
                if len(articles) == 0:
                    logger.info(
                        f"Schedule {schedule_name} ({schedule_id}): no articles found in Redis"
                    )
                else:
                    # Articles exist but all filtered out by ai_mode
                    ai_mode_str = ""
                    if webhook_endpoint:
                        ai_mode_str = webhook_endpoint.get("ai_mode", "off")
                    elif telegram_channel:
                        ai_mode_str = telegram_channel.get("ai_mode", "off")
                    elif twitter_account:
                        ai_mode_str = twitter_account.get("ai_mode", "off")

                    logger.info(
                        f"Schedule {schedule_name} ({schedule_id}): 0/{len(articles)} articles dispatched "
                        f"(all filtered by ai_mode={ai_mode_str}). "
                        f"Hint: No articles match ai_mode filter. Check if synthesis/AI job has run."
                    )
            else:
                logger.info(
                    f"Schedule {schedule_name} ({schedule_id}): dispatched {dispatched_count}/{len(articles)} articles"
                )

            # Update next_run_at
            iter = croniter(cron_expr, now)
            next_run = iter.get_next(datetime)
            await update_schedule_run_time(schedule_id, now, next_run)
            await log_schedule_execution(
                schedule_id, "ok", article_count=dispatched_count
            )

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
