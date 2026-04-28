"""User management API + page (superadmin only).

Endpoints:
  GET  /users               → page render
  GET  /api/users           → list users (json)
  POST /api/users           → create
  PUT  /api/users/{id}      → update (role / permissions / is_active / password)
  DELETE /api/users/{id}    → delete
  POST /api/users/{id}/pat  → generate or rotate PAT (returns plaintext ONCE)
  DELETE /api/users/{id}/pat → revoke PAT
  GET  /api/users/{id}/pat  → meta (prefix, last_used_at, created_at) — no plaintext
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from auth.deps import require_role
from auth.passwords import WeakPassword, hash_password
from auth.store import PERMISSIONS, ROLES, User, get_auth_store
from auth.tokens import generate_pat
from dashboard.templates_state import templates

logger = logging.getLogger(__name__)


# ── Page (superadmin only) ──────────────────────────────────────────────────

page_router = APIRouter()


@page_router.get(
    "/users", response_class=HTMLResponse,
    dependencies=[Depends(require_role("superadmin"))],
)
async def users_page(request: Request):
    users = await get_auth_store().list_users()
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


# ── JSON API (superadmin only) ──────────────────────────────────────────────

api_router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_role("superadmin"))],
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


@api_router.get("")
async def list_users():
    users = await get_auth_store().list_users()
    return [_user_dict(u) for u in users]


class CreateUserBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str
    role: str
    permissions: dict[str, bool] = Field(default_factory=dict)


@api_router.post("", status_code=201)
async def create_user(body: CreateUserBody):
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
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
    uid = await store.create_user(body.username.strip(), pwd_hash, body.role, body.permissions)
    user = await store.get_user_by_id(uid)
    return _user_dict(user)


class UpdateUserBody(BaseModel):
    role: str | None = None
    permissions: dict[str, bool] | None = None
    is_active: bool | None = None
    password: str | None = None  # if set, must satisfy length rules


@api_router.put("/{user_id}")
async def update_user(user_id: int, body: UpdateUserBody, request: Request):
    store = get_auth_store()
    target = await store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404)

    me: User = request.state.user
    # Guard: can't demote / disable / delete self in ways that lock out the system.
    if target.id == me.id and (body.role and body.role != "superadmin"):
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

    # Last-superadmin guard: if changing this user's role away from superadmin,
    # ensure another active superadmin remains.
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
    if target.id == me.id:
        raise HTTPException(400, "cannot delete yourself")
    if target.role == "superadmin":
        await _ensure_other_active_superadmin(target.id)
    await store.delete_user(user_id)
    return JSONResponse(status_code=204, content=None)


# ── PAT endpoints ───────────────────────────────────────────────────────────

@api_router.post("/{user_id}/pat")
async def create_or_rotate_pat(user_id: int):
    store = get_auth_store()
    if not await store.get_user_by_id(user_id):
        raise HTTPException(404)
    plain, sha, prefix = generate_pat()
    await store.upsert_pat(user_id, sha, prefix)
    # Plaintext shown ONCE.
    return {"token": plain, "prefix": prefix, "warning": "Save this token now — it will not be shown again."}


@api_router.delete("/{user_id}/pat", status_code=204)
async def revoke_pat(user_id: int):
    store = get_auth_store()
    if not await store.get_user_by_id(user_id):
        raise HTTPException(404)
    await store.delete_pat(user_id)
    return JSONResponse(status_code=204, content=None)


@api_router.get("/{user_id}/pat")
async def get_pat_meta(user_id: int):
    store = get_auth_store()
    if not await store.get_user_by_id(user_id):
        raise HTTPException(404)
    meta = await store.get_pat_meta(user_id)
    return meta or {}


# ── helpers ─────────────────────────────────────────────────────────────────

async def _ensure_other_active_superadmin(exclude_user_id: int) -> None:
    others = [
        u for u in await get_auth_store().list_users()
        if u.role == "superadmin" and u.is_active and u.id != exclude_user_id
    ]
    if not others:
        raise HTTPException(400, "cannot remove the last active superadmin")
