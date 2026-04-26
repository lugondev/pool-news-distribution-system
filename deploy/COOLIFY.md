# Deploy Guide — Coolify (backend) + Vercel (frontend)

Split architecture:
- **Backend** (FastAPI + APScheduler + Redis + SQLite) → Coolify
- **Frontend** (Next.js 14 in `ai-news-next-client/`) → Vercel
- **Backups** (SQLite + Redis) → Cloudflare R2

```
┌─────────────────────┐         HTTPS         ┌──────────────────────┐
│ Vercel              │◄────── /api/* ───────►│ Coolify (any VPS)    │
│ Next.js client      │                       │  app + redis +       │
│ news.example.com    │                       │  litestream + backup │
└─────────────────────┘                       │  api.example.com     │
                                              └──────────┬───────────┘
                                                         │
                                                         ▼
                                              ┌──────────────────────┐
                                              │ Cloudflare R2        │
                                              │  sqlite/* (live)     │
                                              │  redis/* (hourly)    │
                                              └──────────────────────┘
```

## Step 1 — Prepare R2 (5 min)

1. Cloudflare Dashboard → R2 → Create bucket `news-aggregator-backups`.
2. Manage R2 API Tokens → Create API token with **Object Read & Write** for that bucket.
3. Save the access key + secret + endpoint URL.

## Step 2 — Deploy backend on Coolify (10 min)

