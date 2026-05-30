# News Aggregator

Automated multilingual news aggregation pipeline with AI-powered summaries and a real-time dashboard.

**Pipeline:** RSS feeds → SimHash dedup → Redis → AI summarization → Webhooks

## Features

- **Multi-source RSS crawling** — hundreds of feeds across English, Vietnamese, Japanese, and Korean, with per-domain rate limiting and anti-ban measures
- **SimHash deduplication** — filters near-duplicate articles using Hamming distance
- **AI processing** — one-to-one rewrites, multi-article topic synthesis, and 4-agent debate, via any OpenAI-compatible API
- **Content channels** — pull-based API for bots/publishers with per-client cursors, on-demand AI, and platform presets (Twitter/Facebook/blog/Telegram)
- **Dispatch** — webhooks (with retry), Telegram, and cron-based scheduled delivery
- **Long-form & social** — social-article generator (2000–3000 words) and persona-driven social posts
- **Real-time dashboard** — HTMX-powered UI with auto-refresh, source/category/webhook management
- **JSON API** — articles, stats, sources, categories, channels, logs, and crawl tracing

## Quick Start

### Prerequisites

- Python 3.12+
- Redis server running

### Setup

```bash
# Clone and install
git clone <repo-url> && cd news-aggregator
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env (Redis URL, ports, optional backup/Supabase settings)

# Configure app data from the bundled examples
cp config/settings.yaml.example   config/settings.yaml
cp config/sources.yaml.example    config/sources.yaml
cp config/social_agents.yaml.example config/social_agents.yaml
cp config/sim_personas.yaml.example  config/sim_personas.yaml
```

> **AI credentials are NOT environment variables.** API key, base URL, and
> model are configured in the dashboard (Settings → AI providers) and stored in
> `config/settings.yaml`. Start the app first, then add a provider in the UI.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |
| `SQLITE_PATH` | Path to SQLite stats DB | `./data/stats.db` |
| `APP_PORT` | Dashboard port | `8000` |
| `CORS_ALLOW_ORIGINS` | Comma-separated origins for a split frontend | _(empty)_ |
| `CONFIG_BACKEND` | Config storage: `yaml` (default) or `db` (Supabase) | `yaml` |

See `.env.example` for the full list (Litestream backup, Supabase, Weaviate).

### Run

```bash
python main.py
```

Dashboard available at [http://localhost:8000](http://localhost:8000).

## Architecture

```
┌─────────────┐    ┌──────────┐    ┌───────┐    ┌──────────┐    ┌──────────┐
│  RSS Feeds  │───▶│  Parser  │───▶│ Dedup │───▶│  Redis   │───▶│ AI Batch │
│  (many)     │    │ + detect │    │SimHash│    │ 24h TTL  │    │ Rewriter │
└─────────────┘    └──────────┘    └───────┘    └───────┘──┘    └────┬─────┘
                                                     │               │
                                                     ▼               ▼
                                                ┌─────────┐    ┌──────────┐
                                                │Dashboard │    │ Webhooks │
                                                │  HTMX    │    │  Retry   │
                                                └─────────┘    └──────────┘
```

**Scheduler jobs** (APScheduler, async — defaults, all tunable in `settings.yaml`):

| Job | Interval | Description |
|-----|----------|-------------|
| Crawl | 3 min | Fetch enabled RSS sources in staggered groups, per-domain rate-limited |
| AI Rewrite | 2 min | Process pending articles → summaries → dispatch webhooks/Telegram |
| Topic synthesis | 10 min | Group by category → AI synthesizes multi-angle summaries (optional) |
| Debate | 30 min | 4-agent debate (optimist/pessimist/analyst/skeptic) on big stories (optional) |
| Scheduled webhook | 1 min | Execute cron-based webhook schedules from SQLite |
| Social article | 6 h | Generate long-form articles with image prompts (optional) |
| Log cleanup | 5 h | Trim log tables older than 5h once they exceed 200 rows |

**Storage:**

- **Redis** — hot store with 24h TTL. Articles as hashes, sorted set for feed ordering, set for dedup fingerprints.
- **SQLite** — analytics logs (`crawl_logs`, `ai_logs`, `webhook_logs`)

## Project Structure

```
├── main.py                  # Entry point, lifespan, dependency wiring
├── jobs/
│   └── scheduler.py         # APScheduler job definitions
├── config/
│   ├── *.yaml.example       # Bundled samples — copy to *.yaml on first run
│   ├── settings.yaml        # Tunable parameters (gitignored; from example)
│   └── sources.yaml         # RSS source definitions (gitignored; from example)
├── crawler/
│   ├── fetcher.py           # Async concurrent RSS fetching
│   ├── rss_parser.py        # RSS parsing + language detection
│   └── dedup.py             # SimHash deduplication
├── storage/
│   ├── redis_store.py       # Redis read/write operations
│   └── sqlite_stats.py      # SQLite logging for analytics
├── ai/
│   └── rewriter.py          # AI summarization (OpenAI-compatible)
├── webhook/
│   └── dispatcher.py        # HTTP POST dispatch with retry
├── dashboard/
│   ├── app.py               # FastAPI routes + HTMX partials
│   └── templates/           # Jinja2 templates
└── data/                    # SQLite DB (auto-created)
```

## Configuration

### RSS Sources

Edit `config/sources.yaml` to add/remove feeds:

```yaml
sources:
  - id: techcrunch
    name: TechCrunch
    url: https://techcrunch.com/feed/
    type: rss
    lang: en
    category: tech
    enabled: true
```

Sources can also be managed from the dashboard UI at `/sources`.

### Settings

All tunable parameters live in `config/settings.yaml`: crawl intervals, batch sizes, AI model, webhook URLs, timeouts, and categories.

## Inspecting Data

```bash
# Recent crawl logs
sqlite3 data/stats.db "SELECT * FROM crawl_logs ORDER BY started_at DESC LIMIT 10;"

# AI processing logs
sqlite3 data/stats.db "SELECT * FROM ai_logs ORDER BY created_at DESC LIMIT 10;"

# Redis: latest articles
redis-cli ZREVRANGE news:feed 0 9 WITHSCORES

# Redis: article details
redis-cli HGETALL news:{article_id}

# Redis: dedup fingerprint count
redis-cli SCARD news:dedup:simhashes
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Web framework | FastAPI + Jinja2 + HTMX |
| Scheduler | APScheduler (async) |
| RSS parsing | feedparser + BeautifulSoup |
| Deduplication | SimHash (64-bit, Hamming distance) |
| Hot storage | Redis (async) |
| Analytics DB | SQLite (aiosqlite) |
| AI | OpenAI-compatible API (default: Gemini 2.5 Flash via OpenRouter) |
| HTTP client | httpx (HTTP/2) |
| Retry | tenacity (exponential backoff) |
| Language detection | langdetect |

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for
development setup and guidelines, and [SECURITY.md](SECURITY.md) for reporting
vulnerabilities.

## License

Released under the [MIT License](LICENSE).
