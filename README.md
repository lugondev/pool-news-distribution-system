# News Aggregator

Automated multilingual news aggregation pipeline with AI-powered summaries and a real-time dashboard.

**Pipeline:** RSS feeds → SimHash dedup → Redis → AI summarization → Webhooks

## Features

- **Multi-source RSS crawling** — 10+ feeds across English, Vietnamese, Japanese, and Korean
- **SimHash deduplication** — filters near-duplicate articles using Hamming distance
- **AI summaries** — bilingual (Vietnamese + English) summaries via OpenAI-compatible API
- **Real-time dashboard** — HTMX-powered UI with auto-refresh, source/category management
- **Webhook dispatch** — pushes processed articles to external endpoints with retry
- **JSON API** — `/api/articles`, `/api/stats`, `/api/sources`, `/api/categories`

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
# Edit .env with your API keys
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | API key for OpenAI-compatible endpoint | — |
| `OPENAI_BASE_URL` | API base URL | `https://api.openai.com/v1` |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |
| `SQLITE_PATH` | Path to SQLite stats DB | `./data/stats.db` |

### Run

```bash
python main.py
```

Dashboard available at [http://localhost:8000](http://localhost:8000).

## Architecture

```
┌─────────────┐    ┌──────────┐    ┌───────┐    ┌──────────┐    ┌──────────┐
│  RSS Feeds  │───▶│  Parser  │───▶│ Dedup │───▶│  Redis   │───▶│ AI Batch │
│  (11 feeds) │    │ + detect │    │SimHash│    │ 24h TTL  │    │ Rewriter │
└─────────────┘    └──────────┘    └───────┘    └───────┘──┘    └────┬─────┘
                                                     │               │
                                                     ▼               ▼
                                                ┌─────────┐    ┌──────────┐
                                                │Dashboard │    │ Webhooks │
                                                │  HTMX    │    │  Retry   │
                                                └─────────┘    └──────────┘
```

**Scheduler jobs** (APScheduler, async):

| Job | Interval | Description |
|-----|----------|-------------|
| Crawl | 10 min | Fetch all enabled RSS sources concurrently (semaphore=20) |
| AI Rewrite | 5 min | Process up to 5 pending articles, generate summaries, dispatch webhooks |

**Storage:**

- **Redis** — hot store with 24h TTL. Articles as hashes, sorted set for feed ordering, set for dedup fingerprints.
- **SQLite** — analytics logs (`crawl_logs`, `ai_logs`, `webhook_logs`)

## Project Structure

```
├── main.py                  # Entry point, lifespan, dependency wiring
├── scheduler.py             # APScheduler job definitions
├── config/
│   ├── settings.yaml        # Tunable parameters (intervals, model, webhooks)
│   └── sources.yaml         # RSS source definitions
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

## License

Private project.
