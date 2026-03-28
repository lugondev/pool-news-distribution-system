"""RAG API — semantic search and Q&A over indexed news articles."""

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ai.rewriter import _load_ai_config
from vector_db import weaviate_store
from vector_db.rag import ask, semantic_search

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rag", tags=["rag"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)
    lang: str = Field("en", pattern="^(en|vi|ja|ko)$")
    limit: int = Field(5, ge=1, le=10)
    category: str | None = None
    alpha: float = Field(0.75, ge=0.0, le=1.0, description="0=BM25 only, 1=vector only")


class AskResponse(BaseModel):
    answer: str
    sources: list[dict]
    retrieved: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def rag_status():
    """Check whether the Weaviate vector store is reachable."""
    available = weaviate_store.is_available()
    count = None
    if available:
        try:
            collection = weaviate_store.get_client().collections.get(weaviate_store.COLLECTION_NAME)
            agg = await collection.aggregate.over_all(total_count=True)
            count = agg.total_count
        except Exception:
            pass
    return {
        "weaviate_available": available,
        "collection": weaviate_store.COLLECTION_NAME,
        "indexed_articles": count,
    }


@router.get("/search")
async def search(
    q: str = Query(..., min_length=2, description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    category: str | None = Query(None),
):
    """
    Semantic + keyword hybrid search. Returns matching articles without LLM generation.
    Fast and cheap — no LLM call, just vector retrieval.
    """
    if not weaviate_store.is_available():
        raise HTTPException(503, "Vector store unavailable — check Weaviate connection")

    cfg = _load_ai_config()
    api_key = cfg.get("api_key", "")
    base_url = cfg.get("base_url", "https://api.openai.com/v1")

    results = await semantic_search(
        query=q,
        limit=limit,
        category=category,
        api_key=api_key,
        base_url=base_url,
    )
    return {"query": q, "results": results, "count": len(results)}


@router.post("/ask", response_model=AskResponse)
async def ask_question(body: AskRequest):
    """
    Full RAG: retrieve relevant articles → generate a grounded answer via LLM.
    Slower than /search (involves an LLM call), but returns a synthesized answer
    with citations.
    """
    if not weaviate_store.is_available():
        raise HTTPException(503, "Vector store unavailable — check Weaviate connection")

    cfg = _load_ai_config()
    api_key = cfg.get("api_key", "")
    base_url = cfg.get("base_url", "https://api.openai.com/v1")

    result = await ask(
        question=body.question,
        lang=body.lang,
        limit=body.limit,
        category=body.category,
        alpha=body.alpha,
        api_key=api_key,
        base_url=base_url,
    )
    return result
