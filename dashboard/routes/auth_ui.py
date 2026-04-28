"""Login / logout / first-run setup pages."""

from __future__ import annotations

import asyncio
import logging
import secrets

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException

from auth.middleware import login_rate_limited, record_login_attempt
from auth.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPassword,
    hash_password,
    needs_rehash,
    verify_password,
)
from auth.sessions import (
    SESSION_TTL,
    attach_session_cookie,
    clear_session_cookie,
    create_session,
)
from auth.setup import consume_setup_token, ensure_setup_token, is_setup_required
from auth.store import PERMISSIONS, get_auth_store
from dashboard.templates_state import templates

logger = logging.getLogger(__name__)
router = APIRouter()


# ── helpers ─────────────────────────────────────────────────────────────────

def _safe_next(request: Request, raw: str | None) -> str:
    """Only allow same-origin paths to prevent open-redirect."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── /login ──────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if await is_setup_required():
        return RedirectResponse("/setup", status_code=303)
    if getattr(request.state, "user", None):
        return RedirectResponse(_safe_next(request, next), status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None, "next": _safe_next(request, next), "username": ""},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    ip = _client_ip(request)
    if login_rate_limited(ip):
        # Constant 250ms delay anyway so behavior is uniform.
        await asyncio.sleep(0.25)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request, "error": "Too many attempts — try again in a minute.",
                "next": _safe_next(request, next), "username": username,
            },
            status_code=429,
        )
    record_login_attempt(ip)

    store = get_auth_store()
    user = await store.get_user_by_username(username)
    pwd_hash = await store.get_password_hash(user.id) if user else None
    valid = bool(user and user.is_active and pwd_hash and verify_password(pwd_hash, password))

    # Constant-time-ish: pad to ≥250ms regardless of outcome
    await asyncio.sleep(0.25)

    if not valid:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request, "error": "Invalid username or password.",
                "next": _safe_next(request, next), "username": username,
            },
            status_code=401,
        )

    # Re-hash if argon2 params were upgraded since this hash was created.
    if needs_rehash(pwd_hash):
        try:
            await store.update_user(user.id, password_hash=hash_password(password))
        except Exception:
            logger.warning("password rehash failed for user_id=%s", user.id, exc_info=True)

    await store.touch_login(user.id)
    sid, exp = await create_session(user.id)
    response = RedirectResponse(_safe_next(request, next), status_code=303)
    attach_session_cookie(response, request, sid, exp)
    return response


# ── /logout ─────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(request: Request):
    sid = getattr(request.state, "session_id", None)
    if sid:
        try:
            await get_auth_store().delete_session(sid)
        except Exception:
            logger.debug("delete_session failed", exc_info=True)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, request)
    return response


@router.get("/logout")
async def logout_get(request: Request):
    """Convenience link target. Same effect as POST."""
    return await logout(request)


# ── /setup (first-run) ──────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if not await is_setup_required():
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "setup.html",
        {"request": request, "error": None, "username": "", "token": ""},
    )


@router.post("/setup")
async def setup_submit(
    request: Request,
    token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if not await is_setup_required():
        raise HTTPException(status_code=404)

    store = get_auth_store()
    expected = await store.get_setup_token()

    def _err(msg: str, status: int = 400):
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": msg, "username": username, "token": token},
            status_code=status,
        )

    if not expected or not secrets.compare_digest(expected, token.strip()):
        return _err("Invalid setup token.", 401)
    if not username or not username.strip():
        return _err("Username required.")
    if password != password_confirm:
        return _err("Passwords do not match.")
    try:
        pwd_hash = hash_password(password)
    except WeakPassword as ex:
        return _err(str(ex))

    # Grant all toggles for the bootstrap superadmin (defensive — they get
    # full access via role anyway, but keeps the JSON consistent in UI).
    perms = {p: True for p in PERMISSIONS}
    user_id = await store.create_user(username.strip(), pwd_hash, "superadmin", perms)
    await consume_setup_token()
    await store.touch_login(user_id)

    sid, exp = await create_session(user_id)
    response = RedirectResponse("/", status_code=303)
    attach_session_cookie(response, request, sid, exp)
    return response


# Re-exposed for app startup to call after schema init.
__all__ = ["router", "ensure_setup_token"]
