"""
Topic-based news synthesis: AI automatically decides how many summaries to generate.

Instead of translating articles one-by-one, this module:
1. Groups articles by category (e.g., 5 politics articles)
2. AI analyzes content diversity
3. AI generates 1-8 synthetic articles with different angles
4. Each synthetic article is saved to Redis and dispatched to webhooks
"""

import asyncio
import json
import logging
import hashlib
from datetime import datetime, timezone

import yaml
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

import redis.asyncio as aioredis
from ai.rewriter import get_openai_client, TONE_PROMPTS
from webhook.dispatcher import enqueue_dispatch

logger = logging.getLogger(__name__)


def _load_config() -> dict:
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


# AI prompt: Let AI decide output count based on content diversity
TOPIC_SYNTHESIS_PROMPT = """{tone_instruction}

You are analyzing {count} news articles about {category} from the past few hours.
These articles span {time_span} and come from {num_sources} different sources.

Articles (JSON array):
{articles_json}

Your task:
1. Analyze how many DISTINCT narratives, perspectives, or angles exist in these articles
2. Generate between 1 and 8 summaries, where EACH summary must:
   - Cover a unique angle that is NOT covered by other summaries
   - Provide standalone value (not generic rehash)
   - Be 3-4 sentences in both Vietnamese and English
   - Include a clear, descriptive title

Decision guidelines:
- If articles are very similar or redundant → generate FEWER summaries (minimum 1)
- If articles cover multiple sub-topics or perspectives → generate MORE summaries (maximum 8)
- If there's a clear timeline/progression → include a "timeline" angle
- If there are conflicting viewpoints → include "perspective A" and "perspective B" angles
- If there's significant impact/implications → include an "impact" or "analysis" angle

Output format (JSON only, no other text):
{{
  "analysis": "Brief explanation of what angles you identified and why you chose this number of summaries",
  "num_summaries": <integer 1-8>,
  "summaries": [
    {{
      "angle": "timeline|analysis|comparison|impact|perspective|summary",
      "title_vi": "Vietnamese title (max 100 chars)",
      "content_vi": "Vietnamese summary (3-4 sentences, {length_guidance})",
      "title_en": "English title (max 100 chars)",
      "content_en": "English summary (3-4 sentences, {length_guidance})"
    }},
    ...
  ]
}}

IMPORTANT: Each summary (both vi and en) must be {length_guidance}.
"""


def _generate_synthetic_id(category: str, articles: list[dict]) -> str:
    """Generate deterministic ID based on category + sorted source article IDs.
    Same source articles always produce the same base ID, enabling idempotency checks."""
    article_ids = sorted([a["id"] for a in articles])
    fingerprint = f"{category}:{','.join(article_ids)}"
    return f"synth_{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"


def _calculate_time_span(articles: list[dict]) -> str:
    """Calculate time span of articles (e.g., '2 hours' or '1 day')."""
    timestamps = []
    for a in articles:
        try:
            ts_str = a.get("published_at") or a.get("fetched_at", "")
            if ts_str:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                timestamps.append(dt)
        except (ValueError, TypeError):
            pass

    if len(timestamps) < 2:
        return "unknown timespan"

    oldest = min(timestamps)
    newest = max(timestamps)
    delta_sec = (newest - oldest).total_seconds()

    if delta_sec < 3600:
        return f"{int(delta_sec / 60)} minutes"
    elif delta_sec < 86400:
        return f"{int(delta_sec / 3600)} hours"
    else:
        return f"{int(delta_sec / 86400)} days"


