"""
Weaviate vector store for news articles.

Collection schema: NewsArticle
  - Stores article text fields as properties
  - Vector = pre-computed embedding from ai/embedder.py (BYOV — no internal vectorizer)
  - Supports near_vector search and hybrid (vector + BM25) search

Lifecycle:
  - init_weaviate()  → called once at app startup (main.py lifespan)
  - close_weaviate() → called at shutdown
  - get_client()     → returns the shared async client instance
"""

import json
import logging

import yaml

logger = logging.getLogger(__name__)

_client = None
COLLECTION_NAME = "NewsArticle"


def _load_weaviate_config() -> dict:
    try:
        from dashboard.config_io import read_settings
        return read_settings().get("weaviate", {})
    except Exception:
        return {}


async def init_weaviate() -> None:
    """Connect to Weaviate and ensure the NewsArticle collection exists."""
    global _client
    try:
        import weaviate
        from weaviate.classes.config import Configure, DataType, Property
    except ImportError:
        logger.warning(
            "[weaviate] weaviate-client not installed — vector store disabled. "
            "Run: pip install 'weaviate-client>=4.5'"
        )
        return

    cfg = _load_weaviate_config()
    if not cfg.get("enabled", True):
        logger.info("[weaviate] disabled in settings — skipping init")
        return

    import os
    host = os.environ.get("WEAVIATE_HOST") or cfg.get("host", "localhost")
    port = int(os.environ.get("WEAVIATE_PORT") or cfg.get("port", 8080))
    grpc_port = int(os.environ.get("WEAVIATE_GRPC_PORT") or cfg.get("grpc_port", 50051))

    try:
        _client = weaviate.use_async_with_custom(
            http_host=host,
            http_port=port,
            http_secure=False,
            grpc_host=host,
            grpc_port=grpc_port,
            grpc_secure=False,
        )
        await _client.connect()
        logger.info(f"[weaviate] connected to {host}:{port}")
    except Exception as exc:
        logger.warning(f"[weaviate] connection failed: {exc} — vector store disabled")
        _client = None
        return

    await _ensure_collection()


async def _ensure_collection() -> None:
    """Create the NewsArticle collection if it does not exist."""
    if _client is None:
        return
    try:
        from weaviate.classes.config import Configure, DataType, Property

        exists = await _client.collections.exists(COLLECTION_NAME)
        if exists:
            logger.info(f"[weaviate] collection '{COLLECTION_NAME}' already exists")
            return

        await _client.collections.create(
            name=COLLECTION_NAME,
            # BYOV: we supply our own vectors from ai/embedder.py
            vectorizer_config=Configure.Vectorizer.none(),
            properties=[
                # Searchable text fields (BM25 index for hybrid search)
                Property(name="article_id", data_type=DataType.TEXT),
                Property(name="title",      data_type=DataType.TEXT),
                Property(name="content",    data_type=DataType.TEXT),
                Property(name="summary_en", data_type=DataType.TEXT),
                Property(name="summary_vi", data_type=DataType.TEXT),
                # Metadata / filter fields (skip BM25 — not useful for keyword search)
                Property(name="entities",     data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="sentiment",    data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="category",     data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="source_name",  data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="lang",         data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="published_at", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="url",          data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="topic_id",     data_type=DataType.TEXT, skip_vectorization=True),
            ],
        )
        logger.info(f"[weaviate] collection '{COLLECTION_NAME}' created")
    except Exception as exc:
        logger.error(f"[weaviate] failed to ensure collection: {exc}")


async def close_weaviate() -> None:
    global _client
    if _client is not None:
        try:
            await _client.close()
            logger.info("[weaviate] connection closed")
        except Exception as exc:
            logger.warning(f"[weaviate] close error: {exc}")
        finally:
            _client = None


def get_client():
    """Return the shared async Weaviate client, or None if unavailable."""
    return _client


