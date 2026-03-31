"""
RAG engine — Retrieval-Augmented Generation for news Q&A.

Pipeline:
  1. Embed the user's question (ai/embedder.py)
  2. Retrieve top-K relevant articles from Weaviate (hybrid search)
  3. Build a context string from retrieved articles (build_rag_context)
  4. Call the LLM with [context + question] to generate an answer
  5. Return answer + source articles (for citation)
"""

import json
import logging
from typing import Any

import yaml
from openai import AsyncOpenAI

from ai.embedder import get_embedding
from ai.rewriter import get_openai_client, _load_ai_config
from vector_db.weaviate_store import hybrid_search, search_similar, is_available

logger = logging.getLogger(__name__)

# ── Configurable limits ───────────────────────────────────────────────────────

MAX_CONTEXT_ARTICLES = 6     # cap: more articles = richer context but slower LLM call
MAX_CONTENT_PER_ARTICLE = 400  # chars — keep total context under ~3000 chars


def build_rag_context(articles: list[dict[str, Any]], lang: str = "en") -> str:
    """
    Format retrieved articles into a numbered context block for the LLM.
    Prefers AI summaries over raw content. Includes source + date for attribution.
    Entities are included when available to help resolve ambiguous references.
    """
    blocks = []
    for i, art in enumerate(articles[:MAX_CONTEXT_ARTICLES], 1):
        source = art.get("source_name", "Unknown")
        date = (art.get("published_at") or "")[:10]
        title = art.get("title", "")
        summary_key = f"summary_{lang}" if lang != "en" else "summary_en"
        body = (
            art.get(summary_key)
            or art.get("summary_en")
            or art.get("content", "")
        )
        if body:
            body = body[:MAX_CONTENT_PER_ARTICLE]
        entities = art.get("entities")
        entity_line = ""
        if entities:
            try:
                ent_list = json.loads(entities) if isinstance(entities, str) else entities
                names = [e.get("name", "") for e in ent_list[:5] if e.get("name")]
                if names:
                    entity_line = f"Entities: {', '.join(names)}\n"
            except Exception:
                pass
        blocks.append(
            f"[{i}] {source} | {date}\nTitle: {title}\n{entity_line}Summary: {body}"
        )
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# RAG prompt templates
# ---------------------------------------------------------------------------

RAG_SYSTEM_PROMPT = {
    "en": (
        "You are a knowledgeable news analyst. "
        "Answer the user's question using ONLY the provided news articles as context. "
        "If the context does not contain enough information to answer, say so clearly. "
        "Cite the article number [1], [2], etc. when referencing specific facts. "
        "Be concise and factual."
    ),
    "vi": (
        "Bạn là chuyên gia phân tích tin tức. "
        "Trả lời câu hỏi của người dùng CHỈ dựa trên các bài báo được cung cấp. "
        "Nếu thông tin không đủ để trả lời, hãy nói rõ. "
        "Trích dẫn số bài báo [1], [2], v.v. khi nhắc đến sự kiện cụ thể. "
        "Ngắn gọn và thực tế."
    ),
}


# ---------------------------------------------------------------------------
# Main RAG entrypoint
# ---------------------------------------------------------------------------

async def ask(
    question: str,
    lang: str = "en",
    limit: int = 5,
    category: str | None = None,
    alpha: float = 0.75,
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """
    Full RAG pipeline: retrieve relevant articles → build context → generate answer.

    Returns:
        {
            "answer": str,
            "sources": [{"article_id", "title", "url", "source_name", "published_at", "_score"}],
            "retrieved": int,
        }
    """
    if not is_available():
        return {
            "answer": "Vector store (Weaviate) is not available.",
            "sources": [],
            "retrieved": 0,
        }

    # Step 1 — Embed the question
    query_embedding = await get_embedding(
        question, api_key=api_key, base_url=base_url
    )

    # Step 2 — Retrieve relevant articles (hybrid: vector + BM25)
    articles = await hybrid_search(
        query_text=question,
        query_vector=query_embedding,
        limit=min(limit, MAX_CONTEXT_ARTICLES),
        alpha=alpha,
        category=category,
    )

    # Fallback to pure vector search if hybrid returns nothing
    if not articles and query_embedding:
        articles = await search_similar(
            query_vector=query_embedding,
            limit=min(limit, MAX_CONTEXT_ARTICLES),
            category=category,
        )

    if not articles:
        return {
            "answer": "No relevant articles found for this question.",
            "sources": [],
            "retrieved": 0,
        }

    # Step 3 — Build context
    context = build_rag_context(articles, lang=lang)

    # Step 4 — Generate answer
    cfg = _load_ai_config()
    client = get_openai_client(api_key=api_key, base_url=base_url)
    model = cfg.get("model", "")
    system_prompt = RAG_SYSTEM_PROMPT.get(lang, RAG_SYSTEM_PROMPT["en"])

    user_message = f"Context (news articles):\n{context}\n\nQuestion: {question}"

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_tokens=600,
            temperature=0.2,
        )
        answer = response.choices[0].message.content or ""
    except Exception as exc:
        logger.error(f"[rag] LLM call failed: {exc}")
        answer = f"Failed to generate answer: {exc}"

    # Step 5 — Return answer + sources for citation
    sources = [
        {
            "article_id":   a.get("article_id", ""),
            "title":        a.get("title", ""),
            "url":          a.get("url", ""),
            "source_name":  a.get("source_name", ""),
            "published_at": a.get("published_at", ""),
            "score":        a.get("_score") or a.get("_distance"),
        }
        for a in articles
    ]

    return {
        "answer":    answer,
        "sources":   sources,
        "retrieved": len(articles),
    }


async def semantic_search(
    query: str,
    limit: int = 10,
    category: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """
    Semantic search only — returns matching articles without LLM generation.
    Useful for the /api/rag/search endpoint (fast, no LLM cost).
    """
    if not is_available():
        return []

    embedding = await get_embedding(query, api_key=api_key, base_url=base_url)

    results = await hybrid_search(
        query_text=query,
        query_vector=embedding,
        limit=limit,
        alpha=0.75,
        category=category,
    )
    return results
