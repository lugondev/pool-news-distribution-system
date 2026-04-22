"""
Redis storage cho articles.

Key structure:
  news:{article_id}          → Hash (article fields), TTL 24h
  news:crawl:schedule        → Sorted Set (score=next_crawl_at unix ts, member=source_id)
  news:feed                  → Sorted Set (score=timestamp, member=article_id)
  news:feed:{YYYYMMDD}       → Sorted Set daily
  news:feed:{YYYYMMDDHH}     → Sorted Set hourly
  news:source:{source_id}    → Sorted Set per source
"""

import json
import logging
import time
from datetime import datetime, timezone

import redis.asyncio as aioredis

from crawler.rss_parser import Article
from storage.redis_keys import (
    ARTICLE_TTL_SECONDS as TTL_SECONDS,
    AI_PENDING_KEY,
    CRAWL_SCHEDULE_KEY,
    ENRICH_PENDING_KEY,
    EMBED_PREFIX,
    DEDUP_TTL_SECONDS,
)
from ai.scorer import score_article
from storage.config_cache import cached_yaml

logger = logging.getLogger(__name__)

_SETTINGS_PATH = "config/settings.yaml"

# Backpressure cache: avoid config reload + ZCARD on every save_article call.
# Refreshes every 30s and only runs the ZCARD check every 10 saves.
_bp_cache: dict = {"cfg": None, "ts": 0.0, "counter": 0}


def _load_queue_config() -> dict:
    return cached_yaml(_SETTINGS_PATH).get("scoring", {})


def _ts(dt: datetime) -> float:
    return (
        dt.replace(tzinfo=timezone.utc).timestamp()
        if dt.tzinfo is None
        else dt.timestamp()
    )


def _article_to_hash(article: Article, priority_score: float | None = None) -> dict:
    data = {
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
        "type": article.type,
        "entities": json.dumps(article.entities),
        "sentiment": article.sentiment,
        "topic_id": article.topic_id,
        "ai_enrich_status": article.ai_enrich_status,
    }
    if priority_score is not None:
        data["priority_score"] = str(round(priority_score, 2))
    return data


def _hash_to_article(data: dict) -> dict:
    return data  # return as dict, consumers decide what to do


async def save_article(redis: aioredis.Redis, article: Article) -> None:
    """Save article to Redis với TTL và index vào các Sorted Sets."""
    key = f"news:{article.id}"

    # Fetch only the fields we need to decide overwrite logic — batch with HMGET
    # to avoid a separate HGET round-trip before the main pipeline.
    existing_vals = await redis.hmget(key, "ai_status", "ai_enrich_status")
    existing_status_raw, existing_enrich_raw = existing_vals
    existing_status = existing_status_raw.decode() if existing_status_raw else None
    already_processed = existing_status and existing_status != "pending"

    ts = _ts(article.published_at)
    now = article.fetched_at
    date_key = now.strftime("%Y%m%d")
    hour_key = now.strftime("%Y%m%d%H")

    # Compute priority score (timestamp + weighted bonus)
    # Falls back to raw ts when scoring disabled/misconfigured
    try:
        priority_score = score_article(article)
    except Exception:
        priority_score = ts

    hash_data = _article_to_hash(article, priority_score=priority_score if not already_processed else None)
    if already_processed:
        # Preserve AI results — don't overwrite with empty fields from re-crawl
        hash_data.pop("ai_summary_vi", None)
        hash_data.pop("ai_summary_en", None)
        hash_data.pop("ai_status", None)

    pipe = redis.pipeline()
    pipe.hset(key, mapping=hash_data)
    pipe.expire(key, TTL_SECONDS)

    # Global feed (score = publish timestamp — display order unchanged)
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

    # AI pending queue — priority score as sorted set score
    # ZPOPMAX in pop_pending_ai_articles picks highest score first
    if not already_processed:
        pipe.zadd(AI_PENDING_KEY, {article.id: priority_score})
        pipe.expire(AI_PENDING_KEY, TTL_SECONDS)

    # Enqueue for enrichment if not already done — check fetched via HMGET above.
    # Gộp vào cùng pipeline để giảm 2 RTTs riêng biệt.
    if not existing_enrich_raw or existing_enrich_raw.decode() != "done":
        pipe.sadd(ENRICH_PENDING_KEY, article.id)
        pipe.expire(ENRICH_PENDING_KEY, DEDUP_TTL_SECONDS)

    await pipe.execute()

    # Backpressure: trim lowest-priority articles when queue exceeds cap
    if not already_processed:
        await _apply_backpressure(redis)


