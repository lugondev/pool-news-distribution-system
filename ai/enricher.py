"""
Phase 2 – Enrichment: entity extraction + sentiment analysis.
Single AI call returning structured JSON alongside the article's summary.
"""

import json
import logging
from typing import Any

from openai import RateLimitError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_not_exception_type

from ai.rewriter import get_openai_client, _load_ai_config
from ai.provider_utils import build_response_format, parse_ai_json, SCHEMA_ENRICHMENT

logger = logging.getLogger(__name__)

ENRICH_PROMPT = """You are an expert news analyst. Given the following news article, extract:
1. The most important named entities (people, organizations, countries, products, technologies — up to 8)
2. The overall sentiment of the story

Article title: {title}
Article content: {content}

Respond ONLY in JSON, no explanation:
{{"entities": ["Entity1", "Entity2"], "sentiment": "positive|negative|neutral"}}"""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_not_exception_type(RateLimitError)
)
async def enrich_article(
    article: dict[str, Any],
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """
    Extract entities and sentiment for a single article.

    Returns:
        {"entities": ["OpenAI", "Microsoft"], "sentiment": "positive|negative|neutral"}
    Falls back to empty entities + neutral sentiment on parse errors.
    """
    client = get_openai_client(api_key=api_key, base_url=base_url)
    cfg = _load_ai_config()
    resolved_model = model or cfg.get("model", "")

    title = article.get("title", "")
    # Prefer full content; fall back to summary
    content = article.get("content") or article.get("summary") or ""

    prompt = ENRICH_PROMPT.format(
        title=title,
        content=content[:1200],
    )

    create_kwargs: dict = {
        "model": resolved_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.1,
        "response_format": build_response_format(base_url, "enrichment", SCHEMA_ENRICHMENT),
    }

    response = await client.chat.completions.create(**create_kwargs)

    raw = response.choices[0].message.content if response.choices else None
    if not raw:
        logger.warning(f"[enricher] empty response for article {article.get('id')}")
        return {"entities": [], "sentiment": "neutral"}

    try:
        result = parse_ai_json(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(f"[enricher] JSON parse error: {exc} — raw={str(raw)[:200]}")
        return {"entities": [], "sentiment": "neutral"}

    entities = result.get("entities", [])
    sentiment = result.get("sentiment", "neutral")

    # Sanitize
    if not isinstance(entities, list):
        entities = []
    entities = [str(e).strip() for e in entities if e][:8]

    if sentiment not in ("positive", "negative", "neutral"):
        sentiment = "neutral"

    tokens = response.usage.total_tokens if response.usage else 0
    logger.debug(
        f"[enricher] {article.get('id')}: {len(entities)} entities, "
        f"sentiment={sentiment}, tokens={tokens}"
    )

    return {"entities": entities, "sentiment": sentiment}


async def batch_enrich(
    articles: list[dict[str, Any]],
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """
    Enrich a batch of articles concurrently (up to 5 parallel calls).
    Returns list of enrichment results in same order as input.
    """
    import asyncio

    sem = asyncio.Semaphore(5)

    async def _enrich_one(art: dict) -> dict:
        async with sem:
            try:
                return await enrich_article(art, api_key=api_key, base_url=base_url, model=model)
            except Exception as exc:
                logger.warning(f"[enricher] failed for {art.get('id')}: {exc}")
                return {"entities": [], "sentiment": "neutral"}

    return await asyncio.gather(*[_enrich_one(a) for a in articles])
