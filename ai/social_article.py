"""
Social Article Generator — AI-powered long-form content creation with image prompts.

Workflow:
  1. User selects: agent (provider), topic (category), style (preset/custom), time range
  2. Pull relevant articles from Redis based on filters
  3. Single AI call → structured long-form article JSON with sections
  4. Generate detailed image prompts for thumbnail + illustrations
  5. Store result in Redis with TTL
  6. Optionally dispatch to webhooks/channels

Output structure:
  {
    "title": "Main article title",
    "subtitle": "Optional subtitle/hook",
    "sections": [
      {
        "heading": "Section title",
        "content": "Long-form paragraph content...",
        "image_prompt": "Detailed DALL-E/Midjourney prompt for this section"
      }
    ],
    "thumbnail_prompt": "Detailed prompt for main article thumbnail",
    "tags": ["tag1", "tag2"],
    "estimated_read_time": 5
  }
"""

import asyncio
import json
import logging
import hashlib
from datetime import datetime, timezone, timedelta

from openai import AsyncOpenAI, RateLimitError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_not_exception_type

import redis.asyncio as aioredis
from ai.rewriter import get_openai_client, TONE_PROMPTS, LANG_NAMES
from ai.provider_utils import build_response_format, parse_ai_json
from storage.config_cache import cached_yaml

logger = logging.getLogger(__name__)


def _load_ai_config_for_provider(provider_id: str | None) -> dict:
    """
    Load AI config for a specific provider.
    
    Args:
        provider_id: Provider ID to load, or None to use default from settings
        
    Returns:
        dict with api_key, base_url, model
    """
    cfg = cached_yaml("config/settings.yaml")
    ai_cfg = cfg.get("ai", {})
    
    # If no provider_id specified, use default from settings
    if not provider_id:
        provider_id = ai_cfg.get("provider_id")
    
    # Find provider by ID
    if provider_id:
        for p in ai_cfg.get("providers", []):
            if p.get("id") == provider_id:
                return {
                    "api_key": p.get("api_key", ""),
                    "base_url": p.get("base_url", "https://api.openai.com/v1"),
                    "model": p.get("model", ""),
                }
    
    # Fallback to top-level config (legacy)
    return {
        "api_key": ai_cfg.get("api_key", ""),
        "base_url": ai_cfg.get("base_url", "https://api.openai.com/v1"),
        "model": ai_cfg.get("model", ""),
    }

# Style presets for different platforms/purposes
STYLE_PRESETS = {
    "blog_formal": {
        "name": "Blog (Formal)",
        "description": "Professional blog post with clear structure, data-driven insights",
        "tone": "formal",
        "length": "2000-3000 words",
        "sections": 5,
    },
    "blog_casual": {
        "name": "Blog (Casual)",
        "description": "Conversational blog post, storytelling approach, engaging",
        "tone": "casual",
        "length": "1500-2500 words",
        "sections": 4,
    },
    "linkedin": {
        "name": "LinkedIn Article",
        "description": "Professional insights, industry analysis, thought leadership",
        "tone": "formal",
        "length": "1200-1800 words",
        "sections": 4,
    },
    "medium": {
        "name": "Medium Story",
        "description": "Narrative-driven, personal perspective, deep dive",
        "tone": "casual",
        "length": "1500-2500 words",
        "sections": 5,
    },
    "newsletter": {
        "name": "Newsletter Feature",
        "description": "Curated insights, actionable takeaways, scannable format",
        "tone": "general",
        "length": "1000-1500 words",
        "sections": 3,
    },
    "twitter_thread": {
        "name": "Twitter Thread (Long)",
        "description": "Multi-tweet thread with clear narrative arc, punchy insights",
        "tone": "casual",
        "length": "800-1200 words",
        "sections": 6,
    },
}

# JSON schema for AI response
SCHEMA_SOCIAL_ARTICLE = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "content": {"type": "string"},  # Full article as continuous prose
        "image_prompts": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
        "thumbnail_prompt": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "estimated_read_time": {"type": "integer"},
    },
    "required": ["title", "content", "thumbnail_prompt"],
}

