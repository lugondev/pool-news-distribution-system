"""
Social Poster — AI agent that writes social media posts based on a persona config.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONCEPT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each social agent has a persona (background, tone, knowledge focus) and
a list of platforms it posts to (tweet / twitter_thread / facebook_post).

When triggered:
  1. Load agent config from config/social_agents.yaml
  2. Fetch recent articles matching agent's category filter from Redis
  3. For each platform, build a persona-aware prompt and call AI
  4. Parse and store the generated posts in Redis
  5. Return post dicts for API response

──────────────────────────────────────────────────────────────
REDIS LAYOUT
──────────────────────────────────────────────────────────────

  social:post:{post_id}     → Hash {agent_id, platform, language, content,
                                     tweets (JSON for thread), generated_at,
                                     model, article_ids (JSON)}
  social:posts:recent       → Sorted Set (score=ts, member=post_id)
  social:posts:agent:{id}   → Sorted Set per agent (score=ts, member=post_id)

──────────────────────────────────────────────────────────────
MANUAL TRIGGER ONLY — no scheduler job
Triggered via POST /api/social-agents/{agent_id}/run
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from ai.rewriter import get_openai_client, _load_ai_config
from dashboard.config_io import read_social_agents

logger = logging.getLogger(__name__)

POST_TTL = 7 * 24 * 3600   # 7 days
POST_PREFIX = "social:post:"
POSTS_RECENT_KEY = "social:posts:recent"
POSTS_AGENT_PREFIX = "social:posts:agent:"

LANG_NAMES = {
    "en": "English", "vi": "Vietnamese", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "fr": "French",
}

LENGTH_WORDS = {"short": "~100 words", "medium": "~200 words", "long": "~400 words"}


# ── Config loading ────────────────────────────────────────────────────────────

def load_social_agents() -> list[dict]:
    """Load all agents from the active config backend (yaml or db)."""
    return read_social_agents()


def get_agent(agent_id: str) -> dict | None:
    for agent in load_social_agents():
        if agent.get("id") == agent_id:
            return agent
    return None


# ── Article fetching ──────────────────────────────────────────────────────────

async def _fetch_articles_for_agent(redis: aioredis.Redis, agent: dict) -> list[dict]:
    """
    Pull recent articles matching the agent's source_filter.
    Returns a list of article dicts (title + summary/content).
    """
    sf = agent.get("source_filter", {})
    categories = sf.get("categories", [])
    max_articles = sf.get("max_articles", 5)
    recency_minutes = sf.get("recency_minutes", 120)

    cutoff = datetime.now(timezone.utc).timestamp() - recency_minutes * 60

    # Collect candidate IDs from each category sorted set
    candidate_ids: list[str] = []
    if categories:
        pipe = redis.pipeline()
        for cat in categories:
            pipe.zrevrangebyscore(f"news:cat:{cat}", "+inf", cutoff, start=0, num=max_articles * 2)
        results = await pipe.execute()
        for ids in results:
            for raw in ids:
                aid = raw.decode() if isinstance(raw, bytes) else raw
                if aid not in candidate_ids:
                    candidate_ids.append(aid)
    else:
        raw_ids = await redis.zrevrangebyscore("news:feed", "+inf", cutoff, start=0, num=max_articles * 2)
        candidate_ids = [r.decode() if isinstance(r, bytes) else r for r in raw_ids]

    if not candidate_ids:
        return []

    pipe = redis.pipeline()
    for aid in candidate_ids:
        pipe.hgetall(f"news:{aid}")
    raw_articles = await pipe.execute()

    articles = []
    for raw in raw_articles:
        if not raw:
            continue
        art = {k.decode(): v.decode() for k, v in raw.items()}
        # Only include articles that have been AI-processed or at least have content
        if not art.get("title"):
            continue
        articles.append(art)
        if len(articles) >= max_articles:
            break

    return articles


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_system_prompt(persona: dict) -> str:
    background = persona.get("background", "")
    tone = persona.get("tone", "professional")
    knowledge = persona.get("knowledge_focus", "")
    style = persona.get("writing_style", "")
    avoid = persona.get("avoid", "")

    parts = [f"You are: {background}"]
    if tone:
        parts.append(f"Your tone: {tone}")
    if knowledge:
        parts.append(f"Your expertise: {knowledge}")
    if style:
        parts.append(f"Your writing style: {style}")
    if avoid:
        parts.append(f"Avoid: {avoid}")

    parts.append("Write authentically as this persona — not as a generic news bot.")
    return "\n".join(parts)


def _build_tweet_prompt(articles_text: str, language: str, max_chars: int, hashtags: int) -> str:
    lang_name = LANG_NAMES.get(language, language)
    ht_instruction = (
        f"Include {hashtags} relevant hashtag(s) at the end." if hashtags > 0
        else "No hashtags."
    )
    return f"""Based on the news below, write a single tweet in {lang_name}.

