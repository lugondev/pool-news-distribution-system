/**
 * RSS Crawler — runs on Cron Trigger (every 3 min).
 *
 * Flow:
 *   load sources from KV (or fallback defaults)
 *     → fetch RSS feed
 *     → parse XML
 *     → dedup via KV (url hash → TTL 24h)
 *     → send new articles to Queue
 */

import { XMLParser } from "fast-xml-parser";
import type { Env, NewsSource, RawArticle } from "./types.js";

// ── Default sources (mirrors config/sources.yaml) ────────────────────────────
// Override at runtime by storing JSON in SOURCES_KV under key "sources"

const DEFAULT_SOURCES: NewsSource[] = [
  { id: "bbc-world",     name: "BBC World",     url: "https://feeds.bbci.co.uk/news/world/rss.xml",       lang: "en", category: "world",   enabled: true },
  { id: "reuters-tech",  name: "Reuters Tech",  url: "https://feeds.reuters.com/reuters/technologyNews",  lang: "en", category: "tech",    enabled: true },
  { id: "ars-technica",  name: "Ars Technica",  url: "https://feeds.arstechnica.com/arstechnica/index",   lang: "en", category: "tech",    enabled: true },
  { id: "techcrunch",    name: "TechCrunch",    url: "https://techcrunch.com/feed/",                      lang: "en", category: "tech",    enabled: true },
  { id: "the-verge",     name: "The Verge",     url: "https://www.theverge.com/rss/index.xml",            lang: "en", category: "tech",    enabled: true },
  { id: "vnexpress-tech",name: "VnExpress Tech",url: "https://vnexpress.net/rss/khoa-hoc-cong-nghe.rss", lang: "vi", category: "tech",    enabled: true },
];

const XML_PARSER = new XMLParser({ ignoreAttributes: false, attributeNamePrefix: "@_" });

// ── Public entry ─────────────────────────────────────────────────────────────

export async function runCrawler(env: Env): Promise<void> {
  const sources = await loadSources(env);
  const enabled = sources.filter((s) => s.enabled);

  const results = await Promise.allSettled(
    enabled.map((source) => crawlSource(source, env))
  );

  let totalQueued = 0;
  for (const [i, result] of results.entries()) {
    if (result.status === "fulfilled") {
      totalQueued += result.value;
    } else {
      console.error(`[crawler] ${enabled[i].id} failed:`, result.reason);
    }
  }
  console.log(`[crawler] tick done — ${totalQueued} new articles queued from ${enabled.length} sources`);
}

// ── Per-source crawl ──────────────────────────────────────────────────────────

async function crawlSource(source: NewsSource, env: Env): Promise<number> {
  const maxArticles = parseInt(env.MAX_ARTICLES_PER_SOURCE) || 50;

  const resp = await fetch(source.url, {
    headers: {
      "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
      "Accept": "application/rss+xml, application/xml, text/xml",
    },
    signal: AbortSignal.timeout(12_000),
  });

  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} from ${source.url}`);
  }

  const xml = await resp.text();
  const articles = parseRss(xml, source, maxArticles);

  // Dedup + enqueue
  let queued = 0;
  const ttl = parseInt(env.DEDUP_TTL_SECONDS) || 86400;

  for (const article of articles) {
    const dedupKey = `url:${article.id}`;
    const seen = await env.DEDUP_KV.get(dedupKey);
    if (seen) continue;

    await env.DEDUP_KV.put(dedupKey, "1", { expirationTtl: ttl });
    await env.ARTICLE_QUEUE.send(article);
    queued++;
  }

  return queued;
}

// ── RSS XML parser ────────────────────────────────────────────────────────────

function parseRss(xml: string, source: NewsSource, limit: number): RawArticle[] {
  let parsed: Record<string, unknown>;
  try {
    parsed = XML_PARSER.parse(xml) as Record<string, unknown>;
  } catch {
    return [];
  }

  // Handle both RSS 2.0 and Atom feeds
  const channel =
    (parsed?.rss as Record<string, unknown>)?.channel ??
    (parsed?.feed as Record<string, unknown>);

  if (!channel) return [];

  const rawItems =
    (channel as Record<string, unknown>).item ??
    (channel as Record<string, unknown>).entry ??
    [];

  const items = Array.isArray(rawItems) ? rawItems : [rawItems];
  const now = new Date().toISOString();
  const articles: RawArticle[] = [];

  for (const item of items.slice(0, limit)) {
    const entry = item as Record<string, unknown>;

    const url = extractUrl(entry);
    const title = stripHtml(String(entry.title ?? "")).trim();
    if (!url || !title) continue;

    const id = makeId(source.id, url);
    const summary = stripHtml(
      String(entry.description ?? entry.summary ?? entry.content ?? "")
    ).slice(0, 500);

    articles.push({
      id,
      source_id:   source.id,
      source_name: source.name,
      url,
      title,
      summary,
      content:     summary,   // will be enriched by processor if needed
      lang:        source.lang,
      category:    source.category,
      published_at: parseDate(entry) ?? now,
      fetched_at:   now,
    });
  }

  return articles;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function extractUrl(entry: Record<string, unknown>): string {
  // RSS 2.0: <link>
  if (typeof entry.link === "string") return entry.link;
  // Atom: <link href="...">
  const link = entry.link as Record<string, unknown> | undefined;
  if (link?.["@_href"]) return String(link["@_href"]);
  return "";
}

function parseDate(entry: Record<string, unknown>): string | null {
  const raw =
    entry.pubDate ?? entry.published ?? entry.updated ?? entry["dc:date"];
  if (!raw) return null;
  try {
    return new Date(String(raw)).toISOString();
  } catch {
    return null;
  }
}

function stripHtml(html: string): string {
  return html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

async function makeId(sourceId: string, url: string): Promise<string>;
function makeId(sourceId: string, url: string): string {
  // Sync SHA-256 is not available in Workers — use a fast FNV-1a hash instead.
  // Full SHA-256 is done async below; this sync version is a fallback.
  const str = `${sourceId}:${url}`;
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h.toString(16).padStart(8, "0") + str.length.toString(16).padStart(8, "0");
}

async function loadSources(env: Env): Promise<NewsSource[]> {
  try {
    const stored = await env.SOURCES_KV.get("sources", "json");
    if (stored) return stored as NewsSource[];
  } catch {
    // fall through to defaults
  }
  return DEFAULT_SOURCES;
}
