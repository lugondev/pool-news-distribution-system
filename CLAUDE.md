# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Start the app (FastAPI + APScheduler on port 8000)
python main.py

# Or directly with uvicorn
uvicorn main:dashboard_app --host 0.0.0.0 --port 8000 --reload
```

**Required services:** Redis must be running. Set `REDIS_URL` in `.env` (default: `redis://localhost:6379/0`).

**Required environment variables** (see `.env.example`):
- `OPENAI_API_KEY` — API key for OpenAI-compatible endpoint
- `OPENAI_BASE_URL` — API base URL (default OpenAI; project uses OpenRouter with `google/gemini-2.5-flash`)
- `REDIS_URL` — Redis connection URL
- `SQLITE_PATH` — Path to SQLite stats DB (default `./data/stats.db`)

## Architecture

The system is a pipeline: **RSS feeds → SimHash dedup → Redis → AI batch → Webhooks**.

**Entry point:** `main.py` initializes SQLite, Redis, mounts the FastAPI app from `dashboard/app.py`, and starts `APScheduler` (two jobs).

**Two async scheduler jobs** (`scheduler.py`):
1. **Crawl job** (every 10 min): `fetch_all_sources()` → concurrent RSS fetch (semaphore=20) → parse → SimHash dedup check → save to Redis
2. **AI rewrite job** (every 5 min): pulls up to 5 pending articles from Redis → calls OpenAI-compatible API → stores summaries → dispatches to webhooks

**Storage dual-layer:**
- **Redis** (24h TTL hot store): articles as hashes (`news:{id}`), sorted sets for feeds (`news:feed`), dedup simhash set (`news:dedup:simhashes`)
- **SQLite** (analytics): `crawl_logs`, `webhook_logs`, `ai_logs` tables

**Dashboard** (`dashboard/app.py`): FastAPI routes serve Jinja2 templates; HTMX polls `/partials/stats` and `/partials/feed` every 30 seconds — no custom JavaScript.

## Key Design Decisions

- **Article ID**: SHA256 of `source_id:url` (first 16 chars hex)
- **Deduplication**: 64-bit SimHash on normalized titles; Hamming distance ≤ 3 = duplicate
- **AI model**: Configurable in `config/settings.yaml` under `ai.model`; summaries output in Vietnamese + English (2-3 sentences each)
- **Retry logic**: `tenacity` with exponential backoff for AI (max 3 attempts, 2–10s) and webhooks (3 attempts, 5s delay)
- **Language handling**: `langdetect` auto-detects article language; falls back to source-declared language

## Configuration

- `config/settings.yaml` — all tunable parameters (crawl interval, batch sizes, AI model, webhook URLs, timeouts)
- `config/sources.yaml` — RSS source definitions (11 feeds: EN, VI, JA, KO)

To add a new RSS source, add an entry to `config/sources.yaml` and restart.

## Inspecting Data

```bash
# SQLite stats
sqlite3 data/stats.db "SELECT * FROM crawl_logs ORDER BY started_at DESC LIMIT 10;"
sqlite3 data/stats.db "SELECT * FROM ai_logs ORDER BY created_at DESC LIMIT 10;"

# Redis article store
redis-cli ZREVRANGE news:feed 0 9 WITHSCORES
redis-cli HGETALL news:{article_id}
redis-cli SCARD news:dedup:simhashes
```

## Module Map

| Module | Responsibility |
|--------|---------------|
| `main.py` | App startup, lifespan, dependency wiring |
| `scheduler.py` | APScheduler job definitions |
| `crawler/fetcher.py` | Async concurrent RSS fetching |
| `crawler/rss_parser.py` | RSS parsing + language detection |
| `crawler/dedup.py` | SimHash deduplication logic |
| `storage/redis_store.py` | Redis read/write (articles, indices, dedup) |
| `storage/sqlite_stats.py` | SQLite logging for analytics |
| `ai/rewriter.py` | OpenAI-compatible API calls + batch processing |
| `webhook/dispatcher.py` | HTTP POST dispatch with retry |
| `dashboard/app.py` | FastAPI routes + HTMX partial endpoints |
