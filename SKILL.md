---
name: news-aggregator-ops
description: >-
  Manage, monitor, and troubleshoot the News Aggregator via its JSON API.
  Use when the user asks to check system health, inspect articles, view
  crawl/AI/webhook logs, manage RSS sources, categories, webhooks, webhook
  schedules (cron-based triggers), AI settings, providers, AI configs,
  diagnose pipeline issues, search articles semantically (RAG), or view
  intelligence features (trends, stories, newsletter, debates).
---

# News Aggregator Operations

All interactions go through the JSON API. Use `curl` with `jq` for readable output. The app must be running (`python main.py`).

```bash
API=http://localhost:8000/api
```

> All examples below use `$API`. Run the line above first, or replace `$API` with the actual base URL.

## Health Check

```bash
curl -s $API/health | jq .
```

Response: `{"status": "ok", "redis": true}` or `{"status": "degraded", "redis": false}`.

If degraded, check Redis: `redis-cli ping`.

## Articles

### List articles (preferred)

```bash
curl -s "$API/news?page=1&limit=20" | jq .
```

Query params: `page` (default 1), `limit` (default 20, max 100), `source`, `category`, `lang`, `ai_status`, `article_type` (`original` or `synthetic`).

Response includes `articles`, `pagination` (page, limit, total, total_pages, has_prev, has_next), and `filters`.

### List articles (legacy)

```bash
curl -s "$API/articles?limit=10" | jq .
```

Query params: `limit` (default 50), `offset`, `source`, `category`. Prefer `/api/news` for new integrations.

### Get single article

```bash
curl -s $API/articles/{article_id} | jq .
```

### List pending AI articles

```bash
curl -s "$API/articles/pending/list?limit=10" | jq .
```

## Stats

### Full stats (Redis + SQLite)

```bash
curl -s $API/stats | jq .
```

Returns:
- `redis`: `total_in_redis`, `today`, `this_hour`
- `db.crawl`: runs, found, saved, duplicates today
- `db.ai`: articles processed, total tokens today
- `db.webhook`: total sent, successes today
- `db.top_sources`: top 10 sources by saved count
- `db.hourly`: saved articles per hour (last 24h)

### Dedup stats

```bash
curl -s $API/stats/dedup | jq .
```

Returns `simhash_count` — number of fingerprints in the dedup pool.

## Sources (RSS Feeds)

### List all sources

```bash
curl -s $API/sources | jq .
```

### Add a source

```bash
curl -s -X POST $API/sources \
  -H "Content-Type: application/json" \
  -d '{"id":"new_source","name":"New Source","url":"https://example.com/rss.xml","lang":"en","category":"tech"}' | jq .
```

Fields: `id` (required, unique), `name`, `url`, `lang` (default "en"), `category` (default "world").

### Update a source

```bash
curl -s -X PUT $API/sources/{source_id} \
  -H "Content-Type: application/json" \
  -d '{"name":"Updated Name","url":"https://new-url.com/rss.xml"}' | jq .
```

Partial update — only send fields to change: `name`, `url`, `lang`, `category`.

### Toggle source enabled/disabled

```bash
curl -s -X POST $API/sources/{source_id}/toggle | jq .
```

### Delete a source

```bash
curl -s -X DELETE $API/sources/{source_id} | jq .
```

## Categories

### List categories

```bash
curl -s $API/categories | jq .
```

### Add a category

```bash
curl -s -X POST $API/categories \
  -H "Content-Type: application/json" \
  -d '{"id":"sports","name":"Sports"}' | jq .
```

### Toggle category

```bash
curl -s -X POST $API/categories/{cat_id}/toggle | jq .
```

Disabling a category skips all its sources during crawl.

### Delete a category

```bash
curl -s -X DELETE $API/categories/{cat_id} | jq .
```

## AI Settings

### Get current AI config

```bash
curl -s $API/settings/ai | jq .
```

### Update AI settings

```bash
curl -s -X PUT $API/settings/ai \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"model":"google/gemini-2.5-flash","batch_size":5,"max_tokens_summary":300}' | jq .
```

Partial update — fields: `enabled`, `model`, `temperature`, `batch_size`, `max_tokens_summary`, `retry_attempts`, `output_languages` (array).

### Toggle AI summary on/off

```bash
curl -s -X POST $API/settings/ai/toggle | jq .
```

### Toggle topic synthesis on/off

```bash
curl -s -X POST $API/settings/ai/synthesis/toggle | jq .
```

Topic synthesis groups articles by category and generates 1-8 synthetic summaries per category batch.

### Toggle debate mode on/off

```bash
curl -s -X POST $API/settings/ai/debate/toggle | jq .
```

Debate mode runs multi-agent AI debates (4 perspectives) on stories with enough articles. Configurable via Settings UI → AI → Multi-Agent Debate.

### Get social article settings

```bash
curl -s $API/settings/social-article | jq .
```

Returns current social article configuration (enabled, provider_id, default_style, default_category, default_hours, min_articles, max_articles, temperature, max_tokens, interval_minutes, auto_generate).

### Update social article settings

```bash
curl -s -X PUT $API/settings/social-article \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"provider_id":"together-ai","default_style":"blog_formal","auto_generate":true}' | jq .
```

Partial update — fields: `enabled`, `provider_id`, `default_style`, `default_category`, `default_hours`, `min_articles`, `max_articles`, `temperature`, `max_tokens`, `interval_minutes`, `auto_generate`.

### Toggle social article on/off

```bash
curl -s -X POST $API/settings/social-article/toggle | jq .
```

## AI Providers

Providers hold API credentials (api_key, base_url, model). Multiple providers can be configured.

### List providers (api_key masked)

```bash
curl -s $API/providers | jq .
```

### Get single provider (full api_key)

```bash
curl -s $API/providers/{provider_id} | jq .
```

### Add a provider

```bash
curl -s -X POST $API/providers \
  -H "Content-Type: application/json" \
  -d '{"name":"OpenRouter","api_key":"sk-...","base_url":"https://openrouter.ai/api/v1","model":"google/gemini-2.5-flash"}' | jq .
```

### Update a provider

