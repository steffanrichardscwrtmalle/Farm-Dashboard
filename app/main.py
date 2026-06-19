from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.api.admin_routes import router as admin_api_router
from app.api.routes import router as api_router
from app.auth.deps import require_admin
from app.auth.middleware import AuthMiddleware
from app.auth.passwords import verify_password
from app.auth.roles import ROLE_LABELS, ROLES
from app.auth.users import get_user_by_email, seed_admin_user
from app.config import COOKIE_SECURE, SECRET_KEY, SESSION_MAX_AGE_SECONDS
from app.db import SessionLocal, get_db, init_db
from app.models import User
from app.services.invoice_ops import ensure_mappings_seeded

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"
_STATIC_DIR = _PROJECT_ROOT / "static"

app = FastAPI(title="Farm Dashboard")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=SESSION_MAX_AGE_SECONDS,
    https_only=COOKIE_SECURE,
    same_site="lax",
)

app.include_router(api_router)
app.include_router(admin_api_router)

_WYNNSTAY_BREADCRUMB = '<a href="/">Farm Dashboard</a> &rsaquo; <a href="/wynnstay">Wynnstay</a>'

_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300


def _template_ctx(request: Request, **extra) -> dict:
    user = getattr(request.state, "user", None)
    return {
        "request": request,
        "current_user": user,
        "roles": [{"id": r, "label": ROLE_LABELS[r]} for r in ROLES],
        **extra,
    }


def _wynnstay_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _WYNNSTAY_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "suppliers",
        "active_section": "wynnstay",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _login_rate_ok(client_key: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[client_key] if now - t < _LOGIN_WINDOW_SECONDS]
    _login_attempts[client_key] = attempts
    return len(attempts) < _LOGIN_MAX_ATTEMPTS


def _record_login_failure(client_key: str) -> None:
    _login_attempts[client_key].append(time.time())


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    db = SessionLocal()
    try:
        ensure_mappings_seeded(db)
        seed_admin_user(db)
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None):
    if request.session.get("user_id"):
        return RedirectResponse(_safe_next_path(next), status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        _template_ctx(request, error=None, next_path=next or ""),
    )


def _safe_next_path(next_path: str | None) -> str:
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    client_key = request.client.host if request.client else "unknown"
    if not _login_rate_ok(client_key):
        return templates.TemplateResponse(
            request,
            "login.html",
            _template_ctx(
                request,
                error="Too many failed attempts. Try again in a few minutes.",
                next_path=next,
            ),
            status_code=429,
        )

    db = SessionLocal()
    try:
        user = get_user_by_email(db, email)
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            _record_login_failure(client_key)
            return templates.TemplateResponse(
                request,
                "login.html",
                _template_ctx(request, error="Invalid email or password.", next_path=next),
                status_code=401,
            )
        request.session["user_id"] = user.id
    finally:
        db.close()

    _login_attempts.pop(client_key, None)
    return RedirectResponse(_safe_next_path(next), status_code=302)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    from sqlalchemy import select

    users = list(db.scalars(select(User).order_by(User.email)).all())
    ctx = _template_ctx(
        request,
        title="User management",
        active_nav="admin-users",
        active_nav_group=None,
        active_section=None,
        breadcrumb='<a href="/">Farm Dashboard</a> &rsaquo; User management',
        users=[u.to_dict() for u in users],
        current_user=admin,
    )
    return templates.TemplateResponse(request, "admin/users.html", ctx)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _template_ctx(
            request,
            title="Farm Dashboard",
            active_nav="home",
            active_section=None,
            active_nav_group=None,
        ),
    )


@app.get("/wynnstay", response_class=HTMLResponse)
def wynnstay_home(request: Request):
    return templates.TemplateResponse(
        request,
        "wynnstay/home.html",
        _template_ctx(request, **_wynnstay_context("Wynnstay", "overview")),
    )


@app.get("/wynnstay/invoices", response_class=HTMLResponse)
def invoices_page(request: Request):
    return templates.TemplateResponse(
        request,
        "invoices.html",
        _template_ctx(request, **_wynnstay_context("Invoices", "invoices", "Invoices")),
    )


@app.get("/wynnstay/category-breakdown", response_class=HTMLResponse)
def category_breakdown_page(request: Request):
    return templates.TemplateResponse(
        request,
        "category_breakdown.html",
        _template_ctx(request, **_wynnstay_context("Category Breakdown", "breakdown", "Category breakdown")),
    )


@app.get("/wynnstay/product-price-by-month", response_class=HTMLResponse)
def product_price_by_month_page(request: Request):
    return templates.TemplateResponse(
        request,
        "product_price_by_month.html",
        _template_ctx(request, **_wynnstay_context("Product Prices", "price-by-month", "Product Prices")),
    )


@app.get("/wynnstay/product-quantity-by-month", response_class=HTMLResponse)
def product_quantity_by_month_page(request: Request):
    return templates.TemplateResponse(
        request,
        "product_quantity_by_month.html",
        _template_ctx(request, **_wynnstay_context("Product Quantities", "quantity-by-month", "Product Quantities")),
    )


@app.get("/wynnstay/monthly-spend", response_class=HTMLResponse)
def monthly_spend_page(request: Request):
    return templates.TemplateResponse(
        request,
        "monthly_spend.html",
        _template_ctx(request, **_wynnstay_context("Monthly Spend", "monthly-spend", "Monthly Spend")),
    )


@app.get("/wynnstay/mappings", response_class=HTMLResponse)
def mappings_page(request: Request):
    return templates.TemplateResponse(
        request,
        "mappings.html",
        _template_ctx(request, **_wynnstay_context("Product Mappings", "mappings", "Product mappings")),
    )


@app.get("/invoices")
def redirect_invoices():
    return RedirectResponse("/wynnstay/invoices", status_code=301)


@app.get("/category-breakdown")
def redirect_category_breakdown():
    return RedirectResponse("/wynnstay/category-breakdown", status_code=301)


@app.get("/product-price-by-month")
def redirect_product_price_by_month():
    return RedirectResponse("/wynnstay/product-price-by-month", status_code=301)


@app.get("/product-quantity-by-month")
def redirect_product_quantity_by_month():
    return RedirectResponse("/wynnstay/product-quantity-by-month", status_code=301)


@app.get("/monthly-spend")
def redirect_monthly_spend():
    return RedirectResponse("/wynnstay/monthly-spend", status_code=301)


@app.get("/mappings")
def redirect_mappings():
    return RedirectResponse("/wynnstay/mappings", status_code=301)
