"""
AI rewriter: tóm tắt + dịch bài sang VI/EN dùng OpenAI-compatible API.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

import redis.asyncio as aioredis
from crawler.dedup import check_ai_duplicate, register_ai_simhash
from storage.redis_store import pop_pending_ai_articles, update_article_ai, update_article_ai_config
from storage.sqlite_stats import log_ai_usage
from webhook.dispatcher import enqueue_dispatch

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_client_fingerprint: str | None = None


def _load_ai_config() -> dict:
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("ai", {})


def get_openai_client(
    api_key: str | None = None, base_url: str | None = None
) -> AsyncOpenAI:
    """
    Return a cached AsyncOpenAI client. Recreates if connection params changed.
    All config comes from settings.yaml (managed via Settings UI).
    """
    global _client, _client_fingerprint

    if not api_key or not base_url:
        cfg = _load_ai_config()
        api_key = api_key or cfg.get("api_key", "")
        base_url = base_url or cfg.get("base_url", "https://api.openai.com/v1")

    fingerprint = f"{api_key}|{base_url}"

    if _client is not None and _client_fingerprint == fingerprint:
        return _client

    _client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers={
            "HTTP-Referer": "https://github.com/news-aggregator",
            "X-Title": "News Aggregator",
        },
    )
    _client_fingerprint = fingerprint
    logger.info(f"OpenAI client initialized: base_url={base_url}")
    return _client


TONE_PROMPTS = {
    "formal": (
        "You are a serious, authoritative news editor. "
        "Write in a formal, objective tone — like a broadcast anchor or newspaper editorial. "
        "Use precise language, avoid colloquialisms."
    ),
    "casual": (
        "You are a friendly, upbeat news writer. "
        "Write in a light, conversational tone — engaging and easy to read. "
        "Use natural everyday language, keep it fun but still accurate."
    ),
    "general": (
        "You are a professional news editor. "
        "Write in a clear, neutral, informative tone. "
        "Be concise, factual, and natural."
    ),
}

LANG_NAMES: dict[str, str] = {
    "en": "English", "vi": "Vietnamese", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "fr": "French", "es": "Spanish", "de": "German",
    "pt": "Portuguese", "ar": "Arabic", "th": "Thai", "id": "Indonesian",
    "ms": "Malay", "ru": "Russian", "tr": "Turkish", "it": "Italian",
}

SUMMARIZE_PROMPT = """{tone_instruction}

You are writing for readers who have NOT read the original article. Deliver the complete information clearly so they understand the story without needing the source.

Given the following news article, write {lang_count} that answers:
- WHAT happened (the core event/development)
- WHO is involved (key people, organizations, countries)
- WHERE/WHEN it occurred (if relevant)
- WHY it matters (impact, significance, context)

Structure each summary:
1. Lead sentence: Most important information first (what + who)
2. Context: Background or why this matters
3. Impact/Outcome: What this means or what happens next

Requirements:
{lang_requirements}- Write naturally in each language — not a word-for-word translation
- Each summary must be standalone (no references to "the article")
- NO marketing fluff, NO opinions — just facts with context

Article title: {title}
Article content: {content}

