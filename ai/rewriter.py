"""
AI rewriter: tóm tắt + dịch bài sang VI/EN dùng OpenAI-compatible API.
"""
import asyncio
import logging
import os
from typing import Any

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

import redis.asyncio as aioredis
from storage.redis_store import get_pending_ai_articles, update_article_ai
from storage.sqlite_stats import log_ai_usage
from webhook.dispatcher import dispatch_article

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            default_headers={
                "HTTP-Referer": "https://github.com/news-aggregator",
                "X-Title": "News Aggregator",
            },
        )
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
) -> tuple[str, str, int]:
    """
    Returns (vi_summary, en_summary, tokens_used).
    """
    client = get_openai_client()
    content = article.get("content") or article.get("summary") or ""
    title = article.get("title", "")

    prompt = SUMMARIZE_PROMPT.format(title=title, content=content[:1500])

    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        temperature=0.3,
    )

    import json
    result = json.loads(response.choices[0].message.content)
    tokens = response.usage.total_tokens if response.usage else 0
    return result.get("vi", ""), result.get("en", ""), tokens


async def process_pending_articles(
    redis: aioredis.Redis,
    model: str = "gpt-4o-mini",
    batch_size: int = 5,
    webhook_urls: list[str] | None = None,
) -> int:
    """
    Lấy các bài pending, rewrite rồi dispatch webhook.
    Returns số bài đã xử lý.
    """
    articles = await get_pending_ai_articles(redis, limit=batch_size)
    if not articles:
        return 0

    tasks = [rewrite_article(a, model=model) for a in articles]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    processed = 0
    for article, result in zip(articles, results):
        if isinstance(result, Exception):
            logger.warning(f"AI failed for {article['id']}: {result}")
            continue

        vi, en, tokens = result
        await update_article_ai(redis, article["id"], vi, en)
        await log_ai_usage(article["id"], model, tokens)

        # Enrich article với AI output rồi dispatch
        article["ai_summary_vi"] = vi
        article["ai_summary_en"] = en
        if webhook_urls:
            await dispatch_article(article, webhook_urls)

        processed += 1
        logger.info(f"Processed article {article['id']}: {article['title'][:60]}...")

    return processed