async def _apply_backpressure(redis: aioredis.Redis) -> None:
    """
    Drop lowest-priority articles from the AI queue when it exceeds queue_max_size.

    Uses ZPOPMIN to remove the least important articles (lowest score = oldest
    or lowest-signal). This prevents unbounded queue growth during crawl spikes
    and ensures high-priority articles are never starved.

    Optimised: config reloads at most every 30s; ZCARD runs every 10 saves
    to avoid a Redis round-trip on every single article save.
    """
    try:
        # Reset counter every 1000 to prevent overflow
        _bp_cache["counter"] = (_bp_cache["counter"] + 1) % 1000
        # Skip ZCARD check on every save — only check every 10 saves
        if _bp_cache["counter"] % 10 != 0:
            return

        now = time.monotonic()
        if now - _bp_cache["ts"] > 30:
            _bp_cache["cfg"] = _load_queue_config()
            _bp_cache["ts"] = now

        cfg = _bp_cache["cfg"] or {}
        max_size = int(cfg.get("queue_max_size", 0))
        if max_size <= 0:
            return
        queue_size = await redis.zcard(AI_PENDING_KEY)
        if queue_size > max_size:
            overflow = queue_size - max_size
            dropped = await redis.zpopmin(AI_PENDING_KEY, overflow)
            if dropped:
                logger.debug(
                    f"Backpressure: dropped {len(dropped)} low-priority articles "
                    f"(queue was {queue_size}, cap={max_size})"
                )
    except Exception as e:
        logger.warning(f"Backpressure check failed: {e}")


async def get_article(redis: aioredis.Redis, article_id: str) -> dict | None:
    data = await redis.hgetall(f"news:{article_id}")
    if not data:
        return None
    return {k.decode(): v.decode() for k, v in data.items()}


async def get_articles_batch(
    redis: aioredis.Redis, ids: list[str], chunk_size: int = 100
) -> list[dict]:
    """
    Fetch multiple articles in chunked pipelines.

    Chunking keeps each pipeline under chunk_size commands to avoid large
    memory allocations on both client and server side.
    """
    results: list[dict] = []
    for i in range(0, len(ids), chunk_size):
        pipe = redis.pipeline()
        for aid in ids[i : i + chunk_size]:
            pipe.hgetall(f"news:{aid}")
        batch = await pipe.execute()
        for raw in batch:
            if raw:
                results.append({k.decode(): v.decode() for k, v in raw.items()})
    return results


async def update_article_content(
    redis: aioredis.Redis, article_id: str, content: str
) -> None:
    """Update content field for an existing article (used by defuddle enrichment)."""
    await redis.hset(f"news:{article_id}", "content", content)


async def update_article_ai(
    redis: aioredis.Redis,
    article_id: str,
    summaries: dict[str, str],
) -> None:
    """Store AI summaries. summaries = {lang_code: text, ...}"""
    mapping: dict = {"ai_status": "done"}
    for lang, text in summaries.items():
        mapping[f"ai_summary_{lang}"] = text
    await redis.hset(f"news:{article_id}", mapping=mapping)
    # Auto-enqueue for Phase 2 enrichment (entity extraction, embedding, clustering)
    await redis.sadd(ENRICH_PENDING_KEY, article_id)
    await redis.expire(ENRICH_PENDING_KEY, DEDUP_TTL_SECONDS)


async def update_article_ai_config(
    redis: aioredis.Redis,
    article_id: str,
    summaries: dict[str, str],
    config_id: str,
) -> None:
    """Store AI summaries for a specific (config, lang) group."""
    mapping: dict = {}
    for lang, text in summaries.items():
        mapping[f"ai_{config_id}_{lang}"] = text
    if mapping:
        await redis.hset(f"news:{article_id}", mapping=mapping)