# AI prompt template
SOCIAL_ARTICLE_PROMPT = """{tone_instruction}

You are a professional content writer creating a long-form article for {platform}.

Style guidelines:
- Tone: {tone}
- Target length: {length}
- Purpose: {description}

Source material ({count} articles from the past {hours} hours):
{articles_json}

Your task:
1. Analyze the source articles and identify the most compelling narrative
2. Write a cohesive, flowing article that reads naturally - like a human writer would
3. DO NOT use numbered sections or artificial structure
4. Write continuous prose with natural paragraph breaks
5. Generate 1-2 image prompts for key moments/concepts in the article (not per section)
6. Generate a compelling thumbnail prompt for the main article image
7. Add relevant tags and estimate reading time

Image prompt guidelines:
- Be specific about style, composition, mood, colors
- Avoid text in images (AI image generators struggle with text)
- Focus on visual metaphors and symbolic representations
- Example: "A futuristic cityscape at dusk with glowing neural network patterns overlaying buildings, cyberpunk aesthetic, vibrant blues and purples, wide angle, cinematic lighting"

Output format (JSON only, no other text):
{{
  "title": "Compelling article title (max 100 chars)",
  "subtitle": "Optional hook or subtitle (max 150 chars)",
  "content": "Full article content as continuous prose. Write naturally with paragraph breaks (use \\n\\n for new paragraphs). Target {length}. Be detailed, insightful, and engaging. This should read like a real article, not a structured report.",
  "image_prompts": [
    "Detailed DALL-E/Midjourney prompt for first illustration",
    "Detailed DALL-E/Midjourney prompt for second illustration (optional)"
  ],
  "thumbnail_prompt": "Detailed prompt for the main article thumbnail image",
  "tags": ["tag1", "tag2", "tag3"],
  "estimated_read_time": 5
}}

IMPORTANT:
- Write as continuous flowing prose, NOT numbered sections
- Natural paragraph breaks, not artificial structure
- 1-2 image prompts total (not per section)
- Focus on storytelling and insights, not just summarizing
- Make it read like a human wrote it
"""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_not_exception_type(RateLimitError),
    reraise=True,
)
async def _call_ai_for_social_article(
    client: AsyncOpenAI,
    model: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    articles: list[dict],
    style_preset: str,
    custom_style: dict | None,
    tone_instruction: str,
    hours: int,
) -> dict:
    """Call AI to generate long-form social article."""
    
    # Determine style config
    if custom_style:
        style_cfg = custom_style
    else:
        style_cfg = STYLE_PRESETS.get(style_preset, STYLE_PRESETS["blog_formal"])
    
    # Build articles JSON
    articles_data = []
    for art in articles:
        articles_data.append({
            "title": art.get("title", ""),
            "content": art.get("content", ""),
            "source": art.get("source_name", ""),
            "published": art.get("published_at", ""),
            "url": art.get("url", ""),
        })
    
    articles_json = json.dumps(articles_data, indent=2, ensure_ascii=False)
    
    # Build prompt
    prompt = SOCIAL_ARTICLE_PROMPT.format(
        tone_instruction=tone_instruction,
        platform=style_cfg.get("name", "Blog"),
        tone=style_cfg.get("tone", "general"),
        length=style_cfg.get("length", "2000 words"),
        description=style_cfg.get("description", ""),
        count=len(articles),
        hours=hours,
        articles_json=articles_json,
    )
    
    # Call AI
    logger.info(f"Calling AI for social article: {len(articles)} articles, style={style_preset or 'custom'}")
    
    messages = [{"role": "user", "content": prompt}]
    
    create_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": build_response_format(base_url, "social_article", SCHEMA_SOCIAL_ARTICLE),
    }
    
    completion = await client.chat.completions.create(**create_kwargs)
    
    # Extract content from response
    resp_content = completion.choices[0].message.content if completion.choices else None
    if not resp_content:
        raise ValueError(f"Model returned empty content (model={model})")
    
    result = parse_ai_json(resp_content)
    
    # Normalize field names - AI sometimes returns "image" instead of "image_prompt"
    # (No longer needed for new schema, but keep for backward compatibility)
    
    logger.info(f"AI returned social article: {result.get('title', 'N/A')}, {len(result.get('content', ''))} chars")
    
    return result


