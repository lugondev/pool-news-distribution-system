"""
Async fetcher: crawl tất cả sources song song, dedup, lưu vào Redis.
Per-domain rate limiting + random delays to avoid IP bans.
"""

import asyncio
import logging
import random
import time
import weakref
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import redis.asyncio as aioredis

from crawler.content_extractor import enrich_article_content, needs_enrichment
from crawler.dedup import check_duplicate
from crawler.rss_parser import Article, _make_article_id, parse_rss_feed
from storage.redis_store import (
    get_article,
    save_article,
    set_source_next_crawl,
    update_article_content,
)
from storage.sqlite_stats import log_crawl_result

logger = logging.getLogger(__name__)

# Import WebSocket manager - optional to avoid circular dependency
try:
    from realtime.manager import ws_manager
except ImportError:
    ws_manager = None

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
]

# WeakValueDictionary: lock entries are garbage-collected automatically when
# no coroutine holds the lock — prevents unbounded memory growth over days.
_domain_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _get_domain_lock(domain: str) -> asyncio.Lock:
    lock = _domain_locks.get(domain)
    if lock is None:
        lock = asyncio.Lock()
        _domain_locks[domain] = lock
    return lock


def compute_next_crawl_ts(
    http_status: int | None,
    default_sec: int,
    backoff_429_sec: int = 1800,
    backoff_403_sec: int = 7200,
) -> float:
    """Compute Unix timestamp for a source's next crawl based on HTTP response.

    - 429 Too Many Requests → back off 30 min (default)
    - 403 Forbidden         → back off 2 h   (default) — likely IP block
    - 5xx server error      → back off 2× default interval, max 10 min
    - success / other       → use default crawl interval
    """
    now = time.time()
    if http_status == 429:
        return now + backoff_429_sec
    if http_status == 403:
        return now + backoff_403_sec
    if http_status is not None and http_status >= 500:
        return now + min(default_sec * 2, 600)
    return now + default_sec


def _get_domain(url: str) -> str:
    """Extract base domain for rate-limiting (e.g. cnbc.com from www.cnbc.com/id/...)."""
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


async def fetch_source(
    source: dict,
    client: httpx.AsyncClient,
    redis: aioredis.Redis,
    dedup_threshold: int = 3,
    max_articles: int = 50,
    domain_delay: tuple[float, float] = (1.0, 3.0),
    enrich_content: bool = True,
) -> dict:
    """
    Crawl 1 source: per-domain lock → random delay → fetch → parse → dedup → save.
    """
    started_at = datetime.now(timezone.utc)
    domain = _get_domain(source["url"])
    source_name = source.get("name", source["id"])
    stats = {
        "source_id": source["id"],
        "domain": domain,
        "http_status": None,
        "found": 0,
        "saved": 0,
        "duplicates": 0,
        "errors": 0,
        "error_msg": None,
    }

    if ws_manager:
        asyncio.create_task(
            ws_manager.emit_crawl_start(source["id"], source_name, source["url"])
        )

    async with _get_domain_lock(domain):
        await asyncio.sleep(random.uniform(*domain_delay))

        try:
            articles = await parse_rss_feed(source, client, max_articles=max_articles)
            stats["http_status"] = 200
            stats["found"] = len(articles)

            for article in articles:
                result = await check_duplicate(
                    redis, article.title, threshold=dedup_threshold
                )
                if result.is_duplicate:
                    stats["duplicates"] += 1
                    # Enrich existing Redis article if its content is still thin
                    if enrich_content and needs_enrichment(
                        article.content, article.summary
                    ):
                        article_id = _make_article_id(source["id"], article.url)
                        existing = await get_article(redis, article_id)
                        if existing and needs_enrichment(
                            existing.get("content", ""), existing.get("summary", "")
                        ):
                            enriched = await enrich_article_content(
                                article.url,
                                existing.get("content", ""),
                                existing.get("summary", ""),
                                client,
                            )
                            if enriched != existing.get("content", ""):
                                await update_article_content(
                                    redis, article_id, enriched
                                )
                    continue

                if enrich_content:
                    article.content = await enrich_article_content(
                        article.url, article.content, article.summary, client
                    )

                await save_article(redis, article)
                stats["saved"] += 1
                if ws_manager:
                    asyncio.create_task(
                        ws_manager.emit_article_saved(
                            article.id,
                            article.title,
                            source["id"],
                            source.get("category", ""),
                            False,
                        )
                    )

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            stats["http_status"] = status
            stats["errors"] = 1
            stats["error_msg"] = f"HTTP {status}: {e}"
            if status == 429:
                logger.warning(f"[{source['id']}] rate-limited (429) from {domain}")
            elif status == 403:
                logger.warning(
                    f"[{source['id']}] forbidden (403) from {domain}, possible IP block"
                )
            else:
                logger.warning(f"[{source['id']}] HTTP {status} from {domain}")

        except Exception as e:
            stats["errors"] = 1
            stats["error_msg"] = str(e)[:500]
            logger.warning(f"[{source['id']}] failed ({domain}): {e}")

    await log_crawl_result(source_id=source["id"], stats=stats, started_at=started_at)

    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    if ws_manager:
        if stats["errors"] == 0:
            asyncio.create_task(
                ws_manager.emit_crawl_success(
                    source["id"], source_name,
                    stats["found"], stats["duplicates"], duration_ms,
                )
            )
        else:
            asyncio.create_task(
                ws_manager.emit_crawl_error(
                    source["id"], source_name, stats.get("error_msg", "unknown error")
                )
            )

    return stats


async def fetch_all_sources(
    sources: list[dict],
    redis: aioredis.Redis,
    max_concurrent: int = 3,
    dedup_threshold: int = 3,
    max_articles_per_source: int = 50,
    request_timeout: int = 15,
    domain_delay: tuple[float, float] = (0.5, 1.5),
    default_crawl_interval_sec: int = 600,
    backoff_429_sec: int = 1800,
    backoff_403_sec: int = 7200,
) -> list[dict]:
    """
    Crawl sources with controlled concurrency, then update each source's
    next_crawl_at in Redis based on the HTTP response (429/403 → longer backoff).
    """
    enabled = [s for s in sources if s.get("enabled", True)]
    if not enabled:
        return []

    random.shuffle(enabled)
    semaphore = asyncio.Semaphore(max_concurrent)
    ua = random.choice(_USER_AGENTS)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=request_timeout,
        headers={
            "User-Agent": ua,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:

        async def _fetch_with_sem(source: dict) -> dict:
            async with semaphore:
                return await fetch_source(
                    source,
                    client,
                    redis,
                    dedup_threshold=dedup_threshold,
                    max_articles=max_articles_per_source,
                    domain_delay=domain_delay,
                )

        results = await asyncio.gather(
            *[_fetch_with_sem(s) for s in enabled],
            return_exceptions=False,
        )

    # Update crawl schedule for each source based on its response
    for r in results:
        next_ts = compute_next_crawl_ts(
            r.get("http_status"),
            default_sec=default_crawl_interval_sec,
            backoff_429_sec=backoff_429_sec,
            backoff_403_sec=backoff_403_sec,
        )
        await set_source_next_crawl(redis, r["source_id"], next_ts)

    total_saved = sum(r["saved"] for r in results)
    total_found = sum(r["found"] for r in results)
    failed = sum(1 for r in results if r["errors"] > 0)
    logger.info(
        f"Crawl tick done: {total_found} found, {total_saved} saved, "
        f"{failed} failed from {len(results)} sources"
    )

    return results