```bash
curl -s -X PUT $API/providers/{provider_id} \
  -H "Content-Type: application/json" \
  -d '{"name":"OpenRouter","api_key":"sk-...","base_url":"https://openrouter.ai/api/v1","model":"gpt-4o"}' | jq .
```

### Delete a provider

```bash
curl -s -X DELETE $API/providers/{provider_id} | jq .
```

### Test a provider connection

```bash
curl -s -X POST $API/providers/{provider_id}/test | jq .
```

## AI Configs

AI Configs are named presets (tone + custom prompt) that can be assigned to individual webhooks or Telegram channels.

### List configs

```bash
curl -s $API/ai-configs | jq .
```

### Create a config

```bash
curl -s -X POST $API/ai-configs \
  -H "Content-Type: application/json" \
  -d '{"name":"Formal English","tone":"formal","prompt_system":"You are a professional news editor.","prompt_template":"","is_default":false}' | jq .
```

Fields: `name` (required), `tone` (`formal`|`casual`|`general`), `prompt_system`, `prompt_template`, `is_default`.

### Update a config

```bash
curl -s -X PUT $API/ai-configs/{config_id} \
  -H "Content-Type: application/json" \
  -d '{"name":"Casual VI","tone":"casual"}' | jq .
```

### Set as default

```bash
curl -s -X POST $API/ai-configs/{config_id}/set-default | jq .
```

### Delete a config

```bash
curl -s -X DELETE $API/ai-configs/{config_id} | jq .
```

## Crawl Logs & Tracing

### Browse all crawl logs (filterable)

```bash
curl -s "$API/logs/crawl?page=1&limit=20" | jq .
```

Query params: `page`, `limit`, `source`, `domain`, `errors_only` (bool), `http_status` (int), `since` (ISO datetime).

Each log entry: `id`, `source_id`, `started_at`, `finished_at`, `duration_ms`, `http_status`, `domain`, `found`, `saved`, `duplicates`, `errors`, `error_msg`.

### Filter: only errors

```bash
curl -s "$API/logs/crawl?errors_only=true&limit=50" | jq .
```

### Filter: by HTTP status (e.g. 429 rate-limited)

```bash
curl -s "$API/logs/crawl?http_status=429" | jq .
```

### Filter: by domain

```bash
curl -s "$API/logs/crawl?domain=cnbc.com" | jq .
```

### Filter: by source

```bash
curl -s "$API/logs/crawl?source=reuters_business" | jq .
```

### Per-source summary (success rate, avg latency, error counts)

```bash
curl -s "$API/logs/crawl/sources" | jq .
```

Optional: `?since=2026-03-20T00:00:00` to limit time range.

Returns per source: `source_id`, `domain`, `total_runs`, `success_runs`, `failed_runs`, `success_rate`, `avg_duration_ms`, `max_duration_ms`, `total_found`, `total_saved`, `total_duplicates`, `last_run`.

### Single source history

```bash
curl -s "$API/logs/crawl/sources/cnbc_economy?page=1" | jq .
```

### Per-domain summary (IP-ban detection)

```bash
curl -s "$API/logs/crawl/domains" | jq .
```

Returns per domain: `domain`, `total_requests`, `failed_requests`, `rate_limited` (429 count), `forbidden` (403 count), `avg_duration_ms`, `max_duration_ms`, `success_rate`, `source_count`, `last_request`.

Use this to detect which domains are rate-limiting or blocking.

### Error breakdown by type

```bash
curl -s "$API/logs/crawl/errors" | jq .
```

Returns: `error_type` (429 Rate Limited, 403 Forbidden, Timeout, Connection Error, etc.), `count`, `affected_sources`.

### Hourly crawl timeline

```bash
curl -s "$API/logs/crawl/timeline?hours=24" | jq .
```

Returns per hour: `hour`, `runs`, `found`, `saved`, `duplicates`, `errors`, `avg_duration_ms`.

## AI Logs

```bash
curl -s "$API/logs/ai?page=1&limit=20" | jq .
```

Returns `logs` (article_id, model, tokens_used, created_at), `total`, `page`, `total_pages`.

## System Logs (Scheduler Events)

### Browse all system events

```bash
curl -s "$API/logs/system?page=1&limit=20" | jq .
```

Query params: `event_type` (crawl_job, ai_job, log_cleanup_job), `status` (ok, error, skipped), `since` (ISO datetime).

Each entry: `id`, `event_type`, `started_at`, `finished_at`, `duration_ms`, `status`, `metadata` (job-specific JSON), `error_msg`.

**Event types:**
- `crawl_job` — RSS crawl execution
- `ai_job` — AI rewrite batch processing
- `topic_synthesis_job` — Multi-article synthesis
- `debate_job` — Multi-agent debate
- `webhook_schedule` — Scheduled webhook execution
- `social_article_job` — Long-form article generation
- `log_cleanup_job` — Log cleanup execution (metadata includes `total_deleted`, `cutoff`, `results` per table)

### Filter: only errors

```bash
curl -s "$API/logs/system?status=error" | jq .
```

### Filter: crawl jobs only

```bash
curl -s "$API/logs/system?event_type=crawl_job&limit=10" | jq .
```

### Summary by event type

```bash
curl -s "$API/logs/system/summary" | jq .
```

Returns per event_type: `total_runs`, `success_runs`, `error_runs`, `avg_duration_ms`, `max_duration_ms`, `last_run`.

## Scheduler Status

```bash
curl -s $API/logs/scheduler/status | jq .
```

Returns `{"jobs": [...]}` — current state of all APScheduler jobs (next run time, status, etc.).

**Active jobs:**
- `crawl_all` — RSS feed crawler (default every 3 min)
- `ai_rewrite` — AI article rewriter (default every 2 min)
- `topic_synthesis` — Multi-article synthesis (default every 10 min)
- `debate` — Multi-agent debate (default every 30 min, opt-in)
- `scheduled_webhook` — Cron-based webhook triggers (every 1 min)
- `social_article` — Long-form article generator (default every 6h, opt-in)
- `log_cleanup` — Log cleanup job (every 5h) — deletes logs older than 5h from all log tables if table has ≥200 rows

