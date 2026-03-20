"""
Async fetcher: crawl tất cả sources song song, dedup, lưu vào Redis.
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx
import redis.asyncio as aioredis

from crawler.dedup import check_duplicate
from crawler.rss_parser import Article, parse_rss_feed
from storage.redis_store import save_article
from storage.sqlite_stats import log_crawl_result

logger = logging.getLogger(__name__)


async def fetch_source(
    source: dict,
    client: httpx.AsyncClient,
    redis: aioredis.Redis,
    dedup_threshold: int = 3,
    max_articles: int = 50,
) -> dict:
    """
    Crawl 1 source: fetch → parse → dedup → save.
    Returns stats dict.
    """
    started_at = datetime.now(timezone.utc)
    stats = {
        "source_id": source["id"],
        "found": 0,
        "saved": 0,
        "duplicates": 0,
        "errors": 0,
        "error_msg": None,
    }

    try:
        articles = await parse_rss_feed(source, client, max_articles=max_articles)
        stats["found"] = len(articles)

        for article in articles:
            result = await check_duplicate(redis, article.title, threshold=dedup_threshold)
            if result.is_duplicate:
                stats["duplicates"] += 1
                continue

            await save_article(redis, article)
            stats["saved"] += 1

    except Exception as e:
        stats["errors"] = 1
        stats["error_msg"] = str(e)
        logger.warning(f"Source {source['id']} failed: {e}")

    await log_crawl_result(source_id=source["id"], stats=stats, started_at=started_at)
    return stats


async def fetch_all_sources(
    sources: list[dict],
    redis: aioredis.Redis,
    max_concurrent: int = 20,
    dedup_threshold: int = 3,
    max_articles_per_source: int = 50,
    request_timeout: int = 15,
) -> list[dict]:
    """
    Crawl tất cả sources song song (semaphore giới hạn concurrency).
    Returns list of stats per source.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=request_timeout,
        headers={"User-Agent": "NewsAggregator/1.0 (+https://github.com/your/repo)"},
    ) as client:

        async def _fetch_with_sem(source: dict) -> dict:
            async with semaphore:
                return await fetch_source(
                    source, client, redis,
                    dedup_threshold=dedup_threshold,
                    max_articles=max_articles_per_source,
                )

        tasks = [_fetch_with_sem(s) for s in sources if s.get("enabled", True)]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    total_saved = sum(r["saved"] for r in results)
    total_found = sum(r["found"] for r in results)
    logger.info(f"Crawl done: {total_found} found, {total_saved} saved from {len(results)} sources")

    return results