async def generate_social_article(
    redis_client: aioredis.Redis,
    provider_id: str | None,
    categories: list[str] | None,
    style_preset: str | None,
    custom_style: dict | None,
    hours: int = 24,
    min_articles: int = 3,
    max_articles: int = 20,
    temperature: float = 0.7,
    max_tokens: int = 4000,
) -> dict:
    """
    Generate a long-form social article from recent news.
    
    Args:
        redis_client: Redis connection
        provider_id: AI provider ID (from settings.yaml)
        categories: Category filters (e.g., ["tech", "crypto"]) or None for all
        style_preset: Style preset key (e.g., "blog_formal", "linkedin")
        custom_style: Custom style config (overrides preset)
        hours: Look back N hours for articles
        min_articles: Minimum articles required
        max_articles: Maximum articles to include
        temperature: AI temperature
        max_tokens: Max tokens for AI response
    
    Returns:
        {
            "title": "...",
            "subtitle": "...",
            "sections": [...],
            "thumbnail_prompt": "...",
            "tags": [...],
            "estimated_read_time": 5,
            "metadata": {
                "generated_at": "...",
                "source_count": 10,
                "categories": ["tech", "crypto"],
                "style": "blog_formal"
            }
        }
    """
    
    # Load AI config
    ai_cfg = _load_ai_config_for_provider(provider_id)
    client = get_openai_client(ai_cfg["api_key"], ai_cfg["base_url"])
    model = ai_cfg["model"]
    
    # Get tone instruction from settings
    cfg = cached_yaml("config/settings.yaml")
    tone = cfg.get("ai", {}).get("tone", "general")
    tone_instruction = TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])
    
    # Fetch articles from Redis
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    
    # Get all articles from feed (fetch more than needed for category filtering)
    feed_key = "news:feed"
    article_ids = await redis_client.zrangebyscore(
        feed_key,
        min=cutoff_ts,
        max="+inf",
        start=0,
        num=max_articles * 10,  # Fetch 10x more for category filtering
    )
    
    # Fetch article data
    articles = []
    for aid in article_ids:
        if isinstance(aid, bytes):
            aid = aid.decode("utf-8")
        
        article_key = f"news:{aid}"
        article_data = await redis_client.hgetall(article_key)
        
        if not article_data:
            continue
        
        # Decode bytes
        article = {}
        for k, v in article_data.items():
            key = k.decode("utf-8") if isinstance(k, bytes) else k
            val = v.decode("utf-8") if isinstance(v, bytes) else v
            article[key] = val
        
        # Filter by categories if specified (OR logic - article matches ANY selected category)
        if categories:
            article_category = article.get("category")
            if article_category not in categories:
                continue
        
        articles.append(article)
        
        if len(articles) >= max_articles:
            break
    
    if len(articles) < min_articles:
        raise ValueError(
            f"Insufficient articles: found {len(articles)}, need at least {min_articles}"
        )
    
    logger.info(
        f"Generating social article from {len(articles)} articles "
        f"(categories={categories or 'all'}, hours={hours})"
    )
    
    # Call AI
    result = await _call_ai_for_social_article(
        client=client,
        model=model,
        base_url=ai_cfg["base_url"],
        temperature=temperature,
        max_tokens=max_tokens,
        articles=articles,
        style_preset=style_preset or "blog_formal",
        custom_style=custom_style,
        tone_instruction=tone_instruction,
        hours=hours,
    )
    
    # Add metadata
    result["metadata"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(articles),
        "categories": categories or ["all"],
        "style": style_preset or "custom",
        "provider_id": provider_id,
    }
    
    return result


async def save_social_article(
    redis_client: aioredis.Redis,
    article: dict,
    ttl_hours: int = 168,  # 7 days default
) -> str:
    """
    Save generated social article to Redis.
    
    Returns:
        article_id: Unique ID for the saved article
    """
    
    # Generate ID from title + timestamp
    timestamp = article["metadata"]["generated_at"]
    title = article.get("title", "untitled")
    raw_id = f"{title}:{timestamp}"
    article_id = hashlib.sha256(raw_id.encode()).hexdigest()[:16]
    
    # Save to Redis
    key = f"social_article:{article_id}"
    await redis_client.set(
        key,
        json.dumps(article, ensure_ascii=False),
        ex=ttl_hours * 3600,
    )
    
    # Add to sorted set for listing
    score = datetime.fromisoformat(timestamp).timestamp()
    await redis_client.zadd("social_articles:index", {article_id: score})
    
    logger.info(f"Saved social article: {article_id} (TTL={ttl_hours}h)")
    
    return article_id


async def get_social_article(
    redis_client: aioredis.Redis,
    article_id: str,
) -> dict | None:
    """Retrieve a saved social article by ID."""
    
    key = f"social_article:{article_id}"
    data = await redis_client.get(key)
    
    if not data:
        return None
    
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    
    return json.loads(data)


async def list_social_articles(
    redis_client: aioredis.Redis,
    limit: int = 50,
) -> list[dict]:
    """List recent social articles (metadata only)."""
    
    # Get IDs from sorted set (newest first)
    article_ids = await redis_client.zrevrange("social_articles:index", 0, limit - 1)
    
    articles = []
    for aid in article_ids:
        if isinstance(aid, bytes):
            aid = aid.decode("utf-8")
        
        article = await get_social_article(redis_client, aid)
        if article:
            # Return metadata only for listing
            articles.append({
                "id": aid,
                "title": article.get("title"),
                "subtitle": article.get("subtitle"),
                "tags": article.get("tags", []),
                "estimated_read_time": article.get("estimated_read_time"),
                "metadata": article.get("metadata", {}),
            })
    
    return articles