async def get_latest_articles(
    redis: aioredis.Redis,
    limit: int = 50,
    offset: int = 0,
    source_id: str | None = None,
    category: str | None = None,
    article_type: str | None = None,  # NEW: "original", "synthetic", or None (all)
) -> tuple[list[dict], int]:
    """Lấy tin mới nhất (score DESC = newest first). Returns (articles, total_count)."""
    if source_id:
        feed_key = f"news:source:{source_id}"
    elif category:
        # If filtering by type, use type-specific feed
        if article_type == "synthetic":
            feed_key = f"news:synth:cat:{category}"
        else:
            feed_key = f"news:cat:{category}"
    elif article_type == "synthetic":
        feed_key = "news:synth:feed"
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
            article = {k.decode(): v.decode() for k, v in raw.items()}
            # Apply type filter if specified and not using type-specific feed
            if article_type == "original" and article.get("type") == "synthetic":
                continue
            articles.append(article)
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


async def pop_pending_ai_articles(redis: aioredis.Redis, limit: int = 10) -> list[dict]:
    """
    Atomically pop up to `limit` newest articles from the AI pending queue.
    ZPOPMAX removes items from the sorted set while returning them, so the
    same article cannot be picked up by two concurrent jobs.
    """
    items = await redis.zpopmax(AI_PENDING_KEY, limit)
    if not items:
        return []

    pipe = redis.pipeline()
    for aid_bytes, _score in items:
        aid = aid_bytes.decode() if isinstance(aid_bytes, bytes) else aid_bytes
        pipe.hgetall(f"news:{aid}")
    results = await pipe.execute()

    articles = []
    for raw in results:
        if raw:
            articles.append({k.decode(): v.decode() for k, v in raw.items()})
    return articles


async def get_due_source_ids(
    redis: aioredis.Redis,
    all_source_ids: list[str],
    limit: int,
) -> list[str]:
    """Return up to `limit` source IDs whose next crawl time is due.

    Uses a Sorted Set (score = unix timestamp) as the schedule store.
    Sources not yet in the schedule (first run) are treated as immediately due.
    """
    now = datetime.now(timezone.utc).timestamp()
    active_ids_set = set(all_source_ids)

    # Sources already scheduled and overdue — filter to active sources only
    # (disabled sources may still exist in Redis schedule from previous runs)
    raw_due = await redis.zrangebyscore(
        CRAWL_SCHEDULE_KEY, 0, now, start=0, num=limit * 5
    )
    due = [
        sid
        for b in raw_due
        for sid in [(b.decode() if isinstance(b, bytes) else b)]
        if sid in active_ids_set
    ]

    # Sources never scheduled (brand new or after Redis flush)
    all_raw = await redis.zrange(CRAWL_SCHEDULE_KEY, 0, -1)
    already_scheduled = {b.decode() if isinstance(b, bytes) else b for b in all_raw}
    unscheduled = [sid for sid in all_source_ids if sid not in already_scheduled]

    return (due + unscheduled)[:limit]


async def set_source_next_crawl(
    redis: aioredis.Redis, source_id: str, next_ts: float
) -> None:
    """Schedule a source's next crawl at the given Unix timestamp."""
    await redis.zadd(CRAWL_SCHEDULE_KEY, {source_id: next_ts})


async def get_feed_stats(redis: aioredis.Redis) -> dict:
    """Stats tổng quan từ Redis — dùng pipeline để giảm RTTs."""
    now = datetime.now(timezone.utc)
    date_key = now.strftime("%Y%m%d")
    hour_key = now.strftime("%Y%m%d%H")
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    pipe = redis.pipeline()
    pipe.zcard("news:feed")
    pipe.zcard(f"news:feed:{date_key}")
    pipe.zcard(f"news:feed:{hour_key}")
    pipe.zcard("news:synth:feed")
    pipe.zcount("news:synth:feed", today_start, "+inf")
    total, today, this_hour, synthetic_total, synthetic_today = await pipe.execute()

    return {
        "total_in_redis": total,
        "today": today,
        "this_hour": this_hour,
        "synthetic_total": synthetic_total,
        "synthetic_today": synthetic_today,
    }