1. Coolify → New Resource → **Docker Compose** → paste the repo URL (or upload `docker-compose.yml` + `Dockerfile`).
2. Domain: assign `api.your-domain.com` (Coolify generates Let's Encrypt cert automatically).
3. **Environment Variables** (Settings → Environment):
   ```
   # Backups (R2)
   LITESTREAM_BUCKET=news-aggregator-backups
   LITESTREAM_ENDPOINT=https://<r2-account-id>.r2.cloudflarestorage.com
   LITESTREAM_ACCESS_KEY_ID=<r2 access key>
   LITESTREAM_SECRET_ACCESS_KEY=<r2 secret>

   # CORS — allow your Vercel frontend
   CORS_ALLOW_ORIGINS=https://news.your-domain.com,https://*.vercel.app
   ```
4. Deploy. Wait for healthcheck (~60s).
5. Verify: `curl https://api.your-domain.com/api/health` → `{"status":"ok","redis":true}`
6. Open Settings UI at `https://api.your-domain.com/settings` to configure AI provider (api_key, model). Saved to the `app-config` volume.

**RAM required:** ~250 MB (without RAG). Hetzner CX11 (€4/mo) or DigitalOcean $4 droplet is enough.

## Step 3 — Deploy frontend on Vercel (5 min)

1. Vercel → Import Git Repository → select this repo.
2. **Root Directory**: `ai-news-next-client`
3. **Framework**: Next.js (auto-detected)
4. **Environment Variables**:
   ```
   API_URL=https://api.your-domain.com
   NEXT_PUBLIC_API_URL=https://api.your-domain.com
   # Set this AFTER you enable Basic Auth on Coolify (see Security section below).
   # Server-only — must NOT have NEXT_PUBLIC_ prefix, otherwise creds leak to browser.
   API_BASIC_AUTH=admin:YOUR_STRONG_PASSWORD
   ```
   `API_URL` = used by Server Components (server-side fetch, no CORS).
   `NEXT_PUBLIC_API_URL` = used by client components in the browser (needs CORS, see Step 2).
5. Deploy. Vercel assigns a `*.vercel.app` URL; add a custom domain like `news.your-domain.com`.

## Step 4 — Verify end-to-end

```bash
# Backend health
curl https://api.your-domain.com/api/health

# CORS check (simulating Vercel browser request)
curl -H "Origin: https://news.your-domain.com" -I https://api.your-domain.com/api/news
# Look for: access-control-allow-origin: https://news.your-domain.com

# Frontend loads
open https://news.your-domain.com
```

## Enable RAG (optional, +300 MB RAM)

In Coolify env vars:
```
COMPOSE_PROFILES=rag
```
Or local: `docker compose --profile rag up -d`. Need ≥4 GB RAM host (CX21 or equivalent).

## Migrate to a new host (≤ 5 minutes)

Since all state replicates to R2, migration = fresh deploy:

1. On the new host, set the same env vars from Step 2.
2. (Optional) Restore Redis state — only if you care about in-flight data:
   ```
   ./scripts/restore-redis.sh
   ```
3. `docker compose up -d` — `entrypoint.sh` auto-restores SQLite from R2.
4. Update DNS `api.your-domain.com` → new host IP. Done.

If you skip the Redis restore, the app re-crawls fresh articles (24h TTL data, not critical).

## Migrate AWAY from this stack

Same Dockerfile, same env vars, same R2 backups work everywhere:

| Target | How |
|---|---|
| Fly.io | `fly launch` reads Dockerfile → `fly secrets set` → `fly volumes create app-data` |
| Railway | Connect repo → set env vars → reads Dockerfile |
| K8s | `kompose convert` → tweak volume mounts to PVCs |
| Plain VPS | `git clone && docker compose up -d` |

## Cost ballpark (USD/mo)

| Component | Cost |
|---|---|
| Hetzner CX11 (1 vCPU / 2 GB) — Coolify base | ~$5 |
| R2 (10 GB stored, free egress) | ~$0.15 |
| Vercel Hobby plan | $0 |
| **Total** | **~$5/mo** |
| + RAG (need CX21 with 4 GB RAM) | +$5 |

## Security: Basic Auth via Coolify proxy

The backend has no built-in admin auth (write endpoints like `/api/sources`, `/api/settings`, `/api/webhooks` are open). We protect the entire backend at the proxy level — **no code changes**.

This works cleanly because the Next.js client already does **all fetches server-side (RSC)**, so credentials live only in Vercel's server env vars and never touch a browser.

### Step A — Enable Basic Auth in Coolify

Coolify uses Traefik under the hood. Add the basic-auth middleware via Traefik labels on the `app` service.

1. Generate a bcrypt-hashed credential (locally):
   ```bash
   docker run --rm httpd:2.4-alpine htpasswd -nbB admin 'YOUR_STRONG_PASSWORD'
   # → admin:$2y$05$xxxxxxxxx...
   ```
   Copy the full `user:hash` line.

2. In Coolify → your service → **Settings → Labels** (or edit the compose), add:
   ```yaml
   labels:
     - "traefik.http.middlewares.api-auth.basicauth.users=admin:$$2y$$05$$xxxxxxxxx..."
     - "traefik.http.routers.api-https.middlewares=api-auth"
   ```
   **Note:** in Traefik labels you must escape every `$` as `$$` (the bcrypt hash has 3+ of them).

3. Redeploy. Test:
   ```bash
   curl -i https://api.your-domain.com/api/health
   # → HTTP/2 401  WWW-Authenticate: Basic
   curl -i -u 'admin:YOUR_STRONG_PASSWORD' https://api.your-domain.com/api/health
   # → HTTP/2 200  {"status":"ok",...}
   ```

### Step B — Wire credentials to Vercel

Add **server-only** env var in Vercel (do NOT use `NEXT_PUBLIC_` prefix):
```
API_BASIC_AUTH=admin:YOUR_STRONG_PASSWORD
```

`lib/api.ts` reads this at module load and injects `Authorization: Basic ...` into every server-side fetch automatically. Browsers never see the password.

Redeploy Vercel. Frontend works as before; backend is locked down.

### Caveats

- **Browser fetches** (`clientFetch()` in `lib/api.ts`) will fail with this setup — Basic Auth dialog pops up. Currently no caller uses `clientFetch`, so this is fine. If you add interactive client-side fetches later, proxy them through a Next.js Route Handler, or upgrade to Cloudflare Access.
- **Direct API access** (curl, Postman) needs the credentials. Useful for ops, painful for casual debugging — keep a `.env.local` with creds for `httpie`/`curl` aliases.
- **Settings UI at `api.your-domain.com/settings`** also requires auth. If you prefer the bundled Jinja UI for ops, you'll see the browser auth dialog — fine for admin use.

### When to upgrade

Move to **Cloudflare Access** (SSO/MFA, free ≤50 users) when:
- You need to grant access to teammates without sharing a password
- You want MFA / device posture checks
- Audit logs become important

It's a 10-minute setup later — Coolify proxy auth is a fine starting point.

## Backup verification (do this monthly)

```bash
# List recent SQLite snapshots
docker compose exec app litestream snapshots -config /etc/litestream.yml /app/data/stats.db

# List Redis backups
aws --endpoint-url $LITESTREAM_ENDPOINT s3 ls s3://$LITESTREAM_BUCKET/news-aggregator/redis/

# Test SQLite restore (dry run, throwaway DB)
docker compose exec app litestream restore -o /tmp/test.db -config /etc/litestream.yml /app/data/stats.db
docker compose exec app sqlite3 /tmp/test.db "SELECT COUNT(*) FROM crawl_logs;"
```

## Config storage: YAML files vs Postgres tables (`CONFIG_BACKEND`)

The app has TWO interchangeable config backends. The choice is made at boot via one env var; **117+ call sites in the codebase use the same API regardless** — they don't know (or care) which backend is active.

### The two modes

| `CONFIG_BACKEND` | App reads from | App writes to | External dep |
|---|---|---|---|
| `yaml` *(default)* | `config/*.yaml` in `app-config` volume | same files (mtime-cached) | none |
| `db` | 4 Postgres tables in Supabase | same tables (TTL-cached 30s) | Supabase reachable |

In `db` mode the entrypoint runs a fast pre-boot Supabase reachability check. If it fails, the container exits with a clear FATAL — no silent degradation.

### When to use which

| Scenario | Mode | Why |
|---|---|---|
| Single host, simple deploy | `yaml` | No external service. R2 + Litestream still backs up logs/redis. |
| Multiple Coolify hosts sharing config | `db` | One canonical state. Edit on host A → host B sees it within 30s (cache TTL). |
| Want to edit config in Supabase Studio web SQL | `db` | Direct UPDATEs land instantly (after cache expires). |
| Strict isolation per host (different toggles per env) | `yaml` | Each host's volume = its own world. |
| Testing without external services | `yaml` | Zero setup. |

### Setup — one-time per Supabase project

1. Create Supabase project + grab Pooler URL (Transaction mode, port 6543 — see "Step 2" above for password encoding).
2. Initialize tables and seed from local YAMLs (run from your dev machine):
   ```bash
   export SUPABASE_DB_URL="postgresql://postgres.<ref>:<encoded-pass>@aws-1-<region>.pooler.supabase.com:6543/postgres?sslmode=require"
   .venv/bin/python config/sync.py init        # creates 4 tables (sources, social_agents, sim_personas, settings)
   .venv/bin/python config/sync.py yaml-to-db  # seeds 367+ rows from current YAMLs
   ```
3. Add to Coolify env vars:
   ```
   CONFIG_BACKEND=db
   SUPABASE_DB_URL=<same URL as above>
   ```
4. Redeploy. Watch logs for `[entrypoint] Supabase reachable — app will read/write config from DB`.

### Failure semantics

| Situation | Behavior |
|---|---|
| `CONFIG_BACKEND` unset / `yaml` | App reads volume, no DB call ever |
| `CONFIG_BACKEND=db`, no `SUPABASE_DB_URL` | Entrypoint exits FATAL — fix env vars |
| `CONFIG_BACKEND=db`, DB unreachable on boot | Entrypoint exits FATAL — fix Supabase or switch to `yaml` |
| `CONFIG_BACKEND=db`, DB goes down AFTER boot | App keeps serving from 30s in-memory cache; cache miss raises `psycopg.OperationalError` to caller |

The strict-fail-on-boot is intentional: in `db` mode, you explicitly chose Postgres as source of truth. Falling back to stale YAML silently would be more confusing than a clear error.

### Manual sync CLI (always available, regardless of backend)

The 4-command CLI in `config/sync.py` works at any time:

```bash
.venv/bin/python config/sync.py init           # apply schema.sql (creates 4 tables)
.venv/bin/python config/sync.py status         # row count diff (yaml vs db)
.venv/bin/python config/sync.py yaml-to-db     # local YAMLs → upsert into tables
.venv/bin/python config/sync.py db-to-yaml     # tables → write local YAMLs (atomic)

# Inside a Coolify container:
docker compose exec app python config/sync.py status
```

Useful for: bootstrapping, ad-hoc backups, switching backends without losing data, debugging.

### Workflows

**Switch from YAML → DB mode (zero downtime)**
```bash
# 1. From local dev or any container, push current YAML state to DB
.venv/bin/python config/sync.py yaml-to-db
# 2. Set CONFIG_BACKEND=db in Coolify env vars
# 3. Redeploy. App now reads from DB, ignores volume YAMLs.
# (Volume YAMLs are kept as-is — easy rollback by setting CONFIG_BACKEND=yaml)
```

**Switch from DB → YAML mode (rollback / decoupling)**
```bash
# 1. Pull latest DB state into local YAMLs
.venv/bin/python config/sync.py db-to-yaml
# 2. Set CONFIG_BACKEND=yaml in Coolify (or unset)
# 3. Redeploy. App reads from volume again. SUPABASE_DB_URL becomes optional.
```

**Edit configs directly in Supabase Studio (db mode only)**
```sql
-- In Supabase → SQL Editor:
UPDATE sources SET enabled = false WHERE category = 'gaming';
-- Within 30s (cache TTL), all hosts see the change. No restart needed.
```

### Schema (4 tables)

Defined in `config/schema.sql`. Each YAML file maps to one table:

| YAML file | Table | Layout |
|---|---|---|
| `sources.yaml` | `sources` | 8 columns + `extra JSONB` for forward-compat |
| `social_agents.yaml` | `social_agents` | id+name+enabled + 3 JSONB columns (persona, platforms, source_filter) |
| `sim_personas.yaml` | `sim_personas` | composite PK `(type, name)` + JSONB `data` |
| `settings.yaml` | `settings` | one row per top-level section, `data JSONB` |

All tables have `display_order` (where order matters), `created_at`, `updated_at`. Schema changes to YAML structure never require DB migration — JSONB absorbs new fields automatically.

### Cache behavior

In `db` mode, every read hits a 30s TTL cache. So 100 requests within 30s = 1 DB query. Writes invalidate the relevant cache key immediately (UI saves are instantly visible).

To override the TTL: edit `dashboard/config_backend.py:PostgresBackend.__init__(cache_ttl=...)`. 30s matches HTMX dashboard poll cadence.

### Cost

Free Supabase tier (500 MB DB, 5 GB egress) covers this trivially:
- Current data: 4 tables, ~96 KB total
- Even with 5000+ sources: < 5 MB
- Read traffic: ~1 query/30s/cache-key/host = negligible

## What lives where

| Data | Storage | Backup |
|---|---|---|
| Articles (24h TTL hot) | Redis | RDB snapshot → R2 (hourly) |
| Logs, schedules | SQLite (`app-data` volume) | Litestream → R2 (continuous, ~10s RPO) |
| Settings, sources, agents, personas | `app-config` volume (yaml mode) **OR** 4 Postgres tables (db mode) | Backend chosen via `CONFIG_BACKEND`. Yaml mode → volume tarball. Db mode → Supabase backups (free tier 7d PITR). See "Config storage" above. |
| Vector embeddings (RAG) | Weaviate (`weaviate-data`) | None — rebuildable from articles |
