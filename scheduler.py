"""
APScheduler: chạy crawler + AI rewriter theo lịch.
Tích hợp vào FastAPI lifespan.
"""
import logging
import os

import redis.asyncio as aioredis
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from crawler.fetcher import fetch_all_sources
from ai.rewriter import process_pending_articles

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _load_config() -> dict:
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def _load_sources() -> list[dict]:
    with open("config/sources.yaml") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def get_scheduler(redis: aioredis.Redis) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    cfg = _load_config()
    sources = _load_sources()
    crawler_cfg = cfg.get("crawler", {})
    ai_cfg = cfg.get("ai", {})
    webhook_cfg = cfg.get("webhook", {})

    scheduler = AsyncIOScheduler()

    # Job 1: Crawl sources in active categories (reload YAML each run for hot-reload)
    async def crawl_job():
        logger.info("=== Crawl job started ===")
        current_cfg = _load_config()
        active_cats = {
            c["id"] for c in current_cfg.get("categories", [])
            if c.get("enabled", True)
        }
        current_sources = _load_sources()
        filtered = [s for s in current_sources if s.get("category", "world") in active_cats]
        skipped = len(current_sources) - len(filtered)
        if skipped:
            logger.info(f"Skipping {skipped} source(s) in disabled categories")
        await fetch_all_sources(
            sources=filtered,
            redis=redis,
            max_concurrent=crawler_cfg.get("max_concurrent_sources", 20),
            dedup_threshold=cfg.get("dedup", {}).get("simhash_distance_threshold", 3),
            max_articles_per_source=crawler_cfg.get("max_articles_per_source", 50),
            request_timeout=crawler_cfg.get("request_timeout_seconds", 15),
        )

    scheduler.add_job(
        crawl_job,
        "interval",
        minutes=crawler_cfg.get("fetch_interval_minutes", 10),
        id="crawl_all",
        max_instances=1,
        coalesce=True,
    )

    # Job 2: AI rewrite pending articles (hot-reload config mỗi lần chạy)
    if ai_cfg.get("enabled", True):
        async def ai_job():
            current_cfg = _load_config()
            ai = current_cfg.get("ai", {})
            wh = current_cfg.get("webhook", {})
            if not ai.get("enabled", True):
                return
            endpoints = wh.get("endpoints", [])
            processed = await process_pending_articles(
                redis=redis,
                model=ai.get("model", "gpt-4o-mini"),
                batch_size=ai.get("batch_size", 5),
                max_tokens=ai.get("max_tokens_summary", 300),
                temperature=ai.get("temperature", 0.3),
                api_key=ai.get("api_key") or None,
                base_url=ai.get("base_url") or None,
                webhook_endpoints=endpoints,
            )
            if processed:
                logger.info(f"AI job: processed {processed} articles")

        scheduler.add_job(
            ai_job,
            "interval",
            minutes=5,
            id="ai_rewrite",
            max_instances=1,
            coalesce=True,
        )

    _scheduler = scheduler
    return scheduler
