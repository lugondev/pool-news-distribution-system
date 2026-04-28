"""Session lifecycle + cookie helpers."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response

from auth.store import get_auth_store

COOKIE_NAME = "na_session"
SESSION_TTL = timedelta(days=int(os.getenv("AUTH_SESSION_DAYS", "7")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return secrets.token_hex(32)  # 64 hex chars


def _is_https(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )


async def create_session(user_id: int) -> tuple[str, datetime]:
    sid = _new_session_id()
    expires_at = _now() + SESSION_TTL
    await get_auth_store().create_session(sid, user_id, expires_at)
    return sid, expires_at


def attach_session_cookie(
    response: Response, request: Request, session_id: str, expires_at: datetime
) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        max_age=int((expires_at - _now()).total_seconds()),
        expires=int(expires_at.timestamp()),
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


async def resolve_session(session_id: str):
    """Return (user, session_id) on valid+active session, else None.

    Auto-deletes expired session rows. Updates last_seen_at on hit.
    """
    if not session_id:
        return None
    store = get_auth_store()
    row = await store.get_session(session_id)
    if not row:
        return None
    user_id, expires_at = row
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _now():
        await store.delete_session(session_id)
        return None
    user = await store.get_user_by_id(user_id)
    if not user or not user.is_active:
        return None
    await store.bump_session(session_id)
    return user
