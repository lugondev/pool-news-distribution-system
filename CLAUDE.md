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

**Environment variables** (see `.env.example`):
- `REDIS_URL` — Redis connection URL (default `redis://localhost:6379/0`)
- `SQLITE_PATH` — Path to SQLite stats DB (default `./data/stats.db`)

**AI config** (api_key, base_url, model, tone) is managed entirely via the Settings UI → stored in `config/settings.yaml`. No AI env vars needed.

## Architecture

The system is a pipeline: **RSS feeds → SimHash dedup → Redis → AI batch → Webhooks + Telegram**.

**Entry point:** `main.py` initializes SQLite, Redis, mounts the FastAPI app from `dashboard/app.py`, and starts `APScheduler` (three jobs).

**Three async scheduler jobs** (`scheduler.py`):
1. **Crawl job** (default every 3 min): sources split into N stagger groups; each tick crawls one group round-robin → per-domain rate-limited RSS fetch → parse → SimHash dedup → save to Redis. Full cycle = interval × groups (e.g. 3min × 3 groups = ~9min).
2. **AI rewrite job** (default every 2 min): pulls up to 10 pending articles from Redis → calls OpenAI-compatible API → stores summaries → dispatches to webhooks + Telegram
3. **Topic synthesis job** (default every 5 min, optional): groups articles by category → AI analyzes content diversity → generates 1-8 synthetic summaries with different angles → saves to Redis. AI autonomously decides output count.

**Anti-ban measures** (`crawler/fetcher.py`): per-domain locks (same-domain feeds serialized), random delays (1-3s), User-Agent rotation, 429 retry with Retry-After, request order shuffling.

**Storage dual-layer:**
- **Redis** (24h TTL hot store): articles as hashes (`news:{id}`), sorted sets for feeds (`news:feed`), dedup simhash set (`news:dedup:simhashes`)
- **SQLite** (analytics): `crawl_logs` (with duration_ms, http_status, domain), `webhook_logs`, `ai_logs` tables

**Dashboard** (`dashboard/app.py`): FastAPI routes serve Jinja2 templates; HTMX polls `/partials/stats` and `/partials/feed` every 30 seconds — no custom JavaScript.

## Key Design Decisions

- **Article ID**: SHA256 of `source_id:url` (first 16 chars hex)
- **Deduplication**: 64-bit SimHash on normalized titles; Hamming distance ≤ 3 = duplicate
- **AI config**: All AI settings (api_key, base_url, model, tone) in `settings.yaml`, managed via Settings UI. Three tones: `formal`, `casual`, `general`. Test button verifies connectivity.
- **Retry logic**: `tenacity` with exponential backoff for AI (max 3 attempts, 2–10s) and webhooks (3 attempts, 5s delay)
- **Payload modes**: Each webhook/Telegram channel configures `payload_mode`: `full` (all data), `fields` (pick specific), `template` (Jinja2 custom)
- **Article type filtering**: Webhooks/Telegram can filter by article type (`original` from RSS, `synthetic` from AI). Modes: `all` (default), `include`, `exclude`. See `ARTICLE_TYPE_FILTER.md` for details.
- **Language handling**: `langdetect` auto-detects article language; falls back to source-declared language

## Configuration

- `config/settings.yaml` — all tunable parameters (AI api_key/base_url/model/tone, crawl interval, batch sizes, webhook URLs, timeouts)
- `config/sources.yaml` — RSS source definitions (39 feeds: EN, VI, JA, KO across US/EU/ME/Asia)

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

# Crawl tracing via API (app must be running)
curl -s http://localhost:8000/api/logs/crawl/domains | jq .     # per-domain health
curl -s http://localhost:8000/api/logs/crawl/errors | jq .      # error breakdown
curl -s http://localhost:8000/api/logs/crawl/sources | jq .     # per-source stats
curl -s http://localhost:8000/api/logs/crawl/timeline | jq .    # hourly performance
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
| `webhook/payload.py` | Shared payload builder (full/fields/template modes) |
| `webhook/telegram.py` | Telegram Bot API dispatcher + HTML formatting |
| `webhook/filters.py` | Article filtering logic (category, source, article type) |
| `dashboard/api_router.py` | JSON API: CRUD, logs, crawl tracing, Telegram endpoints |
| `ai/topic_synthesis.py` | Multi-article AI synthesis (generates synthetic articles) |
