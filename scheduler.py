"""
APScheduler: due-based crawler + AI rewriter.
Every tick (default 30s), picks the N sources whose next_crawl_at is due
and fetches them. 403/429 responses push next_crawl_at further into the future.
"""

import logging
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from crawler.fetcher import fetch_all_sources
from ai.rewriter import process_pending_articles
from ai.topic_synthesis import process_category_synthesis
from storage.redis_store import get_due_source_ids
from storage.sqlite_stats import log_system_event
from webhook.dispatcher import enqueue_dispatch

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# Config cache: avoid disk I/O on every job tick — re-read only when file changes.
_config_cache: dict | None = None
_config_mtime: float = 0.0
_sources_cache: list[dict] | None = None
_sources_mtime: float = 0.0


def _load_config() -> dict:
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime("config/settings.yaml")
    except OSError:
        mtime = 0.0
    if _config_cache is None or mtime > _config_mtime:
        with open("config/settings.yaml") as f:
            _config_cache = yaml.safe_load(f)
        _config_mtime = mtime
    return _config_cache


def _load_sources() -> list[dict]:
    global _sources_cache, _sources_mtime
    try:
        mtime = os.path.getmtime("config/sources.yaml")
    except OSError:
        mtime = 0.0
    if _sources_cache is None or mtime > _sources_mtime:
        with open("config/sources.yaml") as f:
            data = yaml.safe_load(f)
        _sources_cache = data.get("sources", [])
        _sources_mtime = mtime
    return _sources_cache


