"""
Story Detection — groups related articles into ongoing "stories".

A story is NOT the same as a topic cluster.
  - Topic cluster (ai/clusterer.py): semantic similarity via embeddings, ~12h TTL
  - Story (this module): entity-overlap + time window, ~48h TTL, tracks a real event

Algorithm:
  1. New enriched article arrives (has entities list from enricher.py)
  2. Load active stories in same category (from news:stories:cat:{category})
  3. For each candidate story, compute entity overlap score
  4. If max overlap ≥ STORY_MATCH_THRESHOLD AND story < STORY_MAX_AGE_H hours old
       → add article to story, update story entities + last_updated
  5. Otherwise → create new story seeded by this article

Redis layout:
  news:story:{story_id}           → Hash
      headline_vi, headline_en    : top-voted title (from most-recent article)
      category                    : news category
      first_seen                  : ISO timestamp
      last_updated                : ISO timestamp
      article_count               : int (number of articles in story)
      entities                    : JSON list[str] — union of all article entities
      top_sources                 : JSON list[str] — unique source names
      status                      : "active" | "resolved"

  news:story:articles:{story_id}  → Sorted Set (score=unix_ts, member=article_id)
  news:stories:active             → Sorted Set (score=last_updated, member=story_id)
  news:stories:cat:{category}     → Sorted Set (score=last_updated, member=story_id)
"""

import json
import logging
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis

