// ── Article ───────────────────────────────────────────────────────────────────

export interface RawArticle {
  id: string;           // SHA-256 hex[:16] of "source_id:url"
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  summary: string;
  content: string;
  lang: string;
  category: string;
  published_at: string; // ISO 8601
  fetched_at: string;   // ISO 8601
}

export interface EnrichedArticle extends RawArticle {
  embedding?: number[];
  vectorize_id?: string;
  r2_key: string;       // "articles/{id}.json"
}

// ── Source ────────────────────────────────────────────────────────────────────

export interface NewsSource {
  id: string;
  name: string;
  url: string;        // RSS feed URL
  lang: string;
  category: string;
  enabled: boolean;
}

// ── Workers bindings ─────────────────────────────────────────────────────────

export interface Env {
  // Queues
  ARTICLE_QUEUE: Queue<RawArticle>;

  // R2
  ARTICLES_BUCKET: R2Bucket;

  // Vectorize
  VECTORIZE: VectorizeIndex;

  // Workers AI
  AI: Ai;

  // KV
  DEDUP_KV: KVNamespace;
  SOURCES_KV: KVNamespace;

  // Vars
  DEDUP_TTL_SECONDS: string;
  MAX_ARTICLES_PER_SOURCE: string;
}

// ── RAG ───────────────────────────────────────────────────────────────────────

export interface SearchResult {
  article_id: string;
  title: string;
  url: string;
  source_name: string;
  category: string;
  published_at: string;
  score: number;
  summary?: string;
}

export interface AskRequest {
  question: string;
  lang?: "en" | "vi";
  limit?: number;
  category?: string;
}

export interface AskResponse {
  answer: string;
  sources: SearchResult[];
  model: string;
}