{lang_json_format}"""


def _build_lang_spec(
    origin_lang: str,
    target_lang: str | None,
    length_guidance: str,
) -> tuple[str, str, str]:
    """Return (lang_count, lang_requirements, lang_json_format) for the prompt."""
    origin_name = LANG_NAMES.get(origin_lang, origin_lang.upper())

    if target_lang and target_lang != origin_lang:
        target_name = LANG_NAMES.get(target_lang, target_lang.upper())
        count = "two summaries"
        reqs = (
            f"- {origin_name}: {length_guidance}\n"
            f"- {target_name}: {length_guidance}\n"
        )
        fmt = (
            f'Respond in JSON format:\n'
            f'{{"{origin_lang}": "{origin_name} summary here",'
            f' "{target_lang}": "{target_name} summary here"}}'
        )
    else:
        count = "one summary"
        reqs = f"- {origin_name}: {length_guidance}\n"
        fmt = f'Respond in JSON format:\n{{"{origin_lang}": "{origin_name} summary here"}}'

    return count, reqs, fmt


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def rewrite_article(
    article: dict,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.3,
    tone: str = "general",
    api_key: str | None = None,
    base_url: str | None = None,
    prompt_system_override: str | None = None,
    prompt_template_override: str | None = None,
    origin_lang: str = "en",
    target_lang: str | None = None,
) -> tuple[dict[str, str], int]:
    """
    Returns (summaries, tokens_used).
    summaries = {lang_code: text} — always has origin_lang key, has target_lang key if set.
    """
    client = get_openai_client(api_key=api_key, base_url=base_url)
    cfg = _load_ai_config()
    resolved_model = model or cfg.get("model", "gpt-4o-mini")
    custom_system = (prompt_system_override or cfg.get("prompt_system") or "").strip()
    custom_template = (prompt_template_override or cfg.get("prompt_template") or "").strip()
    tone_instruction = custom_system or TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])

    if cfg.get("output_limit_enabled"):
        max_chars = int(cfg.get("output_limit_chars") or 250)
        tone_instruction += (
            f"\nIMPORTANT: Each summary must be at most {max_chars} characters. "
            "Count every character including spaces."
        )
        length_guidance = f"at most {max_chars} characters"
    else:
        length_guidance = "2-3 sentences"

    lang_count, lang_requirements, lang_json_format = _build_lang_spec(
        origin_lang, target_lang, length_guidance
    )

    article_content = article.get("content") or article.get("summary") or ""
    title = article.get("title", "")

    if custom_template:
        # Custom templates can use {lang_count}, {lang_requirements}, {lang_json_format}
        # or the old-style {length_guidance} for backward compat
        prompt = custom_template.format(
            tone_instruction=tone_instruction,
            length_guidance=length_guidance,
            lang_count=lang_count,
            lang_requirements=lang_requirements,
            lang_json_format=lang_json_format,
            title=title,
            content=article_content[:1500],
        )
    else:
        prompt = SUMMARIZE_PROMPT.format(
            tone_instruction=tone_instruction,
            lang_count=lang_count,
            lang_requirements=lang_requirements,
            lang_json_format=lang_json_format,
            title=title,
            content=article_content[:1500],
        )

    response = await client.chat.completions.create(
        model=resolved_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        temperature=temperature,
    )

    resp_content = response.choices[0].message.content if response.choices else None
    if not resp_content:
        raise ValueError(f"Model returned empty content (model={resolved_model})")
    result = json.loads(resp_content)
    tokens = response.usage.total_tokens if response.usage else 0

    summaries: dict[str, str] = {}
    summaries[origin_lang] = result.get(origin_lang, "")
    if target_lang and target_lang != origin_lang:
        summaries[target_lang] = result.get(target_lang, "")

    return summaries, tokens


async def test_ai_connection(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    tone: str = "general",
) -> dict:
    """Send a short test prompt to verify AI connectivity. Returns result dict."""
    import time

    cfg = _load_ai_config()
    resolved_key = api_key or cfg.get("api_key", "")
    resolved_url = base_url or cfg.get("base_url", "https://api.openai.com/v1")
    resolved_model = model or cfg.get("model", "gpt-4o-mini")
    custom_system = (cfg.get("prompt_system") or "").strip()
    tone_instruction = custom_system or TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])

    if not resolved_key:
        return {"ok": False, "error": "API key is empty", "ms": 0}

    try:
        client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=resolved_url,
            default_headers={
                "HTTP-Referer": "https://github.com/news-aggregator",
                "X-Title": "News Aggregator",
            },
        )
        t0 = time.monotonic()
        response = await client.chat.completions.create(
            model=resolved_model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{tone_instruction}\n\n"
                        "Summarize this test headline in JSON: "
                        '{"vi": "...", "en": "..."}\n\n'
                        "Headline: Global markets rally on trade deal optimism"
                    ),
                }
            ],
            max_tokens=120,
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        ms = int((time.monotonic() - t0) * 1000)
        content = response.choices[0].message.content if response.choices else None
        if not content:
            return {
                "ok": False,
                "error": "Model returned empty content",
                "model": resolved_model,
                "base_url": resolved_url,
                "ms": ms,
            }
        tokens = response.usage.total_tokens if response.usage else 0
        result = json.loads(content)
        return {
            "ok": True,
            "model": resolved_model,
            "base_url": resolved_url,
            "tone": tone,
            "ms": ms,
            "tokens": tokens,
            "vi": result.get("vi", ""),
            "en": result.get("en", ""),
        }
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000) if "t0" in dir() else 0
        return {
            "ok": False,
            "error": str(e),
            "model": resolved_model,
            "base_url": resolved_url,
            "ms": ms,
        }


async def _get_category_counts(
    redis: aioredis.Redis, window_hours: float = 2.0
) -> dict[str, int]:
    """
    Count articles per category fetched within the last `window_hours`.
    Samples up to 500 recent entries from the main feed sorted set.
    Used to distinguish high-volume ("busy") from low-volume ("quiet") categories.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).timestamp()
    ids = await redis.zrangebyscore("news:feed", cutoff, "+inf", start=0, num=500)
    if not ids:
        return {}
    pipe = redis.pipeline()
    for aid in ids:
        aid_str = aid.decode() if isinstance(aid, bytes) else aid
        pipe.hget(f"news:{aid_str}", "category")
    cats = await pipe.execute()
    counts: dict[str, int] = {}
    for c in cats:
        if c:
            key = c.decode() if isinstance(c, bytes) else c
            counts[key] = counts.get(key, 0) + 1
    return counts


