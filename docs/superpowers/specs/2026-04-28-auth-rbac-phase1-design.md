# Auth + RBAC — Phase 1 Design

**Date:** 2026-04-28
**Status:** Draft (awaiting user review)
**Builds on:** `2026-04-28-auth-rbac-design.md` (MVP shipped in commit `ba5de46`)

**Phase 1 scope:** Multi-PAT per user, manager-managed users, self-service password change, admin-initiated password reset.

**Out of scope (deferred):** 2FA, OAuth/OIDC, forgot-password email flow, audit log, "logout all sessions" button.

---

## 1. Goals

1. Allow each user (when admin-managed) to have multiple named, individually-revocable Personal Access Tokens with optional expiry.
2. Allow `manager` role to fully manage `creator` users (create / edit / disable / delete / reset password / manage PATs) without touching other managers or superadmins.
3. Allow every authenticated user to change their own password via a self-service `/account` page.
4. Confirm that `superadmin` retains full control over all users; `manager` cannot escalate privileges.

PAT creation remains admin-only (Phase-1 user choice "C") — creators do not create their own PATs.

## 2. Roles & permissions delta

| Action | superadmin | manager | creator |
|---|---|---|---|
| List all users | ✓ | ✗ | ✗ |
| List creators | ✓ | ✓ | ✗ |
| Create user (any role) | ✓ | ✗ | ✗ |
| Create user (role=creator only) | ✓ | ✓ | ✗ |
| Edit superadmin/manager | ✓ | ✗ | ✗ |
| Edit creator | ✓ | ✓ | ✗ |
| Change role of any user | ✓ | ✗ | ✗ |
| Reset another user's password | ✓ (any) | ✓ (creators only) | ✗ |
| Manage PATs of another user | ✓ (any) | ✓ (creators only) | ✗ |
| Change **own** password | ✓ | ✓ | ✓ |
| View `/account` page | ✓ | ✓ | ✓ |
| View own PAT list (read only) | ✗ (admins use /users) | ✗ | ✗ |

**Hard rule:** manager never gets to create or modify a `superadmin` or `manager` row, nor change any role to `manager`/`superadmin`. Only `superadmin` can mint other admins.

## 3. Schema delta

### 3.1 `personal_access_tokens` becomes multi-row per user

Before (MVP):
```sql
UNIQUE (user_id),  token_hash UNIQUE,  prefix,  last_used_at,  created_at
```

After:
```sql
-- removed: UNIQUE(user_id)
-- added:
name        TEXT NOT NULL DEFAULT 'default',  -- human label, 1-64 chars
expires_at  TIMESTAMPTZ NULL                  -- NULL = never expires
-- existing kept:
id (PK), user_id (FK CASCADE), token_hash (UNIQUE), prefix, last_used_at, created_at
```

`UNIQUE(token_hash)` and `INDEX(user_id)` remain. A new partial filter happens at lookup time: `expires_at IS NULL OR expires_at > now()`.

### 3.2 SQLite migration

SQLite cannot `DROP CONSTRAINT`. Migration is "create-new-and-rename":

```sql
-- guarded by detecting absence of `name` column
CREATE TABLE personal_access_tokens_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT 'default',
    prefix        TEXT NOT NULL,
    expires_at    TIMESTAMP,
    last_used_at  TIMESTAMP,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO personal_access_tokens_new
    (id, user_id, token_hash, name, prefix, last_used_at, created_at)
  SELECT id, user_id, token_hash, 'default', prefix, last_used_at, created_at
  FROM personal_access_tokens;
DROP TABLE personal_access_tokens;
ALTER TABLE personal_access_tokens_new RENAME TO personal_access_tokens;
CREATE INDEX pat_hash_idx ON personal_access_tokens(token_hash);
CREATE INDEX pat_user_idx ON personal_access_tokens(user_id);
```

Migration runs from `SqliteAuthStore.init_schema()` after the base schema. Detection: `PRAGMA table_info(personal_access_tokens)` lacks `name` column.

