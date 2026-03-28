"""
Trend Detection — velocity-based trending analysis across categories and entities.

Algorithm (per category):
  velocity_1h  = articles in last 1 hour  (from news:cat:{category} sorted set)
  baseline_3h  = articles in last 3 hours ÷ 3  (rolling hourly average)
  trend_ratio  = velocity_1h / max(baseline_3h, 1.0)
  trending     = ratio > TREND_THRESHOLD

Entity trending:
  Scan all articles from the last 1h → count entity frequency.
  Entities appearing in ≥ MIN_ENTITY_COUNT articles are "trending entities".

Redis layout written:
  news:trend:cat:{category}   → Hash {velocity_1h, baseline_3h, ratio, updated_at, top_entities}
  news:trends:ranking         → Sorted Set (score=ratio, member=category)
  news:trend:entities         → Sorted Set (score=count, member=entity_name)
"""

import json
import logging
from collections import Counter
from datetime import datetime, timezone

import redis.asyncio as aioredis

from storage.redis_keys import (
    TREND_CAT_PREFIX,
    TRENDS_RANKING_KEY,
    TREND_ENTITIES_KEY,
    TREND_TTL_SECONDS,
    ARTICLE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────
TREND_THRESHOLD = 2.0        # ratio above which a category is "trending"
MIN_ENTITY_COUNT = 2         # min articles an entity must appear in to be "trending"
MAX_TRENDING_ENTITIES = 30   # top-N entities to store
WINDOW_1H = 3600
WINDOW_3H = 10800


# ── Core computation ──────────────────────────────────────────────────────────

async def compute_category_trend(
    redis: aioredis.Redis,
    category: str,
    now_ts: float,
) -> dict:
    """
    Compute trend metrics for one category.
    Returns dict with velocity_1h, baseline_3h, ratio, trending (bool).
    """
    cat_key = f"news:cat:{category}"

    # Count articles in last 1h and last 3h using sorted set score range
    velocity_1h = await redis.zcount(cat_key, now_ts - WINDOW_1H, now_ts)
    count_3h = await redis.zcount(cat_key, now_ts - WINDOW_3H, now_ts)

    baseline_3h = count_3h / 3.0
    ratio = velocity_1h / max(baseline_3h, 1.0)

    return {
        "velocity_1h": int(velocity_1h),
        "baseline_3h": round(baseline_3h, 2),
        "ratio": round(ratio, 2),
        "trending": ratio > TREND_THRESHOLD,
    }


async def collect_trending_entities(
    redis: aioredis.Redis,
    categories: list[str],
    now_ts: float,
) -> list[tuple[str, int]]:
    """
    Scan articles published in the last 1h across all categories.
    Return [(entity_name, count)] sorted by count DESC.
    """
    entity_counter: Counter = Counter()

    for category in categories:
        cat_key = f"news:cat:{category}"
        # Get article_ids from last 1h
        raw_ids = await redis.zrangebyscore(cat_key, now_ts - WINDOW_1H, now_ts)
        if not raw_ids:
            continue

        article_ids = [b.decode() if isinstance(b, bytes) else b for b in raw_ids]

        pipe = redis.pipeline()
        for aid in article_ids:
            pipe.hget(f"news:{aid}", "entities")
        results = await pipe.execute()

        for raw in results:
            if not raw:
                continue
            try:
                entities: list[str] = json.loads(
                    raw.decode() if isinstance(raw, bytes) else raw
                )
                for entity in entities:
                    entity_counter[entity.strip()] += 1
            except (json.JSONDecodeError, AttributeError):
                pass

    return entity_counter.most_common(MAX_TRENDING_ENTITIES)


# ── Persistence ───────────────────────────────────────────────────────────────

async def _persist_category_trend(
    redis: aioredis.Redis,
    category: str,
    metrics: dict,
    top_entities: list[tuple[str, int]],
    now_iso: str,
) -> None:
    cat_trend_key = f"{TREND_CAT_PREFIX}{category}"
    pipe = redis.pipeline()
    pipe.hset(cat_trend_key, mapping={
        "velocity_1h": metrics["velocity_1h"],
        "baseline_3h": metrics["baseline_3h"],
        "ratio": metrics["ratio"],
        "trending": "1" if metrics["trending"] else "0",
        "updated_at": now_iso,
        "top_entities": json.dumps([e for e, _ in top_entities[:5]]),
    })
    pipe.expire(cat_trend_key, TREND_TTL_SECONDS)
    pipe.zadd(TRENDS_RANKING_KEY, {category: metrics["ratio"]})
    pipe.expire(TRENDS_RANKING_KEY, TREND_TTL_SECONDS)
    await pipe.execute()


# ── Public API ────────────────────────────────────────────────────────────────

async def run_trend_detection(
    redis: aioredis.Redis,
    categories: list[str],
) -> dict:
    """
    Run full trend detection pass across all categories.
    Returns summary dict for logging.

    Called by trend_job in scheduler.py every N minutes.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    now_iso = datetime.now(timezone.utc).isoformat()

    trending_cats: list[str] = []
    results: dict[str, dict] = {}

    # Step 1 — per-category velocity
    for category in categories:
        metrics = await compute_category_trend(redis, category, now_ts)
        results[category] = metrics
        if metrics["trending"]:
            trending_cats.append(category)

    # Step 2 — entity frequency across all categories
    entity_counts = await collect_trending_entities(redis, categories, now_ts)

    # Step 3 — persist
    for category, metrics in results.items():
        cat_top_entities = [
            (e, c) for e, c in entity_counts
        ]
        await _persist_category_trend(redis, category, metrics, cat_top_entities, now_iso)

    # Step 4 — persist global trending entity sorted set
    if entity_counts:
        pipe = redis.pipeline()
        pipe.delete(TREND_ENTITIES_KEY)
        pipe.zadd(TREND_ENTITIES_KEY, {e: c for e, c in entity_counts if c >= MIN_ENTITY_COUNT})
        pipe.expire(TREND_ENTITIES_KEY, TREND_TTL_SECONDS)
        await pipe.execute()

    logger.info(
        f"[trend] {len(trending_cats)} trending categories: {trending_cats} | "
        f"top entities: {[e for e, _ in entity_counts[:5]]}"
    )

    return {
        "trending_categories": trending_cats,
        "all_ratios": {c: m["ratio"] for c, m in results.items()},
        "top_entities": entity_counts[:10],
    }


async def get_trend_snapshot(
    redis: aioredis.Redis,
    limit: int = 20,
) -> list[dict]:
    """
    Return categories sorted by trend ratio DESC.
    Used by the Intelligence dashboard.
    """
    raw = await redis.zrevrange(TRENDS_RANKING_KEY, 0, limit - 1, withscores=True)
    if not raw:
        return []

    categories = [(b.decode() if isinstance(b, bytes) else b, score) for b, score in raw]

    pipe = redis.pipeline()
    for cat, _ in categories:
        pipe.hgetall(f"{TREND_CAT_PREFIX}{cat}")
    results = await pipe.execute()

    snapshot = []
    for (cat, ratio), raw_hash in zip(categories, results):
        if raw_hash:
            item = {k.decode(): v.decode() for k, v in raw_hash.items()}
            item["category"] = cat
            item["ratio"] = float(item.get("ratio", ratio))
            item["trending"] = item.get("trending") == "1"
            try:
                item["top_entities"] = json.loads(item.get("top_entities", "[]"))
            except json.JSONDecodeError:
                item["top_entities"] = []
            snapshot.append(item)
    return snapshot


async def get_trending_entities(
    redis: aioredis.Redis,
    limit: int = 20,
) -> list[tuple[str, int]]:
    """Return [(entity, count)] sorted by count DESC."""
    raw = await redis.zrevrange(TREND_ENTITIES_KEY, 0, limit - 1, withscores=True)
    return [
        (b.decode() if isinstance(b, bytes) else b, int(score))
        for b, score in raw
    ]
