"""
AI rewriter: tóm tắt + dịch bài sang VI/EN dùng OpenAI-compatible API.
"""
import asyncio
import json
import logging
from typing import Any

import yaml
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

import redis.asyncio as aioredis
from crawler.dedup import check_ai_duplicate, register_ai_simhash
from storage.redis_store import pop_pending_ai_articles, update_article_ai
from storage.sqlite_stats import log_ai_usage
from webhook.dispatcher import enqueue_dispatch

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_client_fingerprint: str | None = None


def _load_ai_config() -> dict:
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("ai", {})


def get_openai_client(api_key: str | None = None, base_url: str | None = None) -> AsyncOpenAI:
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

SUMMARIZE_PROMPT = """{tone_instruction}

Given the following news article, provide:
1. A concise Vietnamese summary (2-3 sentences, natural Vietnamese)
2. A concise English summary (2-3 sentences)

Article title: {title}
Article content: {content}

Respond in JSON format:
{{"vi": "Vietnamese summary here", "en": "English summary here"}}

Do not add opinions."""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def rewrite_article(
    article: dict,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.3,
    tone: str = "general",
    api_key: str | None = None,
    base_url: str | None = None,
) -> tuple[str, str, int]:
    """Returns (vi_summary, en_summary, tokens_used)."""
    client = get_openai_client(api_key=api_key, base_url=base_url)
    resolved_model = model or _load_ai_config().get("model", "gpt-4o-mini")
    tone_instruction = TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])
    content = article.get("content") or article.get("summary") or ""
    title = article.get("title", "")

    prompt = SUMMARIZE_PROMPT.format(
        tone_instruction=tone_instruction,
        title=title,
        content=content[:1500],
    )

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
    tokens = response.usage.total_tokens if response.usage else 0
    return result.get("vi", ""), result.get("en", ""), tokens


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
    tone_instruction = TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])

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
            messages=[{"role": "user", "content": (
                f"{tone_instruction}\n\n"
                "Summarize this test headline in JSON: "
                '{"vi": "...", "en": "..."}\n\n'
                "Headline: Global markets rally on trade deal optimism"
            )}],
            max_tokens=120,
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        ms = int((time.monotonic() - t0) * 1000)
        content = response.choices[0].message.content if response.choices else None
        if not content:
            return {"ok": False, "error": "Model returned empty content", "model": resolved_model, "base_url": resolved_url, "ms": ms}
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
        return {"ok": False, "error": str(e), "model": resolved_model, "base_url": resolved_url, "ms": ms}


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
    spread_seconds: float = 0,
    ai_dedup_threshold: int = 6,
) -> int:
    """
    Lấy các bài pending, rewrite rồi dispatch webhook + telegram.
    If spread_seconds > 0, articles are processed one-by-one with even delays
    instead of all-at-once, to avoid bursts.
    Returns số bài đã xử lý.
    """
    articles = await pop_pending_ai_articles(redis, limit=batch_size)
    if not articles:
        return 0

    # inter_delay spaces out actual AI calls — computed against full batch size
    # so throughput matches the spread window even if some articles are dedup-skipped.
    inter_delay = spread_seconds / len(articles) if spread_seconds > 0 and len(articles) > 1 else 0

    processed = 0
    ai_call_count = 0  # tracks actual AI calls made (for rate limiting)

    for article in articles:
        title = article.get("title", "")

        # Pre-AI semantic dedup: check BEFORE sleeping to avoid wasted delay
        if ai_dedup_threshold > 0 and title:
            ai_dup = await check_ai_duplicate(redis, title, threshold=ai_dedup_threshold)
            if ai_dup.is_duplicate:
                await redis.hset(f"news:{article['id']}", "ai_status", "dedup_skipped")
                logger.info(f"AI dedup skip {article['id']}: similar story already summarised")
                processed += 1
                continue

        # Rate-limit only actual AI calls
        if ai_call_count > 0 and inter_delay > 0:
            await asyncio.sleep(inter_delay)

        try:
            vi, en, tokens = await rewrite_article(
                article, model=model, max_tokens=max_tokens,
                temperature=temperature, tone=tone,
                api_key=api_key, base_url=base_url,
            )
        except Exception as e:
            # AI failed after tenacity retries — mark failed so it won't block the queue.
            # It will NOT be re-enqueued; the article stays in Redis with ai_status="failed".
            logger.warning(f"AI failed for {article['id']}: {e}")
            await redis.hset(f"news:{article['id']}", "ai_status", "failed")
            ai_call_count += 1
            continue

        ai_call_count += 1
        await update_article_ai(redis, article["id"], vi, en)
        await log_ai_usage(article["id"], model, tokens)
        if title:
            await register_ai_simhash(redis, title)

        article["ai_summary_vi"] = vi
        article["ai_summary_en"] = en
        if webhook_endpoints or telegram_channels:
            await enqueue_dispatch(
                article,
                webhook_endpoints or [],
                telegram_channels=telegram_channels,
            )

        processed += 1
        logger.info(f"Processed article {article['id']}: {article['title'][:60]}...")

    return processed