# ---------------------------------------------------------------------------
# Phase 2 — Enrichment storage helpers
# ---------------------------------------------------------------------------

async def pop_pending_enrichments(
    redis: aioredis.Redis, count: int = 20
) -> list[str]:
    """Atomically pop up to `count` article_ids from the enrichment queue.

    Uses SPOP count (single command) instead of N individual SPOP calls
    in a pipeline — reduces both round-trips and server-side overhead.
    """
    raw = await redis.spop(ENRICH_PENDING_KEY, count)
    if not raw:
        return []
    return [r.decode() if isinstance(r, bytes) else r for r in raw]


async def save_article_enrichment(
    redis: aioredis.Redis,
    article_id: str,
    entities: list[str],
    sentiment: str,
    topic_id: str,
) -> None:
    """Persist enrichment results (entities, sentiment, topic_id) back onto the article hash."""
    await redis.hset(f"news:{article_id}", mapping={
        "entities": json.dumps(entities),
        "sentiment": sentiment,
        "topic_id": topic_id,
        "ai_enrich_status": "done",
    })


async def save_embedding(
    redis: aioredis.Redis,
    article_id: str,
    embedding: list[float],
) -> None:
    """Store the embedding vector separately (not in article hash — too large).
    
    For batch operations, use save_embeddings_batch() instead (10× faster).
    """
    key = f"{EMBED_PREFIX}{article_id}"
    await redis.set(key, json.dumps(embedding), ex=TTL_SECONDS)


async def save_articles_batch(redis: aioredis.Redis, articles: list[Article]) -> None:
    """
    Save multiple articles using Redis pipeline (20× faster than sequential saves).
    
    Performance:
        - Sequential: 10 articles × 2 RTTs = 20 RTTs
        - Batch: 1 RTT total
    
    Note: Backpressure is applied once at the end (not per-article).
    """
    if not articles:
        return
    
    pipe = redis.pipeline()
    
    for article in articles:
        key = f"news:{article.id}"
        
        # Note: We skip the HMGET check for existing status in batch mode
        # This is acceptable because batch saves are typically for new articles from crawl
        ts = _ts(article.published_at)
        now = article.fetched_at
        date_key = now.strftime("%Y%m%d")
        hour_key = now.strftime("%Y%m%d%H")
        
        # Compute priority score
        try:
            priority_score = score_article(article)
        except Exception:
            priority_score = ts
        
        hash_data = _article_to_hash(article, priority_score=priority_score)
        
        # Article hash
        pipe.hset(key, mapping=hash_data)
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
        
        # AI pending queue
        pipe.zadd(AI_PENDING_KEY, {article.id: priority_score})
        pipe.expire(AI_PENDING_KEY, TTL_SECONDS)
        
        # Enrich pending queue
        pipe.sadd(ENRICH_PENDING_KEY, article.id)
        pipe.expire(ENRICH_PENDING_KEY, DEDUP_TTL_SECONDS)
    
    await pipe.execute()
    
    # Apply backpressure once for the entire batch
    await _apply_backpressure(redis)


async def save_embeddings_batch(
    redis: aioredis.Redis,
    embeddings: list[tuple[str, list[float]]]  # [(article_id, embedding), ...]
) -> None:
    """
    Save multiple embeddings using Redis pipeline.
    
    Args:
        embeddings: List of (article_id, embedding_vector) tuples
    
    Performance:
        - Sequential: 10 embeddings × 1 RTT = 10 RTTs
        - Batch: 1 RTT total (10× speedup)
    """
    if not embeddings:
        return
    
    pipe = redis.pipeline()
    
    for article_id, embedding in embeddings:
        key = f"{EMBED_PREFIX}{article_id}"
        pipe.set(key, json.dumps(embedding), ex=TTL_SECONDS)
    
    await pipe.execute()


async def get_embedding(
    redis: aioredis.Redis,
    article_id: str,
) -> list[float] | None:
    raw = await redis.get(f"{EMBED_PREFIX}{article_id}")
    if not raw:
        return None
    return json.loads(raw)
