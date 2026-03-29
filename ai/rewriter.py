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
from ai.provider_utils import build_response_format, parse_ai_json, SCHEMA_LANG_SUMMARY, SCHEMA_TEST_SUMMARY
from crawler.dedup import check_ai_duplicate, register_ai_simhash
from storage.redis_store import (
    pop_pending_ai_articles,
    update_article_ai,
    update_article_ai_config,
)
from storage.sqlite_stats import log_ai_usage
from webhook.dispatcher import enqueue_dispatch

logger = logging.getLogger(__name__)

try:
    from realtime.manager import ws_manager
except ImportError:
    ws_manager = None

_client: AsyncOpenAI | None = None
_client_fingerprint: str | None = None


def _load_ai_config() -> dict:
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)
    ai = cfg.get("ai", {})
    # Inject the active provider's model as top-level "model" so all callers
    # that do cfg.get("model") get the value from settings.yaml, not a hardcode.
    if not ai.get("model"):
        pid = ai.get("provider_id")
        for p in ai.get("providers", []):
            if p.get("id") == pid and p.get("model"):
                ai = {**ai, "model": p["model"]}
                break
    return ai


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

    # Cloudflare Workers AI uses "Bearer <token>" — the SDK sends "Bearer <api_key>"
    # by default, so api_key should be set to just the token (no "Bearer " prefix).
    # Cloudflare requires response_format={"type":"json_schema"} — use build_response_format().
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
    "en": "English",
    "vi": "Vietnamese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "pt": "Portuguese",
    "ar": "Arabic",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "ru": "Russian",
    "tr": "Turkish",
    "it": "Italian",
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
            f"- {origin_name}: {length_guidance}\n- {target_name}: {length_guidance}\n"
        )
        fmt = (
            f"Respond in JSON format:\n"
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
    resolved_model = model or cfg.get("model", "")
    custom_system = (prompt_system_override or cfg.get("prompt_system") or "").strip()
    custom_template = (
        prompt_template_override or cfg.get("prompt_template") or ""
    ).strip()
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

    create_kwargs: dict = {
        "model": resolved_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": build_response_format(base_url, "summary", SCHEMA_LANG_SUMMARY),
    }

    response = await client.chat.completions.create(**create_kwargs)

    resp_content = response.choices[0].message.content if response.choices else None
    if not resp_content:
        raise ValueError(f"Model returned empty content (model={resolved_model})")

    result = parse_ai_json(resp_content)
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
    resolved_model = model or cfg.get("model", "")
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
            response_format=build_response_format(resolved_url, "test_summary", SCHEMA_TEST_SUMMARY),
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
        result = parse_ai_json(content)
        return {
            "ok": True,
            "model": resolved_model,
            "base_url": resolved_url,
            "tone": tone,
            "ms": ms,
            "tokens": tokens,
            "vi": result.get("vi", ""),
            "en": result.get("en", ""),
            "raw": result,
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


def _max_age_for_category(
    cat: str, counts: dict[str, int], ai_cfg: dict | None = None
) -> int:
    """
    Return max article age (seconds) before it is skipped for AI processing.

    Thresholds scale with category volume:
      - busy   (top third by article count)  → configurable (default 15 min)
      - moderate (middle third)              → configurable (default 20 min)
      - quiet  (bottom third / unknown)      → configurable (default 30 min)

    If no count data is available, defaults to moderate threshold.
    """
    if ai_cfg is None:
        ai_cfg = _load_ai_config()

    busy_mins = ai_cfg.get("age_threshold_busy_minutes", 15)
    moderate_mins = ai_cfg.get("age_threshold_moderate_minutes", 20)
    quiet_mins = ai_cfg.get("age_threshold_quiet_minutes", 30)

    if not counts:
        return moderate_mins * 60

    sorted_vals = sorted(counts.values())
    n = len(sorted_vals)
    low_thresh = sorted_vals[n // 3]
    high_thresh = sorted_vals[(n * 2) // 3]

    cat_count = counts.get(cat, 0)
    if cat_count >= high_thresh:
        return busy_mins * 60
    elif cat_count >= low_thresh:
        return moderate_mins * 60
    else:
        return quiet_mins * 60


async def _is_age_stale(
    redis: aioredis.Redis,
    article: dict,
    category_counts: dict,
    ai_cfg: dict,
) -> bool:
    """Return True and mark age_skipped if the article is too old to process."""
    fetched_at_str = article.get("fetched_at", "")
    if not fetched_at_str:
        return False
    try:
        fetched_dt = datetime.fromisoformat(fetched_at_str)
        if fetched_dt.tzinfo is None:
            fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
        age_sec = (datetime.now(timezone.utc) - fetched_dt).total_seconds()
        max_age = _max_age_for_category(article.get("category", ""), category_counts, ai_cfg)
        if age_sec > max_age:
            await redis.hset(f"news:{article['id']}", "ai_status", "age_skipped")
            logger.debug(
                f"Age-skipped {article['id']} "
                f"(age={age_sec:.0f}s > max={max_age}s, cat={article.get('category')})"
            )
            return True
    except (ValueError, TypeError):
        pass
    return False


async def _is_ai_duplicate(
    redis: aioredis.Redis,
    article: dict,
    title: str,
    perform_ai_dedup: bool,
    ai_dedup_threshold: int,
) -> bool:
    """Return True and mark dedup_skipped if a similar story was already processed."""
    if not (perform_ai_dedup and ai_dedup_threshold > 0 and title):
        return False
    ai_dup = await check_ai_duplicate(redis, title, threshold=ai_dedup_threshold)
    if ai_dup.is_duplicate:
        await redis.hset(f"news:{article['id']}", "ai_status", "dedup_skipped")
        logger.info(f"AI dedup skip {article['id']}: similar story already summarised")
        return True
    return False


async def _run_ai_rewrite(
    redis: aioredis.Redis,
    article: dict,
    title: str,
    origin_lang: str,
    tgt_lang: str | None,
    config_id: str | None,
    dedup_key: str,
    perform_ai_dedup: bool,
    model: str | None,
    max_tokens: int,
    temperature: float,
    tone: str,
    api_key: str | None,
    base_url: str | None,
    prompt_system_override: str | None,
    prompt_template_override: str | None,
) -> dict | None:
    """Call AI, store results, register dedup hash. Returns enriched article or None on failure."""
    if ws_manager:
        asyncio.create_task(ws_manager.emit_ai_start(article["id"], title))

    ai_t0 = datetime.now(timezone.utc)
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
        if ws_manager:
            asyncio.create_task(ws_manager.emit_ai_error(article["id"], title, str(e)[:200]))
        return None

    ai_duration_ms = int((datetime.now(timezone.utc) - ai_t0).total_seconds() * 1000)
    if ws_manager:
        asyncio.create_task(ws_manager.emit_ai_success(article["id"], title, ai_duration_ms))

    await update_article_ai(redis, article["id"], summaries)
    if config_id or tgt_lang:
        await update_article_ai_config(redis, article["id"], summaries, config_id or "builtin")
        await redis.hset(f"news:{article['id']}", dedup_key, "1")
    await log_ai_usage(article["id"], model, tokens)
    if title and perform_ai_dedup:
        await register_ai_simhash(redis, title)

    # Enrich article dict for downstream dispatch
    for lang, text in summaries.items():
        article[f"ai_summary_{lang}"] = text
    article["ai_summary_origin"] = summaries.get(origin_lang, "")
    article["ai_summary_target"] = summaries.get(tgt_lang, "") if tgt_lang else ""
    return article


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
    twitter_accounts: list[dict] | None = None,
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
    Fetch pending articles, rewrite with AI, then dispatch to webhooks + Telegram.

    - spread_seconds > 0: space out AI calls evenly across the window
    - pre_fetched_articles: use these instead of popping from the queue
    - config_id: store summaries under config-specific fields for per-config dispatch
    Returns the number of articles processed.
    """
    articles = pre_fetched_articles if pre_fetched_articles is not None else await pop_pending_ai_articles(redis, limit=batch_size)
    if not articles:
        return 0

    ai_cfg = _load_ai_config()
    category_counts = await _get_category_counts(redis)
    # Spread delay is computed against the full batch so throughput holds even when articles are skipped.
    inter_delay = spread_seconds / len(articles) if spread_seconds > 0 and len(articles) > 1 else 0

    processed = 0
    ai_call_count = 0

    for article in articles:
        title = article.get("title", "")
        dedup_key = f"ai_done_{config_id or 'builtin'}_{target_language or 'origin'}"

        # Per-config+lang dedup: skip if already processed with this exact combo
        if config_id or target_language:
            if await redis.hget(f"news:{article['id']}", dedup_key):
                processed += 1
                continue

        # Raw dispatch for ai_mode="off" hooks — no AI processing needed
        if raw_webhook_endpoints or raw_telegram_channels:
            await enqueue_dispatch(article, raw_webhook_endpoints or [], telegram_channels=raw_telegram_channels)

        if await _is_age_stale(redis, article, category_counts, ai_cfg):
            processed += 1
            continue

        if not webhook_endpoints and not telegram_channels:
            processed += 1
            continue

        if await _is_ai_duplicate(redis, article, title, perform_ai_dedup, ai_dedup_threshold):
            processed += 1
            continue

        if ai_call_count > 0 and inter_delay > 0:
            await asyncio.sleep(inter_delay)

        raw_lang = article.get("lang") or article.get("declared_lang") or "en"
        origin_lang = raw_lang[:2].lower() if raw_lang and raw_lang != "und" else "en"
        tgt_lang = target_language if target_language and target_language != origin_lang else None

        enriched = await _run_ai_rewrite(
            redis, article, title, origin_lang, tgt_lang,
            config_id, dedup_key, perform_ai_dedup,
            model, max_tokens, temperature, tone, api_key, base_url,
            prompt_system_override, prompt_template_override,
        )
        ai_call_count += 1

        if enriched and (webhook_endpoints or telegram_channels or twitter_accounts):
            await enqueue_dispatch(
                enriched,
                webhook_endpoints or [],
                telegram_channels=telegram_channels,
                twitter_accounts=twitter_accounts,
            )

        processed += 1
        if enriched:
            logger.info(f"Processed article {article['id']}: {article['title'][:60]}...")

    return processed
