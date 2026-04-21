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

**Entry point:** `main.py` initializes SQLite, Redis, mounts the FastAPI app from `dashboard/app.py`, and starts `APScheduler` (eight jobs).

**Async scheduler jobs** (`jobs/scheduler.py`):
1. **Crawl job** (default every 3 min): sources split into N stagger groups; each tick crawls one group round-robin → per-domain rate-limited RSS fetch → parse → SimHash dedup → save to Redis. Full cycle = interval × groups (e.g. 3min × 3 groups = ~9min).
2. **AI rewrite job** (default every 2 min): pulls up to 10 pending articles from Redis → **age filter** (busy categories: 15min, moderate: 20min, quiet: 30min — configurable) → calls OpenAI-compatible API → stores summaries → dispatches to webhooks + Telegram. **Only runs when ai_mode="rewrite" hooks exist.**
3. **Topic synthesis job** (default every 10 min, optional): groups articles by category → AI analyzes content diversity → generates 1-8 synthetic summaries with different angles → saves to Redis. AI autonomously decides output count. **Supports two trigger modes:**
   - `trigger_mode: interval` — process all active categories on schedule (wastes quota on unused categories)
   - `trigger_mode: on_demand` — only process categories with enabled synthetic webhooks (recommended)
4. **Debate job** (default every 30 min, optional): multi-agent AI debate on stories with enough articles → 4 agents (optimist, pessimist, analyst, skeptic) debate different perspectives → generates balanced analysis → dispatches to webhooks + Telegram. **Only runs when debate.enabled=true and ai_mode="debate" hooks exist.** Model inherited from selected AI provider.
5. **Scheduled webhook job** (default every 1 min): checks SQLite for due webhook schedules (cron-based) → fetches articles by filter → **respects ai_mode** → dispatches to configured endpoints. Articles are filtered based on endpoint's ai_mode:
   - `ai_mode: off` — dispatch all articles (default)
   - `ai_mode: rewrite` — only dispatch articles with ai_status="done"
   - `ai_mode: synthetic` — only dispatch synthetic articles (type="synthetic")
   - `ai_mode: debate` — only dispatch debate articles (type="debate")
6. **Social article job** (default every 6 hours, optional): generates long-form articles (2000-3000 words) from recent news → AI creates structured content with multiple sections → generates detailed image prompts for thumbnails and illustrations → saves to Redis. **Only runs when social_article.enabled=true and social_article.auto_generate=true.**

**Anti-ban measures** (`crawler/fetcher.py`): per-domain locks (same-domain feeds serialized), random delays (1-3s), User-Agent rotation, 429 retry with Retry-After, request order shuffling.

**Storage dual-layer:**
- **Redis** (24h TTL hot store): articles as hashes (`news:{id}`), sorted sets for feeds (`news:feed`), dedup simhash set (`news:dedup:simhashes`)
- **SQLite** (analytics): `crawl_logs` (with duration_ms, http_status, domain), `webhook_logs`, `ai_logs`, `webhook_schedules` tables

**Dashboard** (`dashboard/app.py`): FastAPI routes serve Jinja2 templates; HTMX polls `/partials/stats` and `/partials/feed` every 30 seconds — no custom JavaScript.

## Key Design Decisions

- **Article ID**: SHA256 of `source_id:url` (first 16 chars hex)
- **Deduplication**: 64-bit SimHash on normalized titles; Hamming distance ≤ 3 = duplicate
- **AI config**: All AI settings managed via Settings UI → stored in `settings.yaml`. AI providers hold credentials (api_key, base_url, model). Each feature (rewrite, synthesis, debate) can select its own provider or inherit from global AI config. Three tones: `formal`, `casual`, `general`. Test button verifies connectivity.
- **Retry logic**: `tenacity` with exponential backoff for AI (max 3 attempts, 2–10s) and webhooks (3 attempts, 5s delay)
- **Payload modes**: Each webhook/Telegram channel configures `payload_mode`: `full` (all data), `fields` (pick specific), `template` (Jinja2 custom)
- **Article type filtering**: Webhooks/Telegram can filter by article type (`original` from RSS, `synthetic` from AI). Modes: `all` (default), `include`, `exclude`
- **AI mode filter**: All dispatch paths (ai_job, synthesis_job, debate_job, scheduled_webhook_job) respect the `ai_mode` setting:
  - `off` — raw articles, no AI processing
  - `rewrite` — one-to-one AI summaries, only dispatches ai_status="done"
  - `synthetic` — multi-article synthesis, only dispatches type="synthetic"
  - `debate` — multi-agent debate (4 perspectives), only dispatches type="debate"
