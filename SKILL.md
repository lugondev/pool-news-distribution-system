---
name: news-aggregator-ops
description: >-
  Manage, monitor, and troubleshoot the News Aggregator via its JSON API.
  Use when the user asks to check system health, inspect articles, view
  crawl/AI/webhook logs, manage RSS sources, categories, webhooks, AI
  settings, or diagnose pipeline issues.
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

### List latest articles

```bash
curl -s "$API/articles?limit=10" | jq .
```

Query params: `limit` (default 50), `offset`, `source`, `category`.

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

Query params: `event_type` (crawl_job, ai_job), `status` (ok, error, skipped), `since` (ISO datetime).

Each entry: `id`, `event_type`, `started_at`, `finished_at`, `duration_ms`, `status`, `metadata` (job-specific JSON), `error_msg`.

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
  -d '{"id":"my_hook","name":"My Hook","url":"https://example.com/hook","payload_mode":"full"}' | jq .
```

Payload mode options (applies to both webhooks and Telegram):
- `"payload_mode": "full"` — send all article fields (default)
- `"payload_mode": "fields", "payload_fields": ["title","url","ai_summary_vi"]` — only selected fields
- `"payload_mode": "template", "payload_template": "..."` — custom Jinja2 template

### Update a webhook

```bash
curl -s -X PUT $API/webhooks/{wh_id} \
  -H "Content-Type: application/json" \
  -d '{"name":"Updated Hook","payload_mode":"fields","payload_fields":["title","url","ai_summary_en","category"]}' | jq .
```

### Toggle webhook

```bash
curl -s -X POST $API/webhooks/{wh_id}/toggle | jq .
```

### Delete a webhook

```bash
curl -s -X DELETE $API/webhooks/{wh_id} | jq .
```

## Webhook Logs

```bash
curl -s "$API/logs/webhooks?page=1&limit=20" | jq .
```

Returns `logs` (article_id, webhook_url, sent_at, status_code, success, error_msg), `total`, `page`, `total_pages`.

Telegram delivery logs also appear here with `webhook_url` = `telegram:{chat_id}`.

## Telegram Channels

### List channels

```bash
curl -s $API/telegram | jq .
```

### Add a channel

```bash
curl -s -X POST $API/telegram \
  -H "Content-Type: application/json" \
  -d '{"id":"news_channel","name":"Finance News","bot_token":"123456:ABC...","chat_id":"-1001234567890","lang":"both","payload_mode":"full"}' | jq .
```

Fields: `id` (required, unique), `name`, `bot_token` (from @BotFather), `chat_id`, `lang` ("vi", "en", or "both"), `retry_attempts`, `timeout_seconds`, `payload_mode`, `payload_fields`, `payload_template`.

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

Available fields: `id`, `source_id`, `source_name`, `url`, `title`, `summary`, `content`, `lang`, `declared_lang`, `category`, `published_at`, `fetched_at`, `ai_summary_vi`, `ai_summary_en`, `ai_status`.

### Mode 3: `template` — custom Jinja2

```bash
curl -s -X PUT $API/webhooks/my_hook \
  -H "Content-Type: application/json" \
  -d '{"payload_mode":"template","payload_template":"{\"title\":\"{{ title }}\",\"link\":\"{{ url }}\",\"summary\":\"{{ ai_summary_en }}\"}"}' | jq .
```

Telegram template example (HTML):
```
<b>{{ title }}</b>\n📡 {{ source_name }}\n\n{{ ai_summary_vi }}\n\n🔗 <a href="{{ url }}">Read more</a>
```

All article fields are available as template variables. Use `{{ "{{" }} if ... {{ "}}" }}` for conditionals.

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

## Troubleshooting Workflows

### Scheduler jobs not running or crashing

1. `curl -s "$API/logs/system?event_type=crawl_job&limit=5" | jq .` — recent crawl job executions
2. `curl -s "$API/logs/system?status=error" | jq '.logs[] | {event_type,error_msg,started_at}'` — all job errors
3. `curl -s "$API/logs/system/summary" | jq .` — overall job health (success/error ratio, avg duration)
4. High `avg_duration_ms` → crawl batch is slow, reduce `sources_per_tick`
5. Repeated `status=skipped` with `reason=no sources due` → all sources are in backoff, check crawl logs

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

### Webhook failures

1. `curl -s "$API/logs/webhooks?limit=10" | jq '.logs[] | select(.success==0)'` — find failures
2. `curl -s $API/webhooks | jq .endpoints` — verify endpoint config
3. Common causes: endpoint down, timeout too low, URL incorrect

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

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | System health + Redis status |
| GET | `/api/articles?limit=&offset=&source=&category=` | List articles |
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
| GET | `/api/logs/ai?page=&limit=` | AI processing logs |
| GET | `/api/webhooks` | List webhook endpoints |
| POST | `/api/webhooks` | Add webhook |
| PUT | `/api/webhooks/{id}` | Update webhook |
| POST | `/api/webhooks/{id}/toggle` | Toggle webhook |
| DELETE | `/api/webhooks/{id}` | Delete webhook |
| GET | `/api/logs/webhooks?page=&limit=` | Webhook delivery logs |
| GET | `/api/logs/crawl?page=&source=&domain=&errors_only=&http_status=&since=` | Browse crawl logs (filterable) |
| GET | `/api/logs/crawl/sources` | Per-source summary (success rate, latency) |
| GET | `/api/logs/crawl/sources/{id}` | Single source crawl history |
| GET | `/api/logs/crawl/domains?since=` | Per-domain stats (IP-ban detection) |
| GET | `/api/logs/crawl/errors?since=` | Error breakdown by type |
| GET | `/api/logs/crawl/timeline?hours=` | Hourly crawl performance |
| GET | `/api/logs/system?event_type=&status=&since=` | Scheduler & system event logs |
| GET | `/api/logs/system/summary` | Per event_type aggregated stats |
| GET | `/api/logs/api?method=&path=&status_code=&errors_only=&since=` | API request logs |
| GET | `/api/logs/api/summary` | Per-endpoint stats (latency, error rate) |
| GET | `/api/telegram` | List Telegram channels |
| POST | `/api/telegram` | Add Telegram channel |
| PUT | `/api/telegram/{id}` | Update Telegram channel |
| POST | `/api/telegram/{id}/toggle` | Toggle Telegram channel |
| DELETE | `/api/telegram/{id}` | Delete Telegram channel |
| POST | `/api/telegram/{id}/test` | Send test message to channel |
| POST | `/api/payload/validate` | Validate Jinja2 template + preview |
| GET | `/api/payload/fields` | List available payload fields |
| POST | `/api/payload/preview` | Preview payload output for any mode |