def _count_unique_sources(articles: list[dict]) -> int:
    """Count number of unique source IDs."""
    return len(set(a.get("source_id", "") for a in articles if a.get("source_id")))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def synthesize_topic_articles(
    articles: list[dict],
    category: str,
    redis: aioredis.Redis,
    model: str | None = None,
    tone: str = "general",
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.5,
) -> list[dict]:
    """
    Analyze multiple articles from same category and generate 1-8 synthetic summaries.
    AI autonomously decides the output count based on content diversity.

    Returns: List of synthetic article dicts (length determined by AI).
    """
    if len(articles) < 3:
        logger.debug(
            f"Category {category}: only {len(articles)} articles, skipping synthesis"
        )
        return []

    client = get_openai_client(api_key=api_key, base_url=base_url)
    cfg = _load_config()
    ai_cfg = cfg.get("ai", {})
    resolved_model = model or ai_cfg.get("model", "gpt-4o-mini")

    # Build tone instruction
    custom_system = (ai_cfg.get("prompt_system") or "").strip()
    tone_instruction = custom_system or TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])

    # Length guidance
    if ai_cfg.get("output_limit_enabled"):
        max_chars = int(ai_cfg.get("output_limit_chars") or 250)
        length_guidance = f"at most {max_chars} characters"
    else:
        length_guidance = "approximately 200-300 characters"

    # Prepare article metadata
    time_span = _calculate_time_span(articles)
    num_sources = _count_unique_sources(articles)

    # Build compact article representation (avoid token bloat)
    compact_articles = []
    for a in articles:
        compact_articles.append(
            {
                "id": a.get("id", ""),
                "source": a.get("source_name", ""),
                "title": a.get("title", ""),
                "summary": (a.get("summary") or a.get("content") or "")[
                    :500
                ],  # limit to 500 chars
                "published_at": a.get("published_at", ""),
            }
        )

    articles_json = json.dumps(compact_articles, ensure_ascii=False, indent=2)

    # Build prompt
    prompt = TOPIC_SYNTHESIS_PROMPT.format(
        tone_instruction=tone_instruction,
        category=category,
        count=len(articles),
        time_span=time_span,
        num_sources=num_sources,
        articles_json=articles_json,
        length_guidance=length_guidance,
    )

    logger.info(
        f"Synthesizing {len(articles)} {category} articles (span={time_span}, sources={num_sources})"
    )

    # Call AI
    response = await client.chat.completions.create(
        model=resolved_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        temperature=temperature,
    )

    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise ValueError(f"Model returned empty content (model={resolved_model})")

    result = json.loads(content)
    tokens_used = response.usage.total_tokens if response.usage else 0

    # Validate response structure
    if "summaries" not in result:
        logger.warning(f"AI response missing 'summaries' key: {result}")
        return []

    summaries = result["summaries"]
    analysis = result.get("analysis", "No analysis provided")

    if not isinstance(summaries, list) or not (1 <= len(summaries) <= 8):
        logger.warning(f"AI returned invalid summary count: {len(summaries)}")
        return []

    logger.info(
        f"AI synthesis complete: {len(articles)} inputs → {len(summaries)} outputs "
        f"({tokens_used} tokens). Analysis: {analysis[:100]}..."
    )

    # Build synthetic article objects
    synthetic_articles = []
    base_synth_id = _generate_synthetic_id(category, articles)
    source_article_ids = [a["id"] for a in articles]

    for idx, summary in enumerate(summaries):
        # Validate each summary
        if not summary.get("content_vi") or not summary.get("content_en"):
            logger.warning(f"Skipping invalid summary #{idx}: missing content")
            continue

        if (
            len(summary.get("content_vi", "")) < 50
            or len(summary.get("content_en", "")) < 50
        ):
            logger.warning(f"Skipping summary #{idx}: content too short")
            continue

        synth_id = f"{base_synth_id}_{idx}"
        synth = {
            "id": synth_id,
            "type": "synthetic",
            "category": category,
            "angle": summary.get("angle", "summary"),
            "title_vi": summary.get("title_vi", ""),
            "title_en": summary.get("title_en", ""),
            "content_vi": summary.get("content_vi", ""),
            "content_en": summary.get("content_en", ""),
            "source_article_ids": source_article_ids,
            "num_source_articles": len(articles),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ai_model": resolved_model,
            "ai_tokens": tokens_used // len(summaries),  # approximate per-summary cost
            "ai_analysis": analysis if idx == 0 else "",  # only store once
        }
        synthetic_articles.append(synth)

    return synthetic_articles


async def save_synthetic_article(redis: aioredis.Redis, synth: dict) -> None:
    """Save a synthetic article to Redis with appropriate indices."""
    key = f"news:{synth['id']}"
    ttl_seconds = 43200  # 12h (same as original articles)

    # Convert to hash format
    hash_data = {k: str(v) if not isinstance(v, str) else v for k, v in synth.items()}
    if "source_article_ids" in hash_data:
        hash_data["source_article_ids"] = json.dumps(hash_data["source_article_ids"])

    now_ts = datetime.now(timezone.utc).timestamp()

    pipe = redis.pipeline()

    # Save article hash
    pipe.hset(key, mapping=hash_data)
    pipe.expire(key, ttl_seconds)

    # Index in main feed
    pipe.zadd("news:feed", {synth["id"]: now_ts})
    pipe.expire("news:feed", ttl_seconds)

    # Index in category feed
    pipe.zadd(f"news:cat:{synth['category']}", {synth["id"]: now_ts})
    pipe.expire(f"news:cat:{synth['category']}", ttl_seconds)

    # Index in synthetic-specific feeds
    pipe.zadd("news:synth:feed", {synth["id"]: now_ts})
    pipe.expire("news:synth:feed", ttl_seconds)

    pipe.zadd(f"news:synth:cat:{synth['category']}", {synth["id"]: now_ts})
    pipe.expire(f"news:synth:cat:{synth['category']}", ttl_seconds)

    await pipe.execute()
    logger.debug(f"Saved synthetic article {synth['id']} (angle={synth['angle']})")


SYNTH_USED_KEY = "news:synth:used"
SYNTH_MAX_AGE_SECONDS = 39600  # 11h — avoid articles very close to their 12h TTL


