"""AuthMiddleware — resolves request.state.user from cookie or bearer token.

Cookie wins if both present (UI takes precedence). Sets request.state.user
to a `User` instance or `None`. Does NOT enforce — gating happens via deps.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from time import monotonic

from starlette.middleware.base import BaseHTTPMiddleware

from auth.sessions import COOKIE_NAME, resolve_session
from auth.store import get_auth_store
from auth.tokens import hash_pat, looks_like_pat

logger = logging.getLogger(__name__)


# Per-IP login attempt tracker. In-memory bucket — process-local; scales for
# single-instance deploy. Move to Redis if multi-instance later.
_LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=10))
LOGIN_RATE_WINDOW_SEC = 60
LOGIN_RATE_MAX = 5


def record_login_attempt(ip: str) -> None:
    _LOGIN_ATTEMPTS[ip].append(monotonic())


def login_rate_limited(ip: str) -> bool:
    now = monotonic()
    cutoff = now - LOGIN_RATE_WINDOW_SEC
    bucket = _LOGIN_ATTEMPTS[ip]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return len(bucket) >= LOGIN_RATE_MAX


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.user = None
        request.state.session_id = None

        cookie_sid = request.cookies.get(COOKIE_NAME)
        if cookie_sid:
            user = await resolve_session(cookie_sid)
            if user:
                request.state.user = user
                request.state.session_id = cookie_sid
                return await call_next(request)

        # Bearer token fallback (PAT). Header form: "Authorization: Bearer nag_pat_..."
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if looks_like_pat(token):
                store = get_auth_store()
                user = await store.get_user_by_pat_hash(hash_pat(token))
                if user and user.is_active:
                    request.state.user = user
                    # Fire-and-forget bump; don't block request.
                    try:
                        await store.bump_pat(hash_pat(token))
                    except Exception:
                        logger.debug("bump_pat failed", exc_info=True)

        return await call_next(request)