## API Request Logs

### Browse all API requests

```bash
curl -s "$API/logs/api?page=1&limit=20" | jq .
```

Query params: `method` (GET, POST, PUT, DELETE), `path` (partial match), `status_code` (exact), `errors_only` (bool), `since` (ISO datetime).

Each entry: `id`, `method`, `path`, `status_code`, `duration_ms`, `requested_at`, `error_msg`.

### Filter: only errors (4xx/5xx)

```bash
curl -s "$API/logs/api?errors_only=true&limit=50" | jq .
```

### Filter: slow requests > 500ms (post-filter client-side)

```bash
curl -s "$API/logs/api/summary" | jq '[.endpoints[] | select(.avg_duration_ms > 500)]'
```

### Per-endpoint summary

```bash
curl -s "$API/logs/api/summary" | jq .
```

Returns per endpoint: `method`, `path`, `total_requests`, `success_count`, `error_count`, `error_rate`, `avg_duration_ms`, `max_duration_ms`, `last_request`.

## Webhooks

### List webhooks

```bash
curl -s $API/webhooks | jq .
```

### Add a webhook

```bash
curl -s -X POST $API/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my_hook",
    "name": "My Hook",
    "url": "https://example.com/hook",
    "http_method": "POST",
    "content_type": "application/json",
    "payload_mode": "full",
    "filter_categories_mode": "all",
    "filter_sources_mode": "all",
    "filter_article_types_mode": "all",
    "filter_article_types": [],
    "ai_mode": "rewrite",
    "ai_config_id": "",
    "target_language": "vi",
    "rate_limit_max": 0,
    "rate_limit_window_minutes": 60
  }' | jq .
```

**Webhook fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | required | Unique slug |
| `name` | string | required | Display name |
| `url` | string | required | Endpoint URL |
| `http_method` | string | `POST` | `POST` or `GET` |
| `content_type` | string | `application/json` | Content-Type header |
| `retry_attempts` | int | 3 | Max retries on failure |
| `retry_delay_seconds` | int | 5 | Delay between retries |
| `timeout_seconds` | int | 10 | Request timeout |
| `payload_mode` | string | `full` | `full`, `fields`, or `template` |
| `payload_fields` | list | `[]` | Fields to include (for `fields` mode) |
| `payload_template` | string | `""` | Jinja2 template (for `template` mode) |
| `filter_categories_mode` | string | `all` | `all`, `include`, or `exclude` |
| `filter_categories` | list | `[]` | Category IDs to filter |
| `filter_sources_mode` | string | `all` | `all`, `include`, or `exclude` |
| `filter_sources` | list | `[]` | Source IDs to filter |
| `filter_article_types_mode` | string | `all` | `all`, `include`, or `exclude` |
| `filter_article_types` | list | `[]` | `original`, `synthetic`, or both |
| `ai_mode` | string | `rewrite` | `rewrite` (per-article), `synthetic` (batch), `off` (raw) |
| `ai_config_id` | string | `""` | AI Config ID (empty = built-in global) |
| `target_language` | string | `""` | Translate to this language code (empty = origin only) |
| `rate_limit_max` | int | 0 | Max deliveries per window (0 = unlimited) |
| `rate_limit_window_minutes` | int | 60 | Rate limit window in minutes |

### Update a webhook

```bash
curl -s -X PUT $API/webhooks/{wh_id} \
  -H "Content-Type: application/json" \
  -d '{"name":"Updated Hook","payload_mode":"fields","payload_fields":["title","url","ai_summary_en","category"]}' | jq .
```

Partial update — only send fields to change.

### Toggle webhook

```bash
curl -s -X POST $API/webhooks/{wh_id}/toggle | jq .
```

### Delete a webhook

```bash
curl -s -X DELETE $API/webhooks/{wh_id} | jq .
```

### Test a webhook (sends mock article)

```bash
curl -s -X POST $API/webhooks/{wh_id}/test | jq .
```

Returns request details, status code, elapsed ms, and a preview of the payload sent.

## Webhook Logs

```bash
curl -s "$API/logs/webhooks?page=1&limit=20" | jq .
```

Returns `logs` (article_id, webhook_id, webhook_url, sent_at, status_code, success, error_msg), `total`, `page`, `total_pages`.

## Webhook Schedules

Scheduled webhooks trigger automatically based on cron expressions, independent of RSS crawl or AI processing. Use cases: periodic digests, daily summaries, hourly updates.

### List all schedules

```bash
curl -s $API/schedules | jq .
```

Returns all webhook schedules with `id`, `name`, `cron_expression`, `enabled`, `webhook_endpoint_id` or `telegram_channel_id`, `query_params` (category, source, limit filters), `last_run_at`, `next_run_at`.

### Get single schedule

```bash
curl -s $API/schedules/{schedule_id} | jq .
```

### Create a schedule

```bash
curl -s -X POST $API/schedules \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AI News Digest",
    "cron_expression": "*/5 * * * *",
    "enabled": true,
    "webhook_endpoint_id": "my-webhook",
    "telegram_channel_id": null,
    "query_params": {
      "category": "ai",
      "source": null,
      "limit": 3
    }
  }' | jq .
```

**Fields:**
- `name` (required): Display name
- `cron_expression` (required): Standard cron format `minute hour day month weekday`
  - Examples: `*/5 * * * *` (every 5min), `0 9 * * *` (daily 9am), `0 */2 * * *` (every 2h)
- `enabled` (default true): Active/inactive toggle
- `webhook_endpoint_id` or `telegram_channel_id` (one required): Target delivery endpoint
- `query_params.category` (optional): Filter by category
- `query_params.source` (optional): Filter by source_id
- `query_params.limit` (default 10, max 50): Max articles to fetch per trigger

**Auto-computed fields:**
- `next_run_at`: Calculated from cron expression (croniter)
- `last_run_at`: Updated after each execution

### Update a schedule

```bash
curl -s -X PUT $API/schedules/{schedule_id} \
  -H "Content-Type: application/json" \
  -d '{"name":"Updated Name","cron_expression":"0 9 * * *","query_params":{"limit":5}}' | jq .
```