async def get_recent_articles_by_category(
    redis: aioredis.Redis,
    category: str,
    limit: int = 10,
    exclude_synthetic: bool = True,
) -> list[dict]:
    """Get most recent articles from a specific category."""
    feed_key = f"news:cat:{category}"
    ids = await redis.zrevrange(feed_key, 0, limit - 1)

    if not ids:
        return []

    pipe = redis.pipeline()
    for aid in ids:
        pipe.hgetall(f"news:{aid.decode()}")
    results = await pipe.execute()

    articles = []
    for raw in results:
        if not raw:
            continue
        article = {k.decode(): v.decode() for k, v in raw.items()}
        # Skip synthetic articles if requested
        if exclude_synthetic and article.get("type") == "synthetic":
            continue
        articles.append(article)

    return articles


async def _get_unseen_articles(
    redis: aioredis.Redis,
    category: str,
    hook_id: str,
    max_articles: int,
) -> list[dict]:
    """Return articles for the category that this hook has not yet synthesized.

    Fetches 3× max_articles from the feed (newest first) to have a large enough
    pool after filtering, then removes:
      - already-used article IDs (tracked per-hook per-category)
      - articles older than SYNTH_MAX_AGE_SECONDS (too stale)
    Result is returned newest-first, capped at max_articles.
    """
    candidates = await get_recent_articles_by_category(
        redis, category, limit=max_articles * 3, exclude_synthetic=True
    )
    if not candidates:
        return []

    # Age filter: drop articles older than SYNTH_MAX_AGE_SECONDS
    now_ts = datetime.now(timezone.utc).timestamp()
    fresh = []
    for art in candidates:
        ts_str = art.get("fetched_at") or art.get("published_at", "")
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now_ts - dt.timestamp()) <= SYNTH_MAX_AGE_SECONDS:
                fresh.append(art)
        except (ValueError, TypeError):
            fresh.append(art)  # if no parseable timestamp, keep it

    if not fresh:
        return []

    # Used-article filter: per-hook per-category seen set
    used_key = f"{SYNTH_USED_KEY}:{hook_id}:{category}"
    used_raw = await redis.zrange(used_key, 0, -1)
    used_ids = {m.decode() if isinstance(m, bytes) else m for m in used_raw}

    unseen = [a for a in fresh if a["id"] not in used_ids]
    return unseen[:max_articles]


async def _mark_articles_used(
    redis: aioredis.Redis,
    category: str,
    hook_id: str,
    article_ids: list[str],
) -> None:
    """Record article IDs as used for this hook+category. Auto-prunes stale entries."""
    used_key = f"{SYNTH_USED_KEY}:{hook_id}:{category}"
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - SYNTH_MAX_AGE_SECONDS

    pipe = redis.pipeline()
    for aid in article_ids:
        pipe.zadd(used_key, {aid: now_ts})
    # Prune entries older than the max-age window
    pipe.zremrangebyscore(used_key, 0, cutoff)
    pipe.expire(used_key, SYNTH_MAX_AGE_SECONDS + 3600)  # slight buffer over article TTL
    await pipe.execute()


async def process_category_synthesis(
    redis: aioredis.Redis,
    category: str,
    hook_id: str,
    min_articles: int = 5,
    max_articles: int = 15,
    model: str | None = None,
    tone: str = "general",
    api_key: str | None = None,
    base_url: str | None = None,
    webhook_endpoints: list[dict] | None = None,
    telegram_channels: list[dict] | None = None,
) -> int:
    """
    Process one category for one hook: fetch unseen recent articles, synthesize,
    save, dispatch, and track used articles. Returns number of synthetic articles generated.

    Per-hook tracking (news:synth:used:{hook_id}:{category}) ensures the same
    source articles are never synthesized twice for the same hook, and that
    each hook independently accumulates fresh batches.
    """
    articles = await _get_unseen_articles(redis, category, hook_id, max_articles)

    if len(articles) < min_articles:
        logger.debug(
            f"Category {category} / hook {hook_id}: only {len(articles)} unseen articles "
            f"(min {min_articles} required), skipping"
        )
        return 0

    logger.info(
        f"Category {category} / hook {hook_id}: {len(articles)} unseen articles → synthesizing"
    )

    try:
        synthetics = await synthesize_topic_articles(
            articles=articles,
            category=category,
            redis=redis,
            model=model,
            tone=tone,
            api_key=api_key,
            base_url=base_url,
        )
    except Exception as e:
        logger.error(f"Synthesis failed for category={category} hook={hook_id}: {e}")
        return 0

    if not synthetics:
        return 0

    # Mark source articles as used BEFORE dispatch so a crash doesn't cause re-synthesis
    await _mark_articles_used(redis, category, hook_id, [a["id"] for a in articles])

    for synth in synthetics:
        await save_synthetic_article(redis, synth)

        if webhook_endpoints or telegram_channels:
            await enqueue_dispatch(
                synth,
                webhook_endpoints or [],
                telegram_channels=telegram_channels,
            )
            logger.debug(
                f"Enqueued dispatch for synthetic article {synth['id']} "
                f"(category={category}, hook={hook_id}, angle={synth.get('angle', 'summary')})"
            )

    return len(synthetics)