- **Debate mode**: Configurable via Settings UI → AI → Multi-Agent Debate. Toggle on/off, select AI provider (inherits from global if empty), set interval (5-120 min, default 30). Model is inherited from the selected provider. Requires webhooks/channels with `ai_mode: debate`.
- **Social Article**: Long-form content generator (2000-3000 words) with AI-generated image prompts. Configurable via Settings UI → AI → Social Article Generator. Features:
  - **Style presets**: blog_formal, blog_casual, linkedin, medium, newsletter, twitter_thread
  - **Custom styles**: Define tone, length, section count, description
  - **Image prompts**: Detailed DALL-E/Midjourney prompts for thumbnail + each section
  - **Auto-generation**: Optional scheduled generation (default every 6h)
  - **On-demand**: Manual generation via `/social-articles` UI
  - **Storage**: Redis with 7-day TTL, indexed in `social_articles:index` sorted set
  - **API**: Full CRUD via `/api/social-article/*` endpoints
- **Webhook scheduling**: Cron-based scheduled webhook triggers managed via UI. SQLite stores schedules, APScheduler executes every minute. Scheduled webhooks respect ai_mode filters.
- **Synthesis trigger modes**: `interval` (process all categories on schedule) vs `on_demand` (only categories with enabled synthetic hooks). On-demand mode prevents wasting API quota on unused categories.
- **Age-based skip**: Articles older than category-specific thresholds (busy: 15min, moderate: 20min, quiet: 30min) are skipped during AI processing to save API quota. Configurable via `settings.yaml`. See `docs/AGE_SKIP_EXPLAINED.md` for details.
- **Language handling**: `langdetect` auto-detects article language; falls back to source-declared language

## Configuration

- `config/settings.yaml` — all tunable parameters (AI providers, crawl interval, batch sizes, webhook URLs, timeouts, debate config)
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

# Webhook schedules (SQLite)
sqlite3 data/stats.db "SELECT * FROM webhook_schedules ORDER BY next_run_at;"
sqlite3 data/stats.db "SELECT * FROM system_logs WHERE event_type = 'webhook_schedule' ORDER BY started_at DESC LIMIT 10;"

# Content channels (Redis — per-client state)
redis-cli KEYS "channel:*:client:*"                              # all client state keys
redis-cli GET "channel:{channel_id}:client:{client_id}:cursor"   # client cursor
redis-cli SMEMBERS "channel:{channel_id}:client:{client_id}:delivered"  # delivered article IDs
redis-cli HGETALL "channel:{channel_id}:client:{client_id}:stats"      # pull stats

# Content channels via API (app must be running)
curl -s -H "X-API-Key: KEY" -H "X-Client-ID: bot-1" http://localhost:8000/api/channels/{id}/feed | jq .
curl -s -H "X-API-Key: KEY" -H "X-Client-ID: bot-1" http://localhost:8000/api/channels/{id}/next | jq .
curl -s -H "X-Client-ID: bot-1" http://localhost:8000/api/channels/{id}/stats | jq .
curl -s http://localhost:8000/api/channels/{id}/clone-data | jq .  # clone config for duplication

# Content channel API logs (SQLite — all consumer endpoints tracked)
sqlite3 data/stats.db "SELECT * FROM channel_logs ORDER BY requested_at DESC LIMIT 20;"
sqlite3 data/stats.db "SELECT auth_method, COUNT(*) FROM channel_logs GROUP BY auth_method;"
sqlite3 data/stats.db "SELECT client_id, endpoint, COUNT(*) FROM channel_logs GROUP BY client_id, endpoint;"
sqlite3 data/stats.db "SELECT endpoint, AVG(duration_ms) FROM channel_logs GROUP BY endpoint;"
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
| `storage/webhook_schedules.py` | SQLite CRUD for cron-based webhook schedules |
| `jobs/scheduled_webhook.py` | Scheduled webhook execution job (runs every minute) |
| `dashboard/routes/schedules_api.py` | REST API for webhook schedules management |
| `dashboard/routes/channels_api.py` | Pull-based content channels API (CRUD + feed/ack), global key auth |
| `ai/channel_processor.py` | On-demand AI processing for channels — **MERGED**: rewrite+style in ONE call |
| `ai/style_transform.py` | Style transform helpers (platform presets, format instructions) — used by channel_processor |
| `ai/social_article.py` | Long-form article generator with image prompts (2000-3000 words, structured sections) |
| `dashboard/routes/social_article_api.py` | REST API for social article generation, CRUD, and management |
| `jobs/social_article_job.py` | Scheduled social article generation job (runs every 6h by default) |
| `dashboard/config_io.py` | YAML I/O helpers (sources, settings, webhooks, channels, channels_config) |

