"""
On-demand AI processing for content channels.

Channels are pull-based processors that generate content when clients request it,
rather than consuming pre-generated content like webhooks do.

MERGED APPROACH: AI processing + style transform in ONE call to save API quota.
Cache keys include output_format for reusability.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from ai.rewriter import rewrite_article, LANG_NAMES, TONE_PROMPTS, _load_ai_config, get_openai_client
from ai.provider_utils import build_response_format, parse_ai_json
from ai.style_transform import PLATFORM_PRESETS, OUTPUT_FORMAT_INSTRUCTIONS
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


def _build_rewrite_style_prompt(
    article: dict,
    target_lang: str,
    platform: str,
    output_format: str,
    style_config: dict | None,
    client_style_prompt: str | None,
    channel: dict,
) -> str:
    """Build combined prompt for rewrite + style formatting."""
    preset = PLATFORM_PRESETS.get(platform, PLATFORM_PRESETS["custom"])
    style = style_config or {}
    
    # Resolve constraints
    max_length = style.get("max_length") or preset["max_length"]
    tone = style.get("tone") or preset["tone"]
    include_hashtags = style.get("include_hashtags", preset.get("include_hashtags", False))
    include_link = style.get("include_link", preset.get("include_link", False))
    
    # Tone instruction
    if client_style_prompt:
        tone_instruction = client_style_prompt
    elif style.get("custom_prompt"):
        tone_instruction = style["custom_prompt"]
    elif preset["instruction"]:
        tone_instruction = preset["instruction"]
    else:
        tone_instruction = TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])
    
    # Format instruction
    format_instruction = OUTPUT_FORMAT_INSTRUCTIONS.get(output_format, OUTPUT_FORMAT_INSTRUCTIONS["summary"])
    
    # Language
    lang_name = LANG_NAMES.get(target_lang, target_lang.upper())
    
    # Article content
    title = article.get("title", "")
    content = article.get("content") or article.get("summary", "")
    url = article.get("url", "")
    source = article.get("source_name", "")
    
    prompt = f"""You are rewriting this article into {lang_name}.

TONE & STYLE: {tone_instruction}

OUTPUT FORMAT: {format_instruction}

CONSTRAINTS:
- Maximum length: {max_length} characters
- Language: {lang_name}
- {"Include 1-3 relevant hashtags" if include_hashtags else "Do NOT include hashtags"}
- {"Include the source URL at the end" if include_link else "Do NOT include any URLs"}

SOURCE ARTICLE:
Title: {title}
Source: {source}
URL: {url}
Content: {content[:2000]}

Respond in JSON with TWO outputs:
1. ai_summary_{target_lang}: Full rewritten article in {lang_name} (can be longer, detailed)
2. styled_content: Formatted output following the OUTPUT FORMAT and CONSTRAINTS above (must be ≤ {max_length} chars)

{{"ai_summary_{target_lang}": "full rewritten content here", "styled_content": "formatted output here", "hashtags": ["tag1", "tag2"], "char_count": 123}}

