/**
 * Queue Consumer — processes articles from the "news-articles" queue.
 *
 * For each article in the batch:
 *   1. Store raw JSON in R2  (key: articles/{id}.json)
 *   2. Generate embedding    via Workers AI (BGE-M3, multilingual)
 *   3. Upsert into Vectorize (id, vector, metadata)
 */

import type { Env, RawArticle } from "./types.js";

const EMBED_MODEL = "@cf/baai/bge-large-en-v1.5" as const;
// Dimensions: bge-small=384, bge-base=768, bge-large=1024
// For multilingual (vi/ja/ko), consider "@cf/baai/bge-m3" when available

export async function processArticleBatch(
  batch: MessageBatch<RawArticle>,
  env: Env
): Promise<void> {
  const articles = batch.messages.map((m) => m.body);
  console.log(`[processor] batch of ${articles.length} articles`);

  const results = await Promise.allSettled(
    articles.map((article) => processOne(article, env))
  );

  // ACK all — failed ones go to DLQ after max_retries
  for (const [i, result] of results.entries()) {
    if (result.status === "rejected") {
      console.error(`[processor] ${articles[i].id} failed:`, result.reason);
      batch.messages[i].retry();   // requeue for retry
    } else {
      batch.messages[i].ack();
    }
  }
}

// ── Single article pipeline ───────────────────────────────────────────────────

async function processOne(article: RawArticle, env: Env): Promise<void> {
  const r2Key = `articles/${article.id}.json`;

  // Step 1 — Store raw article in R2
  await env.ARTICLES_BUCKET.put(r2Key, JSON.stringify(article), {
    httpMetadata: { contentType: "application/json" },
    customMetadata: {
      source_id:    article.source_id,
      category:     article.category,
      lang:         article.lang,
      published_at: article.published_at,
    },
  });

  // Step 2 — Generate embedding
  const textToEmbed = buildEmbedText(article);
  const embedding = await generateEmbedding(textToEmbed, env);

  if (!embedding) {
    console.warn(`[processor] no embedding for ${article.id} — skipping Vectorize`);
    return;
  }

  // Step 3 — Upsert into Vectorize
  await env.VECTORIZE.upsert([
    {
      id:     article.id,
      values: embedding,
      metadata: {
        title:        article.title,
        url:          article.url,
        source_name:  article.source_name,
        category:     article.category,
        lang:         article.lang,
        published_at: article.published_at,
        r2_key:       r2Key,
        summary:      article.summary.slice(0, 300),
      },
    },
  ]);

  console.log(`[processor] ✓ ${article.id} — ${article.title.slice(0, 60)}`);
}

// ── Embedding ─────────────────────────────────────────────────────────────────

async function generateEmbedding(text: string, env: Env): Promise<number[] | null> {
  try {
    const response = await env.AI.run(EMBED_MODEL, { text: text.slice(0, 512) });

    // Workers AI returns: { data: [[...floats]] }
    const data = (response as { data?: number[][] }).data;
    if (data && data[0]) return data[0];

    // Some models return: { shape: [...], data: [...] }
    const flat = (response as { data?: number[] }).data;
    if (Array.isArray(flat) && flat.length > 0) return flat;

    return null;
  } catch (err) {
    console.error("[processor] embedding failed:", err);
    return null;
  }
}

function buildEmbedText(article: RawArticle): string {
  // Title is the strongest signal; summary adds context
  return `${article.title}. ${article.summary}`.trim();
}