## Content Channels (Pull-based API)

Channels are pull-based alternatives to webhooks. External services (Twitter bots, Facebook pages, blog publishers) poll for articles on their own schedule.

**⚡ OPTIMIZATION:** AI processing + style transform merged into **ONE AI call** to save API quota. Previously required 2 calls (rewrite → style), now only 1.

**Two-axis architecture:**
- **Content Mode** (source): `rewrite`, `synthetic`, `newsletter`, `long_article`, `debate`
- **Output Format** (presentation): `summary`, `thread`, `breaking`, `listicle`, `hot_take`, `deep_dive`, `quote_highlight`, `carousel`

**On-demand AI processing** — Channels ALWAYS pull from `news:feed` (original articles). AI processing happens when client requests:
- `ai_mode=off`: Raw articles, no processing
- `ai_mode=rewrite`: 1 article → AI rewrite → return (cached 1h per article)
- `ai_mode=synthetic`: 3-10 articles → AI synthesize → return 1 result (cached 1h per batch)
- `ai_mode=debate`: 3-10 articles → AI debate → return 1 result (cached 1h per batch)

**Timeout:** AI processing timeout is configurable via `channels_config.ai_timeout_seconds` in settings.yaml (default: 60s). If AI call exceeds this limit, request fails with 500 error. Applies to all AI modes (rewrite/synthetic/debate).

**Minimum articles:** synthetic/debate modes require ≥3 articles. If insufficient, endpoints return 204 No Content.

**Caching strategy:** AI results cached in Redis to save API quota:
- Rewrite (with style): `channel:{id}:rewrite:{article_id}:{lang}:{format}` (1 hour TTL) — includes both rewrite + style
- Rewrite (no style): `channel:{id}:rewrite:{article_id}:{lang}` (1 hour TTL) — backward compat
- Synthetic: `channel:{id}:synthetic:{category}:{batch_hash}` (1 hour TTL)
- Debate: `channel:{id}:debate:{category}:{batch_hash}` (1 hour TTL)

**Platform presets:** `twitter` (280 chars, punchy), `facebook` (2000, engaging), `blog` (5000, formal), `telegram` (4096, concise), `custom`

**AI Source:** `system` (server AI config) or `client` (credentials via `X-AI-API-Key`, `X-AI-Base-URL`, `X-AI-Model` headers)

**Style Source:** `preset` (platform defaults), `custom` (channel config), `client` (style_prompt query param)

**Auth:** Three-tier API key system:
1. **Global API key** (`channels_config.global_api_key` in settings.yaml) — shared across all channels
2. **Per-channel API key** — overrides global key if set
3. **Public access** — `require_api_key: false` disables auth entirely

Auth priority: per-channel key → global key → skip if `require_api_key=false`.

**Client tracking:** All consumer endpoints (`/feed`, `/next`, `/ack`, `/reset-cursor`, `/stats`) require `X-Client-ID` header. Multiple clients can share one channel — each gets independent cursor, delivered set, and stats.

**Logging:** All channel API requests are logged to SQLite `channel_logs` table with fields: `channel_id`, `client_id`, `endpoint`, `method`, `status_code`, `auth_method` (`per_channel_key`, `global_key`, or `public`), `items_count`, `requested_at`, `duration_ms`, `error_msg`. Use this for monitoring client activity, auth method usage, error rates, and performance.

**Clone feature:** Use `/api/channels/{id}/clone-data` to fetch all channel config (except `id` and `api_key`) with " (Copy)" appended to name. Useful for quickly duplicating channel settings when creating similar channels.

**Redis keys:** `channel:{id}:client:{client_id}:cursor`, `channel:{id}:client:{client_id}:stats`, `channel:{id}:client:{client_id}:delivered`, `channel:{id}:rewrite:{article_id}:{lang}`, `channel:{id}:synthetic:{category}:{batch_hash}`, `channel:{id}:debate:{category}:{batch_hash}`, `channel:{id}:rewrite:{article_id}:{lang}:{format}` (merged rewrite+style)

**Global config:** Managed via Settings UI → Channels tab → Global Config section. Set once, works on all channels (unless overridden per-channel).
