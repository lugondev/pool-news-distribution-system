/**
 * Cloudflare Worker — entry point.
 *
 * Handles three event types in a single Worker:
 *
 *   scheduled  → Cron Trigger (every 3 min) — crawl RSS feeds
 *   queue      → Queue Consumer             — process articles (R2 + embed + Vectorize)
 *   fetch      → HTTP                       — RAG API endpoints
 *
 * API endpoints:
 *   GET  /health
 *   GET  /search?q=<query>&limit=10&category=tech
 *   POST /ask    { question, lang?, limit?, category? }
 *   PUT  /sources          (admin: update source list in KV)
 */

import { runCrawler }           from "./crawler.js";
import { processArticleBatch }  from "./processor.js";
import { semanticSearch, ask }  from "./rag.js";
import type { Env, RawArticle, AskRequest } from "./types.js";

export default {

  // ── Cron Trigger: RSS Crawler ───────────────────────────────────────────────

  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runCrawler(env));
  },

  // ── Queue Consumer: Article Processor ──────────────────────────────────────

  async queue(batch: MessageBatch<RawArticle>, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(processArticleBatch(batch, env));
  },

  // ── HTTP: RAG API ───────────────────────────────────────────────────────────

  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // CORS headers for dashboard access
    const cors = {
      "Access-Control-Allow-Origin":  "*",
      "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    try {
      const response = await route(url, request, env);
      // Merge CORS headers onto every response
      for (const [k, v] of Object.entries(cors)) response.headers.set(k, v);
      return response;
    } catch (err) {
      return json({ error: String(err) }, 500, cors);
    }
  },
};

// ── Router ────────────────────────────────────────────────────────────────────

async function route(url: URL, request: Request, env: Env): Promise<Response> {
  const path = url.pathname;

  if (path === "/health") {
    return json({ status: "ok", timestamp: new Date().toISOString() });
  }

  // GET /search?q=...&limit=10&category=tech
  if (path === "/search" && request.method === "GET") {
    const q        = url.searchParams.get("q") ?? "";
    const limit    = parseInt(url.searchParams.get("limit") ?? "10");
    const category = url.searchParams.get("category") ?? undefined;

    if (!q) return json({ error: "Missing ?q= parameter" }, 400);

    const results = await semanticSearch(q, env, limit, category);
    return json({ query: q, results, count: results.length });
  }

  // POST /ask  { question, lang?, limit?, category? }
  if (path === "/ask" && request.method === "POST") {
    let body: AskRequest;
    try {
      body = await request.json<AskRequest>();
    } catch {
      return json({ error: "Invalid JSON body" }, 400);
    }

    if (!body.question || body.question.trim().length < 3) {
      return json({ error: "question must be at least 3 characters" }, 400);
    }

    const result = await ask(body, env);
    return json(result);
  }

  // PUT /sources  — update source list from request body
  if (path === "/sources" && request.method === "PUT") {
    const sources = await request.json();
    await env.SOURCES_KV.put("sources", JSON.stringify(sources));
    return json({ ok: true, message: "Sources updated" });
  }

  return json({ error: "Not found" }, 404);
}

// ── Utility ───────────────────────────────────────────────────────────────────

function json(data: unknown, status = 200, extraHeaders: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...extraHeaders,
    },
  });
}
