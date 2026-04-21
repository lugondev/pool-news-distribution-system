"""
On-demand AI style transform for Content Channels.

Transforms raw articles into platform-specific styled content when a client
pulls from a channel's feed endpoint.  AI runs **only on pull** — no pull = no
quota spent.

Architecture (2-axis):
  Content Mode  (what to generate):
    rewrite      — single-article summary
    synthetic    — multi-article topic synthesis (reuses topic_synthesis)
    newsletter   — daily digest with sections
    long_article — long-form narrative piece
    debate       — multi-perspective debate

  Output Format (how to present):
    summary          — concise paragraph
    thread           — numbered thread (Twitter-style)
    breaking         — urgent breaking-news style
    listicle         — bullet-point list
    hot_take         — opinionated punchy take
    deep_dive        — detailed analysis
    quote_highlight  — pull-quote focused
    carousel         — slide-by-slide cards

AI Source:
    system — use the system's configured AI provider
    client — use client-provided credentials (X-AI-Base-URL, X-AI-API-Key, X-AI-Model headers)

Style Source:
    preset — platform preset (twitter, facebook, blog, telegram)
    custom — channel-level custom style config
    client — client sends style_prompt via query param

Redis cache: channel:{ch_id}:styled:{article_id}:{output_format} — avoids re-running AI
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from ai.provider_utils import build_response_format, parse_ai_json
from ai.rewriter import LANG_NAMES, TONE_PROMPTS, _load_ai_config, get_openai_client

logger = logging.getLogger(__name__)

# ── Platform presets ─────────────────────────────────────────────────────────

PLATFORM_PRESETS: dict[str, dict] = {
    "twitter": {
        "max_length": 280,
        "tone": "casual",
        "include_hashtags": True,
        "include_link": True,
        "instruction": (
            "Write for Twitter/X. Be punchy, concise, attention-grabbing. "
            "Use 1-2 relevant hashtags. Stay under 280 characters including hashtags. "
            "Make every word count."
        ),
    },
    "facebook": {
        "max_length": 2000,
        "tone": "casual",
        "include_hashtags": False,
        "include_link": True,
        "instruction": (
            "Write for Facebook. Be engaging and conversational. "
            "Encourage discussion — ask a question or share an insight. "
            "Use line breaks for readability. 2-4 paragraphs max."
        ),
    },
    "blog": {
        "max_length": 5000,
        "tone": "formal",
        "include_hashtags": False,
        "include_link": False,
        "instruction": (
            "Write for a professional blog. Use formal, authoritative tone. "
            "Include context, analysis, and implications. "
            "Structure with clear paragraphs. Be thorough but not verbose."
        ),
    },
    "telegram": {
        "max_length": 4096,
        "tone": "general",
        "include_hashtags": True,
        "include_link": True,
        "instruction": (
            "Write for Telegram. Be concise and informative. "
            "Use bold for key points. Keep it scannable. "
            "1-2 relevant hashtags at the end."
        ),
    },
    "custom": {
        "max_length": 2000,
        "tone": "general",
        "include_hashtags": False,
        "include_link": False,
        "instruction": "",
    },
}

# ── Output format instructions ───────────────────────────────────────────────

OUTPUT_FORMAT_INSTRUCTIONS: dict[str, str] = {
    "summary": (
        "Write a concise summary paragraph. Lead with the most important fact. "
        "Include who, what, when, where, why."
    ),
    "thread": (
        "Write as a numbered thread (1/N format). Each point should be a standalone "
        "insight. Start with a hook. End with a takeaway. 3-7 points."
    ),
    "breaking": (
        "Write as a BREAKING NEWS alert. Lead with the core fact in ALL CAPS or bold. "
        "Follow with 1-2 sentences of essential context. Urgent, factual tone."
    ),
    "listicle": (
        "Write as a bullet-point list. Each bullet is a key fact or insight. "
        "Use emoji bullets where appropriate. 4-8 bullets. Brief intro line."
    ),
    "hot_take": (
        "Write a sharp, opinionated take. Be bold and provocative but grounded in facts. "
        "State a clear position. 2-3 sentences max. Conversational and punchy."
    ),
    "deep_dive": (
        "Write a detailed analysis. Cover background, current situation, implications, "
        "and what to watch next. Use subheadings if needed. Be thorough."
    ),
    "quote_highlight": (
        "Extract or craft the most impactful quote or statement from the content. "
        "Present it prominently, then add 1-2 sentences of context below."
    ),
    "carousel": (
        "Write content as a series of 3-6 slides/cards. Each card has a bold headline "
        "and 1-2 sentences. First card is the hook, last card is the CTA/takeaway. "
        'Format: "Slide 1: [headline]\\n[content]\\nSlide 2: ..." etc.'
    ),
}

VALID_CONTENT_MODES = {"rewrite", "synthetic", "newsletter", "long_article", "debate"}
VALID_OUTPUT_FORMATS = set(OUTPUT_FORMAT_INSTRUCTIONS.keys())
VALID_PLATFORMS = set(PLATFORM_PRESETS.keys())
VALID_AI_SOURCES = {"system", "client"}
VALID_STYLE_SOURCES = {"preset", "custom", "client"}

# ── JSON schema for styled output ────────────────────────────────────────────

SCHEMA_STYLED_OUTPUT = {
    "type": "object",
    "properties": {
        "styled_content": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "char_count": {"type": "integer"},
    },
    "required": ["styled_content"],
}


# ── Core transform function ─────────────────────────────────────────────────


def _build_style_prompt(
    article: dict,
    platform: str,
    output_format: str,
    style_config: dict | None = None,
    style_prompt_override: str | None = None,
    language: str = "en",
) -> str:
    """Build the full prompt for style transformation."""
    preset = PLATFORM_PRESETS.get(platform, PLATFORM_PRESETS["custom"])
    style = style_config or {}

    # Resolve max_length: style config > preset
    max_length = style.get("max_length") or preset["max_length"]
    tone = style.get("tone") or preset["tone"]
    include_hashtags = style.get("include_hashtags", preset.get("include_hashtags", False))
    include_link = style.get("include_link", preset.get("include_link", False))

    # Build tone instruction
    custom_prompt = style.get("custom_prompt", "")
    if style_prompt_override:
        tone_instruction = style_prompt_override
    elif custom_prompt:
        tone_instruction = custom_prompt
    elif preset["instruction"]:
        tone_instruction = preset["instruction"]
    else:
        tone_instruction = TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])

    # Output format instruction
    format_instruction = OUTPUT_FORMAT_INSTRUCTIONS.get(
        output_format, OUTPUT_FORMAT_INSTRUCTIONS["summary"]
    )

    # Language
    lang_name = LANG_NAMES.get(language, language.upper())

    # Article content
    title = article.get("title", "")
    content = (
        article.get("ai_summary_vi")
        or article.get("ai_summary_en")
        or article.get("content")
        or article.get("summary")
        or ""
    )
    url = article.get("url", "")
    source = article.get("source_name", "")

    prompt = f"""{tone_instruction}

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

