"""
APScheduler: staggered crawler + AI rewriter.
Sources are split into N groups; each tick crawls one group in round-robin,
so fresh articles arrive continuously instead of in large bursts.
"""
import logging

import redis.asyncio as aioredis
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from crawler.fetcher import fetch_all_sources
from ai.rewriter import process_pending_articles

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_crawl_group_idx: int = 0


def _load_config() -> dict:
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def _load_sources() -> list[dict]:
    with open("config/sources.yaml") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def _split_groups(items: list, n: int) -> list[list]:
    """Split items into n roughly-equal groups."""
    n = max(1, min(n, len(items)))
    k, m = divmod(len(items), n)
    groups = []
    idx = 0
    for i in range(n):
        size = k + (1 if i < m else 0)
        groups.append(items[idx : idx + size])
        idx += size
    return groups


def get_scheduler(redis: aioredis.Redis) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    cfg = _load_config()
    crawler_cfg = cfg.get("crawler", {})
    ai_cfg = cfg.get("ai", {})

    scheduler = AsyncIOScheduler()

    async def crawl_job():
        global _crawl_group_idx
        current_cfg = _load_config()
        crawler = current_cfg.get("crawler", {})
        active_cats = {
            c["id"] for c in current_cfg.get("categories", [])
            if c.get("enabled", True)
        }
        current_sources = _load_sources()
        filtered = [s for s in current_sources if s.get("category", "world") in active_cats]
        skipped = len(current_sources) - len(filtered)

        n_groups = crawler.get("stagger_groups", 3)
        groups = _split_groups(filtered, n_groups)

        if not groups:
            return

        group_idx = _crawl_group_idx % len(groups)
        batch = groups[group_idx]
        _crawl_group_idx += 1

        logger.info(
            f"=== Crawl group {group_idx + 1}/{len(groups)} "
            f"({len(batch)} sources) ==="
        )
        if skipped:
            logger.info(f"Skipping {skipped} source(s) in disabled categories")

        crawl_interval_sec = crawler.get("fetch_interval_minutes", 3) * 60
        spread = crawl_interval_sec * 0.8

        await fetch_all_sources(
            sources=batch,
            redis=redis,
            max_concurrent=crawler.get("max_concurrent_sources", 10),
            dedup_threshold=current_cfg.get("dedup", {}).get("simhash_distance_threshold", 3),
            max_articles_per_source=crawler.get("max_articles_per_source", 50),
            request_timeout=crawler.get("request_timeout_seconds", 15),
            domain_delay=(
                crawler.get("domain_delay_min", 0.5),
                crawler.get("domain_delay_max", 1.5),
            ),
            spread_seconds=spread,
        )

    crawl_interval = crawler_cfg.get("fetch_interval_minutes", 3)
    scheduler.add_job(
        crawl_job,
        "interval",
        minutes=crawl_interval,
        id="crawl_all",
        max_instances=1,
        coalesce=True,
    )

    if ai_cfg.get("enabled", True):
        async def ai_job():
            current_cfg = _load_config()
            ai = current_cfg.get("ai", {})
            wh = current_cfg.get("webhook", {})
            tg = current_cfg.get("telegram", {})
            if not ai.get("enabled", True):
                return
            endpoints = wh.get("endpoints", [])
            tg_channels = tg.get("channels", [])
            ai_interval_sec = ai.get("interval_minutes", 2) * 60
            spread = ai_interval_sec * 0.8

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
            )
            if processed:
                logger.info(f"AI job: processed {processed} articles")

        ai_interval = ai_cfg.get("interval_minutes", 2)
        scheduler.add_job(
            ai_job,
            "interval",
            minutes=ai_interval,
            id="ai_rewrite",
            max_instances=1,
            coalesce=True,
        )

    _scheduler = scheduler
    return scheduler