Partial update — only send fields to change. Updating `cron_expression` recalculates `next_run_at`.

### Toggle schedule

```bash
curl -s -X POST $API/schedules/{schedule_id}/toggle | jq .
```

Enables/disables schedule execution.

### Delete a schedule

```bash
curl -s -X DELETE $API/schedules/{schedule_id} | jq .
```

### Trigger schedule manually

```bash
curl -s -X POST $API/schedules/{schedule_id}/trigger | jq .
```

Executes schedule immediately (ignores cron timing) and updates `next_run_at`.

### View schedule execution logs

```bash
curl -s "$API/logs/system?event_type=webhook_schedule&page=1&limit=20" | jq .
```

Filter system logs by `event_type=webhook_schedule`. Each entry includes `metadata.schedule_id`, `metadata.article_count`, `metadata.webhook_id` or `metadata.telegram_id`, and execution status.

## Social Articles (Long-form Content)

Social Articles are AI-generated long-form content (2000-3000 words) with structured sections and detailed image prompts for DALL-E/Midjourney.

### List style presets

```bash
curl -s $API/social-article/styles | jq .
```

Returns available style presets: `blog_formal`, `blog_casual`, `linkedin`, `medium`, `newsletter`, `twitter_thread`. Each preset includes tone, length, section count, and description.

### Generate a social article

```bash
curl -s -X POST $API/social-article/generate \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "together-ai",
    "category": "tech",
    "style_preset": "blog_formal",
    "hours": 24,
    "min_articles": 3,
    "max_articles": 20,
    "temperature": 0.7,
    "max_tokens": 4000,
    "save": true
  }' | jq .
```

**Fields:**
- `provider_id` (optional): AI provider ID (null = use default from settings)
- `category` (optional): Filter articles by category (null = all categories)
- `style_preset` (optional): Style preset ID (default: blog_formal)
- `custom_style` (optional): Custom style object (overrides preset)
- `hours` (default 24): Look back N hours for source articles
- `min_articles` (default 3): Minimum articles required
- `max_articles` (default 20): Maximum articles to analyze
- `temperature` (default 0.7): AI temperature
- `max_tokens` (default 4000): Max tokens for AI response
- `save` (default true): Save to Redis after generation

Returns generated article with `title`, `subtitle`, `sections` (each with heading, content, image_prompt), `thumbnail_prompt`, `tags`, `estimated_read_time`, and `metadata`.

### Quick generate (with defaults)

```bash
curl -s -X POST $API/social-article/quick-generate | jq .
```

Generates article using default settings from config. Requires `social_article.enabled=true` in settings.

### List recent articles

```bash
curl -s "$API/social-article/list?limit=10" | jq .
```

Returns recent social articles (metadata only): `id`, `title`, `subtitle`, `tags`, `estimated_read_time`, `metadata`.

### Get single article

```bash
curl -s $API/social-article/{article_id} | jq .
```

Returns full article with all sections and image prompts.

### Delete an article

```bash
curl -s -X DELETE $API/social-article/{article_id} | jq .
```

Removes article from Redis and index.

### Custom style example

```bash
curl -s -X POST $API/social-article/generate \
  -H "Content-Type: application/json" \
  -d '{
    "category": "ai",
    "custom_style": {
      "name": "Technical Deep Dive",
      "description": "In-depth technical analysis with code examples",
      "tone": "formal",
      "length": "3000-4000 words",
      "sections": 6
    },
    "hours": 48,
    "save": true
  }' | jq .
```

## Telegram Channels

### List channels

```bash
curl -s $API/telegram | jq .
```

### Add a channel

```bash
curl -s -X POST $API/telegram \
  -H "Content-Type: application/json" \
  -d '{
    "id": "news_channel",
    "name": "Finance News",
    "bot_token": "123456:ABC...",
    "chat_id": "-1001234567890",
    "payload_mode": "full",
    "filter_categories_mode": "all",
    "filter_sources_mode": "all",
    "filter_article_types_mode": "all",
    "filter_article_types": [],
    "ai_mode": "rewrite",
    "target_language": "vi",
    "rate_limit_max": 0,
    "rate_limit_window_minutes": 60
  }' | jq .
```

Fields mirror webhook fields above (no `url`, `http_method`, `content_type`; adds `bot_token`, `chat_id`).

### Update a channel

```bash
curl -s -X PUT $API/telegram/{ch_id} \
  -H "Content-Type: application/json" \
  -d '{"name":"Updated Name","chat_id":"-100999"}' | jq .
```

### Toggle channel

```bash
curl -s -X POST $API/telegram/{ch_id}/toggle | jq .
```

### Delete a channel

```bash
curl -s -X DELETE $API/telegram/{ch_id} | jq .
```

### Test send (verify bot_token + chat_id)

```bash
curl -s -X POST $API/telegram/{ch_id}/test | jq .
```

Sends a test message to the channel. Returns `{"ok": true}` on success.

## Content Channels (Pull-based API)

Content channels are pull-based alternatives to webhooks. External services (Twitter bots, Facebook pages, blog publishers) poll for articles on their own schedule.

### Authentication

Channels support a **three-tier API key system**:

1. **Global API key** — shared across all channels (set once in Settings UI → Channels → Global Config)
2. **Per-channel API key** — overrides global key if set
3. **Public access** — `require_api_key: false` disables auth entirely

Auth priority: per-channel key → global key → skip if `require_api_key=false`.

All consumer endpoints require `X-API-Key` header (if auth enabled) and `X-Client-ID` header for tracking.

### List channels

```bash
curl -s $API/channels | jq .
```

### Get single channel

```bash
curl -s $API/channels/{channel_id} | jq .
```

### Create a channel

```bash
curl -s -X POST $API/channels \
  -H "Content-Type: application/json" \
  -d '{
    "id": "twitter_bot",
    "name": "Twitter Bot Feed",
    "enabled": true,
    "require_api_key": true,
    "max_items_per_fetch": 20,
    "platform": "twitter",
    "content_mode": "rewrite",
    "output_format": "summary",
    "ai_source": "system",
    "style_source": "preset",
    "payload_mode": "full",
    "filter_categories_mode": "all",
    "filter_sources_mode": "all",
    "filter_article_types_mode": "all",
    "ai_mode": "rewrite",
    "target_language": "en"
  }' | jq .
```

