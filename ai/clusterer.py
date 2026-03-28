"""
Phase 2 – Topic clustering: online centroid-based topic assignment.

How it works:
  1. When a new article is enriched, its embedding is compared against
     all active cluster centroids (per-category).
  2. If similarity ≥ threshold → article joins that cluster.
  3. Otherwise → a new cluster is created with this article as seed.
  4. Centroids are updated as a moving average after each new member.

Redis layout:
  news:topic:centroid:{topic_id}  → JSON list[float] (normalized centroid vector)
  news:topic:meta:{topic_id}      → Hash {category, created_at, member_count, label}
  news:topics:cat:{category}      → Set of topic_ids in this category
  news:topic:articles:{topic_id}  → Set of article_ids in this cluster
"""

import json
import logging
import math
from datetime import datetime, timezone

import redis.asyncio as aioredis

from storage.redis_keys import ARTICLE_TTL_SECONDS

logger = logging.getLogger(__name__)

# How long topics live (same as articles: 12 h).
# Stale topics are automatically removed when their TTL expires.
TOPIC_TTL = ARTICLE_TTL_SECONDS

# Redis key helpers
def _centroid_key(topic_id: str) -> str:
    return f"news:topic:centroid:{topic_id}"

def _meta_key(topic_id: str) -> str:
    return f"news:topic:meta:{topic_id}"

def _cat_topics_key(category: str) -> str:
    return f"news:topics:cat:{category}"

def _topic_articles_key(topic_id: str) -> str:
    return f"news:topic:articles:{topic_id}"


# ---------------------------------------------------------------------------
# Core similarity helpers — pure functions, easy to test
# ---------------------------------------------------------------------------

def dot(a: list[float], b: list[float]) -> float:
    """Dot product of two vectors (equal length assumed)."""
    return sum(x * y for x, y in zip(a, b))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Cosine similarity between two vectors.
    If vectors are pre-normalized (unit length), this equals dot(a, b).
    Returns 0.0 on dimension mismatch or zero-length vectors.
    """
    if len(a) != len(b) or not a:
        return 0.0
    # Embedder pre-normalizes, so dot product is sufficient.
    # We still guard against un-normalized vectors from legacy data.
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    denom = mag_a * mag_b
    return dot(a, b) / denom if denom else 0.0


def update_centroid(
    centroid: list[float], new_vector: list[float], member_count: int
) -> list[float]:
    """
    Incremental moving-average centroid update.
    New centroid = (old_centroid * N + new_vector) / (N + 1), then re-normalized.
    """
    n = max(member_count, 1)
    updated = [(c * n + v) / (n + 1) for c, v in zip(centroid, new_vector)]
    # Re-normalize to keep all centroids at unit length
    mag = math.sqrt(sum(x * x for x in updated))
    return [x / mag for x in updated] if mag else updated


# ---------------------------------------------------------------------------
# TODO: implement this function (5-10 lines)
# ---------------------------------------------------------------------------

def _find_matching_topic(
    embedding: list[float],
    candidates: list[tuple[str, list[float]]],  # [(topic_id, centroid), ...]
    threshold: float,
) -> str | None:
    """
    Given a new article embedding and a list of candidate (topic_id, centroid) pairs,
    return the topic_id of the best match — or None to create a new cluster.

    Parameters
    ----------
    embedding   : normalized embedding vector for the new article
    candidates  : list of (topic_id, centroid_vector) for all active topics
                  in this category (may be empty on first article)
    threshold   : minimum cosine similarity to consider a match (e.g. 0.75)

    Trade-offs to consider
    ----------------------
    - "Nearest-match" (always pick highest similarity even if below threshold):
        Creates fewer, broader clusters. Good for sparse data.
    - "Threshold-only" (only match if similarity ≥ threshold, otherwise new cluster):
        Creates tighter, more specific clusters. May fragment fast-moving stories.
    - Tiebreaking when two topics are equally similar:
        Pick the older one (stable) vs. the newer one (recency bias)?

    Returns: topic_id string if matched, None if a new cluster should be created.

    """
    if not candidates:
        return None
    best_id: str | None = None
    best_sim: float = threshold  # only accept matches at or above threshold
    for topic_id, centroid in candidates:
        sim = cosine_similarity(embedding, centroid)
        if sim > best_sim:
            best_sim = sim
            best_id = topic_id
    return best_id


# ---------------------------------------------------------------------------
# High-level API — called from the enrich job
# ---------------------------------------------------------------------------

async def assign_topic(
    redis: aioredis.Redis,
    article_id: str,
    embedding: list[float],
    category: str,
    threshold: float = 0.75,
) -> str:
    """
    Assign article to an existing topic cluster or create a new one.
    Updates the cluster centroid and returns the topic_id.
    """
    # Load all active topic centroids for this category
    topic_ids_raw = await redis.smembers(_cat_topics_key(category))
    topic_ids = [tid.decode() if isinstance(tid, bytes) else tid for tid in topic_ids_raw]

    candidates: list[tuple[str, list[float]]] = []
    for tid in topic_ids:
        raw = await redis.get(_centroid_key(tid))
        if raw:
            centroid = json.loads(raw)
            candidates.append((tid, centroid))

    matched_id = _find_matching_topic(embedding, candidates, threshold)

    if matched_id:
        topic_id = matched_id
        # Update centroid: moving average
        meta_raw = await redis.hgetall(_meta_key(topic_id))
        meta = {k.decode(): v.decode() for k, v in meta_raw.items()} if meta_raw else {}
        member_count = int(meta.get("member_count", 1))
        old_centroid_raw = await redis.get(_centroid_key(topic_id))
        old_centroid = json.loads(old_centroid_raw) if old_centroid_raw else embedding
        new_centroid = update_centroid(old_centroid, embedding, member_count)
        await redis.set(_centroid_key(topic_id), json.dumps(new_centroid), ex=TOPIC_TTL)
        await redis.hset(_meta_key(topic_id), "member_count", str(member_count + 1))
        await redis.expire(_meta_key(topic_id), TOPIC_TTL)
    else:
        # Create a new cluster seeded by this article
        topic_id = _make_topic_id(category, article_id)
        await redis.set(_centroid_key(topic_id), json.dumps(embedding), ex=TOPIC_TTL)
        await redis.hset(_meta_key(topic_id), mapping={
            "category": category,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "member_count": "1",
        })
        await redis.expire(_meta_key(topic_id), TOPIC_TTL)
        await redis.sadd(_cat_topics_key(category), topic_id)
        await redis.expire(_cat_topics_key(category), TOPIC_TTL)

    # Track which articles belong to this topic
    await redis.sadd(_topic_articles_key(topic_id), article_id)
    await redis.expire(_topic_articles_key(topic_id), TOPIC_TTL)

    return topic_id


def _make_topic_id(category: str, seed_article_id: str) -> str:
    """Deterministic topic ID from category + seed article."""
    import hashlib
    h = hashlib.sha256(f"{category}:{seed_article_id}".encode()).hexdigest()[:8]
    return f"topic_{category}_{h}"
