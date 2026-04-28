"""User management API + page (manager scope, multi-PAT).

Page:
  GET  /users                    → page render (manager+)

JSON API (gated to manager+, then narrowed per-route by scope):
  GET    /api/users                          → list users
                                                manager sees only creators;
                                                superadmin sees all
  POST   /api/users                          → create user
                                                manager may only set role=creator
  PUT    /api/users/{id}                     → update (role / perms / active / pwd)
                                                manager may only touch creators
                                                manager may not change role to non-creator
  DELETE /api/users/{id}                     → delete (manager: creators only)

  GET    /api/users/{id}/pats                → list PATs
  POST   /api/users/{id}/pats                → create new (name + optional expiry days)
                                                returns plaintext ONCE
  DELETE /api/users/{id}/pats/{pat_id}       → revoke one

  Legacy single-PAT shortcuts (back-compat with current users.html):
  POST   /api/users/{id}/pat                 → replace all with one named "default"
  DELETE /api/users/{id}/pat                 → revoke all
  GET    /api/users/{id}/pat                 → meta of most recent

All `/api/users` routes share the manager-or-above gate; per-route scope checks
narrow what each role may actually touch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from auth.deps import require_role
from auth.passwords import WeakPassword, hash_password
from auth.store import PERMISSIONS, ROLES, User, get_auth_store
from auth.tokens import generate_pat
from dashboard.templates_state import templates

logger = logging.getLogger(__name__)


# ── Page (manager+) ─────────────────────────────────────────────────────────

page_router = APIRouter()


@page_router.get(
    "/users", response_class=HTMLResponse,
    dependencies=[Depends(require_role("manager"))],
)
async def users_page(request: Request):
    me: User = request.state.user
    users = await get_auth_store().list_users()
    if not me.has_role("superadmin"):
        users = [u for u in users if u.role == "creator"]
    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "active_page": "users",
            "users": users,
            "roles": ROLES,
            "permissions": PERMISSIONS,
        },
    )


# ── JSON API (manager+) ─────────────────────────────────────────────────────

api_router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_role("manager"))],
)


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "permissions": u.permissions,
        "is_active": u.is_active,
        "last_login_at": u.last_login_at,
        "created_at": u.created_at,
    }


def _can_act_on(actor: User, target: User) -> bool:
    """Scope rule: superadmin → any user; manager → only creators.

    Note: manager touching another manager or a superadmin is forbidden,
    but acting on themselves goes through /account, not /api/users.
    """
    if actor.has_role("superadmin"):
        return True
    if actor.role == "manager":
        return target.role == "creator"
    return False


def _require_writable(actor: User, target: User) -> None:
    if not _can_act_on(actor, target):
        raise HTTPException(403, "Not permitted to act on this user")


@api_router.get("")
async def list_users(request: Request):
    me: User = request.state.user
    users = await get_auth_store().list_users()
    if not me.has_role("superadmin"):
        users = [u for u in users if u.role == "creator"]
    return [_user_dict(u) for u in users]


class CreateUserBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str
    role: str
    permissions: dict[str, bool] = Field(default_factory=dict)


@api_router.post("", status_code=201)
async def create_user(body: CreateUserBody, request: Request):
    me: User = request.state.user
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    # Manager may only mint creators.
    if not me.has_role("superadmin") and body.role != "creator":
        raise HTTPException(403, "manager may only create users with role=creator")
    if body.permissions and any(k not in PERMISSIONS for k in body.permissions):
        bad = [k for k in body.permissions if k not in PERMISSIONS]
        raise HTTPException(400, f"unknown permissions: {bad}")
    try:
        pwd_hash = hash_password(body.password)
    except WeakPassword as ex:
        raise HTTPException(400, str(ex))
    store = get_auth_store()
    if await store.get_user_by_username(body.username):
        raise HTTPException(409, "username already exists")
    uid = await store.create_user(
        body.username.strip(), pwd_hash, body.role, body.permissions
    )
    user = await store.get_user_by_id(uid)
    return _user_dict(user)


class UpdateUserBody(BaseModel):
    role: str | None = None
    permissions: dict[str, bool] | None = None
    is_active: bool | None = None
    password: str | None = None


@api_router.put("/{user_id}")
async def update_user(user_id: int, body: UpdateUserBody, request: Request):
    store = get_auth_store()
    target = await store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404)
    me: User = request.state.user
    _require_writable(me, target)

    # Manager: can never escalate role / change role away from creator.
    if not me.has_role("superadmin") and body.role is not None and body.role != "creator":
        raise HTTPException(403, "manager may not change role")

    # Self-protection (still relevant when superadmin edits self).
    if target.id == me.id and body.role and body.role != "superadmin":
        raise HTTPException(400, "cannot demote your own superadmin role")
    if target.id == me.id and body.is_active is False:
        raise HTTPException(400, "cannot deactivate yourself")

    if body.role is not None and body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    if body.permissions is not None and any(k not in PERMISSIONS for k in body.permissions):
        bad = [k for k in body.permissions if k not in PERMISSIONS]
        raise HTTPException(400, f"unknown permissions: {bad}")

    pwd_hash = None
    if body.password is not None:
        try:
            pwd_hash = hash_password(body.password)
        except WeakPassword as ex:
            raise HTTPException(400, str(ex))

    if body.role and body.role != "superadmin" and target.role == "superadmin":
        await _ensure_other_active_superadmin(target.id)

    await store.update_user(
        user_id,
        password_hash=pwd_hash,
        role=body.role,
        permissions=body.permissions,
        is_active=body.is_active,
    )
    return _user_dict(await store.get_user_by_id(user_id))


@api_router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: int, request: Request):
    store = get_auth_store()
    target = await store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404)
    me: User = request.state.user
    _require_writable(me, target)
    if target.id == me.id:
        raise HTTPException(400, "cannot delete yourself")
    if target.role == "superadmin":
        await _ensure_other_active_superadmin(target.id)
    await store.delete_user(user_id)
    return JSONResponse(status_code=204, content=None)


# ── Multi-PAT endpoints ─────────────────────────────────────────────────────

class CreatePatBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


def _pat_dict(p) -> dict:
    return {
        "id": p.id, "user_id": p.user_id, "name": p.name, "prefix": p.prefix,
        "expires_at": p.expires_at, "last_used_at": p.last_used_at,
        "created_at": p.created_at,
    }


async def _scoped_target(user_id: int, request: Request) -> User:
    store = get_auth_store()
    target = await store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404)
    _require_writable(request.state.user, target)
    return target


@api_router.get("/{user_id}/pats")
async def list_pats(user_id: int, request: Request):
    await _scoped_target(user_id, request)
    pats = await get_auth_store().list_user_pats(user_id)
    return [_pat_dict(p) for p in pats]


@api_router.post("/{user_id}/pats", status_code=201)
async def create_new_pat(user_id: int, body: CreatePatBody, request: Request):
    await _scoped_target(user_id, request)
    expires_at = None
    if body.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
    plain, sha, prefix = generate_pat()
    pid = await get_auth_store().create_pat(
        user_id, body.name.strip(), sha, prefix, expires_at,
    )
    return {
        "id": pid,
        "name": body.name.strip(),
        "prefix": prefix,
        "token": plain,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "warning": "Save this token now — it will not be shown again.",
    }


@api_router.delete("/{user_id}/pats/{pat_id}", status_code=204)
async def delete_pat(user_id: int, pat_id: int, request: Request):
    await _scoped_target(user_id, request)
    ok = await get_auth_store().delete_pat_by_id(pat_id, user_id)
    if not ok:
        raise HTTPException(404)
    return JSONResponse(status_code=204, content=None)


# ── Legacy single-PAT shortcuts (kept until users.html is upgraded) ─────────

@api_router.post("/{user_id}/pat")
async def create_or_rotate_pat(user_id: int, request: Request):
    await _scoped_target(user_id, request)
    plain, sha, prefix = generate_pat()
    await get_auth_store().upsert_pat(user_id, sha, prefix)
    return {
        "token": plain, "prefix": prefix,
        "warning": "Save this token now — it will not be shown again.",
    }


@api_router.delete("/{user_id}/pat", status_code=204)
async def revoke_pat(user_id: int, request: Request):
    await _scoped_target(user_id, request)
    await get_auth_store().delete_pat(user_id)
    return JSONResponse(status_code=204, content=None)


@api_router.get("/{user_id}/pat")
async def get_pat_meta(user_id: int, request: Request):
    await _scoped_target(user_id, request)
    meta = await get_auth_store().get_pat_meta(user_id)
    return meta or {}


# ── helpers ─────────────────────────────────────────────────────────────────

async def _ensure_other_active_superadmin(exclude_user_id: int) -> None:
    others = [
        u for u in await get_auth_store().list_users()
        if u.role == "superadmin" and u.is_active and u.id != exclude_user_id
    ]
    if not others:
        raise HTTPException(400, "cannot remove the last active superadmin")