**Channel-specific fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `require_api_key` | bool | `true` | Require API key auth (false = public) |
| `max_items_per_fetch` | int | 20 | Max articles per `/feed` call |
| `platform` | string | `custom` | Platform preset: `twitter`, `facebook`, `blog`, `telegram`, `custom` |
| `content_mode` | string | `rewrite` | Content source: `rewrite`, `synthetic`, `newsletter`, `long_article`, `debate` |
| `output_format` | string | `summary` | Presentation: `summary`, `thread`, `breaking`, `listicle`, `hot_take`, `deep_dive`, `quote_highlight`, `carousel` |
| `ai_source` | string | `system` | AI credentials: `system` (server config) or `client` (via headers) |
| `style_source` | string | `preset` | Style config: `preset` (platform defaults), `custom` (channel config), `client` (query param) |
| `style` | object | `{}` | Custom style config (tone, max_length, custom_prompt) |

Channels also support all webhook filter fields: `filter_categories_mode`, `filter_categories`, `filter_sources_mode`, `filter_sources`, `filter_article_types_mode`, `filter_article_types`, `ai_mode`, `ai_config_id`, `target_language`, `payload_mode`, `payload_fields`, `payload_template`.

### Update a channel

```bash
curl -s -X PUT $API/channels/{channel_id} \
  -H "Content-Type: application/json" \
  -d '{"name":"Updated Name","max_items_per_fetch":50}' | jq .
```

Partial update — only send fields to change.

### Toggle channel

```bash
curl -s -X POST $API/channels/{channel_id}/toggle | jq .
```

### Delete a channel

```bash
curl -s -X DELETE $API/channels/{channel_id} | jq .
```

### Regenerate API key

```bash
curl -s -X POST $API/channels/{channel_id}/regenerate-key | jq .
```

Returns new `api_key`. Old key stops working immediately.

### Clone channel config

```bash
curl -s $API/channels/{channel_id}/clone-data | jq .
```

Returns all channel config (except `id` and `api_key`) with " (Copy)" appended to name. Use this to quickly duplicate channel settings when creating a new channel.

### Pull articles (feed)

```bash
curl -s -H "X-API-Key: YOUR_KEY" -H "X-Client-ID: bot-1" \
  "$API/channels/{channel_id}/feed?limit=10&auto_ack=false" | jq .
```

Query params:
- `limit` (default 20, max from channel config): Max articles to return
- `auto_ack` (default false): Auto-advance cursor after fetch
- `since` (optional): Unix timestamp — only articles after this time
- `style_prompt` (optional): Override style (if `style_source=client`)

Returns `{"articles": [...], "cursor": 1234567890.123, "count": 10}`.

**Client-provided AI** (if `ai_source=client`):
```bash
curl -s -H "X-API-Key: YOUR_KEY" -H "X-Client-ID: bot-1" \
  -H "X-AI-API-Key: sk-..." -H "X-AI-Base-URL: https://api.openai.com/v1" \
  -H "X-AI-Model: gpt-4o" \
  "$API/channels/{channel_id}/feed" | jq .
```

### Pull next article (single)

```bash
curl -s -H "X-API-Key: YOUR_KEY" -H "X-Client-ID: bot-1" \
  "$API/channels/{channel_id}/next" | jq .
```

Returns one article that hasn't been delivered to this client yet. Returns 204 when no articles available.

### Acknowledge articles (advance cursor)

```bash
curl -s -X POST $API/channels/{channel_id}/ack \
  -H "X-API-Key: YOUR_KEY" -H "X-Client-ID: bot-1" \
  -H "Content-Type: application/json" \
  -d '{"cursor": 1234567890.123}' | jq .
```

Advances client cursor to the given timestamp. Articles before this cursor won't be returned in future `/feed` or `/next` calls.

### Reset cursor (re-fetch from start)

```bash
curl -s -X POST $API/channels/{channel_id}/reset-cursor \
  -H "X-API-Key: YOUR_KEY" -H "X-Client-ID: bot-1" | jq .
```

Resets cursor to 0 and clears delivered set. Next `/feed` or `/next` will return articles from the beginning.

### Get pull stats

```bash
curl -s -H "X-Client-ID: bot-1" \
  "$API/channels/{channel_id}/stats" | jq .
```

Returns `{"total_pulls": 42, "total_items_delivered": 156, "last_pull_at": "..."}`.

No API key required for stats endpoint.

### Channel API logs

All channel consumer endpoints (`/feed`, `/next`, `/ack`, `/reset-cursor`, `/stats`) are logged to SQLite with client tracking.

```bash
# View recent channel API calls
sqlite3 data/stats.db "SELECT * FROM channel_logs ORDER BY requested_at DESC LIMIT 20;"

# Auth method breakdown
sqlite3 data/stats.db "SELECT auth_method, COUNT(*) FROM channel_logs GROUP BY auth_method;"

# Per-client activity
sqlite3 data/stats.db "SELECT client_id, endpoint, COUNT(*) as calls FROM channel_logs GROUP BY client_id, endpoint;"

# Error rate by endpoint
sqlite3 data/stats.db "SELECT endpoint, status_code, COUNT(*) FROM channel_logs GROUP BY endpoint, status_code;"

# Average response time by endpoint
sqlite3 data/stats.db "SELECT endpoint, AVG(duration_ms) as avg_ms FROM channel_logs GROUP BY endpoint;"
```

**Log fields:** `id`, `channel_id`, `client_id`, `endpoint`, `method`, `status_code`, `auth_method` (`per_channel_key`, `global_key`, or `public`), `items_count`, `requested_at`, `duration_ms`, `error_msg`.

## Payload Configuration

Both webhooks and Telegram channels support 3 payload modes:

### Mode 1: `full` (default)

Sends all article data as JSON (webhook) or formatted HTML (Telegram).

### Mode 2: `fields` — pick specific fields

