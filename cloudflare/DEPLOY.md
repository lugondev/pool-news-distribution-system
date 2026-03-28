# Deploy — Cloudflare Pipeline

## Requirements
- Cloudflare account (free tier sufficient for testing)
- Node.js 18+
- `npm install -g wrangler` + `wrangler login`

---

## Step 1 — Create Cloudflare Resources

Run the following commands in sequence (one-time only):

```bash
# 1. Queue
wrangler queues create news-articles
wrangler queues create news-articles-dlq    # dead letter queue

# 2. R2 bucket
wrangler r2 bucket create news-raw-articles

# 3. Vectorize index (dimensions=1024 for bge-large, metric=cosine)
wrangler vectorize create news-embeddings --dimensions=1024 --metric=cosine

# 4. KV namespaces
wrangler kv namespace create DEDUP_KV
wrangler kv namespace create SOURCES_KV
```

Then fill in the generated IDs in `wrangler.toml`:
```toml
[[kv_namespaces]]
binding = "DEDUP_KV"
id      = "<id from the command output above>"

[[kv_namespaces]]
binding = "SOURCES_KV"
id      = "<id from the output>"
```

---

## Step 2 — Install Dependencies

```bash
cd cloudflare/
npm install
```

---

## Step 3 — Deploy

```bash
npm run deploy
# → uploads Worker to Cloudflare edge (300+ locations worldwide)
```

---

## Step 4 — Verify

```bash
# Health check
curl https://news-aggregator.<your-subdomain>.workers.dev/health

# Test search (after crawler has run at least once)
curl "https://news-aggregator.<your-subdomain>.workers.dev/search?q=OpenAI&limit=5"

# Test RAG
curl -X POST https://news-aggregator.<your-subdomain>.workers.dev/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the latest news on AI?", "lang": "en", "limit": 5}'
```

---

## Test Locally

```bash
# HTTP handler
npm run dev

# Cron trigger (manual test)
npm run test-cron
# → curl http://localhost:8787/__scheduled
```

---

## Update Sources

```bash
curl -X PUT https://news-aggregator.<your-subdomain>.workers.dev/sources \
  -H "Content-Type: application/json" \
  -d @../config/sources.json   # export from sources.yaml if needed
```

---

## Estimated Cost (Free Tier)

| Service | Free limit | Estimated usage |
|---|---|---|
| Workers | 100,000 req/day | ~500 req/day (crawl + query) |
| Queues | 1M msg/month | ~50K msg/month (17 sources × 50 articles × 30 days × 2) |
| R2 | 10 GB/month | ~500 MB/month (1KB/article × 50K articles) |
| Vectorize | 30M queried vectors/month | ~10M/month |
| Workers AI | 10,000 neurons/day | Embedding + LLM |

**Conclusion: Free tier is sufficient for small-to-medium traffic.**
