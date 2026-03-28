/**
 * RAG Engine — Retrieval-Augmented Generation over indexed news articles.
 *
 * /search  → semantic search (no LLM, fast)
 * /ask     → full RAG: embed → Vectorize → R2 → Llama → answer
 */

import type { Env, RawArticle, SearchResult, AskRequest, AskResponse } from "./types.js";

const EMBED_MODEL  = "@cf/baai/bge-large-en-v1.5" as const;
const LLM_MODEL    = "@cf/meta/llama-3.3-70b-instruct-fp8-fast" as const;
const MAX_CONTEXT  = 5;    // articles to include in LLM context
const MAX_BODY_LEN = 400;  // chars per article in context

// ── Search (no LLM) ───────────────────────────────────────────────────────────

export async function semanticSearch(
  query: string,
  env: Env,
  limit = 10,
  category?: string
): Promise<SearchResult[]> {
  const vector = await embedQuery(query, env);
  if (!vector) return [];

  const queryOptions: VectorizeQueryOptions = {
    topK: limit,
    returnMetadata: "all",
    returnValues: false,
    ...(category ? { filter: { category } } : {}),
  };

  const result = await env.VECTORIZE.query(vector, queryOptions);

  return result.matches.map((match) => ({
    article_id:   match.id,
    title:        String(match.metadata?.title ?? ""),
    url:          String(match.metadata?.url ?? ""),
    source_name:  String(match.metadata?.source_name ?? ""),
    category:     String(match.metadata?.category ?? ""),
    published_at: String(match.metadata?.published_at ?? ""),
    score:        match.score,
    summary:      String(match.metadata?.summary ?? ""),
  }));
}

// ── Full RAG ──────────────────────────────────────────────────────────────────

export async function ask(req: AskRequest, env: Env): Promise<AskResponse> {
  const { question, lang = "en", limit = 5, category } = req;

  // Step 1 — Retrieve relevant articles
  const hits = await semanticSearch(question, env, Math.min(limit, MAX_CONTEXT), category);
  if (hits.length === 0) {
    return { answer: "No relevant articles found.", sources: [], model: LLM_MODEL };
  }

  // Step 2 — Hydrate from R2 (get full content for top hits)
  const articles = await hydrateArticles(hits, env);

  // Step 3 — Build LLM context
  const context = buildContext(articles, hits, lang);

  // Step 4 — Generate answer
  const systemPrompt = lang === "vi"
    ? "Bạn là chuyên gia phân tích tin tức. Trả lời CHỈ dựa trên các bài báo được cung cấp. Trích dẫn [1], [2] khi nhắc đến sự kiện cụ thể."
    : "You are a news analyst. Answer using ONLY the provided articles. Cite [1], [2] etc. when referencing specific facts. Be concise.";

  const userMessage = `Articles:\n${context}\n\nQuestion: ${question}`;

  const response = await env.AI.run(LLM_MODEL, {
    messages: [
      { role: "system", content: systemPrompt },
      { role: "user",   content: userMessage },
    ],
    max_tokens: 512,
    temperature: 0.2,
  }) as { response?: string };

  return {
    answer:  response.response ?? "Could not generate answer.",
    sources: hits,
    model:   LLM_MODEL,
  };
}

// ── Helpers ───────────────────────────────────────────────────────────────────

async function embedQuery(text: string, env: Env): Promise<number[] | null> {
  try {
    const res = await env.AI.run(EMBED_MODEL, { text: text.slice(0, 512) });
    const data = (res as { data?: number[][] }).data;
    if (data && data[0]) return data[0];
    const flat = (res as { data?: number[] }).data;
    return Array.isArray(flat) ? flat : null;
  } catch (err) {
    console.error("[rag] embed failed:", err);
    return null;
  }
}

async function hydrateArticles(hits: SearchResult[], env: Env): Promise<(RawArticle | null)[]> {
  return Promise.all(
    hits.map(async (hit) => {
      try {
        const r2Key = `articles/${hit.article_id}.json`;
        const obj = await env.ARTICLES_BUCKET.get(r2Key);
        if (!obj) return null;
        return await obj.json<RawArticle>();
      } catch {
        return null;
      }
    })
  );
}

function buildContext(
  articles: (RawArticle | null)[],
  hits: SearchResult[],
  lang: string
): string {
  const lines: string[] = [];

  for (let i = 0; i < hits.length; i++) {
    const hit     = hits[i];
    const article = articles[i];
    const date    = hit.published_at.slice(0, 10);
    const body    = (article?.content || article?.summary || hit.summary || "").slice(0, MAX_BODY_LEN);

    lines.push(`[${i + 1}] ${hit.source_name} | ${date}`);
    lines.push(`Title: ${hit.title}`);
    if (body) lines.push(`Content: ${body}`);
    lines.push("");
  }

  return lines.join("\n");
}
