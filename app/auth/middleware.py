"""Authentication middleware for HTML and API routes."""

from __future__ import annotations

from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.db import SessionLocal
from app.models import User

_PUBLIC_PATHS = frozenset({"/login", "/health", "/favicon.ico"})
_PUBLIC_PREFIXES = ("/static",)


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

        if path.startswith("/api"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)

        next_path = quote(path)
        return RedirectResponse(url=f"/login?next={next_path}", status_code=302)
