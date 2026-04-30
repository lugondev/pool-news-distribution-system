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

import httpx
from openai import APITimeoutError, RateLimitError
from ai.rewriter import (
    rewrite_article,
    LANG_NAMES,
    TONE_PROMPTS,
    _load_ai_config,
    get_openai_client,
    AUTO_LANG,
    is_auto_lang,
    audience_pick_instruction,
)
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

    # Article content
    title = article.get("title", "")
    content = article.get("content") or article.get("summary", "")
    url = article.get("url", "")
    source = article.get("source_name", "")

    if is_auto_lang(target_lang):
        prompt = f"""You are rewriting this article. First, decide the OUTPUT language.

{audience_pick_instruction()}

TONE & STYLE: {tone_instruction}

OUTPUT FORMAT: {format_instruction}

CONSTRAINTS:
- Maximum styled length: {max_length} characters
- {"Include 1-3 relevant hashtags" if include_hashtags else "Do NOT include hashtags"}
- {"Include the source URL at the end" if include_link else "Do NOT include any URLs"}

SOURCE ARTICLE:
Title: {title}
Source: {source}
URL: {url}
Content: {content[:2000]}

Respond in JSON. Use the language you picked for BOTH the rewrite and the styled output.

{{"chosen_lang": "<one of the allowed codes>", "ai_summary_target": "full rewritten article in the chosen language", "styled_content": "formatted output following OUTPUT FORMAT, ≤ {max_length} chars", "hashtags": ["tag1", "tag2"], "char_count": 123}}

IMPORTANT: styled_content must be at most {max_length} characters. Count carefully."""
        return prompt

    # Fixed-language mode (existing behavior)
    lang_name = LANG_NAMES.get(target_lang, target_lang.upper())
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
    auto_target = is_auto_lang(target_lang)
    ai_config_id = channel.get("ai_config_id", "")

    # Determine if we need style formatting
    needs_style = (
        platform != "custom"
        or output_format != "summary"
        or (style_config or {}).get("custom_prompt")
        or client_style_prompt
    )

    # Cache key. In auto mode we cache under a literal "auto" segment so that the
    # first AI choice wins for the TTL — both deterministic and quota-friendly.
    cache_lang_segment = AUTO_LANG if auto_target else target_lang
    if needs_style:
        cache_key = f"channel:{channel['id']}:rewrite:{article['id']}:{cache_lang_segment}:{output_format}"
    else:
        cache_key = f"channel:{channel['id']}:rewrite:{article['id']}:{cache_lang_segment}"
    
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
        base_url = client_base_url or "https://api.openai.com/v1"
        model = client_model
    else:
        # Use provider routing for rewrite action
        from ai.provider_routing import get_provider_for_action
        api_key, base_url, model = get_provider_for_action("rewrite")
    
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
        if auto_target:
            schema = {
                "type": "object",
                "properties": {
                    "chosen_lang": {"type": "string"},
                    "ai_summary_target": {"type": "string"},
                    "styled_content": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "char_count": {"type": "integer"},
                },
                "required": ["chosen_lang", "ai_summary_target", "styled_content"],
            }
        else:
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
        # Load timeout from settings
        from dashboard.config_io import read_settings
        cfg = read_settings()
        timeout = cfg.get("channels_config", {}).get("ai_timeout_seconds", 60)
        
        client = get_openai_client(api_key=api_key, base_url=base_url, timeout=timeout)
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

        if auto_target:
            raw_chosen = (result.get("chosen_lang") or "").strip().lower()
            chosen = "".join(c for c in raw_chosen if c.isalpha())[:2]
            if chosen not in LANG_NAMES:
                raise ValueError(
                    f"AI auto-mode returned invalid chosen_lang={raw_chosen!r}; "
                    f"expected one of {sorted(LANG_NAMES)}"
                )
            effective_lang = chosen
            ai_summary_text = result.get("ai_summary_target", "")
            article["ai_target_lang"] = chosen
            logger.info(
                f"Channel {channel['id']}: auto-detect picked '{chosen}' for {article['id']}"
            )
        else:
            effective_lang = target_lang
            ai_summary_text = result.get(f"ai_summary_{target_lang}", "")

        # Enrich article (always store under the effective language code)
        article[f"ai_summary_{effective_lang}"] = ai_summary_text
        article["styled_content"] = result.get("styled_content", "")
        article["styled_hashtags"] = result.get("hashtags", [])
        article["styled_char_count"] = result.get("char_count", 0)
        article["ai_status"] = "done"
        article["styled_tokens"] = tokens

        # Backward compatibility aliases
        article["content_target"] = article["styled_content"]  # For old templates
        article["title_target"] = article.get("title", "")  # For completeness

        # Cache result (1 hour TTL). For auto mode the chosen lang is also stored
        # so cache hits restore article["ai_target_lang"].
        import json
        cache_data = {
            f"ai_summary_{effective_lang}": ai_summary_text,
            "styled_content": article["styled_content"],
            "styled_hashtags": article["styled_hashtags"],
            "styled_char_count": article["styled_char_count"],
            "content_target": article["styled_content"],  # Backward compat alias
        }
        if auto_target:
            cache_data["ai_target_lang"] = effective_lang
        await redis.setex(cache_key, 3600, json.dumps(cache_data))

        logger.info(f"Channel {channel['id']}: rewrite+style done for {article['id']} ({tokens} tokens)")
        return article
        
    except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException, APITimeoutError) as e:
        logger.error(f"Channel {channel['id']}: rewrite+style timeout for {article['id']} (>{timeout}s): {e}")
        raise ValueError(f"AI processing timeout after {timeout}s")
    except RateLimitError as e:
        logger.error(f"Channel {channel['id']}: rate limit hit for {article['id']}: {e}")
        raise ValueError(f"AI rate limit exceeded: {str(e)}")
    except Exception as e:
        logger.error(f"Channel {channel['id']}: rewrite+style failed for {article['id']}: {e}")
        raise


