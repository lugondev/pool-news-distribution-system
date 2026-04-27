# Auth + RBAC — MVP Design

**Date:** 2026-04-28
**Status:** Draft (awaiting user review)
**Scope:** Minimum viable authentication + role-based access for the news-aggregator dashboard

---

## 1. Goals

- Gate the dashboard (UI + admin API) behind login. Today anyone reaching the URL has full access.
- Three roles: `superadmin`, `manager`, `creator`.
- Creator role has per-content-type toggles (config-driven, not hard-coded).
- Two auth flavors: cookie session (browser) + Personal Access Token (scripts/bots).
- Storage follows existing `CONFIG_BACKEND` switch (SQLite when `yaml`, Postgres when `db`).
- Channel consumer endpoints (`/api/channels/{id}/feed` etc.) keep their existing `X-API-Key` system — out of scope.

## 2. Non-goals (MVP)

- Audit log
- Password reset / forgot password flow (superadmin resets manually)
- "Edit own profile" UI
- 2FA, OAuth, SSO
- Manager creating users (superadmin only)
- Multi-PAT per user (1 PAT per user; rotate by regenerate)
- Email verification / password complexity rules beyond min-length

## 3. Roles & permissions

### 3.1 Roles

| Role | Allowed actions |
|---|---|
| `superadmin` | Everything: all manager perms + manage users + bootstrap |
| `manager` | All config: sources, channels, webhooks, schedules, social agents, AI providers, app settings. Read all content. Cannot create/edit users. |
| `creator` | Read-only on config. Create content per granular toggles. |

### 3.2 Creator toggles

Stored as JSONB on `users.permissions`. Five toggles for v1:

```json
{
  "can_create_social_article": true,
  "can_create_newsletter":     true,
  "can_create_debate":         false,
  "can_create_sim":            true,
  "can_run_social_agent":      false
}
```

Adding a toggle later = new key (no migration needed; missing keys default to `false`).

### 3.3 Route → permission map (concrete gating)

**`require_role("superadmin")`:**
- All `/api/users/*` and `/api/users/*/pat`
- `/setup` (only when no users exist)

**`require_role("manager")`** (superadmin auto-passes; "manager" means "≥ manager"):
- `/api/sources/*` (POST/PUT/DELETE)
- `/api/channels/*` (admin CRUD; consumer endpoints `/feed`, `/next`, `/ack` keep X-API-Key)
- `/api/webhooks/*`
- `/api/schedules/*`
- `/api/social-agents/*` (CRUD only — `/run` gated by creator toggle)
- `/api/providers/*`, `/api/ai-configs/*`, `/api/embedding-providers/*`
- `/api/settings/*` (PUT/POST)
- `/api/sources/*/toggle`, `/api/settings/ai/toggle`, etc.
- All UI routes that mutate config: `/sources` POST, `/settings` POST, `/webhooks` POST, etc.

**`require_perm("can_create_social_article")`** (any role with toggle true):
- `POST /api/social-article/generate`
- `POST /api/social-article/quick-generate`

**`require_perm("can_create_newsletter")`:**
- `POST /api/newsletter/generate`

**`require_perm("can_create_debate")`:**
- `POST /api/debates/run`

**`require_perm("can_create_sim")`:**
- `POST /api/social-sim/run`

**`require_perm("can_run_social_agent")`:**
- `POST /api/social-agents/{id}/run`

**`require_login` (any authenticated user):**
- All page renders (`/`, `/pipeline`, `/intelligence`, …)
- All read-only `GET /api/*` (logs, stats, list endpoints)
- `/partials/*` HTMX endpoints

**Role hierarchy:** `superadmin` ⊃ `manager` ⊃ `creator`. `require_role("manager")` passes for both manager and superadmin. `require_perm(x)` passes for superadmin/manager (full access) and creator with toggle = true.

## 4. Schema

Mirror DDL on both backends.

### 4.1 SQLite (added to `data/stats.db`)

