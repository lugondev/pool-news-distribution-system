"""
AI rewriter: tóm tắt + dịch bài sang VI/EN dùng OpenAI-compatible API.
"""
import asyncio
import json
import logging
import os
from typing import Any

import yaml
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

import redis.asyncio as aioredis
from storage.redis_store import get_pending_ai_articles, update_article_ai
from storage.sqlite_stats import log_ai_usage
from webhook.dispatcher import dispatch_article

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
    Priority: explicit params > settings.yaml > env vars.
    """
    global _client, _client_fingerprint

    resolved_key = api_key or os.getenv("OPENAI_API_KEY", "")
    resolved_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    fingerprint = f"{resolved_key}|{resolved_url}"

    if _client is not None and _client_fingerprint == fingerprint:
        return _client

    _client = AsyncOpenAI(
        api_key=resolved_key,
        base_url=resolved_url,
        default_headers={
            "HTTP-Referer": "https://github.com/news-aggregator",
            "X-Title": "News Aggregator",
        },
    )
    _client_fingerprint = fingerprint
    logger.info(f"OpenAI client initialized: base_url={resolved_url}")
    return _client


SUMMARIZE_PROMPT = """You are a professional news editor. Given the following news article, provide:
1. A concise Vietnamese summary (2-3 sentences, natural Vietnamese)
2. A concise English summary (2-3 sentences)

Article title: {title}
Article content: {content}

Respond in JSON format:
{{"vi": "Vietnamese summary here", "en": "English summary here"}}

Be concise, factual, and natural. Do not add opinions."""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def rewrite_article(
    article: dict,
    model: str = "gpt-4o-mini",
    max_tokens: int = 300,
    temperature: float = 0.3,
    api_key: str | None = None,
    base_url: str | None = None,
) -> tuple[str, str, int]:
    """Returns (vi_summary, en_summary, tokens_used)."""
    client = get_openai_client(api_key=api_key, base_url=base_url)
    content = article.get("content") or article.get("summary") or ""
    title = article.get("title", "")

    prompt = SUMMARIZE_PROMPT.format(title=title, content=content[:1500])

    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        temperature=temperature,
    )

    result = json.loads(response.choices[0].message.content)
    tokens = response.usage.total_tokens if response.usage else 0
    return result.get("vi", ""), result.get("en", ""), tokens


async def process_pending_articles(
    redis: aioredis.Redis,
    model: str = "gpt-4o-mini",
    batch_size: int = 5,
    max_tokens: int = 300,
    temperature: float = 0.3,
    api_key: str | None = None,
    base_url: str | None = None,
    webhook_endpoints: list[dict] | None = None,
) -> int:
    """
    Lấy các bài pending, rewrite rồi dispatch webhook.
    Returns số bài đã xử lý.
    """
    articles = await get_pending_ai_articles(redis, limit=batch_size)
    if not articles:
        return 0

    tasks = [
        rewrite_article(
            a, model=model, max_tokens=max_tokens,
            temperature=temperature, api_key=api_key, base_url=base_url,
        )
        for a in articles
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    processed = 0
    for article, result in zip(articles, results):
        if isinstance(result, Exception):
            logger.warning(f"AI failed for {article['id']}: {result}")
            continue

        vi, en, tokens = result
        await update_article_ai(redis, article["id"], vi, en)
        await log_ai_usage(article["id"], model, tokens)

        article["ai_summary_vi"] = vi
        article["ai_summary_en"] = en
        if webhook_endpoints:
            await dispatch_article(article, webhook_endpoints)

        processed += 1
        logger.info(f"Processed article {article['id']}: {article['title'][:60]}...")

    return processed