```bash
curl -s -X PUT $API/webhooks/my_hook \
  -H "Content-Type: application/json" \
  -d '{"payload_mode":"fields","payload_fields":["title","url","ai_summary_vi","ai_summary_en","category"]}' | jq .
```

**Available fields for original articles:** `id`, `source_id`, `source_name`, `url`, `title`, `summary`, `content`, `lang`, `declared_lang`, `category`, `published_at`, `fetched_at`, `ai_summary_vi`, `ai_summary_en`, `ai_status`.

**Additional fields for synthetic articles:** `title_en`, `title_vi`, `content_en`, `content_vi`, `angle`, `source_article_ids`, `num_source_articles`, `ai_model`, `ai_tokens`, `created_at`.

Note: `ai_summary_{lang}` is dynamic — e.g. `ai_summary_ja` if `target_language="ja"`.

### Mode 3: `template` — custom Jinja2

```bash
curl -s -X PUT $API/webhooks/my_hook \
  -H "Content-Type: application/json" \
  -d '{"payload_mode":"template","payload_template":"{\"title\":\"{{ title }}\",\"link\":\"{{ url }}\",\"summary\":\"{{ ai_summary_en }}\"}" }' | jq .
```

Universal template (works for both original and synthetic):
```
{{ title|default(title_en, true) }} — {{ content_en|default(ai_summary_en, true) }}
```

Conditional example:
```
{% if type == 'synthetic' %}{{ content_en }}{% else %}{{ ai_summary_en }}{% endif %}
```

Telegram template example (HTML):
```
<b>{{ title }}</b>\n📡 {{ source_name }}\n\n{{ ai_summary_vi }}\n\n🔗 <a href="{{ url }}">Read more</a>
```

### Validate & preview a template

```bash
curl -s -X POST $API/payload/validate \
  -H "Content-Type: application/json" \
  -d '{"template":"{{ title }} - {{ source_name }}"}' | jq .
```

Returns `{"ok": true, "preview": "Fed Holds Rates... - Reuters Economy"}` or `{"ok": false, "error": "..."}`.

### List available fields

```bash
curl -s $API/payload/fields | jq .
```

### Preview payload output

```bash
curl -s -X POST $API/payload/preview \
  -H "Content-Type: application/json" \
  -d '{"payload_mode":"fields","payload_fields":["title","url","ai_summary_en"]}' | jq .
```

## Intelligence

Intelligence features provide trend analysis, story clustering, newsletter generation, and debate detection.

### Trending topics & entities

```bash
curl -s "$API/intelligence/trends?limit=20" | jq .
```

Query params: `limit` (default 20, max 50).

Returns `categories` (trending by volume), `trending_entities` (named entities with counts), `trending_count`.

### Active stories (clustered narratives)

```bash
curl -s "$API/intelligence/stories?limit=20" | jq .
```

Query params: `category` (filter by category), `limit` (default 20, max 50).

Returns `stories` list and `total`.

### Story detail

```bash
curl -s "$API/intelligence/stories/{story_id}" | jq .
```

Returns `story` (with `entities`, `top_sources`) and `articles` list (id, title, source_name, published_at, url, ai_status). Returns 404 if not found.

### Newsletter status

```bash
curl -s $API/intelligence/newsletter | jq .
```

Returns `{"available": true, "generated_at": "..."}` or `{"available": false}`.

### View newsletter (HTML)

```bash
curl -s $API/intelligence/newsletter/view
```

Returns HTML content of the latest newsletter (or a placeholder if none available).

### Generate newsletter

```bash
curl -s -X POST "$API/intelligence/newsletter/generate?language=English" | jq .
```

Query params: `language` (default "English"). Triggers AI-powered newsletter generation from recent articles.

### Debates

```bash
curl -s "$API/intelligence/debates?limit=10" | jq .
```

Query params: `limit` (default 10, max 20).

Returns `debates` list and `total` — recent AI-detected debates and contrasting viewpoints across sources.

## RAG (Semantic Search & Q&A)

RAG uses a Weaviate vector store to enable semantic article search and LLM-powered Q&A over indexed articles.

### Check RAG availability

```bash
curl -s $API/rag/status | jq .
```

Returns `{"weaviate_available": bool, "collection": "...", "indexed_articles": 1234}`.

### Semantic search

```bash
curl -s "$API/rag/search?q=federal+reserve+rates&limit=10" | jq .
```

Query params: `q` (required, min 2 chars), `limit` (default 10, max 50), `category` (optional filter).

Hybrid semantic + keyword search. Returns `{"query": "...", "results": [...], "count": N}`.

### Ask a question (full RAG pipeline)

```bash
curl -s -X POST $API/rag/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What are the latest developments in US-China trade?",
    "lang": "en",
    "limit": 5,
    "category": null,
    "alpha": 0.75
  }' | jq .
```

Body fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `question` | string | required | Question (3–500 chars) |
| `lang` | string | `"en"` | Answer language: `en`, `vi`, `ja`, `ko` |
| `limit` | int | 5 | Max source articles to retrieve (1–10) |
| `category` | string\|null | null | Filter articles by category |
| `alpha` | float | 0.75 | Search blend: 0.0 = BM25 keyword only, 1.0 = vector only |

Returns `{"answer": "...", "sources": [...], "retrieved": N}`. Returns 503 if vector store unavailable.

## Troubleshooting Workflows

### Scheduler jobs not running or crashing

1. `curl -s $API/logs/scheduler/status | jq .` — current job states
2. `curl -s "$API/logs/system?event_type=crawl_job&limit=5" | jq .` — recent crawl job executions
3. `curl -s "$API/logs/system?status=error" | jq '.logs[] | {event_type,error_msg,started_at}'` — all job errors
4. `curl -s "$API/logs/system/summary" | jq .` — overall job health (success/error ratio, avg duration)
5. High `avg_duration_ms` → crawl batch is slow, reduce `sources_per_tick`
6. Repeated `status=skipped` with `reason=no sources due` → all sources are in backoff, check crawl logs

### Log cleanup monitoring

Check log cleanup job execution:
```bash
curl -s "$API/logs/system?event_type=log_cleanup_job&limit=5" | jq .
```

