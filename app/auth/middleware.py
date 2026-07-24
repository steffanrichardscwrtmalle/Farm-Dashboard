"""Authentication middleware for HTML and API routes."""

from __future__ import annotations

from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.db import SessionLocal
from app.models import User
from app.auth.import_key import valid_import_key

_PUBLIC_PATHS = frozenset({
    "/login",
    "/health",
    "/favicon.ico",
    # OAuth return from Xero — must work even if localhost/127.0.0.1 session differs.
    "/api/xero/oauth/callback",
})
_PUBLIC_PREFIXES = ("/static",)
_IMPORT_API_PREFIX = "/api/herd"
_IMPORT_KEY_POST_PATHS = frozenset({
    "/api/nml/import",
    "/api/haulier/import",
    "/api/milk-statements/import",
    "/api/parlour/import",
})
_HR_WEBHOOK_PATH = "/api/hr/webhook"


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        request.state.user = None

        if path in _PUBLIC_PATHS or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
            return await call_next(request)

        user_id = request.session.get("user_id")
        if user_id is not None:
            db = SessionLocal()
            try:
                user = db.get(User, int(user_id))
                if user is not None and user.is_active:
                    request.state.user = user
                else:
                    request.session.pop("user_id", None)
            except (TypeError, ValueError):
                request.session.pop("user_id", None)
            finally:
                db.close()

        if request.state.user is not None:
            return await call_next(request)

        if path.startswith(_IMPORT_API_PREFIX) and valid_import_key(request):
            return await call_next(request)

        if path in _IMPORT_KEY_POST_PATHS and request.method == "POST" and valid_import_key(request):
            return await call_next(request)

        if path == _HR_WEBHOOK_PATH and request.method == "POST":
            return await call_next(request)

        if path.startswith("/api"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)

        next_path = quote(path)
        return RedirectResponse(url=f"/login?next={next_path}", status_code=302)