Rules:
- Maximum {max_chars} characters (including hashtags)
- {ht_instruction}
- Write in first person as if YOU are sharing your take on this news
- No quote marks around the tweet
- No "Tweet:" prefix

News articles:
{articles_text}

Respond in JSON: {{"tweet": "your tweet text here"}}"""


def _build_thread_prompt(articles_text: str, language: str, min_tweets: int, max_tweets: int) -> str:
    lang_name = LANG_NAMES.get(language, language)
    return f"""Based on the news below, write a Twitter thread in {lang_name}.

Rules:
- {min_tweets} to {max_tweets} tweets
- Each tweet ≤ 280 characters
- First tweet is the hook — make people want to read more
- Last tweet is your take / conclusion
- Number each tweet like: 1/ 2/ 3/
- Write as YOUR perspective on this story

News articles:
{articles_text}

Respond in JSON: {{"tweets": ["1/ first tweet", "2/ second tweet", ...]}}"""


def _build_facebook_prompt(articles_text: str, language: str, length: str) -> str:
    lang_name = LANG_NAMES.get(language, language)
    word_count = LENGTH_WORDS.get(length, "~200 words")
    return f"""Based on the news below, write a Facebook post in {lang_name}.

Rules:
- Length: {word_count}
- Write as if you're sharing your thoughts with your audience
- Can use paragraph breaks for readability
- Can include a question to engage readers at the end
- No hashtags unless very natural

News articles:
{articles_text}

