# Content Channels

Pull-based article delivery API. External services (bots, websites, mobile apps) poll channels on their own schedule to fetch news — alternative to push-based webhooks.

---

## Table of Contents

**Tutorial**
1. [Quick Start](#1-quick-start)
2. [Choose the Right Endpoint](#2-choose-the-right-endpoint)
3. [Filter by Category, Source, Type](#3-filter-by-category-source-type)
4. [Enable AI Processing](#4-enable-ai-processing)
5. [Multi-Client Setup](#5-multi-client-setup)
6. [Style & Output Format](#6-style--output-format)

**Reference**
7. [Endpoint Reference](#7-endpoint-reference)
8. [Channel Config Fields](#8-channel-config-fields)
9. [Global Channels Config](#9-global-channels-config)
10. [Auth Flow](#10-auth-flow)
11. [AI Modes](#11-ai-modes)
12. [Output Formats & Platforms](#12-output-formats--platforms)
13. [Caching Strategy](#13-caching-strategy)
14. [Redis Keys](#14-redis-keys)
15. [Logging & Monitoring](#15-logging--monitoring)
16. [Troubleshooting](#16-troubleshooting)

---

# Tutorial

## 1. Quick Start

### Step 1 — Create a channel

Via UI: Dashboard → **Channels** → New.

Via API (requires manager login):
```bash
curl -X POST http://localhost:8000/api/channels \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-website",
    "name": "My Website",
    "enabled": true,
    "require_api_key": true,
    "ai_mode": "off"
  }'
```

Response includes the auto-generated API key:
```json
{
  "ok": true,
  "channel": { "id": "my-website", ... },
  "api_key": "GZ7p3...long-token..."
}
```

> ⚠️ Save the `api_key` immediately — it is only returned at creation. Use `POST /api/channels/{id}/regenerate-key` to rotate.

### Step 2 — Fetch news

```bash
curl -H "X-API-Key: GZ7p3..." \
     "http://localhost:8000/api/channels/my-website/news?page=1&limit=20"
```

That's it — your website now has authenticated access to the feed.

---

## 2. Choose the Right Endpoint

Three consumer endpoints serve different use cases:

| Endpoint | Use Case | Pagination | AI | Tracking |
|---|---|---|---|---|
| `/news` | Browse / search / display feed on a website | Page-based | None | Stateless |
| `/feed` | Bot pulls new items since last check | Cursor-based | Optional | Per-client cursor + delivered set |
| `/next` | Bot wants ONE article to post right now | N/A | Optional | Per-client delivered set |

**Pick `/news` when:** you want behavior like `/api/news` (browse, filter, paginate) but with API-key auth instead of session login.

**Pick `/feed` when:** you need exactly-once delivery and don't want to see the same article twice across polls.

**Pick `/next` when:** you want the server to pick one article (e.g. a Twitter bot posts every N minutes).

---

## 3. Filter by Category, Source, Type

Channels have **filter config** that defines their scope. Once set, it applies to all consumer endpoints.

### Whitelist categories
```bash
curl -X PUT http://localhost:8000/api/channels/my-website \
  -H "Content-Type: application/json" \
  -d '{
    "filter_categories_mode": "include",
    "filter_categories": ["technology", "finance"]
  }'
```

Now `/news`, `/feed`, `/next` only return tech and finance articles.

### Blacklist sources
```json
{
  "filter_sources_mode": "exclude",
  "filter_sources": ["spam-rss-feed"]
}
```

### Synthetic articles only
```json
{
  "filter_article_types_mode": "include",
  "filter_article_types": ["synthetic"]
}
```

### Guard rail for `/news`

`/news` accepts `?category=...` and `?source=...` as additional narrowing — but the channel filter is always enforced. If channel allows `[tech, finance]` and you call `?category=sport`, you get empty results.

---

## 4. Enable AI Processing

AI runs on `/feed` and `/next` only — never on `/news`.

Set `ai_mode` on the channel:

| `ai_mode` | Behavior |
|---|---|
| `off` | Raw articles |
| `rewrite` | One article → AI rewrite (+ optional style transform), 1 article in / 1 out |
| `synthetic` | 3–10 articles → AI synthesizes 1 multi-perspective summary |
| `debate` | 3–10 articles → 4-agent debate (optimist, pessimist, analyst, skeptic) |

```bash
curl -X PUT http://localhost:8000/api/channels/my-website \
  -d '{"ai_mode": "rewrite", "target_language": "vi"}'
```

> AI calls are cached for 1 hour (configurable). See [Caching Strategy](#13-caching-strategy).

### Client-provided AI credentials

To avoid burning server quota, clients can supply their own API key:

```json
{ "ai_source": "client" }
```

Then on each request:
```bash
curl -H "X-API-Key: ..." \
     -H "X-AI-API-Key: sk-..." \
     -H "X-AI-Base-URL: https://api.openai.com/v1" \
     -H "X-AI-Model: gpt-4o-mini" \
     "http://localhost:8000/api/channels/my-website/feed"
```

---

## 5. Multi-Client Setup

One channel can serve many clients. Each gets independent cursor, delivered set, and stats — distinguished by the `X-Client-ID` header.

```bash
# Bot A
curl -H "X-API-Key: ..." -H "X-Client-ID: telegram-bot-1" \
     "http://localhost:8000/api/channels/news-feed/feed"

# Bot B (different cursor, never sees Bot A's delivered items)
curl -H "X-API-Key: ..." -H "X-Client-ID: discord-bot-1" \
     "http://localhost:8000/api/channels/news-feed/feed"
```

`X-Client-ID` is **required** for `/feed`, `/next`, `/ack`, `/reset-cursor` and optional (for log attribution) on `/news` and `/stats`.

### Inspect per-client state
```bash
curl -H "X-Client-ID: telegram-bot-1" \
     "http://localhost:8000/api/channels/news-feed/stats"
```

---

## 6. Style & Output Format

For `/feed` and `/next` with `ai_mode != off`, two axes control the output:

- **`platform`** — preset character limits and tone (`twitter`, `facebook`, `blog`, `telegram`, `custom`)
- **`output_format`** — structural template (`summary`, `thread`, `breaking`, `listicle`, `hot_take`, `deep_dive`, `quote_highlight`, `carousel`)

Example: Twitter thread with hot-take tone:
```json
{
  "ai_mode": "rewrite",
  "platform": "twitter",
  "output_format": "hot_take",
  "style_source": "preset"
}
```

For custom styling:
```json
{
  "style_source": "custom",
  "style": {
    "max_length": 500,
    "tone": "playful",
    "include_hashtags": true,
    "include_link": true,
    "custom_prompt": "End each post with a thought-provoking question."
  }
}
```

For per-request override (client provides prompt):
```json
{ "style_source": "client" }
```
```bash
curl "...?style_prompt=Write%20in%20pirate%20speak"
```

---

# Reference

## 7. Endpoint Reference

All endpoints under `/api`.

### Admin / CRUD (requires manager session login)

| Method | Path | Description |
|---|---|---|
| `GET` | `/channels` | List all channels (API keys masked) — **public** |
| `POST` | `/channels` | Create a channel |
| `PUT` | `/channels/{id}` | Update a channel (partial) |
| `DELETE` | `/channels/{id}` | Delete a channel |
| `POST` | `/channels/{id}/toggle` | Toggle enabled on/off |
| `POST` | `/channels/{id}/regenerate-key` | Rotate API key |
| `GET` | `/channels/{id}/clone-data` | Get config for duplication (no `id`, no `api_key`) |
| `GET` | `/channels/{id}/logs` | View per-channel request logs |

### Consumer (requires channel API key)

| Method | Path | Description |
|---|---|---|
| `GET` | `/channels/{id}/news` | Browse articles — page-based, raw, no AI |
| `GET` | `/channels/{id}/feed` | Pull articles since cursor — supports AI processing |
| `GET` | `/channels/{id}/next` | Pick one article right now — supports AI processing |
| `POST` | `/channels/{id}/ack` | Advance cursor to given ISO timestamp |
| `POST` | `/channels/{id}/reset-cursor` | Reset cursor to 0 (will re-deliver) |
| `GET` | `/channels/{id}/stats` | Pull stats (per-client or aggregated) |

### `/news` query params
| Param | Type | Default | Notes |
|---|---|---|---|
| `page` | int | 1 | |
| `limit` | int | 20 | max 100 |
| `source` | str | — | filter by source_id |
| `category` | str | — | filter by category |
| `lang` | str | — | filter by language code |
| `article_type` | str | — | `original` \| `synthetic` |

### `/feed` query params
| Param | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | 20 | max 100 |
| `since` | ISO 8601 | (stored cursor) | timestamp override |
| `auto_ack` | bool | false | auto-advance cursor after fetch |
| `style_prompt` | str | — | only when `style_source=client` |

### Required / optional headers (consumer endpoints)
| Header | When |
|---|---|
| `X-API-Key` | Always (unless `require_api_key=false`) |
| `X-Client-ID` | Required: `/feed`, `/next`, `/ack`, `/reset-cursor`. Optional: `/news`, `/stats` |
| `X-AI-API-Key` | When `ai_source=client` |
| `X-AI-Base-URL` | When `ai_source=client` |
| `X-AI-Model` | When `ai_source=client` |

---

## 8. Channel Config Fields

Stored in `settings.yaml → content_channels[]`.

### Identity
| Field | Type | Default | Description |
|---|---|---|---|
| `id` | str | required | Unique slug (e.g. `twitter-bot`) |
| `name` | str | required | Display name |
| `enabled` | bool | `true` | If false → all consumer endpoints return 403 |
| `api_key` | str | auto | Per-channel API key (generated at create) |
| `require_api_key` | bool | `true` | If false → endpoint is public (no auth) |
| `created_at` | ISO 8601 | auto | |

### Filters (guard rail for all consumer endpoints)
| Field | Type | Default | Description |
|---|---|---|---|
| `filter_categories_mode` | str | `all` | `all` \| `include` \| `exclude` |
| `filter_categories` | list[str] | `[]` | Category IDs |
| `filter_sources_mode` | str | `all` | `all` \| `include` \| `exclude` |
| `filter_sources` | list[str] | `[]` | Source IDs |
| `filter_article_types_mode` | str | `all` | `all` \| `include` \| `exclude` |
| `filter_article_types` | list[str] | `[]` | `original` \| `synthetic` |

### AI processing
| Field | Type | Default | Description |
|---|---|---|---|
| `ai_mode` | str | `off` | `off` \| `rewrite` \| `synthetic` \| `debate` |
| `ai_config_id` | str | `""` | AI provider ID (empty = global) |
| `ai_source` | str | `system` | `system` (server creds) \| `client` (X-AI-* headers) |
| `target_language` | str | `""` | ISO 639-1 code, e.g. `vi`, `ja` |

### Payload formatting
| Field | Type | Default | Description |
|---|---|---|---|
| `payload_mode` | str | `full` | `full` \| `fields` \| `template` |
| `payload_fields` | list[str] | `[]` | Whitelist (when `mode=fields`) |
| `payload_template` | str | `""` | Jinja2 template (when `mode=template`) |
| `max_items_per_fetch` | int | `20` | 1–100; caps `/feed` even if client asks more |

### Style transform (used when `ai_mode != off`)
| Field | Type | Default | Description |
|---|---|---|---|
| `platform` | str | `custom` | `twitter` \| `facebook` \| `blog` \| `telegram` \| `custom` |
| `content_mode` | str | `rewrite` | Display label only — doesn't drive processing |
| `output_format` | str | `summary` | `summary` \| `thread` \| `breaking` \| `listicle` \| `hot_take` \| `deep_dive` \| `quote_highlight` \| `carousel` |
| `style_source` | str | `preset` | `preset` \| `custom` \| `client` |
| `style` | dict | `{}` | `{max_length, tone, include_hashtags, include_link, custom_prompt}` |

---

## 9. Global Channels Config

Stored in `settings.yaml → channels_config{}`. Applies to all channels unless overridden per-channel.

| Field | Type | Default | Description |
|---|---|---|---|
| `global_api_key` | str | `""` | Shared key valid across all channels. Use for bulk auth. |
| `ai_timeout_seconds` | int | `60` | Hard timeout for AI calls in `/feed` and `/next` |

Manage via Dashboard → **Settings → Channels tab**.

---

## 10. Auth Flow

Consumer endpoints check `X-API-Key` against (in this order):

1. **Per-channel `api_key`** → marked as `auth_method=per_channel_key`
2. **Global `global_api_key`** → marked as `auth_method=global_key`
3. **`require_api_key=false`** → skip auth, marked as `auth_method=public`

If none match → `401 Invalid API key`.

If `enabled=false` → `403 Channel is disabled` (even with valid key).

Comparison uses `secrets.compare_digest` to prevent timing attacks.

---

## 11. AI Modes

| Mode | Min Articles | Output | Caching Key |
|---|---|---|---|
| `off` | 1 | Raw article(s) | None |
| `rewrite` | 1 | One AI-rewritten article (1:1) | `channel:{id}:rewrite:{article_id}:{lang}[:{format}]` |
| `synthetic` | 3 | One synthesized article from a batch | `channel:{id}:synthetic:{category}:{batch_hash}` |
| `debate` | 3 | One debate result (4 perspectives merged) | `channel:{id}:debate:{category}:{batch_hash}` |

When `ai_mode` is `synthetic` or `debate` and the channel has < 3 eligible articles, `/feed` and `/next` return `204 No Content`.

`/news` ignores `ai_mode` entirely — always returns raw.

**⚡ Optimization:** `rewrite` mode merges AI rewrite + style transform into a single API call (previously two).

---

## 12. Output Formats & Platforms

### Platform presets

| Platform | Max length | Tone |
|---|---|---|
| `twitter` | 280 | punchy, hook-driven |
| `facebook` | 2000 | engaging, conversational |
| `blog` | 5000 | formal, structured |
| `telegram` | 4096 | concise |
| `custom` | per-channel | per-channel |

### Output formats

| Format | Structure |
|---|---|
| `summary` | Compact paragraph |
| `thread` | Numbered series (Twitter-style) |
| `breaking` | Headline + 2–3 sentence brief |
| `listicle` | Bulleted key points |
| `hot_take` | Opinion-led with stance |
| `deep_dive` | Long analysis with sections |
| `quote_highlight` | Pull-quote + context |
| `carousel` | Slide-by-slide breakdown |

---

## 13. Caching Strategy

AI results are cached in Redis (1-hour TTL) to save API quota.

| Cache Key Pattern | Scope |
|---|---|
| `channel:{id}:rewrite:{article_id}:{lang}:{format}` | Per-article rewrite + style |
| `channel:{id}:rewrite:{article_id}:{lang}` | Backward-compat (no style) |
| `channel:{id}:synthetic:{category}:{batch_hash}` | Per-batch synthesis |
| `channel:{id}:debate:{category}:{batch_hash}` | Per-batch debate |

`batch_hash` is computed from sorted article IDs — same batch = same cache hit.

Cache TTL is not configurable per-channel. To bypass cache: delete the Redis key manually.

---

## 14. Redis Keys

| Key | Type | Description |
|---|---|---|
| `channel:{id}:client:{client_id}:cursor` | String | Float timestamp of last ack'd article |
| `channel:{id}:client:{client_id}:delivered` | Set | Article IDs already sent to this client (24h TTL) |
| `channel:{id}:client:{client_id}:stats` | Hash | `{pull_count, articles_delivered, last_pull_at}` |
| `channel:{id}:rewrite:*` | String | Cached AI rewrite output (1h TTL) |
| `channel:{id}:synthetic:*` | String | Cached synthesis output (1h TTL) |
| `channel:{id}:debate:*` | String | Cached debate output (1h TTL) |

### Inspect manually
```bash
# All state keys for a client
redis-cli KEYS "channel:my-website:client:bot-1:*"

# Current cursor (Unix timestamp)
redis-cli GET "channel:my-website:client:bot-1:cursor"

# Delivered IDs
redis-cli SMEMBERS "channel:my-website:client:bot-1:delivered"

# Stats
redis-cli HGETALL "channel:my-website:client:bot-1:stats"
```

---

## 15. Logging & Monitoring

Every consumer endpoint request is logged to SQLite `channel_logs`.

### View in UI
Dashboard → `/logs` → **Channel API** tab. Expand any row to see the full response body (truncated to 10 KB).

### Query directly
```bash
sqlite3 data/stats.db "SELECT * FROM channel_logs ORDER BY requested_at DESC LIMIT 20;"

# Per-client breakdown
sqlite3 data/stats.db "SELECT client_id, endpoint, COUNT(*) FROM channel_logs GROUP BY client_id, endpoint;"

# Auth method usage
sqlite3 data/stats.db "SELECT auth_method, COUNT(*) FROM channel_logs GROUP BY auth_method;"

# Average duration per endpoint
sqlite3 data/stats.db "SELECT endpoint, AVG(duration_ms) FROM channel_logs GROUP BY endpoint;"

# Recent errors
sqlite3 data/stats.db "SELECT endpoint, status_code, error_msg, requested_at FROM channel_logs WHERE status_code >= 400 ORDER BY requested_at DESC LIMIT 20;"
```

### Logged fields
`channel_id`, `client_id`, `endpoint`, `method`, `status_code`, `auth_method` (`per_channel_key` / `global_key` / `public`), `items_count`, `requested_at`, `duration_ms`, `error_msg`, `response_body` (JSON, ≤10 KB).

---

## 16. Troubleshooting

### `401 Invalid API key`
- Wrong key, or `require_api_key=true` and no key sent.
- Verify: `GET /api/channels` (public) → check `api_key_preview` matches the first 8 chars of your key.
- Or use the global key from Settings → Channels.

### `403 Channel is disabled`
- `enabled=false`. Toggle via `POST /channels/{id}/toggle` or UI.

### `400 X-Client-ID header is required`
- Required on `/feed`, `/next`, `/ack`, `/reset-cursor`. Pick any stable string per client (e.g. `prod-website`).

### `204 No Content`
- `synthetic`/`debate` mode but fewer than 3 articles passed the filter.
- Loosen filters or wait for more articles.

### `504 Gateway Timeout`
- AI call exceeded `channels_config.ai_timeout_seconds` (default 60s).
- Increase timeout in Settings → Channels, or pick a faster model.

### `/feed` returns same articles repeatedly
- You didn't call `/ack` or set `auto_ack=true`. Without ack, cursor never advances.
- Or: cursor was reset via `POST /reset-cursor`.

### `/feed` returns empty even though articles exist
- Channel filter excludes everything. Check `filter_categories`, `filter_sources`, `filter_article_types`.
- Or: all eligible articles are in the per-client `delivered` set. Wait for new articles or reset cursor.

### Same article appears in different channels
- Expected. Channels share the underlying `news:feed` — the `delivered` set is per-channel-per-client.

### AI response looks stale
- Cached for 1 hour. Delete the cache key to force refresh:
  ```bash
  redis-cli DEL "channel:my-website:rewrite:abc123:vi:summary"
  ```

### Client lost cursor / wants to re-fetch everything
- `POST /channels/{id}/reset-cursor` with `X-Client-ID`. This clears both cursor and delivered set.

### CORS error in browser
- Set env `CORS_ALLOW_ORIGINS=https://your-site.com` before starting the app.
- See `dashboard/app.py:120`.

---

## See Also

- `CLAUDE.md` — system overview and architecture
- `dashboard/routes/channels_api.py` — endpoint source code
- `ai/channel_processor.py` — AI processing pipeline
- `webhook/filters.py` — `passes_filter()` logic
- `/docs` (Swagger UI) — auto-generated interactive API explorer
