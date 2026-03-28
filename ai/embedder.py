"""
Phase 2 – Embeddings: generate semantic vectors for articles.
Uses the same OpenAI-compatible provider configured in settings.yaml.
Falls back gracefully if the provider doesn't support embeddings.
"""

import logging

import yaml

from ai.rewriter import get_openai_client

logger = logging.getLogger(__name__)

_DEFAULT_EMBED_MODEL = "text-embedding-3-small"


def _load_embedding_config() -> dict:
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("processing", {})


async def get_embedding(
    text: str,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> list[float] | None:
    """
    Generate an embedding vector for the given text.
    Returns None if the provider does not support embeddings or on error.

    The returned vector is normalized to unit length for efficient cosine similarity:
        cosine_sim(a, b) = dot(a, b)  (since |a| = |b| = 1)
    """
    processing_cfg = _load_embedding_config()
    resolved_model = (
        model
        or processing_cfg.get("embedding_model")
        or _DEFAULT_EMBED_MODEL
    )

    # Trim text to keep costs low — title + first 512 chars of content is enough
    text = text[:1024].strip()
    if not text:
        return None

    client = get_openai_client(api_key=api_key, base_url=base_url)

    try:
        response = await client.embeddings.create(
            model=resolved_model,
            input=text,
        )
    except Exception as exc:
        logger.warning(f"[embedder] embedding call failed ({resolved_model}): {exc}")
        return None

    if not response.data:
        return None

    vector = response.data[0].embedding
    return _normalize(vector)


def _normalize(vector: list[float]) -> list[float]:
    """Return unit-length version of vector (L2 norm = 1)."""
    import math
    magnitude = math.sqrt(sum(x * x for x in vector))
    if magnitude == 0:
        return vector
    return [x / magnitude for x in vector]


def embed_text_for_article(article: dict) -> str:
    """Build the text input for embedding: title + summary/content."""
    title = article.get("title", "")
    body = article.get("content") or article.get("summary") or ""
    return f"{title}. {body[:512]}"