def _max_age_for_category(cat: str, counts: dict[str, int]) -> int:
    """
    Return max article age (seconds) before it is skipped for AI processing.

    Thresholds scale with category volume:
      - busy   (top third by article count)  → 5 min  — plenty of fresh articles
      - moderate (middle third)              → 10 min
      - quiet  (bottom third / unknown)      → 15 min — rare topics, still worth translating

    If no count data is available, defaults to 10 min.
    """
    if not counts:
        return 10 * 60

    sorted_vals = sorted(counts.values())
    n = len(sorted_vals)
    low_thresh = sorted_vals[n // 3]
    high_thresh = sorted_vals[(n * 2) // 3]

    cat_count = counts.get(cat, 0)
    if cat_count >= high_thresh:
        return 5 * 60
    elif cat_count >= low_thresh:
        return 10 * 60
    else:
        return 15 * 60


async def process_pending_articles(
    redis: aioredis.Redis,
    model: str | None = None,
    batch_size: int = 10,
    max_tokens: int = 300,
    temperature: float = 0.3,
    tone: str = "general",
    api_key: str | None = None,
    base_url: str | None = None,
    webhook_endpoints: list[dict] | None = None,
    telegram_channels: list[dict] | None = None,
    raw_webhook_endpoints: list[dict] | None = None,
    raw_telegram_channels: list[dict] | None = None,
    spread_seconds: float = 0,
    ai_dedup_threshold: int = 6,
    pre_fetched_articles: list[dict] | None = None,
    config_id: str | None = None,
    prompt_system_override: str | None = None,
    prompt_template_override: str | None = None,
    perform_ai_dedup: bool = True,
    target_language: str | None = None,
) -> int:
    """
    Lấy các bài pending, rewrite rồi dispatch webhook + telegram.
    If spread_seconds > 0, articles are processed one-by-one with even delays
    instead of all-at-once, to avoid bursts.
    If pre_fetched_articles is provided, uses those instead of popping from queue.
    If config_id is set, stores summaries under config-specific fields for per-config dispatch.
    Returns số bài đã xử lý.
    """
    if pre_fetched_articles is not None:
        articles = pre_fetched_articles
    else:
        articles = await pop_pending_ai_articles(redis, limit=batch_size)
    if not articles:
        return 0

    # Sample category volumes once per batch to determine freshness thresholds.
    category_counts = await _get_category_counts(redis)

    # inter_delay spaces out actual AI calls — computed against full batch size
    # so throughput matches the spread window even if some articles are dedup-skipped.
    inter_delay = (
        spread_seconds / len(articles)
        if spread_seconds > 0 and len(articles) > 1
        else 0
    )

    processed = 0
    ai_call_count = 0  # tracks actual AI calls made (for rate limiting)

    for article in articles:
        title = article.get("title", "")

        # Per-config+lang dedup: skip if already processed with this exact (config, lang) combo
        _dedup_key = f"ai_done_{config_id or 'builtin'}_{target_language or 'origin'}"
        if config_id or target_language:
            cfg_done = await redis.hget(f"news:{article['id']}", _dedup_key)
            if cfg_done:
                processed += 1
                continue

        # Raw dispatch: send immediately without AI processing, for hooks with ai_mode="off"
        if raw_webhook_endpoints or raw_telegram_channels:
            await enqueue_dispatch(
                article,
                raw_webhook_endpoints or [],
                telegram_channels=raw_telegram_channels,
            )

        # Age-based skip: avoid wasting quota on stale articles.
        # Threshold scales with category volume — busy categories expire faster.
        fetched_at_str = article.get("fetched_at", "")
        if fetched_at_str:
            try:
                fetched_dt = datetime.fromisoformat(fetched_at_str)
                if fetched_dt.tzinfo is None:
                    fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
                age_sec = (datetime.now(timezone.utc) - fetched_dt).total_seconds()
                max_age = _max_age_for_category(
                    article.get("category", ""), category_counts
                )
                if age_sec > max_age:
                    await redis.hset(
                        f"news:{article['id']}", "ai_status", "age_skipped"
                    )
                    logger.debug(
                        f"Age-skipped {article['id']} "
                        f"(age={age_sec:.0f}s > max={max_age}s, cat={article.get('category')})"
                    )
                    processed += 1
                    continue
            except (ValueError, TypeError):
                pass

        # Skip AI processing if no rewrite hooks are configured
        if not webhook_endpoints and not telegram_channels:
            processed += 1
            continue

        # Pre-AI semantic dedup: check BEFORE sleeping to avoid wasted delay
        if perform_ai_dedup and ai_dedup_threshold > 0 and title:
            ai_dup = await check_ai_duplicate(
                redis, title, threshold=ai_dedup_threshold
            )
            if ai_dup.is_duplicate:
                await redis.hset(f"news:{article['id']}", "ai_status", "dedup_skipped")
                logger.info(
                    f"AI dedup skip {article['id']}: similar story already summarised"
                )
                processed += 1
                continue

        # Rate-limit only actual AI calls
        if ai_call_count > 0 and inter_delay > 0:
            await asyncio.sleep(inter_delay)

        # Determine the article's origin language
        raw_lang = article.get("lang") or article.get("declared_lang") or "en"
        origin_lang = raw_lang[:2].lower() if raw_lang and raw_lang != "und" else "en"
        tgt_lang = target_language if target_language and target_language != origin_lang else None

        try:
            summaries, tokens = await rewrite_article(
                article,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tone=tone,
                api_key=api_key,
                base_url=base_url,
                prompt_system_override=prompt_system_override,
                prompt_template_override=prompt_template_override,
                origin_lang=origin_lang,
                target_lang=tgt_lang,
            )
        except Exception as e:
            logger.warning(f"AI failed for {article['id']}: {e}")
            await redis.hset(f"news:{article['id']}", "ai_status", "failed")
            ai_call_count += 1
            continue

        ai_call_count += 1
        await update_article_ai(redis, article["id"], summaries)
        if config_id or target_language:
            await update_article_ai_config(redis, article["id"], summaries, config_id or "builtin")
            await redis.hset(f"news:{article['id']}", _dedup_key, "1")
        await log_ai_usage(article["id"], model, tokens)
        if title and perform_ai_dedup:
            await register_ai_simhash(redis, title)

        # Populate article dict for payload dispatch
        for lang, text in summaries.items():
            article[f"ai_summary_{lang}"] = text
        article["ai_summary_origin"] = summaries.get(origin_lang, "")
        article["ai_summary_target"] = summaries.get(tgt_lang, "") if tgt_lang else ""

        if webhook_endpoints or telegram_channels:
            await enqueue_dispatch(
                article,
                webhook_endpoints or [],
                telegram_channels=telegram_channels,
            )

        processed += 1
        logger.info(f"Processed article {article['id']}: {article['title'][:60]}...")

    return processed