Respond in JSON: {{"post": "your facebook post text here"}}"""


# ── Article context builder ───────────────────────────────────────────────────

def _format_articles(articles: list[dict], max_chars_per: int = 300) -> str:
    parts = []
    for art in articles:
        title = art.get("title", "")
        summary = (
            art.get("ai_summary_en")
            or art.get("ai_summary_vi")
            or art.get("summary", "")
        )[:max_chars_per]
        source = art.get("source_name", "")
        parts.append(f"[{source}] {title}\n{summary}")
    return "\n\n---\n\n".join(parts)


# ── Core generation ───────────────────────────────────────────────────────────

async def _generate_for_platform(
    client,
    platform: dict,
    articles_text: str,
    persona: dict,
    model: str,
    temperature: float,
) -> dict | None:
    """Generate a post for one platform config. Returns parsed post dict or None."""
    ptype = platform.get("type", "tweet")
    language = platform.get("language", "en")

    if ptype == "tweet":
        prompt = _build_tweet_prompt(
            articles_text,
            language,
            max_chars=platform.get("max_chars", 280),
            hashtags=platform.get("hashtags", 1),
        )
    elif ptype == "twitter_thread":
        prompt = _build_thread_prompt(
            articles_text,
            language,
            min_tweets=platform.get("min_tweets", 3),
            max_tweets=platform.get("max_tweets", 6),
        )
    elif ptype == "facebook_post":
        prompt = _build_facebook_prompt(
            articles_text,
            language,
            length=platform.get("length", "medium"),
        )
    else:
        logger.warning(f"[social] Unknown platform type: {ptype}")
        return None

    system_prompt = _build_system_prompt(persona)
    full_prompt = f"{system_prompt}\n\n{prompt}"

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": full_prompt}],
            max_tokens=800,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
    except Exception as exc:
        logger.warning(f"[social] AI call failed for platform={ptype}: {exc}")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except Exception:
                logger.warning(f"[social] Could not parse JSON from: {raw[:200]}")
                return None
        else:
            return None

    result = {"type": ptype, "language": language}

    if ptype == "tweet":
        result["content"] = parsed.get("tweet", "")
    elif ptype == "twitter_thread":
        tweets = parsed.get("tweets", [])
        result["tweets"] = tweets
        result["content"] = "\n\n".join(tweets)
    elif ptype == "facebook_post":
        result["content"] = parsed.get("post", "")

    return result


def _make_post_id(agent_id: str, platform_type: str, language: str) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    raw = f"{agent_id}:{platform_type}:{language}:{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def _save_post(
    redis: aioredis.Redis,
    agent_id: str,
    post: dict,
    article_ids: list[str],
    model: str,
) -> str:
    post_id = _make_post_id(agent_id, post["type"], post["language"])
    now_ts = datetime.now(timezone.utc).timestamp()
    now_iso = datetime.now(timezone.utc).isoformat()

    record = {
        "id": post_id,
        "agent_id": agent_id,
        "platform": post["type"],
        "language": post["language"],
        "content": post.get("content", ""),
        "tweets": json.dumps(post.get("tweets", [])),
        "generated_at": now_iso,
        "model": model,
        "article_ids": json.dumps(article_ids),
    }

    key = f"{POST_PREFIX}{post_id}"
    pipe = redis.pipeline()
    pipe.hset(key, mapping=record)
    pipe.expire(key, POST_TTL)
    pipe.zadd(POSTS_RECENT_KEY, {post_id: now_ts})
    pipe.expire(POSTS_RECENT_KEY, POST_TTL)
    pipe.zadd(f"{POSTS_AGENT_PREFIX}{agent_id}", {post_id: now_ts})
    pipe.expire(f"{POSTS_AGENT_PREFIX}{agent_id}", POST_TTL)
    await pipe.execute()

    return post_id


# ── Public API ────────────────────────────────────────────────────────────────

async def run_social_agent(
    redis: aioredis.Redis,
    agent_id: str,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float = 0.7,
) -> dict:
    """
    Run a single social agent: fetch articles → generate posts for all platforms.
    Returns {"posts": [...], "agent_id": ..., "article_count": ...}
    """
    agent = get_agent(agent_id)
    if not agent:
        return {"error": f"Agent '{agent_id}' not found", "posts": []}

    if not agent.get("enabled", True):
        return {"error": f"Agent '{agent_id}' is disabled", "posts": []}

    # Resolve AI config
    ai_cfg = _load_ai_config()
    resolved_key = api_key or ai_cfg.get("api_key", "")
    resolved_url = base_url or ai_cfg.get("base_url", "https://api.openai.com/v1")
    resolved_model = model or ai_cfg.get("model", "")
    resolved_temp = temperature

    client = get_openai_client(api_key=resolved_key, base_url=resolved_url)

    articles = await _fetch_articles_for_agent(redis, agent)
    if not articles:
        return {"error": "No articles found matching agent's category filter", "posts": [], "agent_id": agent_id}

    articles_text = _format_articles(articles)
    article_ids = [art.get("id", "") for art in articles if art.get("id")]
    persona = agent.get("persona", {})
    platforms = agent.get("platforms", [])

    # Generate all platforms concurrently — each is an independent AI call
    async def _gen_platform(platform: dict) -> dict | None:
        post = await _generate_for_platform(
            client, platform, articles_text, persona, resolved_model, resolved_temp
        )
        if post:
            post_id = await _save_post(redis, agent_id, post, article_ids, resolved_model)
            post["id"] = post_id
            post["agent_id"] = agent_id
            post["generated_at"] = datetime.now(timezone.utc).isoformat()
            logger.info(f"[social] agent={agent_id} platform={platform['type']} post_id={post_id}")
        return post

    platform_results = await asyncio.gather(
        *[_gen_platform(p) for p in platforms], return_exceptions=True
    )
    generated_posts = [p for p in platform_results if isinstance(p, dict)]

    return {
        "agent_id": agent_id,
        "agent_name": agent.get("name", agent_id),
        "article_count": len(articles),
        "posts": generated_posts,
    }


async def get_recent_posts(
    redis: aioredis.Redis,
    agent_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return recent posts, optionally filtered by agent_id."""
    if agent_id:
        key = f"{POSTS_AGENT_PREFIX}{agent_id}"
    else:
        key = POSTS_RECENT_KEY

    raw_ids = await redis.zrevrange(key, 0, limit - 1)
    post_ids = [b.decode() if isinstance(b, bytes) else b for b in raw_ids]
    if not post_ids:
        return []

    pipe = redis.pipeline()
    for pid in post_ids:
        pipe.hgetall(f"{POST_PREFIX}{pid}")
    results = await pipe.execute()

    posts = []
    for raw in results:
        if raw:
            p = {k.decode(): v.decode() for k, v in raw.items()}
            # Parse tweets JSON back to list
            if p.get("tweets"):
                try:
                    p["tweets"] = json.loads(p["tweets"])
                except Exception:
                    p["tweets"] = []
            posts.append(p)
    return posts