IMPORTANT: styled_content must be at most {max_length} characters. Count carefully."""
    
    return prompt


async def process_rewrite(
    article: dict[str, Any],
    channel: dict[str, Any],
    redis,
    output_format: str = "summary",
    platform: str = "custom",
    style_config: dict | None = None,
    client_style_prompt: str | None = None,
    client_api_key: str | None = None,
    client_base_url: str | None = None,
    client_model: str | None = None,
) -> dict[str, Any]:
    """
    Process single article with AI rewrite + style formatting in ONE call.
    
    Args:
        article: Original article dict
        channel: Channel config
        redis: Redis connection
        output_format: Style format (summary, thread, breaking, etc.)
        platform: Platform preset (twitter, facebook, blog, telegram, custom)
        style_config: Custom style config dict
        client_style_prompt: Client-provided style override
        client_api_key: Client AI credentials (optional)
        client_base_url: Client AI base URL (optional)
        client_model: Client AI model (optional)
        
    Returns:
        Article dict with ai_summary_{lang} and styled_content
    """
    target_lang = channel.get("target_language", "en")
    ai_config_id = channel.get("ai_config_id", "")
    
    # Determine if we need style formatting
    needs_style = (
        platform != "custom"
        or output_format != "summary"
        or (style_config or {}).get("custom_prompt")
        or client_style_prompt
    )
    
    # Cache key includes output_format if styling is needed
    if needs_style:
        cache_key = f"channel:{channel['id']}:rewrite:{article['id']}:{target_lang}:{output_format}"
    else:
        cache_key = f"channel:{channel['id']}:rewrite:{article['id']}:{target_lang}"
    
    # Check cache
    cached = await redis.get(cache_key)
    if cached:
        logger.info(f"Channel {channel['id']}: rewrite cache hit for {article['id']}")
        import json
        cached_data = json.loads(cached.decode())
        article.update(cached_data)
        article["ai_status"] = "done"
        return article
    
    # Resolve AI credentials
    ai_source = channel.get("ai_source", "system")
    if ai_source == "client" and client_api_key:
        api_key = client_api_key
        base_url = client_base_url
        model = client_model
    else:
        ai_cfg = _load_ai_config()
        api_key = ai_cfg.get("api_key")
        base_url = ai_cfg.get("base_url")
        model = ai_cfg.get("model", "")
    
    if not api_key:
        logger.warning(f"No AI credentials for channel {channel['id']}")
        return article
    
    # Build combined prompt (rewrite + style)
    if needs_style:
        prompt = _build_rewrite_style_prompt(
            article=article,
            target_lang=target_lang,
            platform=platform,
            output_format=output_format,
            style_config=style_config,
            client_style_prompt=client_style_prompt,
            channel=channel,
        )
        schema = {
            "type": "object",
            "properties": {
                f"ai_summary_{target_lang}": {"type": "string"},
                "styled_content": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "char_count": {"type": "integer"},
            },
            "required": [f"ai_summary_{target_lang}", "styled_content"],
        }
    else:
        # Simple rewrite without styling
        result = await rewrite_article(
            article=article,
            target_languages=[target_lang],
            ai_config_id=ai_config_id,
        )
        summary = result.get(f"ai_summary_{target_lang}", "")
        if summary:
            await redis.setex(cache_key, 3600, summary)
        return result
    
    # Call AI with merged prompt
    try:
        client = get_openai_client(api_key=api_key, base_url=base_url)
        response = await client.chat.completions.create(
            model=model or "",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.7,
            response_format=build_response_format(base_url, "rewrite_styled", schema),
        )
        
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise ValueError(f"Model returned empty content")
        
        result = parse_ai_json(content)
        tokens = response.usage.total_tokens if response.usage else 0
        
        # Enrich article
        article[f"ai_summary_{target_lang}"] = result.get(f"ai_summary_{target_lang}", "")
        article["styled_content"] = result.get("styled_content", "")
        article["styled_hashtags"] = result.get("hashtags", [])
        article["styled_char_count"] = result.get("char_count", 0)
        article["ai_status"] = "done"
        article["styled_tokens"] = tokens
        
        # Cache result (1 hour TTL)
        import json
        cache_data = {
            f"ai_summary_{target_lang}": article[f"ai_summary_{target_lang}"],
            "styled_content": article["styled_content"],
            "styled_hashtags": article["styled_hashtags"],
            "styled_char_count": article["styled_char_count"],
        }
        await redis.setex(cache_key, 3600, json.dumps(cache_data))
        
        logger.info(f"Channel {channel['id']}: rewrite+style done for {article['id']} ({tokens} tokens)")
        return article
        
    except Exception as e:
        logger.error(f"Channel {channel['id']}: rewrite+style failed for {article['id']}: {e}")
        raise


async def process_synthetic(
    articles: list[dict[str, Any]],
    channel: dict[str, Any],
    redis,
) -> dict[str, Any]:
    """
    Process multiple articles into synthetic summary.
    
    Args:
        articles: List of source articles (3-10 recommended)
        channel: Channel config
        redis: Redis connection
        
    Returns:
        Synthetic article dict
    """
    if len(articles) < 2:
        raise ValueError("Need at least 2 articles for synthesis")
    
    category = articles[0].get("category", "general")
    target_lang = channel.get("target_language", "en")
    
    # Generate cache key from source article IDs
    source_ids = sorted([a["id"] for a in articles])
    batch_hash = hashlib.sha256("".join(source_ids).encode()).hexdigest()[:16]
    cache_key = f"channel:{channel['id']}:synthetic:{category}:{batch_hash}"
    
    # Check cache
    cached = await redis.get(cache_key)
    if cached:
        logger.info(f"Channel {channel['id']}: synthetic cache hit for {category}")
        import json
        return json.loads(cached.decode())
    
    # Process with AI
    try:
        # Use topic_synthesis logic
        from ai.topic_synthesis import synthesize_topic_articles
        
        results = await synthesize_topic_articles(
            articles=articles,
            category=category,
            redis=redis,
            target_language=target_lang,
        )
        
        if not results:
            raise ValueError("Synthesis produced no results")
        
        # Take first result
        synthetic = results[0]
        synthetic["source_article_ids"] = source_ids
        synthetic["num_source_articles"] = len(articles)
        synthetic["type"] = "synthetic"
        synthetic["category"] = category
        
        # Cache result (1 hour TTL)
        import json
        await redis.setex(cache_key, 3600, json.dumps(synthetic))
        
        return synthetic
    except Exception as e:
        logger.error(f"Channel {channel['id']}: synthesis failed for {category}: {e}")
        raise


async def process_debate(
    articles: list[dict[str, Any]],
    channel: dict[str, Any],
    redis,
) -> dict[str, Any]:
    """
    Process multiple articles into debate format.
    
    Args:
        articles: List of source articles (3-10 recommended)
        channel: Channel config
        redis: Redis connection
        
    Returns:
        Debate article dict
    """
    if len(articles) < 2:
        raise ValueError("Need at least 2 articles for debate")
    
    category = articles[0].get("category", "general")
    target_lang = channel.get("target_language", "en")
    
    # Generate cache key from source article IDs
    source_ids = sorted([a["id"] for a in articles])
    batch_hash = hashlib.sha256("".join(source_ids).encode()).hexdigest()[:16]
    cache_key = f"channel:{channel['id']}:debate:{category}:{batch_hash}"
    
    # Check cache
    cached = await redis.get(cache_key)
    if cached:
        logger.info(f"Channel {channel['id']}: debate cache hit for {category}")
        import json
        return json.loads(cached.decode())
    
    # Process with AI (debate detection + synthesis)
    try:
        # Use synthesis with debate-focused prompt
        from ai.topic_synthesis import synthesize_topic_articles
        
        results = await synthesize_topic_articles(
            articles=articles,
            category=category,
            redis=redis,
            target_language=target_lang,
        )
        
        if not results:
            raise ValueError("Debate generation produced no results")
        
        # Take first result and mark as debate
        debate = results[0]
        debate["source_article_ids"] = source_ids
        debate["num_source_articles"] = len(articles)
        debate["type"] = "debate"
        debate["category"] = category
        
        # Cache result (1 hour TTL)
        import json
        await redis.setex(cache_key, 3600, json.dumps(debate))
        
        return debate
    except Exception as e:
        logger.error(f"Channel {channel['id']}: debate generation failed for {category}: {e}")
        raise