def is_available() -> bool:
    return _client is not None


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def _article_to_properties(article: dict) -> dict:
    """Map a Redis article dict to Weaviate object properties."""
    entities_raw = article.get("entities", "[]")
    if isinstance(entities_raw, list):
        entities_json = json.dumps(entities_raw)
    else:
        entities_json = entities_raw  # already serialized string

    return {
        "article_id":   article.get("id", ""),
        "title":        article.get("title", ""),
        "content":      article.get("content", "")[:2000],
        "summary_en":   article.get("ai_summary_en", "") or article.get("summary", ""),
        "summary_vi":   article.get("ai_summary_vi", ""),
        "entities":     entities_json,
        "sentiment":    article.get("sentiment", ""),
        "category":     article.get("category", ""),
        "source_name":  article.get("source_name", ""),
        "lang":         article.get("lang", ""),
        "published_at": article.get("published_at", ""),
        "url":          article.get("url", ""),
        "topic_id":     article.get("topic_id", ""),
    }


async def index_article(article: dict, embedding: list[float]) -> bool:
    """
    Insert or update a single article in Weaviate.
    Returns True on success, False if Weaviate is unavailable.
    """
    if _client is None:
        return False

    article_id = article.get("id", "")
    if not article_id or not embedding:
        return False

    try:
        collection = _client.collections.get(COLLECTION_NAME)
        props = _article_to_properties(article)

        # Weaviate uses UUID-based IDs — derive from article_id for idempotency
        weaviate_id = _article_id_to_uuid(article_id)

        # Use insert_many with overwrite semantics via replace
        existing = await collection.data.exists(weaviate_id)
        if existing:
            await collection.data.replace(
                uuid=weaviate_id,
                properties=props,
                vector=embedding,
            )
        else:
            await collection.data.insert(
                properties=props,
                vector=embedding,
                uuid=weaviate_id,
            )
        return True
    except Exception as exc:
        logger.warning(f"[weaviate] index_article failed ({article_id}): {exc}")
        return False


async def search_similar(
    query_vector: list[float],
    limit: int = 5,
    category: str | None = None,
) -> list[dict]:
    """
    Pure vector similarity search (near_vector).
    Returns list of article property dicts with added `_distance` field.
    """
    if _client is None:
        return []

    try:
        from weaviate.classes.query import MetadataQuery, Filter

        collection = _client.collections.get(COLLECTION_NAME)

        filters = None
        if category:
            filters = Filter.by_property("category").equal(category)

        results = await collection.query.near_vector(
            near_vector=query_vector,
            limit=limit,
            return_metadata=MetadataQuery(distance=True),
            filters=filters,
        )

        articles = []
        for obj in results.objects:
            props = dict(obj.properties)
            props["_distance"] = round(obj.metadata.distance, 4) if obj.metadata else None
            articles.append(props)
        return articles
    except Exception as exc:
        logger.warning(f"[weaviate] search_similar failed: {exc}")
        return []


async def hybrid_search(
    query_text: str,
    query_vector: list[float] | None,
    limit: int = 5,
    alpha: float = 0.75,
    category: str | None = None,
) -> list[dict]:
    """
    Hybrid search: combines BM25 keyword matching + vector similarity.
    alpha=0 → pure keyword (BM25), alpha=1 → pure vector.
    alpha=0.75 is a good default: vector-dominant but keyword as tiebreaker.
    """
    if _client is None:
        return []

    try:
        from weaviate.classes.query import MetadataQuery, Filter, HybridFusion

        collection = _client.collections.get(COLLECTION_NAME)

        filters = None
        if category:
            filters = Filter.by_property("category").equal(category)

        results = await collection.query.hybrid(
            query=query_text,
            vector=query_vector,
            limit=limit,
            alpha=alpha,
            return_metadata=MetadataQuery(score=True),
            filters=filters,
            fusion_type=HybridFusion.RANKED,
        )

        articles = []
        for obj in results.objects:
            props = dict(obj.properties)
            props["_score"] = round(obj.metadata.score, 4) if obj.metadata else None
            articles.append(props)
        return articles
    except Exception as exc:
        logger.warning(f"[weaviate] hybrid_search failed: {exc}")
        return []


def _article_id_to_uuid(article_id: str) -> str:
    """Convert a 16-char hex article_id to a valid UUID v5 (deterministic)."""
    import uuid
    # Use DNS namespace + article_id for a stable, collision-resistant UUID
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"news-aggregator:{article_id}"))
