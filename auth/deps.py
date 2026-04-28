"""FastAPI dependencies: require_login / require_role / require_perm.

Behavior on failure:
  - Browser request (Accept includes text/html, no Authorization header):
      Page → 303 redirect to /login?next=<path>
      HTMX (HX-Request header) → 401 with HX-Redirect: /login
  - API request (else): 401 / 403 JSON
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse, JSONResponse, Response
from starlette.exceptions import HTTPException

from auth.store import ROLE_HIERARCHY, User


def _wants_html(request: Request) -> bool:
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept and not accept.startswith("application/json")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


class AuthRedirect(HTTPException):
    """Marker exception → handled by app-level handler to issue redirect/401."""
    def __init__(self, status_code: int, redirect_to: str | None = None,
                 message: str = "Unauthorized"):
        super().__init__(status_code=status_code, detail=message)
        self.redirect_to = redirect_to


def _unauth_response(request: Request, message: str = "Authentication required") -> Response:
    next_path = request.url.path
    if request.url.query:
        next_path += "?" + request.url.query
    login_url = f"/login?next={quote(next_path, safe='')}"

    if _is_htmx(request):
        # HTMX honors HX-Redirect → full-page navigate
        return Response(status_code=401, headers={"HX-Redirect": login_url})
    if _wants_html(request):
        return RedirectResponse(login_url, status_code=303)
    return JSONResponse({"error": message}, status_code=401)


def _forbidden_response(request: Request, message: str = "Forbidden") -> Response:
    if _is_htmx(request):
        return Response(status_code=403, content=message)
    if _wants_html(request):
        return Response(content=message, status_code=403, media_type="text/plain")
    return JSONResponse({"error": message}, status_code=403)


def require_login():
    async def dep(request: Request):
        user: User | None = getattr(request.state, "user", None)
        if user is None:
            raise _UnauthEarly(request)
        return user
    return dep


def require_role(role: str):
    async def dep(request: Request):
        user: User | None = getattr(request.state, "user", None)
        if user is None:
            raise _UnauthEarly(request)
        if not user.has_role(role):
            raise _ForbiddenEarly(request, f"Requires role: {role}")
        return user
    return dep


def require_perm(perm: str):
    async def dep(request: Request):
        user: User | None = getattr(request.state, "user", None)
        if user is None:
            raise _UnauthEarly(request)
        if not user.has_perm(perm):
            raise _ForbiddenEarly(request, f"Missing permission: {perm}")
        return user
    return dep


# ── Exceptions caught by app-level handler ──────────────────────────────────
# We use exceptions (not direct Response returns) because FastAPI deps must
# raise to abort dispatch; the handler converts them to the right shape.

class _UnauthEarly(Exception):
    def __init__(self, request: Request):
        self.request = request

class _ForbiddenEarly(Exception):
    def __init__(self, request: Request, message: str):
        self.request = request
        self.message = message


def install_auth_exception_handlers(app) -> None:
    @app.exception_handler(_UnauthEarly)
    async def _h_unauth(_req, exc: _UnauthEarly):
        return _unauth_response(exc.request)

    @app.exception_handler(_ForbiddenEarly)
    async def _h_forbidden(_req, exc: _ForbiddenEarly):
        return _forbidden_response(exc.request, exc.message)