### 3.3 Postgres migration

```sql
-- guarded by IF NOT EXISTS / try-catch
ALTER TABLE personal_access_tokens DROP CONSTRAINT IF EXISTS personal_access_tokens_user_id_key;
ALTER TABLE personal_access_tokens ADD COLUMN IF NOT EXISTS name       TEXT NOT NULL DEFAULT 'default';
ALTER TABLE personal_access_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS pat_user_idx ON personal_access_tokens(user_id);
```

Migration runs from `PostgresAuthStore.init_schema()` after base schema. Both DDL paths are idempotent.

## 4. AuthStore interface changes

### 4.1 New methods

```python
async def list_user_pats(self, user_id: int) -> list[Pat]
async def create_pat(self, user_id: int, name: str, token_hash: str,
                     prefix: str, expires_at: datetime | None) -> int
async def delete_pat_by_id(self, pat_id: int, user_id: int) -> bool   # scoped
async def delete_expired_pats(self) -> int                            # cleanup job
```

`Pat` is a dataclass: `id, user_id, name, prefix, expires_at, last_used_at, created_at`.

### 4.2 Modified methods

```python
# was: async def upsert_pat(user_id, token_hash, prefix)
# kept for back-compat (used by existing single-PAT endpoint),
# now implemented as: delete all existing for user, then create one named "default".

# was: async def get_user_by_pat_hash(token_hash) -> User | None
# changes: must filter `expires_at IS NULL OR expires_at > now()`.
```

`get_pat_meta(user_id)` (single-PAT helper) is kept for back-compat: returns the *most recent* PAT's meta, or `None`. Marked deprecated in code comment.

## 5. API surface

### 5.1 New endpoints

| Method | Path | Who | Body | Returns |
|---|---|---|---|---|
| GET  | `/account` | self | — | HTML page |
| POST | `/api/account/change-password` | self | `{old_password, new_password}` | 204 / 400 / 401 |
| GET  | `/api/users/{id}/pats` | scope | — | `[{id, name, prefix, expires_at, last_used_at, created_at}]` |
| POST | `/api/users/{id}/pats` | scope | `{name, expires_in_days?: int 1..3650}` | `{id, token, prefix, name, expires_at, warning}` (`token` shown ONCE) |
| DELETE | `/api/users/{id}/pats/{pat_id}` | scope | — | 204 / 404 |

`scope` = superadmin (any user) OR manager (only when `target.role == 'creator'`).

### 5.2 Existing endpoints (with new scope rules)

| Endpoint | Old gate | New gate |
|---|---|---|
| `GET /api/users` | superadmin | Allowed for manager — response filtered to creators only |
| `POST /api/users` | superadmin | Allowed for manager — body must have `role == 'creator'`, else 403 |
| `PUT /api/users/{id}` | superadmin | Allowed for manager iff target is creator AND body does not change `role` to non-creator |
| `DELETE /api/users/{id}` | superadmin | Allowed for manager iff target is creator |
| `POST /api/users/{id}/pat` (legacy single) | superadmin | Allowed for manager iff target is creator. Implemented as "delete all → create named 'default'" — for back-compat with current UI |
| `DELETE /api/users/{id}/pat` (legacy revoke-all) | superadmin | Allowed for manager iff target is creator |
| `GET /api/users/{id}/pat` (legacy meta) | superadmin | Allowed for manager iff target is creator |

The legacy single-PAT endpoints stay so the existing `users.html` UI keeps working until it's upgraded to the multi-PAT view.

### 5.3 Self-service guard

`POST /api/account/change-password`:
- Requires authenticated user (any role).
- Verifies `old_password` against current hash.
- Validates `new_password` length ≥ 12 (existing `validate_password`).
- Stores new hash. Does NOT invalidate other sessions (deferred).

## 6. UI

### 6.1 `/account` (new, all roles)
Minimal page — single "Change password" form. Field: old password, new password, confirm. On success: flash "Password updated", remain on page.