async def process_synthetic(
    articles: list[dict[str, Any]],
    channel: dict[str, Any],
    redis,
) -> dict[str, Any] | None:
    """
    Process multiple articles into synthetic summary.
    
    Args:
        articles: List of source articles (3-10 recommended)
        channel: Channel config
        redis: Redis connection
        
    Returns:
        Synthetic article dict, or None if synthesis produces no valid results
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
        # Use topic_synthesis logic with provider routing
        from ai.topic_synthesis import synthesize_topic_articles
        from ai.provider_routing import get_provider_for_action
        
        api_key, base_url, model = get_provider_for_action("synthesis")
        
        results = await synthesize_topic_articles(
            articles=articles,
            category=category,
            redis=redis,
            target_language=target_lang,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        
        if not results:
            logger.warning(f"Channel {channel['id']}: synthesis produced no valid results for {category}")
            return None
        
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
    except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException, APITimeoutError) as e:
        logger.error(f"Channel {channel['id']}: synthesis timeout for {category}: {e}")
        raise ValueError(f"AI synthesis timeout")
    except RateLimitError as e:
        logger.error(f"Channel {channel['id']}: rate limit hit for synthesis {category}: {e}")
        raise ValueError(f"AI rate limit exceeded: {str(e)}")
    except Exception as e:
        logger.error(f"Channel {channel['id']}: synthesis failed for {category}: {e}")
        raise


async def process_debate(
    articles: list[dict[str, Any]],
    channel: dict[str, Any],
    redis,
) -> dict[str, Any] | None:
    """
    Process multiple articles into debate format.
    
    Args:
        articles: List of source articles (3-10 recommended)
        channel: Channel config
        redis: Redis connection
        
    Returns:
        Debate article dict, or None if debate generation produces no valid results
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
        # Use synthesis with debate-focused prompt and provider routing
        from ai.topic_synthesis import synthesize_topic_articles
        from ai.provider_routing import get_provider_for_action
        
        api_key, base_url, model = get_provider_for_action("debate")
        
        results = await synthesize_topic_articles(
            articles=articles,
            category=category,
            redis=redis,
            target_language=target_lang,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        
        if not results:
            logger.warning(f"Channel {channel['id']}: debate generation produced no valid results for {category}")
            return None
        
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
    except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException, APITimeoutError) as e:
        logger.error(f"Channel {channel['id']}: debate timeout for {category}: {e}")
        raise ValueError(f"AI debate timeout")
    except RateLimitError as e:
        logger.error(f"Channel {channel['id']}: rate limit hit for debate {category}: {e}")
        raise ValueError(f"AI rate limit exceeded: {str(e)}")
    except Exception as e:
        logger.error(f"Channel {channel['id']}: debate generation failed for {category}: {e}")
        raise
