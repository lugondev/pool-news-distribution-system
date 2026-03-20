"""
Redis storage cho articles.

Key structure:
  news:{article_id}          → Hash (article fields), TTL 24h
  news:feed                  → Sorted Set (score=timestamp, member=article_id)
  news:feed:{YYYYMMDD}       → Sorted Set daily
  news:feed:{YYYYMMDDHH}     → Sorted Set hourly
  news:source:{source_id}    → Sorted Set per source
"""
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis

from crawler.rss_parser import Article


TTL_SECONDS = 86400  # 24h


def _ts(dt: datetime) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()


def _article_to_hash(article: Article) -> dict:
    return {
        "id": article.id,
        "source_id": article.source_id,
        "source_name": article.source_name,
        "url": article.url,
        "title": article.title,
        "summary": article.summary,
        "content": article.content,
        "lang": article.lang,
        "declared_lang": article.declared_lang,
        "category": article.category,
        "published_at": article.published_at.isoformat(),
        "fetched_at": article.fetched_at.isoformat(),
        "ai_summary_vi": article.ai_summary_vi,
        "ai_summary_en": article.ai_summary_en,
        "ai_status": article.ai_status,
    }


def _hash_to_article(data: dict) -> dict:
    return data  # return as dict, consumers decide what to do


async def save_article(redis: aioredis.Redis, article: Article) -> None:
    """Save article to Redis với TTL và index vào các Sorted Sets."""
    key = f"news:{article.id}"
    ts = _ts(article.published_at)

    now = article.fetched_at
    date_key = now.strftime("%Y%m%d")
    hour_key = now.strftime("%Y%m%d%H")

    pipe = redis.pipeline()
    pipe.hset(key, mapping=_article_to_hash(article))
    pipe.expire(key, TTL_SECONDS)

    # Global feed
    pipe.zadd("news:feed", {article.id: ts})
    pipe.expire("news:feed", TTL_SECONDS)

    # Daily feed
    pipe.zadd(f"news:feed:{date_key}", {article.id: ts})
    pipe.expire(f"news:feed:{date_key}", TTL_SECONDS)

    # Hourly feed
    pipe.zadd(f"news:feed:{hour_key}", {article.id: ts})
    pipe.expire(f"news:feed:{hour_key}", TTL_SECONDS)

    # Per-source
    pipe.zadd(f"news:source:{article.source_id}", {article.id: ts})
    pipe.expire(f"news:source:{article.source_id}", TTL_SECONDS)

    # Per-category
    pipe.zadd(f"news:cat:{article.category}", {article.id: ts})
    pipe.expire(f"news:cat:{article.category}", TTL_SECONDS)

    await pipe.execute()


async def get_article(redis: aioredis.Redis, article_id: str) -> dict | None:
    data = await redis.hgetall(f"news:{article_id}")
    if not data:
        return None
    return {k.decode(): v.decode() for k, v in data.items()}


async def update_article_ai(
    redis: aioredis.Redis,
    article_id: str,
    ai_summary_vi: str,
    ai_summary_en: str,
) -> None:
    await redis.hset(f"news:{article_id}", mapping={
        "ai_summary_vi": ai_summary_vi,
        "ai_summary_en": ai_summary_en,
        "ai_status": "done",
    })


async def get_latest_articles(
    redis: aioredis.Redis,
    limit: int = 50,
    offset: int = 0,
    source_id: str | None = None,
    category: str | None = None,
) -> tuple[list[dict], int]:
    """Lấy tin mới nhất (score DESC = newest first). Returns (articles, total_count)."""
    if source_id:
        feed_key = f"news:source:{source_id}"
    elif category:
        feed_key = f"news:cat:{category}"
    else:
        feed_key = "news:feed"
    total = await redis.zcard(feed_key)
    ids = await redis.zrevrange(feed_key, offset, offset + limit - 1)
    if not ids:
        return [], total

    pipe = redis.pipeline()
    for aid in ids:
        pipe.hgetall(f"news:{aid.decode()}")
    results = await pipe.execute()

    articles = []
    for raw in results:
        if raw:
            articles.append({k.decode(): v.decode() for k, v in raw.items()})
    return articles, total


async def get_pending_ai_articles(redis: aioredis.Redis, limit: int = 20) -> list[dict]:
    """Lấy các bài chưa được AI xử lý."""
    ids = await redis.zrevrange("news:feed", 0, limit * 3 - 1)
    articles = []
    pipe = redis.pipeline()
    for aid in ids:
        pipe.hgetall(f"news:{aid.decode()}")
    results = await pipe.execute()

    for raw in results:
        if not raw:
            continue
        article = {k.decode(): v.decode() for k, v in raw.items()}
        if article.get("ai_status") == "pending":
            articles.append(article)
        if len(articles) >= limit:
            break
    return articles


async def get_feed_stats(redis: aioredis.Redis) -> dict:
    """Stats tổng quan từ Redis."""
    total = await redis.zcard("news:feed")
    now = datetime.now(timezone.utc)
    date_key = now.strftime("%Y%m%d")
    hour_key = now.strftime("%Y%m%d%H")
    today = await redis.zcard(f"news:feed:{date_key}")
    this_hour = await redis.zcard(f"news:feed:{hour_key}")
    return {
        "total_in_redis": total,
        "today": today,
        "this_hour": this_hour,
    }
