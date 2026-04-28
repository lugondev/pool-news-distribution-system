"""/account page + self-service change-password endpoint (any role)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from auth import require_login
from auth.passwords import (
    WeakPassword, hash_password, validate_password, verify_password,
)
from auth.store import User, get_auth_store
from dashboard.templates_state import templates

logger = logging.getLogger(__name__)


# ── Page ────────────────────────────────────────────────────────────────────

page_router = APIRouter()


@page_router.get(
    "/account", response_class=HTMLResponse,
    dependencies=[Depends(require_login())],
)
async def account_page(request: Request):
    return templates.TemplateResponse(
        "account.html", {"request": request, "active_page": "account"},
    )


# ── API ─────────────────────────────────────────────────────────────────────

api_router = APIRouter(
    prefix="/account",
    tags=["account"],
    dependencies=[Depends(require_login())],
)


class ChangePasswordBody(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


@api_router.post("/change-password", status_code=204)
async def change_password(body: ChangePasswordBody, request: Request):
    me: User = request.state.user
    store = get_auth_store()

    current_hash = await store.get_password_hash(me.id)
    valid = bool(current_hash and verify_password(current_hash, body.old_password))

    # Constant-ish delay regardless of outcome to blunt timing oracles.
    await asyncio.sleep(0.25)

    if not valid:
        raise HTTPException(401, "old password incorrect")

    try:
        validate_password(body.new_password)
    except WeakPassword as ex:
        raise HTTPException(400, str(ex))

    if body.new_password == body.old_password:
        raise HTTPException(400, "new password must differ from old")

    new_hash = hash_password(body.new_password)
    await store.update_user(me.id, password_hash=new_hash)
    return JSONResponse(status_code=204, content=None)