```sql
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,                       -- argon2
    role          TEXT NOT NULL CHECK(role IN ('superadmin','manager','creator')),
    permissions   TEXT NOT NULL DEFAULT '{}',          -- JSON blob
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_login_at TIMESTAMP,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,                    -- 32-byte random hex
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at    TIMESTAMP NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,                -- sha256(plaintext_token)
    prefix        TEXT NOT NULL,                       -- first 12 chars for UI display
    last_used_at  TIMESTAMP,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS pat_hash_idx ON personal_access_tokens(token_hash);

CREATE TABLE IF NOT EXISTS auth_setup (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    setup_token TEXT,                                  -- NULL after first superadmin created
    consumed_at TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 4.2 Postgres (appended to `config/schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('superadmin','manager','creator')),
    permissions   JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    prefix        TEXT NOT NULL,
    last_used_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pat_hash_idx ON personal_access_tokens(token_hash);

CREATE TABLE IF NOT EXISTS auth_setup (
    id          INT PRIMARY KEY CHECK (id = 1),
    setup_token TEXT,
    consumed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 5. Auth flows

### 5.1 First-run setup
1. App boot: check `users` table count.
2. If 0 users → check `auth_setup` row 1.
   - If no row → generate 32-byte random hex token, INSERT row, log to stdout (and write `data/setup_token.txt` for ops convenience).
   - If row exists with non-NULL `setup_token` → reuse, log again.
3. `GET /setup` (only when no users exist): renders form requiring token + username + password (min 12 chars).
4. `POST /setup`: validate token, create first user with `role=superadmin`, set `setup_token=NULL` and `consumed_at=now()`, auto-login (create session + cookie), redirect `/`.
5. After consumption, `/setup` returns 404 for any subsequent request.

### 5.2 Login
- `GET /login`: render form (username + password). If already logged in, redirect `/`.
- `POST /login`: verify against argon2 hash. On success: create session row (32 random bytes → 64 hex chars id, 7-day expiry), set cookie `na_session=<id>; HttpOnly; SameSite=Lax; Secure (when HTTPS)`. Redirect `?next=` if safe (same-origin path), else `/`.
- On failure: re-render with error. Constant-time delay ~250ms regardless of outcome.
- Update `users.last_login_at` on success.

### 5.3 Logout
- `POST /logout`: delete session row, clear cookie, redirect `/login`.

### 5.4 Session resolution (middleware)
- For every request: read `na_session` cookie.
  - Not present → `request.state.user = None`
  - Present + valid + not expired → load user, attach to `request.state.user`. Bump `sessions.last_seen_at`.
  - Expired → delete row, clear cookie, treat as anon.
- For Authorization: Bearer header (no cookie or both): hash token (sha256), look up `personal_access_tokens.token_hash`, load user, bump `last_used_at`.
- Cookie wins if both present (UI flow takes precedence).

### 5.5 Authorization (FastAPI deps)
- `require_login()`: `request.state.user is None` → 401 (API) or redirect to `/login?next=<path>` (page).
- `require_role(role)`: hierarchical check. `role="manager"` allows superadmin + manager.
- `require_perm(name)`: superadmin/manager auto-pass; creator must have `permissions[name] == True`.
- Inactive users (`is_active=False`) treated as anon.

## 6. UI surface

### 6.1 New pages
| Route | Template | Access |
|---|---|---|
| `/setup` | `setup.html` | First-run only |
| `/login` | `login.html` | Anon |
| `/users` | `users.html` | superadmin |

### 6.2 Modifications to existing UI
- Base layout (`templates/_base.html` if exists, else inject in each page header): show username + role badge + logout button. Hide nav links the user can't use.
- Add "Account" dropdown (top-right): Username · Role · Generate PAT · Logout.
- HTMX 401 handling: add `hx-on::response-error` global → on 401, redirect `/login`.

### 6.3 User management (superadmin)
List users with columns: username, role, active, last_login. Actions: Create, Edit (role + permissions JSON toggles), Reset password, Toggle active, Generate/revoke PAT, Delete.

PAT display: shown ONCE at generation (`nag_pat_xxx…`), then only prefix visible. Regenerate = new token, old hash deleted.

## 7. Code structure

```
auth/
├── __init__.py
├── store.py            # AuthStore interface + factory(get_auth_store())
├── store_sqlite.py     # SQLiteAuthStore impl
├── store_postgres.py   # PostgresAuthStore impl
├── passwords.py        # argon2 hash/verify
├── sessions.py         # session create/get/delete + cookie helpers
├── tokens.py           # PAT generate/verify
├── middleware.py       # AuthMiddleware (resolves request.state.user)
├── deps.py             # require_login, require_role, require_perm
└── setup.py            # first-run token + bootstrap

dashboard/routes/
├── auth_ui.py          # /login, /logout, /setup pages
└── users_api.py        # /api/users CRUD + PAT endpoints

dashboard/templates/
├── login.html
├── setup.html
└── users.html
```

`AuthStore` is wired in `main.py` startup based on `CONFIG_BACKEND`. Schema init runs alongside existing `init_db()` (SQLite) / `config/sync.py init` (Postgres) — extend both.

## 8. Wiring sequence (startup)

1. `lifespan()`:
   1. Existing: `await init_db()` (SQLite analytics tables).
   2. **NEW**: `await init_auth_db()` — create users/sessions/PAT/setup tables on whichever backend is active.
   3. **NEW**: `await ensure_setup_token()` — if no users, generate token, log it.
2. `app.add_middleware(AuthMiddleware)` BEFORE `_APIRequestLogger` so logging sees authenticated user (future enhancement).
3. Routes attach `dependencies=[Depends(require_role(...))]` per the route map in §3.3.

## 9. Edge cases & error handling

- **Session expiry mid-request**: middleware deletes row, request continues as anon. Next gated route → 401/redirect.
- **PAT for a deleted user**: cascade delete handles it (FK ON DELETE CASCADE).
- **PAT for an inactive user**: `is_active=False` → middleware treats as anon. Returns 401.
- **Setup token leak**: token consumed on first successful POST `/setup`. After that, `/setup` 404s.
- **Mass session cleanup**: nightly job deletes expired sessions. Add to existing log_cleanup job (or new job).
- **Backend switch (yaml ↔ db)**: users do NOT auto-migrate. Operator must re-run setup or extend `config/sync.py` to copy users. v1 documents this; tooling deferred.
- **Concurrent login from same user**: multiple sessions allowed. Logout only deletes current session. "Logout all" deferred.
- **Brute force**: Per-IP rate limit on `/login` (5 attempts/min via in-memory bucket). Accept the simplicity; can upgrade to Redis bucket later.
- **CSRF**: SameSite=Lax cookie blocks cross-site POST from external sites. Internal HTMX POST is same-origin. No CSRF token needed for v1.
- **HTMX 401 on partials**: middleware returns 401 with `HX-Redirect: /login` header — HTMX honors this, full page navigates.

## 10. Security notes

- Argon2 default params (`memory=64MB, time=3, parallelism=4`) — verify ~250ms on target hardware.
- Session cookie: `HttpOnly`, `SameSite=Lax`, `Secure` when `request.url.scheme == "https"`.
- PAT format: `nag_pat_<48 hex chars>` (24 random bytes → 48 hex). Store only sha256 hash; `prefix` column = first 12 chars of plaintext for UI display (e.g. `nag_pat_a3f1`).
- Constant-time string comparison for token verification (`hmac.compare_digest`).
- Min password length: 12 chars. No max. No complexity rules (UX > security theater per NIST 800-63B).

## 11. Out of scope (deferred)

- Audit log (`auth_events` table)
- Password reset email flow
- "Edit my profile" page
- Manager-managed users (with delegated subset of perms)
- Multi-PAT per user with names + expiry per token
- 2FA (TOTP)
- OAuth / SSO
- IP allowlist
- Backend-switch user migration tooling

## 12. Implementation order (preview for plan)

1. **Schema + AuthStore interface** — DDL + 2 impls (SQLite/Postgres), unit tests
2. **Password + session + token primitives** — passwords.py, sessions.py, tokens.py with tests
3. **Middleware + deps** — AuthMiddleware, require_login/role/perm
4. **First-run setup** — token generation, `/setup` route
5. **Login/logout** — `/login`, `/logout`, base template integration
6. **User management API + UI** — `/api/users` CRUD, `/users` page
7. **Route gating sweep** — apply `dependencies=[...]` across all existing routes per §3.3 map
8. **PAT UI** — generate/revoke per user
9. **Manual end-to-end test** — login → access page → API call with PAT → revoke → confirm 401