View cleanup results:
```bash
curl -s "$API/logs/system?event_type=log_cleanup_job&limit=1" | jq '.logs[0].metadata'
```

Metadata includes:
- `total_deleted` — total rows deleted across all tables
- `cutoff` — timestamp cutoff (logs older than this were deleted)
- `results` — per-table breakdown (deleted count, total rows, remaining rows, or skipped if <200 rows)

**Tables cleaned:** `crawl_logs`, `webhook_logs`, `ai_logs`, `telegram_logs`, `system_logs`, `api_logs`, `channel_logs`

**Cleanup policy:** Runs every 5h, deletes logs older than 5h, only if table has ≥200 rows

### API latency or errors

1. `curl -s "$API/logs/api/summary" | jq '[.endpoints[] | select(.avg_duration_ms > 200)]'` — slow endpoints
2. `curl -s "$API/logs/api?errors_only=true&limit=20" | jq .` — recent 4xx/5xx errors
3. `curl -s "$API/logs/api?path=articles&limit=10" | jq .` — trace specific endpoint

### No new articles

1. `curl -s $API/health | jq .` — check Redis is up
2. `curl -s $API/stats | jq .db.crawl` — check if crawl ran today
3. If `found > 0` but `saved = 0` → all duplicates (normal)
4. If `found = 0` and `errors > 0` → RSS feed issue
5. `curl -s $API/sources | jq '.sources[] | select(.enabled==true)'` — verify sources are enabled

### Source keeps failing

1. `curl -s "$API/logs/crawl/sources/SOURCE_ID" | jq .logs[:5]` — recent history
2. Check `http_status` and `error_msg` fields
3. If 429 → being rate-limited, increase `domain_delay` in settings
4. If 403 → IP blocked or feed requires auth, consider disabling

### Detect IP bans / rate limiting

1. `curl -s $API/logs/crawl/domains | jq '.domains[] | select(.rate_limited > 0 or .forbidden > 0)'` — find problematic domains
2. `curl -s $API/logs/crawl/errors | jq .errors` — error breakdown by type
3. If a domain shows high `rate_limited` count → reduce concurrency or increase delay
4. If `forbidden` count is growing → IP is likely blocked, consider proxy or disabling that source

### Slow crawl performance

1. `curl -s $API/logs/crawl/sources | jq '[.sources[] | select(.avg_duration_ms > 5000)]'` — find slow sources
2. `curl -s "$API/logs/crawl/timeline?hours=24" | jq .timeline` — check hourly trends
3. High `avg_duration_ms` on a domain → server is slow or throttling

### AI not processing

1. `curl -s $API/articles/pending/list | jq .count` — check pending queue
2. `curl -s $API/settings/ai | jq .enabled` — verify AI is enabled
3. `curl -s "$API/logs/ai?limit=5" | jq .logs` — check recent AI activity
4. `curl -s $API/ai-configs | jq .` — verify AI configs are correct

### Webhook failures

1. `curl -s "$API/logs/webhooks?limit=10" | jq '.logs[] | select(.success==0)'` — find failures
2. `curl -s $API/webhooks | jq .endpoints` — verify endpoint config
3. Common causes: endpoint down, timeout too low, URL incorrect, article filtered out by type/category/source rules

### Scheduled webhook not firing

1. `curl -s $API/schedules | jq '.schedules[] | {name,enabled,next_run_at}'` — check schedule status and next run time
2. `curl -s "$API/logs/system?event_type=webhook_schedule&limit=10" | jq .` — view recent executions
3. `curl -s $API/logs/scheduler/status | jq '.jobs[] | select(.id | contains("scheduled_webhook"))'` — verify job is running
4. Common causes: schedule disabled, invalid cron expression, target webhook/telegram deleted, no articles match filters
5. Manual trigger test: `curl -s -X POST $API/schedules/{id}/trigger | jq .` — test execution immediately

### Telegram not sending

1. `curl -s -X POST $API/telegram/{ch_id}/test | jq .` — test the channel
2. If error "Forbidden: bot was blocked by the user" → bot was removed from channel
3. If error "Bad Request: chat not found" → wrong chat_id
4. If error "Unauthorized" → invalid bot_token
5. `curl -s "$API/logs/webhooks?limit=10" | jq '.logs[] | select(.webhook_url | startswith("telegram:"))'` — check Telegram delivery logs

### High duplicate rate

Check dedup pool size:
```bash
curl -s $API/stats/dedup | jq .
```

If pool is very large and you want to reset (re-crawl everything):
```bash
redis-cli DEL news:dedup:simhashes
```

### RAG / semantic search not working