def get_scheduler(redis: aioredis.Redis) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    cfg = _load_config()
    crawler_cfg = cfg.get("crawler", {})
    ai_cfg = cfg.get("ai", {})

    scheduler = AsyncIOScheduler()

    async def crawl_job():
        started = datetime.now(timezone.utc)
        current_cfg = _load_config()
        crawler = current_cfg.get("crawler", {})
        active_cats = {
            c["id"] for c in current_cfg.get("categories", []) if c.get("enabled", True)
        }
        all_sources = _load_sources()
        filtered = [
            s
            for s in all_sources
            if s.get("enabled", True) and s.get("category", "world") in active_cats
        ]
        if not filtered:
            await log_system_event(
                "crawl_job",
                started,
                status="skipped",
                metadata={"reason": "no enabled sources"},
            )
            return

        sources_per_tick = crawler.get("sources_per_tick", 3)
        all_ids = [s["id"] for s in filtered]
        source_map = {s["id"]: s for s in filtered}

        # Pick sources whose next_crawl_at is due (or never scheduled)
        due_ids = await get_due_source_ids(redis, all_ids, limit=sources_per_tick)
        if not due_ids:
            await log_system_event(
                "crawl_job",
                started,
                status="skipped",
                metadata={"reason": "no sources due", "eligible": len(filtered)},
            )
            return

        batch = [source_map[sid] for sid in due_ids if sid in source_map]
        logger.info(f"=== Crawl tick: {len(batch)} sources due ({due_ids}) ===")

        default_interval_sec = crawler.get("default_crawl_interval_minutes", 10) * 60

        try:
            results = await fetch_all_sources(
                sources=batch,
                redis=redis,
                max_concurrent=sources_per_tick,
                dedup_threshold=current_cfg.get("dedup", {}).get(
                    "simhash_distance_threshold", 3
                ),
                max_articles_per_source=crawler.get("max_articles_per_source", 50),
                request_timeout=crawler.get("request_timeout_seconds", 15),
                domain_delay=(
                    crawler.get("domain_delay_min", 0.5),
                    crawler.get("domain_delay_max", 1.5),
                ),
                default_crawl_interval_sec=default_interval_sec,
                backoff_429_sec=crawler.get("backoff_429_minutes", 30) * 60,
                backoff_403_sec=crawler.get("backoff_403_minutes", 120) * 60,
            )
            meta = {
                "sources_in_batch": len(batch),
                "source_ids": due_ids,
                "total_found": sum(r.get("found", 0) for r in (results or [])),
                "total_saved": sum(r.get("saved", 0) for r in (results or [])),
                "total_duplicates": sum(
                    r.get("duplicates", 0) for r in (results or [])
                ),
                "errors": sum(1 for r in (results or []) if r.get("errors", 0)),
            }
            await log_system_event("crawl_job", started, status="ok", metadata=meta)
        except Exception as exc:
            await log_system_event(
                "crawl_job", started, status="error", error_msg=str(exc)
            )
            raise

    tick_interval = crawler_cfg.get("tick_interval_seconds", 30)
    scheduler.add_job(
        crawl_job,
        "interval",
        seconds=tick_interval,
        id="crawl_all",
        max_instances=1,
        coalesce=True,
    )

    if ai_cfg.get("enabled", True):

        async def ai_job():
            started = datetime.now(timezone.utc)
            current_cfg = _load_config()
            ai = current_cfg.get("ai", {})
            wh = current_cfg.get("webhook", {})
            tg = current_cfg.get("telegram", {})
            if not ai.get("enabled", True):
                await log_system_event(
                    "ai_job",
                    started,
                    status="skipped",
                    metadata={"reason": "AI disabled"},
                )
                return
            endpoints = wh.get("endpoints", [])
            tg_channels = tg.get("channels", [])
            ai_interval_sec = ai.get("interval_minutes", 2) * 60
            spread = ai_interval_sec * 0.8

            dedup_cfg = current_cfg.get("dedup", {})
            try:
                processed = await process_pending_articles(
                    redis=redis,
                    model=ai.get("model", "gpt-4o-mini"),
                    batch_size=ai.get("batch_size", 10),
                    max_tokens=ai.get("max_tokens_summary", 300),
                    temperature=ai.get("temperature", 0.3),
                    tone=ai.get("tone", "general"),
                    api_key=ai.get("api_key") or None,
                    base_url=ai.get("base_url") or None,
                    webhook_endpoints=endpoints,
                    telegram_channels=tg_channels,
                    spread_seconds=spread,
                    ai_dedup_threshold=dedup_cfg.get(
                        "ai_simhash_distance_threshold", 6
                    ),
                )
                if processed:
                    logger.info(f"AI job: processed {processed} articles")
                await log_system_event(
                    "ai_job",
                    started,
                    status="ok" if processed is not None else "skipped",
                    metadata={
                        "processed": processed or 0,
                        "model": ai.get("model", "gpt-4o-mini"),
                        "batch_size": ai.get("batch_size", 10),
                        "webhooks": len(endpoints),
                        "telegram_channels": len(tg_channels),
                    },
                )
            except Exception as exc:
                await log_system_event(
                    "ai_job", started, status="error", error_msg=str(exc)
                )
                raise

        ai_interval = ai_cfg.get("interval_minutes", 2)
        scheduler.add_job(
            ai_job,
            "interval",
            minutes=ai_interval,
            id="ai_rewrite",
            max_instances=1,
            coalesce=True,
        )

        # NEW: Topic synthesis job — generates synthetic articles from grouped content
        async def topic_synthesis_job():
            started = datetime.now(timezone.utc)
            current_cfg = _load_config()
            ai = current_cfg.get("ai", {})
            synthesis_cfg = ai.get("topic_synthesis", {})

            if not synthesis_cfg.get("enabled", False):
                return

            wh = current_cfg.get("webhook", {})
            tg = current_cfg.get("telegram", {})
            endpoints = wh.get("endpoints", [])
            tg_channels = tg.get("channels", [])

            active_cats = [
                c["id"]
                for c in current_cfg.get("categories", [])
                if c.get("enabled", True)
            ]

            if not active_cats:
                await log_system_event(
                    "topic_synthesis_job",
                    started,
                    status="skipped",
                    metadata={"reason": "no active categories"},
                )
                return

            total_generated = 0
            results_by_cat = {}

            try:
                for category in active_cats:
                    count = await process_category_synthesis(
                        redis=redis,
                        category=category,
                        min_articles=synthesis_cfg.get("min_articles", 5),
                        max_articles=synthesis_cfg.get("max_articles", 15),
                        model=ai.get("model"),
                        tone=ai.get("tone", "general"),
                        api_key=ai.get("api_key"),
                        base_url=ai.get("base_url"),
                        webhook_endpoints=endpoints,
                        telegram_channels=tg_channels,
                    )

                    if count > 0:
                        results_by_cat[category] = count
                        total_generated += count

                        # Synthetic articles are now automatically dispatched
                        logger.info(
                            f"Topic synthesis: {category} generated {count} synthetic articles"
                        )

                await log_system_event(
                    "topic_synthesis_job",
                    started,
                    status="ok",
                    metadata={
                        "total_generated": total_generated,
                        "categories_processed": len(active_cats),
                        "categories_with_output": len(results_by_cat),
                        "results": results_by_cat,
                    },
                )

                if total_generated > 0:
                    logger.info(
                        f"Topic synthesis job: {total_generated} synthetic articles "
                        f"across {len(results_by_cat)} categories"
                    )

            except Exception as exc:
                await log_system_event(
                    "topic_synthesis_job", started, status="error", error_msg=str(exc)
                )
                logger.error(f"Topic synthesis job failed: {exc}", exc_info=True)
                raise

        synthesis_interval = ai_cfg.get("topic_synthesis", {}).get(
            "interval_minutes", 5
        )
        scheduler.add_job(
            topic_synthesis_job,
            "interval",
            minutes=synthesis_interval,
            id="topic_synthesis",
            max_instances=1,
            coalesce=True,
        )

    _scheduler = scheduler
    return scheduler