from storage.redis_keys import (
    STORY_PREFIX,
    STORY_ARTICLES_PREFIX,
    STORIES_ACTIVE_KEY,
    STORIES_CAT_PREFIX,
    STORY_TTL_SECONDS,
    ARTICLE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────
STORY_MATCH_THRESHOLD = 0.20   # min entity overlap to join existing story
STORY_MAX_AGE_H = 48           # hours — stories older than this are not joined
STORY_MAX_CANDIDATES = 20      # max stories to compare per category per call


# ── Entity overlap scoring ────────────────────────────────────────────────────

def entity_overlap_score(a: list[str], b: list[str]) -> float:
    """
    Compute overlap score between two entity lists.

    Both lists contain named entities like ["OpenAI", "Sam Altman", "GPT-5"].
    Matching is case-insensitive.

    Multiple valid approaches — choose one:
      • Jaccard:            |A∩B| / |A∪B|   — balanced, penalizes sparse overlap
      • Dice coefficient:   2|A∩B| / (|A|+|B|)  — slightly more lenient
      • Overlap coeff:      |A∩B| / min(|A|,|B|) — best when entity counts differ
        (e.g., breaking news has 2 entities; follow-up has 8 — overlap coeff handles this)

    Return 0.0 if either list is empty (no match possible).

    Parameters
    ----------
    a : entity list from article A
    b : entity list from story (union of all its articles' entities)
    """
    if not a or not b:
        return 0.0
    set_a = {e.lower() for e in a}
    set_b = {e.lower() for e in b}
    intersection = set_a & set_b
    # Overlap coefficient: |A∩B| / min(|A|,|B|)
    # Chosen over Jaccard because `b` is the story's accumulated entity union
    # (grows over time), so normalizing by the smaller set (the new article)
    # avoids penalizing well-developed stories for having many entities.
    return len(intersection) / min(len(set_a), len(set_b))


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _story_key(story_id: str) -> str:
    return f"{STORY_PREFIX}{story_id}"

def _story_articles_key(story_id: str) -> str:
    return f"{STORY_ARTICLES_PREFIX}{story_id}"

def _stories_cat_key(category: str) -> str:
    return f"{STORIES_CAT_PREFIX}{category}"


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Core story operations ─────────────────────────────────────────────────────

async def _load_candidate_stories(
    redis: aioredis.Redis,
    category: str,
    max_age_cutoff: float,
) -> list[tuple[str, list[str]]]:
    """
    Return [(story_id, entities_list)] for active stories in this category
    that were updated after max_age_cutoff.
    """
    cat_key = _stories_cat_key(category)
    raw_ids = await redis.zrangebyscore(cat_key, max_age_cutoff, "+inf")
    if not raw_ids:
        return []

    candidate_ids = [
        (b.decode() if isinstance(b, bytes) else b)
        for b in raw_ids[-STORY_MAX_CANDIDATES:]  # most recent N
    ]

    pipe = redis.pipeline()
    for sid in candidate_ids:
        pipe.hget(_story_key(sid), "entities")
    results = await pipe.execute()

    candidates = []
    for sid, raw_entities in zip(candidate_ids, results):
        if raw_entities:
            entities = json.loads(
                raw_entities.decode() if isinstance(raw_entities, bytes) else raw_entities
            )
            candidates.append((sid, entities))
    return candidates


async def _create_story(
    redis: aioredis.Redis,
    article: dict,
    entities: list[str],
    ts: float,
) -> str:
    """Create a new story seeded by this article. Returns story_id."""
    story_id = uuid.uuid4().hex[:12]
    now = _now_iso()
    story_data = {
        "headline_vi": article.get("title", ""),
        "headline_en": article.get("title", ""),
        "category": article.get("category", "general"),
        "first_seen": now,
        "last_updated": now,
        "article_count": "1",
        "entities": json.dumps(entities),
        "top_sources": json.dumps([article.get("source_name", "")]),
        "status": "active",
    }
    cat_key = _stories_cat_key(article.get("category", "general"))

    pipe = redis.pipeline()
    pipe.hset(_story_key(story_id), mapping=story_data)
    pipe.expire(_story_key(story_id), STORY_TTL_SECONDS)
    pipe.zadd(_story_articles_key(story_id), {article["id"]: ts})
    pipe.expire(_story_articles_key(story_id), STORY_TTL_SECONDS)
    pipe.zadd(STORIES_ACTIVE_KEY, {story_id: ts})
    pipe.expire(STORIES_ACTIVE_KEY, STORY_TTL_SECONDS)
    pipe.zadd(cat_key, {story_id: ts})
    pipe.expire(cat_key, STORY_TTL_SECONDS)
    await pipe.execute()

    logger.debug(f"[story] new story={story_id} category={article.get('category')} entities={entities[:3]}")
    return story_id


async def _join_story(
    redis: aioredis.Redis,
    story_id: str,
    article: dict,
    article_entities: list[str],
    story_entities: list[str],
    ts: float,
) -> None:
    """Add article to an existing story, update entities union + metadata."""
    now = _now_iso()

    # Merge entity sets
    merged = list({e.lower(): e for e in (story_entities + article_entities)}.values())

    # Fetch current source list
    raw_sources = await redis.hget(_story_key(story_id), "top_sources")
    sources: list[str] = json.loads(
        raw_sources.decode() if isinstance(raw_sources, bytes) else (raw_sources or "[]")
    )
    src = article.get("source_name", "")
    if src and src not in sources:
        sources.append(src)

    pipe = redis.pipeline()
    pipe.hset(_story_key(story_id), mapping={
        "headline_vi": article.get("title", ""),  # latest article becomes headline
        "headline_en": article.get("title", ""),
        "last_updated": now,
        "entities": json.dumps(merged[:30]),       # cap at 30 entities
        "top_sources": json.dumps(sources[:10]),
    })
    pipe.hincrby(_story_key(story_id), "article_count", 1)
    pipe.expire(_story_key(story_id), STORY_TTL_SECONDS)
    pipe.zadd(_story_articles_key(story_id), {article["id"]: ts})
    pipe.expire(_story_articles_key(story_id), STORY_TTL_SECONDS)
    pipe.zadd(STORIES_ACTIVE_KEY, {story_id: ts})
    pipe.zadd(_stories_cat_key(article.get("category", "general")), {story_id: ts})
    await pipe.execute()

    logger.debug(f"[story] joined story={story_id} article={article['id']}")


# ── Public API ────────────────────────────────────────────────────────────────

async def assign_story(
    redis: aioredis.Redis,
    article: dict,
    threshold: float = STORY_MATCH_THRESHOLD,
) -> str:
    """
    Assign an enriched article to an existing story or create a new one.
    Returns the story_id.

    article must have: id, category, entities (JSON or list), title, source_name
    """
    try:
        raw_entities = article.get("entities", "[]")
        article_entities: list[str] = (
            json.loads(raw_entities) if isinstance(raw_entities, str) else raw_entities
        )
    except (json.JSONDecodeError, TypeError):
        article_entities = []

    if not article_entities:
        # No entities → cannot match stories, create placeholder
        return await _create_story(redis, article, [], _now_ts())

    category = article.get("category", "general")
    ts = _now_ts()
    max_age_cutoff = ts - (STORY_MAX_AGE_H * 3600)

    candidates = await _load_candidate_stories(redis, category, max_age_cutoff)

    best_story_id: str | None = None
    best_score: float = 0.0

    for story_id, story_entities in candidates:
        try:
            score = entity_overlap_score(article_entities, story_entities)
        except NotImplementedError:
            # Graceful degradation when user hasn't implemented the function yet
            score = 0.0
        if score > best_score:
            best_score = score
            best_story_id = story_id

    if best_story_id and best_score >= threshold:
        story_entities = next(e for sid, e in candidates if sid == best_story_id)
        await _join_story(redis, best_story_id, article, article_entities, story_entities, ts)
        return best_story_id
    else:
        return await _create_story(redis, article, article_entities, ts)


async def get_active_stories(
    redis: aioredis.Redis,
    category: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Return active stories sorted by last_updated DESC.
    Optionally filter by category.
    """
    if category:
        key = _stories_cat_key(category)
    else:
        key = STORIES_ACTIVE_KEY

    raw_ids = await redis.zrevrange(key, 0, limit - 1)
    if not raw_ids:
        return []

    story_ids = [b.decode() if isinstance(b, bytes) else b for b in raw_ids]

    pipe = redis.pipeline()
    for sid in story_ids:
        pipe.hgetall(_story_key(sid))
        pipe.zcard(_story_articles_key(sid))
    results = await pipe.execute()

    stories = []
    for i, sid in enumerate(story_ids):
        raw = results[i * 2]
        count = results[i * 2 + 1]
        if raw:
            story = {k.decode(): v.decode() for k, v in raw.items()}
            story["id"] = sid
            story["article_count"] = int(story.get("article_count", count or 0))
            try:
                story["entities"] = json.loads(story.get("entities", "[]"))
            except json.JSONDecodeError:
                story["entities"] = []
            try:
                story["top_sources"] = json.loads(story.get("top_sources", "[]"))
            except json.JSONDecodeError:
                story["top_sources"] = []
            stories.append(story)
    return stories


async def get_story_articles(
    redis: aioredis.Redis,
    story_id: str,
    limit: int = 10,
) -> list[str]:
    """Return article_ids for a story, newest first."""
    raw = await redis.zrevrange(_story_articles_key(story_id), 0, limit - 1)
    return [b.decode() if isinstance(b, bytes) else b for b in raw]