### 6.2 `/users` (existing, gated to manager+)
- Manager view: list shows `creator` users only; "Create user" form has role select hidden / forced to `creator`.
- Superadmin view: unchanged from MVP.
- "PAT" button now opens a panel listing all PATs for that user (id, name, prefix, last_used, expires) with "Generate new" form (name + expiry days) and per-row "Revoke" button.
- Generate new returns plaintext token shown once; same UX as MVP.

### 6.3 Nav update
- `/account` link added under user dropdown (above "Logout").
- `/users` link visibility changes from `has_role('superadmin')` → `has_role('manager')`.

## 7. Module changes

| Path | Change |
|---|---|
| `auth/store.py` | Add `Pat` dataclass; new abstract methods |
| `auth/store_sqlite.py` | New methods; migration in `init_schema()` |
| `auth/store_postgres.py` | New methods; migration in `init_schema()` |
| `auth/deps.py` | New helper `require_user_writable_by(target)` for scope checks |
| `dashboard/routes/users_api.py` | Apply manager scope to existing endpoints; add multi-PAT endpoints |
| `dashboard/routes/account_ui.py` | NEW — `/account` page + change-password endpoint |
| `dashboard/templates/users.html` | Multi-PAT panel; manager-friendly create form |
| `dashboard/templates/account.html` | NEW |
| `dashboard/templates/partials/nav.html` | Add `/account` link; change `/users` visibility |
| `jobs/log_cleanup.py` | Add `delete_expired_pats()` call |

## 8. Edge cases

- **Last superadmin protection:** unchanged from MVP. Manager cannot reach this code path because they can't edit superadmins.
- **Manager editing creator they didn't create:** allowed. Ownership is not tracked (per MVP decision).
- **Manager promoting creator to manager:** rejected at `PUT /api/users/{id}` — manager can't set `body.role` to anything but `creator`.
- **Manager creating a user with role=manager via POST:** rejected at `POST /api/users`.
- **PAT name collision per user:** allowed. Names are labels, not unique. Use `id` to revoke.
- **PAT expiry = 0 or negative days:** rejected (400) by Pydantic field validator.
- **Self-edit via /api/users/{id}:** still denied for role-down or self-disable (existing guards). `/account` covers password.
- **Old PAT lookup with expired token:** middleware treats as anon (no error to client beyond 401).
- **Backend switch (yaml ↔ db):** migration runs automatically on the new backend's schema init. Users still don't auto-migrate (out of scope, same as MVP).

## 9. Security notes

- Change-password requires old password (not just authenticated session) — defends against session hijack.
- New password must pass same `validate_password` (min 12 chars).
- PAT generation flow unchanged: shown once, sha256-hashed in DB.
- Expiry enforced at lookup time (not just cleanup), so revoking a PAT by setting `expires_at = now()` works immediately if we ever expose it (we don't — we use DELETE).
- Manager scope checked in API layer (not just UI), so a malicious manager calling `PUT /api/users/{superadmin_id}` directly still gets 403.

## 10. Implementation order

1. **Schema migration + AuthStore changes** — `Pat` dataclass, new methods on both backends, migration code, unit smoke for both backends.
2. **Scope helper + manager-scoped endpoints** — `require_user_writable_by` + apply to legacy `/api/users/*` endpoints. Verify manager can edit creator, cannot edit manager/superadmin.
3. **Multi-PAT endpoints** — `GET/POST /api/users/{id}/pats` and `DELETE /api/users/{id}/pats/{pat_id}`. Keep legacy single-PAT endpoints working.
4. **`/account` page + change-password** — route, template, API endpoint.
5. **`users.html` multi-PAT panel** — replace single-PAT modal with list+revoke+generate.
6. **Nav update** — `/account` link, `/users` visibility for manager.
7. **Cleanup job** — wire `delete_expired_pats()` into existing `log_cleanup.py`.
8. **End-to-end smoke test** — manager flow, password change, PAT lifecycle, manager-can't-touch-superadmin.

Total: ~5-6 hours of focused work.