Respond in JSON:
{{"styled_content": "your styled output here", "hashtags": ["tag1", "tag2"], "char_count": 123}}

IMPORTANT: styled_content must be at most {max_length} characters. Count carefully."""

    return prompt


def _cache_key(channel_id: str, article_id: str, output_format: str) -> str:
    """Redis key for cached styled output."""
    return f"channel:{channel_id}:styled:{article_id}:{output_format}"


async def get_cached_styled(
    redis: aioredis.Redis,
    channel_id: str,
    article_id: str,
    output_format: str,
) -> dict | None:
    """Return cached styled output or None."""
    key = _cache_key(channel_id, article_id, output_format)
    raw = await redis.get(key)
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


async def cache_styled(
    redis: aioredis.Redis,
    channel_id: str,
    article_id: str,
    output_format: str,
    result: dict,
    ttl: int = 43200,  # 12h — match article TTL
) -> None:
    """Cache styled output in Redis."""
    key = _cache_key(channel_id, article_id, output_format)
    await redis.set(key, json.dumps(result, ensure_ascii=False), ex=ttl)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def _call_style_ai(
    prompt: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int = 500,
    temperature: float = 0.5,
) -> tuple[dict, int]:
    """Call AI for style transform. Returns (result_dict, tokens_used)."""
    client = get_openai_client(api_key=api_key, base_url=base_url)

    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=build_response_format(base_url, "styled_output", SCHEMA_STYLED_OUTPUT),
    )

    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise ValueError(f"Model returned empty content (model={model})")

    result = parse_ai_json(content)
    tokens = response.usage.total_tokens if response.usage else 0
    return result, tokens


async def style_transform_article(
    article: dict,
    channel: dict,
    redis: aioredis.Redis,
    # Client-provided overrides (from headers/query params)
    client_api_key: str | None = None,
    client_base_url: str | None = None,
    client_model: str | None = None,
    client_style_prompt: str | None = None,
) -> dict:
    """
    Transform a single article into styled content for a channel.

    Uses Redis cache to avoid re-running AI for the same article+channel+format.
    Returns the original article dict enriched with styled_content, styled_hashtags.
    """
    channel_id = channel.get("id", "")
    platform = channel.get("platform", "custom")
    output_format = channel.get("output_format", "summary")
    ai_source = channel.get("ai_source", "system")
    style_source = channel.get("style_source", "preset")
    style_config = channel.get("style", {})
    article_id = article.get("id", "")

    # Skip if no platform/format configured (backward compat — channels without style)
    if platform == "custom" and output_format == "summary" and not style_config.get("custom_prompt") and not client_style_prompt:
        return article

    # Check cache (skip if client provides custom style_prompt — always fresh)
    if not client_style_prompt:
        cached = await get_cached_styled(redis, channel_id, article_id, output_format)
        if cached:
            article["styled_content"] = cached.get("styled_content", "")
            article["styled_hashtags"] = cached.get("hashtags", [])
            article["styled_char_count"] = cached.get("char_count", 0)
            article["styled_cached"] = True
            return article

    # Resolve AI credentials
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
        logger.warning(f"No AI credentials for channel {channel_id}, skipping style transform")
        return article

    # Resolve style prompt
    if style_source == "client" and client_style_prompt:
        style_prompt_override = client_style_prompt
    else:
        style_prompt_override = None

    # Resolve language
    language = channel.get("target_language") or article.get("lang", "en") or "en"

    # Build prompt
    prompt = _build_style_prompt(
        article=article,
        platform=platform,
        output_format=output_format,
        style_config=style_config if style_source == "custom" else None,
        style_prompt_override=style_prompt_override,
        language=language[:2],
    )

    # Determine max_tokens based on platform
    preset = PLATFORM_PRESETS.get(platform, PLATFORM_PRESETS["custom"])
    max_length = style_config.get("max_length") or preset["max_length"]
    # Rough estimate: 1 token ≈ 4 chars, add buffer
    max_tokens = min(max(max_length // 3, 200), 2000)

    try:
        result, tokens = await _call_style_ai(
            prompt=prompt,
            model=model or "",
            api_key=api_key,
            base_url=base_url,
            max_tokens=max_tokens,
            temperature=0.5,
        )
    except Exception as e:
        logger.error(f"Style transform failed for {article_id} on channel {channel_id}: {e}")
        return article

    # Enrich article
    styled_content = result.get("styled_content", "")
    hashtags = result.get("hashtags", [])
    char_count = result.get("char_count", len(styled_content))

    article["styled_content"] = styled_content
    article["styled_hashtags"] = hashtags
    article["styled_char_count"] = char_count
    article["styled_cached"] = False
    article["styled_tokens"] = tokens

    # Cache result (only for non-client style prompts)
    if not client_style_prompt:
        await cache_styled(redis, channel_id, article_id, output_format, {
            "styled_content": styled_content,
            "hashtags": hashtags,
            "char_count": char_count,
        })

    logger.debug(
        f"Style transform: {article_id} → {platform}/{output_format} "
        f"({char_count} chars, {tokens} tokens)"
    )
    return article


async def style_transform_batch(
    articles: list[dict],
    channel: dict,
    redis: aioredis.Redis,
    client_api_key: str | None = None,
    client_base_url: str | None = None,
    client_model: str | None = None,
    client_style_prompt: str | None = None,
    max_concurrent: int = 3,
) -> list[dict]:
    """Transform a batch of articles with concurrency control."""
    import asyncio

    sem = asyncio.Semaphore(max_concurrent)

    async def _transform_one(art: dict) -> dict:
        async with sem:
            return await style_transform_article(
                article=art,
                channel=channel,
                redis=redis,
                client_api_key=client_api_key,
                client_base_url=client_base_url,
                client_model=client_model,
                client_style_prompt=client_style_prompt,
            )

    results = await asyncio.gather(
        *[_transform_one(a) for a in articles], return_exceptions=True
    )

    # Filter out exceptions, return successfully transformed articles
    out = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Style transform batch error: {r}")
        elif isinstance(r, dict):
            out.append(r)
    return out