1. `curl -s $API/rag/status | jq .` — check if Weaviate is available
2. If `weaviate_available: false` → Weaviate is not running or not reachable
3. If `indexed_articles: 0` → vector store is empty, articles need to be indexed

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | System health + Redis status |
| GET | `/api/news?page=&limit=&source=&category=&lang=&ai_status=&article_type=` | List articles (preferred, paginated) |
| GET | `/api/articles?limit=&offset=&source=&category=` | List articles (legacy) |
| GET | `/api/articles/{id}` | Single article detail |
| GET | `/api/articles/pending/list?limit=` | Pending AI articles |
| GET | `/api/stats` | Full stats (Redis + SQLite) |
| GET | `/api/stats/dedup` | Dedup simhash count |
| GET | `/api/sources` | List RSS sources |
| POST | `/api/sources` | Add source |
| PUT | `/api/sources/{id}` | Update source |
| POST | `/api/sources/{id}/toggle` | Toggle source |
| DELETE | `/api/sources/{id}` | Delete source |
| GET | `/api/categories` | List categories |
| POST | `/api/categories` | Add category |
| POST | `/api/categories/{id}/toggle` | Toggle category |
| DELETE | `/api/categories/{id}` | Delete category |
| GET | `/api/settings/ai` | Get AI config |
| PUT | `/api/settings/ai` | Update AI config |
| POST | `/api/settings/ai/toggle` | Toggle AI summary on/off |
| POST | `/api/settings/ai/synthesis/toggle` | Toggle topic synthesis on/off |
| POST | `/api/settings/ai/debate/toggle` | Toggle debate mode on/off |
| GET | `/api/settings/social-article` | Get social article settings |
| PUT | `/api/settings/social-article` | Update social article settings |
| POST | `/api/settings/social-article/toggle` | Toggle social article on/off |
| GET | `/api/social-article/styles` | List style presets |
| POST | `/api/social-article/generate` | Generate social article |
| POST | `/api/social-article/quick-generate` | Quick generate with defaults |
| GET | `/api/social-article/list?limit=` | List recent articles |
| GET | `/api/social-article/{id}` | Get single article |
| DELETE | `/api/social-article/{id}` | Delete article |
| GET | `/api/providers` | List AI providers (api_key masked) |
| GET | `/api/providers/{id}` | Get provider (full api_key) |
| POST | `/api/providers` | Add provider |
| PUT | `/api/providers/{id}` | Update provider |
| DELETE | `/api/providers/{id}` | Delete provider |
| POST | `/api/providers/{id}/test` | Test provider connection |
| GET | `/api/ai-configs` | List AI config presets |
| POST | `/api/ai-configs` | Create AI config preset |
| PUT | `/api/ai-configs/{id}` | Update AI config preset |
| POST | `/api/ai-configs/{id}/set-default` | Set as default config |
| DELETE | `/api/ai-configs/{id}` | Delete AI config preset |
| GET | `/api/logs/ai?page=&limit=` | AI processing logs |
| GET | `/api/webhooks` | List webhook endpoints |
| POST | `/api/webhooks` | Add webhook |
| PUT | `/api/webhooks/{id}` | Update webhook (partial) |
| POST | `/api/webhooks/{id}/toggle` | Toggle webhook |
| DELETE | `/api/webhooks/{id}` | Delete webhook |
| POST | `/api/webhooks/{id}/test` | Test webhook with mock article |
| GET | `/api/logs/webhooks?page=&limit=` | Webhook delivery logs |
| GET | `/api/logs/crawl?page=&source=&domain=&errors_only=&http_status=&since=` | Browse crawl logs (filterable) |
| GET | `/api/logs/crawl/sources` | Per-source summary (success rate, latency) |
| GET | `/api/logs/crawl/sources/{id}` | Single source crawl history |
| GET | `/api/logs/crawl/domains?since=` | Per-domain stats (IP-ban detection) |
| GET | `/api/logs/crawl/errors?since=` | Error breakdown by type |
| GET | `/api/logs/crawl/timeline?hours=` | Hourly crawl performance |
| GET | `/api/logs/system?event_type=&status=&since=` | Scheduler & system event logs |
| GET | `/api/logs/system/summary` | Per event_type aggregated stats |
| GET | `/api/logs/scheduler/status` | Current APScheduler job states |
| GET | `/api/logs/api?method=&path=&status_code=&errors_only=&since=` | API request logs |
| GET | `/api/logs/api/summary` | Per-endpoint stats (latency, error rate) |
| GET | `/api/telegram` | List Telegram channels |
| POST | `/api/telegram` | Add Telegram channel |
| PUT | `/api/telegram/{id}` | Update Telegram channel (partial) |
| POST | `/api/telegram/{id}/toggle` | Toggle Telegram channel |
| DELETE | `/api/telegram/{id}` | Delete Telegram channel |
| POST | `/api/telegram/{id}/test` | Send test message to channel |
| POST | `/api/payload/validate` | Validate Jinja2 template + preview |
| GET | `/api/payload/fields` | List available payload fields |
| POST | `/api/payload/preview` | Preview payload output for any mode |
| GET | `/api/filter-options` | List categories + sources for autocomplete |
| GET | `/api/intelligence/trends?limit=` | Trending topics and named entities |
| GET | `/api/intelligence/stories?category=&limit=` | Clustered story narratives |
| GET | `/api/intelligence/stories/{id}` | Story detail with article list |
| GET | `/api/intelligence/newsletter` | Newsletter availability + metadata |
| GET | `/api/intelligence/newsletter/view` | Newsletter HTML content |
| POST | `/api/intelligence/newsletter/generate?language=` | Trigger newsletter generation |
| GET | `/api/intelligence/debates?limit=` | Recent AI-detected debates |
| GET | `/api/rag/status` | Weaviate vector store status |
| GET | `/api/rag/search?q=&limit=&category=` | Semantic + keyword hybrid search |
| POST | `/api/rag/ask` | Full RAG: Q&A with LLM-generated answer |
| GET | `/api/schedules` | List all webhook schedules |
| GET | `/api/schedules/{id}` | Get single schedule |
| POST | `/api/schedules` | Create webhook schedule |
| PUT | `/api/schedules/{id}` | Update schedule (partial) |
| POST | `/api/schedules/{id}/toggle` | Toggle schedule enabled/disabled |
| DELETE | `/api/schedules/{id}` | Delete schedule |
| POST | `/api/schedules/{id}/trigger` | Manually trigger schedule execution |
| GET | `/api/channels` | List content channels |
| GET | `/api/channels/{id}` | Get single channel |
| POST | `/api/channels` | Create content channel |
| PUT | `/api/channels/{id}` | Update channel (partial) |
| POST | `/api/channels/{id}/toggle` | Toggle channel enabled/disabled |
| DELETE | `/api/channels/{id}` | Delete channel |
| POST | `/api/channels/{id}/regenerate-key` | Regenerate channel API key |
| GET | `/api/channels/{id}/clone-data` | Clone channel config (for duplication) |
| GET | `/api/channels/{id}/feed` | Pull articles (requires X-API-Key, X-Client-ID) |
| GET | `/api/channels/{id}/next` | Pull next single article (requires X-API-Key, X-Client-ID) |
| POST | `/api/channels/{id}/ack` | Acknowledge articles, advance cursor (requires X-API-Key, X-Client-ID) |
| POST | `/api/channels/{id}/reset-cursor` | Reset cursor to start (requires X-API-Key, X-Client-ID) |
| GET | `/api/channels/{id}/stats` | Get pull stats (requires X-Client-ID) |
