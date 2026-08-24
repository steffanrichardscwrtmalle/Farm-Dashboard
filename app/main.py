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

from app.api.office_admin_routes import router as office_admin_api_router
from app.api.genetics_routes import router as genetics_api_router
from app.api.nml_routes import router as nml_api_router
from app.api.haulier_routes import router as haulier_api_router
from app.api.benchmarking_routes import router as benchmarking_api_router
from app.api.cattle_sales_routes import router as cattle_sales_api_router
from app.api.milk_statements_routes import router as milk_statements_api_router
from app.api.parlour_routes import router as parlour_api_router
from app.api.events_routes import router as events_api_router
from app.api.feed_rate_routes import router as feed_rate_api_router
from app.api.feedlync_routes import router as feedlync_api_router
from app.api.xero_routes import router as xero_api_router
from app.api.stock_inventory_routes import router as stock_inventory_api_router
from app.api.cts_routes import router as cts_api_router
from app.api.admin_routes import router as admin_api_router
from app.api.herd_routes import router as herd_api_router
from app.api.hr_routes import router as hr_api_router
from app.api.prostock_routes import router as prostock_api_router
from app.api.routes import router as api_router
from app.api.schedule_routes import router as schedule_api_router
from app.api.reports_routes import router as reports_api_router
from app.api.sensehub_routes import router as sensehub_api_router
from app.auth.deps import require_admin
from app.auth.middleware import AuthMiddleware
from app.auth.passwords import verify_password
from app.auth.permissions import (
    ACTION_CTS_SYNC,
    ACTION_GENETICS_PEDIGREE,
    ACTION_GENETICS_PENDING_RESULTS,
    ACTION_HERD_IMPORT,
    ACTION_HR_ENROLL,
    ACTION_HR_VIEW_SENSITIVE,
    PAGE_EVENTS,
    PAGE_FEED_RATE,
    ACTION_BENCHMARKING_EDIT,
    ACTION_CATTLE_SALES_IMPORT,
    ACTION_MILK_QUALITY_IMPORT,
    ACTION_MILK_COLLECTIONS_IMPORT,
    ACTION_PARLOUR_IMPORT,
    PAGE_BENCHMARKING,
    PAGE_CATTLE_SALES,
    PAGE_GENETICS,
    PAGE_HR,
    PAGE_MILK_QUALITY,
    PAGE_PARLOUR,
    PAGE_SCHEDULE,
    PAGE_REPORTS,
    PAGE_SENSEHUB,
    PAGE_OFFICE_ADMIN,
    PAGE_XERO,
    PAGE_PROSTOCK,
    PAGE_BCMS,
    PAGE_STOCK_INVENTORY,
    PAGE_WYNNSTAY,
    PermissionContext,
    can_edit_sires,
    can_import_feed,
    can_import_milk_statements,
    has_action,
    has_page,
)
from app.auth.roles import ROLE_LABELS, ROLES
from app.auth.users import get_user_by_email, seed_admin_user
from app.config import COOKIE_SECURE, IS_PRODUCTION, SECRET_KEY, SESSION_MAX_AGE_SECONDS
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


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """Stop browsers serving stale HTML pages (cached markup/inline scripts)."""
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response

app.include_router(api_router)
app.include_router(prostock_api_router)
app.include_router(herd_api_router)
app.include_router(stock_inventory_api_router)
app.include_router(cts_api_router)
app.include_router(events_api_router)
app.include_router(feed_rate_api_router)
app.include_router(feedlync_api_router)
app.include_router(xero_api_router)
app.include_router(office_admin_api_router)
app.include_router(genetics_api_router)
app.include_router(nml_api_router)
app.include_router(haulier_api_router)
app.include_router(milk_statements_api_router)
app.include_router(parlour_api_router)
app.include_router(cattle_sales_api_router)
app.include_router(benchmarking_api_router)
app.include_router(admin_api_router)
app.include_router(hr_api_router)
app.include_router(schedule_api_router)
app.include_router(reports_api_router)
app.include_router(sensehub_api_router)

_WYNNSTAY_BREADCRUMB = '<a href="/">Farm Dashboard</a> &rsaquo; <a href="/wynnstay">Wynnstay</a>'
_PROSTOCK_BREADCRUMB = '<a href="/">Farm Dashboard</a> &rsaquo; <a href="/prostock">Prostock</a>'
_STOCK_INVENTORY_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/stock-inventory/heifer-inventory">Stock Inventory</a>'
)
_BCMS_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/bcms/reconcile">BCMS</a>'
)
_EVENTS_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/events/calvings">Events</a>'
)
_FEED_RATE_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/feed-rate">Feed</a>'
)
_OFFICE_ADMIN_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/office-admin/sales-payments">Office Admin</a>'
)
_XERO_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/xero">Xero</a>'
)
_HR_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/hr/staff">Staff / HR</a>'
)
_GENETICS_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/genetics/genomic-progress">Genetics</a>'
)
_MILK_QUALITY_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/milk-quality/collections">Milk Sales</a>'
)
_PARLOUR_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/parlour/shift-summary">Parlour</a>'
)
_CATTLE_SALES_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/cattle-sales">Cattle Sales</a>'
)
_SCHEDULE_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/schedule">Schedule</a>'
)
_REPORTS_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/reports">Reports</a>'
)
_SENSEHUB_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/sensehub">SenseHub</a>'
)
_BENCHMARKING_BREADCRUMB = (
    '<a href="/">Farm Dashboard</a> &rsaquo; '
    '<a href="/benchmarking/forecasts">Budgeting</a>'
)

_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300


def _template_ctx(request: Request, **extra) -> dict:
    user = getattr(request.state, "user", None)
    can_refresh_offline = (not IS_PRODUCTION) and has_action(user, ACTION_HERD_IMPORT)
    return {
        "request": request,
        "current_user": user,
        "roles": [{"id": r, "label": ROLE_LABELS[r]} for r in ROLES],
        "perms": PermissionContext(user),
        "can_import_feed": can_import_feed(user),
        "can_edit_sires": can_edit_sires(user),
        "can_refresh_offline": can_refresh_offline,
        **extra,
    }


def _page_guard(request: Request, page_key: str) -> HTMLResponse | None:
    user = getattr(request.state, "user", None)
    if has_page(user, page_key):
        return None
    return templates.TemplateResponse(
        request,
        "forbidden.html",
        _template_ctx(
            request,
            title="Access denied",
            active_nav=None,
            active_nav_group=None,
            active_section=None,
            breadcrumb=None,
        ),
        status_code=403,
    )


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


def _prostock_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _PROSTOCK_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "suppliers",
        "active_section": "prostock",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _stock_inventory_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _STOCK_INVENTORY_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "stock-inventory",
        "active_section": "stock-inventory",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _bcms_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _BCMS_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "bcms",
        "active_section": "bcms",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _events_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _EVENTS_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "events",
        "active_section": "events",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _feed_rate_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _FEED_RATE_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "feed-rate",
        "active_section": "feed-rate",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _office_admin_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _OFFICE_ADMIN_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "office-admin",
        "active_section": "office-admin",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _xero_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _XERO_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "xero",
        "active_section": "xero",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _genetics_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _GENETICS_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "genetics",
        "active_section": "genetics",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _milk_quality_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _MILK_QUALITY_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "milk-quality",
        "active_section": "milk-quality",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _parlour_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _PARLOUR_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "parlour",
        "active_section": "parlour",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _cattle_sales_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _CATTLE_SALES_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "cattle-sales",
        "active_section": "cattle-sales",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _schedule_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _SCHEDULE_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "schedule",
        "active_section": "schedule",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _reports_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _REPORTS_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "reports",
        "active_section": "reports",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _sensehub_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _SENSEHUB_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "sensehub",
        "active_section": "sensehub",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _benchmarking_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _BENCHMARKING_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "benchmarking",
        "active_section": "benchmarking",
        "active_nav": active_nav,
        "breadcrumb": breadcrumb,
    }


def _hr_context(title: str, active_nav: str, page_name: str | None = None) -> dict:
    breadcrumb = _HR_BREADCRUMB
    if page_name:
        breadcrumb += f" &rsaquo; {page_name}"
    return {
        "title": title,
        "active_nav_group": "hr",
        "active_section": "hr",
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
    if denied := _page_guard(request, PAGE_WYNNSTAY):
        return denied
    return templates.TemplateResponse(
        request,
        "wynnstay/home.html",
        _template_ctx(request, **_wynnstay_context("Import New Data", "overview")),
    )


@app.get("/wynnstay/invoices", response_class=HTMLResponse)
def invoices_page(request: Request):
    if denied := _page_guard(request, PAGE_WYNNSTAY):
        return denied
    return templates.TemplateResponse(
        request,
        "invoices.html",
        _template_ctx(request, **_wynnstay_context("Invoices", "invoices", "Invoices")),
    )


@app.get("/wynnstay/category-breakdown", response_class=HTMLResponse)
def category_breakdown_page(request: Request):
    if denied := _page_guard(request, PAGE_WYNNSTAY):
        return denied
    return templates.TemplateResponse(
        request,
        "category_breakdown.html",
        _template_ctx(request, **_wynnstay_context("Category Breakdown", "breakdown", "Category breakdown")),
    )


@app.get("/wynnstay/product-price-by-month", response_class=HTMLResponse)
def product_price_by_month_page(request: Request):
    if denied := _page_guard(request, PAGE_WYNNSTAY):
        return denied
    return templates.TemplateResponse(
        request,
        "product_price_by_month.html",
        _template_ctx(request, **_wynnstay_context("Product Prices", "price-by-month", "Product Prices")),
    )


@app.get("/wynnstay/product-quantity-by-month", response_class=HTMLResponse)
def product_quantity_by_month_page(request: Request):
    if denied := _page_guard(request, PAGE_WYNNSTAY):
        return denied
    return templates.TemplateResponse(
        request,
        "product_quantity_by_month.html",
        _template_ctx(request, **_wynnstay_context("Product Quantities", "quantity-by-month", "Product Quantities")),
    )


@app.get("/wynnstay/monthly-spend", response_class=HTMLResponse)
def monthly_spend_page(request: Request):
    if denied := _page_guard(request, PAGE_WYNNSTAY):
        return denied
    return templates.TemplateResponse(
        request,
        "monthly_spend.html",
        _template_ctx(request, **_wynnstay_context("Monthly Spend", "monthly-spend", "Monthly Spend")),
    )


@app.get("/wynnstay/mappings", response_class=HTMLResponse)
def mappings_page(request: Request):
    if denied := _page_guard(request, PAGE_WYNNSTAY):
        return denied
    return templates.TemplateResponse(
        request,
        "mappings.html",
        _template_ctx(request, **_wynnstay_context("Product Mappings", "mappings", "Product mappings")),
    )


@app.get("/prostock", response_class=HTMLResponse)
def prostock_home(request: Request):
    if denied := _page_guard(request, PAGE_PROSTOCK):
        return denied
    return templates.TemplateResponse(
        request,
        "prostock/home.html",
        _template_ctx(request, **_prostock_context("Import New Data", "overview")),
    )


@app.get("/prostock/mappings", response_class=HTMLResponse)
def prostock_mappings_page(request: Request, db: Session = Depends(get_db)):
    if denied := _page_guard(request, PAGE_PROSTOCK):
        return denied
    from app.models import SUPPLIER_PROSTOCK
    from app.services.mapping_options import list_mapping_options
    from app.services.mappings import list_mapping_rules
    from app.services.prostock_mappings import ensure_prostock_mappings_seeded

    ensure_prostock_mappings_seeded(db)
    rules = list_mapping_rules(db, supplier=SUPPLIER_PROSTOCK)
    options = list_mapping_options(db, supplier=SUPPLIER_PROSTOCK)
    return templates.TemplateResponse(
        request,
        "prostock/mappings.html",
        _template_ctx(
            request,
            initial_rules=[r.to_dict() for r in rules],
            initial_options=options,
            **_prostock_context("Product Mappings", "mappings", "Product mappings"),
        ),
    )


@app.get("/prostock/invoices", response_class=HTMLResponse)
def prostock_invoices_page(request: Request):
    if denied := _page_guard(request, PAGE_PROSTOCK):
        return denied
    from app.models import PROSTOCK_BUSINESS_OPTIONS

    return templates.TemplateResponse(
        request,
        "prostock/invoices.html",
        _template_ctx(
            request,
            business_options=list(PROSTOCK_BUSINESS_OPTIONS),
            **_prostock_context("Invoices", "invoices", "Invoices"),
        ),
    )


@app.get("/prostock/product-prices", response_class=HTMLResponse)
def prostock_product_prices_page(request: Request):
    if denied := _page_guard(request, PAGE_PROSTOCK):
        return denied
    from app.models import PROSTOCK_BUSINESS_OPTIONS

    return templates.TemplateResponse(
        request,
        "prostock/product_prices.html",
        _template_ctx(
            request,
            business_options=list(PROSTOCK_BUSINESS_OPTIONS),
            **_prostock_context("Product Prices", "product-prices", "Product Prices"),
        ),
    )


@app.get("/prostock/product-quantity", response_class=HTMLResponse)
def prostock_product_quantity_page(request: Request):
    if denied := _page_guard(request, PAGE_PROSTOCK):
        return denied
    from app.models import PROSTOCK_BUSINESS_OPTIONS

    return templates.TemplateResponse(
        request,
        "prostock/product_quantity.html",
        _template_ctx(
            request,
            business_options=list(PROSTOCK_BUSINESS_OPTIONS),
            **_prostock_context("Product Quantity", "product-quantity", "Product Quantity"),
        ),
    )


@app.get("/prostock/monthly-spend", response_class=HTMLResponse)
def prostock_monthly_spend_page(request: Request):
    if denied := _page_guard(request, PAGE_PROSTOCK):
        return denied
    from app.models import PROSTOCK_BUSINESS_OPTIONS

    return templates.TemplateResponse(
        request,
        "prostock/monthly_spend.html",
        _template_ctx(
            request,
            business_options=list(PROSTOCK_BUSINESS_OPTIONS),
            **_prostock_context("Monthly Spend", "monthly-spend", "Monthly Spend"),
        ),
    )


@app.get("/stock-inventory/cow-inventory", response_class=HTMLResponse)
def stock_inventory_cow_page(request: Request):
    if denied := _page_guard(request, PAGE_STOCK_INVENTORY):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "stock_inventory/cow_inventory.html",
        _template_ctx(
            request,
            farm_options=list(HERD_FARM_OPTIONS),
            **_stock_inventory_context(
                "Cow Inventory",
                "cow-inventory",
                "Cow Inventory",
            ),
        ),
    )


@app.get("/stock-inventory/heifer-inventory", response_class=HTMLResponse)
def stock_inventory_heifer_page(request: Request):
    if denied := _page_guard(request, PAGE_STOCK_INVENTORY):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "stock_inventory/heifer_inventory.html",
        _template_ctx(
            request,
            farm_options=list(HERD_FARM_OPTIONS),
            **_stock_inventory_context(
                "Heifer Inventory",
                "heifer-inventory",
                "Heifer Inventory",
            ),
        ),
    )


@app.get("/stock-inventory/beef-inventory", response_class=HTMLResponse)
def stock_inventory_beef_page(request: Request):
    if denied := _page_guard(request, PAGE_STOCK_INVENTORY):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "stock_inventory/beef_inventory.html",
        _template_ctx(
            request,
            farm_options=list(HERD_FARM_OPTIONS),
            **_stock_inventory_context(
                "Beef Inventory",
                "beef-inventory",
                "Beef Inventory",
            ),
        ),
    )


@app.get("/stock-inventory/calves-due", response_class=HTMLResponse)
def stock_inventory_calves_due_page(request: Request):
    if denied := _page_guard(request, PAGE_STOCK_INVENTORY):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "stock_inventory/calves_due.html",
        _template_ctx(
            request,
            farm_options=list(HERD_FARM_OPTIONS),
            **_stock_inventory_context(
                "Calves Due",
                "calves-due",
                "Calves Due",
            ),
        ),
    )


@app.get("/stock-inventory/heifers-due", response_class=HTMLResponse)
def stock_inventory_heifers_due_page(request: Request):
    if denied := _page_guard(request, PAGE_STOCK_INVENTORY):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "stock_inventory/heifers_due.html",
        _template_ctx(
            request,
            farm_options=list(HERD_FARM_OPTIONS),
            **_stock_inventory_context(
                "Heifers Due",
                "heifers-due",
                "Heifers Due",
            ),
        ),
    )


@app.get("/bcms/reconcile", response_class=HTMLResponse)
def bcms_reconcile_page(request: Request):
    if denied := _page_guard(request, PAGE_BCMS):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "bcms/cts_reconcile.html",
        _template_ctx(
            request,
            page_heading="CTS Reconcile",
            farm_options=list(HERD_FARM_OPTIONS),
            can_sync=has_action(request.state.user, ACTION_CTS_SYNC),
            can_dc305_sync=(
                has_action(request.state.user, ACTION_CTS_SYNC)
                or has_action(request.state.user, ACTION_HERD_IMPORT)
            ),
            **_bcms_context(
                "CTS Reconcile",
                "cts-reconcile",
                "CTS Reconcile",
            ),
        ),
    )


@app.get("/bcms/record-movements", response_class=HTMLResponse)
def bcms_record_movements_page(request: Request):
    if denied := _page_guard(request, PAGE_BCMS):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "bcms/record_movements.html",
        _template_ctx(
            request,
            page_heading="Record Movements",
            farm_options=list(HERD_FARM_OPTIONS),
            can_send=has_action(request.state.user, ACTION_CTS_SYNC),
            **_bcms_context(
                "Record Movements",
                "record-movements",
                "Record Movements",
            ),
        ),
    )


@app.get("/cts/reconcile", response_class=HTMLResponse)
def cts_reconcile_redirect():
    return RedirectResponse(url="/bcms/reconcile", status_code=307)


def _events_page_response(
    request: Request,
    *,
    slug: str,
    title: str,
    chart_title: str,
    show_lact_filter: bool = False,
    show_parity_filter: bool = False,
    parity_exclusive: bool = False,
    show_parity_beef: bool = False,
    parity_default_both: bool = False,
    show_disease_filter: bool = False,
    show_disease_scatter: bool = False,
    show_reason_table: bool = False,
    show_breedings_semen_chart: bool = False,
    show_breedings_semen_table: bool = False,
    show_breedings_sire_settings: bool = False,
    show_hooftrimming_charts: bool = False,
):
    from app.models import HERD_FARM_OPTIONS
    from app.services.events_common import DISEASE_EVENT_LABELS, DISEASE_FILTER_OPTIONS

    if denied := _page_guard(request, PAGE_EVENTS):
        return denied

    disease_options = []
    if show_disease_filter:
        disease_options = [
            {"value": code, "label": DISEASE_EVENT_LABELS.get(code, code)}
            for code in DISEASE_FILTER_OPTIONS
        ]

    return templates.TemplateResponse(
        request,
        "events/report.html",
        _template_ctx(
            request,
            farm_options=list(HERD_FARM_OPTIONS),
            api_slug=slug,
            page_heading=title,
            chart_title=chart_title,
            show_lact_filter=show_lact_filter,
            show_parity_filter=show_parity_filter,
            parity_exclusive=parity_exclusive,
            show_parity_beef=show_parity_beef,
            parity_default_both=parity_default_both,
            show_disease_filter=show_disease_filter,
            show_disease_scatter=show_disease_scatter,
            disease_options=disease_options,
            show_reason_table=show_reason_table,
            show_breedings_semen_chart=show_breedings_semen_chart,
            show_breedings_semen_table=show_breedings_semen_table,
            show_breedings_sire_settings=show_breedings_sire_settings,
            show_hooftrimming_charts=show_hooftrimming_charts,
            **_events_context(title, slug, title),
        ),
    )


@app.get("/events/calvings", response_class=HTMLResponse)
def events_calvings_page(request: Request):
    return _events_page_response(
        request,
        slug="calvings",
        title="Calvings",
        chart_title="Calvings by Month — Stacked by Farm",
        show_lact_filter=True,
    )


@app.get("/events/sales", response_class=HTMLResponse)
def events_sales_page(request: Request):
    return _events_page_response(
        request,
        slug="sales",
        title="Sales",
        chart_title="Sales by Month — Stacked by Farm",
        show_parity_filter=True,
        show_parity_beef=True,
        show_reason_table=True,
    )


@app.get("/events/deaths", response_class=HTMLResponse)
def events_deaths_page(request: Request):
    return _events_page_response(
        request,
        slug="deaths",
        title="Deaths",
        chart_title="Deaths by Month — Stacked by Farm",
        show_parity_filter=True,
    )


@app.get("/events/disease", response_class=HTMLResponse)
def events_disease_page(request: Request):
    return _events_page_response(
        request,
        slug="disease",
        title="Disease",
        chart_title="Disease Events by Month — Stacked by Farm",
        show_parity_filter=True,
        parity_exclusive=True,
        show_disease_filter=True,
        show_disease_scatter=True,
    )


@app.get("/events/hooftrimming", response_class=HTMLResponse)
def events_hooftrimming_page(request: Request):
    return _events_page_response(
        request,
        slug="hooftrimming",
        title="Hoof Trimming",
        chart_title="Footrim & Lame Events by Month — Stacked by Farm",
        show_hooftrimming_charts=True,
    )


@app.get("/events/breedings", response_class=HTMLResponse)
def events_breedings_page(request: Request):
    return _events_page_response(
        request,
        slug="breedings",
        title="Breedings",
        chart_title="Breedings by Month — Stacked by Farm",
        show_parity_filter=True,
        parity_default_both=True,
        show_breedings_semen_chart=True,
        show_breedings_semen_table=True,
        show_breedings_sire_settings=True,
    )


@app.get("/events/births", response_class=HTMLResponse)
def events_births_page(request: Request):
    from app.models import HERD_FARM_OPTIONS

    if denied := _page_guard(request, PAGE_EVENTS):
        return denied

    return templates.TemplateResponse(
        request,
        "events/births.html",
        _template_ctx(
            request,
            farm_options=list(HERD_FARM_OPTIONS),
            page_heading="Births",
            **_events_context("Births", "births", "Births"),
        ),
    )


@app.get("/events/total-protein", response_class=HTMLResponse)
def events_total_protein_page(request: Request):
    from app.models import HERD_FARM_OPTIONS

    if denied := _page_guard(request, PAGE_EVENTS):
        return denied

    return templates.TemplateResponse(
        request,
        "events/total_protein.html",
        _template_ctx(
            request,
            farm_options=list(HERD_FARM_OPTIONS),
            page_heading="Total Protein",
            **_events_context("Total Protein", "total-protein", "Total Protein"),
        ),
    )


@app.get("/feed-rate", response_class=HTMLResponse)
def feed_rate_page(request: Request):
    if denied := _page_guard(request, PAGE_FEED_RATE):
        return denied
    return templates.TemplateResponse(
        request,
        "feed_rate/report.html",
        _template_ctx(
            request,
            page_heading="Feed Rations",
            **_feed_rate_context("Feed Rations", "feed-rate", "Feed Rations"),
        ),
    )


@app.get("/feed-rate/contracts", response_class=HTMLResponse)
def feed_contracts_page(request: Request):
    if denied := _page_guard(request, PAGE_FEED_RATE):
        return denied
    return templates.TemplateResponse(
        request,
        "feed_rate/contracts.html",
        _template_ctx(
            request,
            page_heading="Feed Contracts",
            **_feed_rate_context("Feed Contracts", "feed-contracts", "Contracts"),
        ),
    )


@app.get("/feed-rate/connect", response_class=HTMLResponse)
def feed_rate_connect_page(request: Request):
    if denied := _page_guard(request, PAGE_FEED_RATE):
        return denied
    if not can_import_feed(request.state.user):
        return RedirectResponse("/feed-rate", status_code=302)
    return_to = request.query_params.get("return", "/feed-rate")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/feed-rate"
    return templates.TemplateResponse(
        request,
        "feed_rate/connect.html",
        _template_ctx(
            request,
            page_heading="Connect FeedLync",
            error=request.query_params.get("error"),
            return_to=return_to,
            **_feed_rate_context("Connect FeedLync", "feed-rate", "Connect FeedLync"),
        ),
    )


@app.get("/office-admin/sales-payments", response_class=HTMLResponse)
def office_admin_sales_payments_page(request: Request):
    if denied := _page_guard(request, PAGE_OFFICE_ADMIN):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "office_admin/sales_payments.html",
        _template_ctx(
            request,
            page_heading="Sales Payments",
            farm_options=list(HERD_FARM_OPTIONS),
            **_office_admin_context("Sales Payments", "sales-payments", "Sales Payments"),
        ),
    )


@app.get("/office-admin/fallen-stock", response_class=HTMLResponse)
def office_admin_fallen_stock_page(request: Request):
    if denied := _page_guard(request, PAGE_OFFICE_ADMIN):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "office_admin/fallen_stock.html",
        _template_ctx(
            request,
            page_heading="Fallen Stock",
            farm_options=list(HERD_FARM_OPTIONS),
            **_office_admin_context("Fallen Stock", "fallen-stock", "Fallen Stock"),
        ),
    )


@app.get("/office-admin/stock-valuations", response_class=HTMLResponse)
def office_admin_stock_valuations_page(request: Request):
    if denied := _page_guard(request, PAGE_OFFICE_ADMIN):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "office_admin/stock_valuations.html",
        _template_ctx(
            request,
            page_heading="Stock Valuations",
            farm_options=list(HERD_FARM_OPTIONS),
            **_office_admin_context("Stock Valuations", "stock-valuations", "Stock Valuations"),
        ),
    )


@app.get("/office-admin/stock-accruals", response_class=HTMLResponse)
def office_admin_stock_accruals_page(request: Request):
    if denied := _page_guard(request, PAGE_OFFICE_ADMIN):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "office_admin/stock_accruals.html",
        _template_ctx(
            request,
            page_heading="Stock Accruals",
            farm_options=list(HERD_FARM_OPTIONS),
            **_office_admin_context("Stock Accruals", "stock-accruals", "Stock Accruals"),
        ),
    )


@app.get("/office-admin/purchases", response_class=HTMLResponse)
def office_admin_purchases_page(request: Request):
    if denied := _page_guard(request, PAGE_OFFICE_ADMIN):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "office_admin/stock_purchases.html",
        _template_ctx(
            request,
            page_heading="Purchases",
            farm_options=list(HERD_FARM_OPTIONS),
            **_office_admin_context("Purchases", "purchases", "Purchases"),
        ),
    )


@app.get("/xero", response_class=HTMLResponse)
def xero_page(request: Request):
    if denied := _page_guard(request, PAGE_XERO):
        return denied
    from app.models import BUSINESS_OPTIONS

    return templates.TemplateResponse(
        request,
        "xero/index.html",
        _template_ctx(
            request,
            page_heading="Xero",
            business_options=list(BUSINESS_OPTIONS),
            error=request.query_params.get("error"),
            connected_flash=request.query_params.get("xero") == "connected",
            **_xero_context("Xero", "xero", "Connection"),
        ),
    )


@app.get("/xero/actual-data", response_class=HTMLResponse)
def xero_actual_data_page(request: Request):
    if denied := _page_guard(request, PAGE_XERO):
        return denied
    from app.models import BUSINESS_GROUP_OPTIONS, BUSINESS_OPTIONS
    from app.services.xero_actuals import available_actual_fiscal_years

    with SessionLocal() as db:
        fiscal_year_options = available_actual_fiscal_years(db)

    return templates.TemplateResponse(
        request,
        "xero/actual_data.html",
        _template_ctx(
            request,
            page_heading="Actual Data",
            business_options=list(BUSINESS_OPTIONS),
            business_group_options=list(BUSINESS_GROUP_OPTIONS.keys()),
            fiscal_year_options=fiscal_year_options,
            **_xero_context("Actual Data", "actual-data", "Actual Data"),
        ),
    )


@app.get("/xero/pnl", response_class=HTMLResponse)
def xero_pnl_page(request: Request):
    if denied := _page_guard(request, PAGE_XERO):
        return denied
    from app.models import BUSINESS_GROUP_OPTIONS, BUSINESS_OPTIONS
    from app.services.xero_actuals import available_actual_fiscal_years

    with SessionLocal() as db:
        fiscal_year_options = available_actual_fiscal_years(db)

    return templates.TemplateResponse(
        request,
        "xero/pnl.html",
        _template_ctx(
            request,
            page_heading="P&L",
            business_options=list(BUSINESS_OPTIONS),
            business_group_options=list(BUSINESS_GROUP_OPTIONS.keys()),
            fiscal_year_options=fiscal_year_options,
            **_xero_context("P&L", "pnl", "P&L"),
        ),
    )


@app.get("/office-admin/xero", response_class=HTMLResponse)
def office_admin_xero_redirect(request: Request):
    query = request.url.query
    target = "/xero"
    if query:
        target = f"{target}?{query}"
    return RedirectResponse(target, status_code=302)


@app.get("/genetics/pedigree-registrations", response_class=HTMLResponse)
def genetics_pedigree_registrations_page(request: Request):
    if denied := _page_guard(request, PAGE_GENETICS):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "genetics/pedigree_registrations.html",
        _template_ctx(
            request,
            page_heading="Pedigree Registrations",
            farm_options=list(HERD_FARM_OPTIONS),
            can_register=has_action(request.state.user, ACTION_GENETICS_PEDIGREE),
            **_genetics_context(
                "Pedigree Registrations",
                "pedigree-registrations",
                "Pedigree Registrations",
            ),
        ),
    )


@app.get("/genetics/bull-search", response_class=HTMLResponse)
def genetics_bull_search_page(request: Request):
    if denied := _page_guard(request, PAGE_GENETICS):
        return denied
    return templates.TemplateResponse(
        request,
        "genetics/bull_search.html",
        _template_ctx(
            request,
            page_heading="Bull Search",
            **_genetics_context("Bull Search", "bull-search", "Bull Search"),
        ),
    )


@app.get("/genetics/genomic-progress", response_class=HTMLResponse)
def genetics_genomic_progress_page(request: Request):
    if denied := _page_guard(request, PAGE_GENETICS):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "genetics/genomic_progress.html",
        _template_ctx(
            request,
            page_heading="Genomic Progress",
            farm_options=list(HERD_FARM_OPTIONS),
            **_genetics_context(
                "Genomic Progress",
                "genomic-progress",
                "Genomic Progress",
            ),
        ),
    )


@app.get("/genetics/pending-results", response_class=HTMLResponse)
def genetics_pending_results_page(request: Request):
    if denied := _page_guard(request, PAGE_GENETICS):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "genetics/pending_results.html",
        _template_ctx(
            request,
            page_heading="Pending Results",
            farm_options=list(HERD_FARM_OPTIONS),
            can_email=has_action(request.state.user, ACTION_GENETICS_PENDING_RESULTS),
            can_refresh_genomics=(
                has_action(request.state.user, ACTION_GENETICS_PENDING_RESULTS)
                or has_action(request.state.user, ACTION_HERD_IMPORT)
            ),
            **_genetics_context(
                "Pending Results",
                "pending-results",
                "Pending Results",
            ),
        ),
    )


@app.get("/genetics/sire-conflicts", response_class=HTMLResponse)
def genetics_sire_conflicts_page(request: Request):
    if denied := _page_guard(request, PAGE_GENETICS):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "genetics/sire_conflicts.html",
        _template_ctx(
            request,
            page_heading="Sire Conflicts",
            farm_options=list(HERD_FARM_OPTIONS),
            **_genetics_context(
                "Sire Conflicts",
                "sire-conflicts",
                "Sire Conflicts",
            ),
        ),
    )


@app.get("/milk-quality/results", response_class=HTMLResponse)
def milk_quality_results_page(request: Request):
    """Legacy NML page URL — redirect to Collections (which includes NML)."""
    if denied := _page_guard(request, PAGE_MILK_QUALITY):
        return denied
    return RedirectResponse(url="/milk-quality/collections", status_code=302)


@app.get("/milk-quality/collections", response_class=HTMLResponse)
def milk_quality_collections_page(request: Request):
    if denied := _page_guard(request, PAGE_MILK_QUALITY):
        return denied
    from app.config import HAULIER_LOOKBACK_DAYS, NML_LOOKBACK_DAYS
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "milk_quality/collections.html",
        _template_ctx(
            request,
            page_heading="Milk Collections",
            farm_options=list(HERD_FARM_OPTIONS),
            can_import=has_action(request.state.user, ACTION_MILK_COLLECTIONS_IMPORT),
            can_import_nml=has_action(request.state.user, ACTION_MILK_QUALITY_IMPORT),
            lookback_days=HAULIER_LOOKBACK_DAYS,
            nml_lookback_days=NML_LOOKBACK_DAYS,
            **_milk_quality_context(
                "Milk Collections",
                "haulier-collections",
                "Collections",
            ),
        ),
    )


@app.get("/milk-quality/statements", response_class=HTMLResponse)
def milk_quality_statements_page(request: Request):
    if denied := _page_guard(request, PAGE_MILK_QUALITY):
        return denied
    from app.config import STATEMENTS_LOOKBACK_DAYS
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "milk_quality/statements.html",
        _template_ctx(
            request,
            page_heading="Milk Statements",
            can_import=can_import_milk_statements(request.state.user),
            lookback_days=STATEMENTS_LOOKBACK_DAYS,
            farm_options=list(HERD_FARM_OPTIONS),
            **_milk_quality_context(
                "Milk Statements",
                "milk-statements",
                "Statements",
            ),
        ),
    )


@app.get("/parlour/shift-summary", response_class=HTMLResponse)
def parlour_shift_summary_page(request: Request):
    if denied := _page_guard(request, PAGE_PARLOUR):
        return denied
    from app.config import PARLOUR_LOOKBACK_DAYS

    return templates.TemplateResponse(
        request,
        "parlour/shift_summary.html",
        _template_ctx(
            request,
            page_heading="Shift Summary",
            can_import=has_action(request.state.user, ACTION_PARLOUR_IMPORT),
            lookback_days=PARLOUR_LOOKBACK_DAYS,
            **_parlour_context("Shift Summary", "shift-summary", "Shift Summary"),
        ),
    )


@app.get("/parlour/performance", response_class=HTMLResponse)
def parlour_performance_page(request: Request):
    if denied := _page_guard(request, PAGE_PARLOUR):
        return denied
    return RedirectResponse(url="/parlour/stall-issues", status_code=302)


@app.get("/parlour/scatter-graphs", response_class=HTMLResponse)
def parlour_scatter_graphs_page(request: Request):
    if denied := _page_guard(request, PAGE_PARLOUR):
        return denied
    return templates.TemplateResponse(
        request,
        "parlour/scatter.html",
        _template_ctx(
            request,
            page_heading="Scatter Graphs",
            **_parlour_context("Scatter Graphs", "scatter-graphs", "Scatter Graphs"),
        ),
    )


@app.get("/parlour/stall-issues", response_class=HTMLResponse)
def parlour_stall_issues_page(request: Request):
    if denied := _page_guard(request, PAGE_PARLOUR):
        return denied
    return templates.TemplateResponse(
        request,
        "parlour/stall_issues.html",
        _template_ctx(
            request,
            page_heading="Stall Issues",
            **_parlour_context("Stall Issues", "stall-issues", "Stall Issues"),
        ),
    )


@app.get("/parlour/efficiency", response_class=HTMLResponse)
def parlour_efficiency_page(request: Request):
    if denied := _page_guard(request, PAGE_PARLOUR):
        return denied
    return templates.TemplateResponse(
        request,
        "parlour/efficiency.html",
        _template_ctx(
            request,
            page_heading="Efficiency",
            **_parlour_context("Efficiency", "efficiency", "Efficiency"),
        ),
    )


@app.get("/cattle-sales", response_class=HTMLResponse)
def cattle_sales_page(request: Request):
    if denied := _page_guard(request, PAGE_CATTLE_SALES):
        return denied
    from app.config import CATTLE_SALES_LOOKBACK_DAYS
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "cattle_sales/index.html",
        _template_ctx(
            request,
            page_heading="Cattle Sales",
            can_import=has_action(request.state.user, ACTION_CATTLE_SALES_IMPORT),
            lookback_days=CATTLE_SALES_LOOKBACK_DAYS,
            farm_options=list(HERD_FARM_OPTIONS),
            **_cattle_sales_context("Cattle Sales", "cattle-sales", None),
        ),
    )


@app.get("/schedule", response_class=HTMLResponse)
def schedule_hub_page(request: Request):
    if denied := _page_guard(request, PAGE_SCHEDULE):
        return denied
    return templates.TemplateResponse(
        request,
        "schedule/index.html",
        _template_ctx(
            request,
            page_heading="Schedule",
            **_schedule_context("Schedule", "schedule", None),
        ),
    )


@app.get("/schedule/{farm}", response_class=HTMLResponse)
def schedule_farm_page(request: Request, farm: str):
    if denied := _page_guard(request, PAGE_SCHEDULE):
        return denied
    from app.services.farm_schedule import FARM_LABELS, normalize_farm

    try:
        farm_key = normalize_farm(farm)
    except ValueError:
        return RedirectResponse(url="/schedule", status_code=302)
    return templates.TemplateResponse(
        request,
        "schedule/farm.html",
        _template_ctx(
            request,
            page_heading=f"Schedule · {FARM_LABELS[farm_key]}",
            farm=farm_key,
            **_schedule_context("Schedule", "schedule", FARM_LABELS[farm_key]),
        ),
    )


@app.get("/sensehub", response_class=HTMLResponse)
def sensehub_page(request: Request):
    if denied := _page_guard(request, PAGE_SENSEHUB):
        return denied
    return templates.TemplateResponse(
        request,
        "sensehub/youngstock.html",
        _template_ctx(
            request,
            page_heading="Youngstock Health Report",
            **_sensehub_context("Youngstock Health Report", "sensehub", "Youngstock Health Report"),
        ),
    )


@app.get("/sensehub/tags-to-remove", response_class=HTMLResponse)
def sensehub_tags_to_remove_page(request: Request):
    if denied := _page_guard(request, PAGE_SENSEHUB):
        return denied
    return templates.TemplateResponse(
        request,
        "sensehub/tags_to_remove.html",
        _template_ctx(
            request,
            page_heading="Tags To Remove",
            **_sensehub_context("Tags To Remove", "sensehub-tags-to-remove", "Tags To Remove"),
        ),
    )


@app.get("/sensehub/unassigned", response_class=HTMLResponse)
def sensehub_unassigned_page(request: Request):
    if denied := _page_guard(request, PAGE_SENSEHUB):
        return denied
    return templates.TemplateResponse(
        request,
        "sensehub/unassigned.html",
        _template_ctx(
            request,
            page_heading="Calves Not Assigned",
            **_sensehub_context("Calves Not Assigned", "sensehub-unassigned", "Calves Not Assigned"),
        ),
    )


@app.get("/sensehub/reports", response_class=HTMLResponse)
def sensehub_reports_page(request: Request):
    if denied := _page_guard(request, PAGE_SENSEHUB):
        return denied
    return templates.TemplateResponse(
        request,
        "sensehub/reports.html",
        _template_ctx(
            request,
            page_heading="SenseHub reports",
            **_sensehub_context("SenseHub reports", "sensehub-reports", "Reports"),
        ),
    )


@app.get("/reports", response_class=HTMLResponse)
def reports_hub_page(request: Request):
    if denied := _page_guard(request, PAGE_REPORTS):
        return denied
    return templates.TemplateResponse(
        request,
        "reports/index.html",
        _template_ctx(
            request,
            page_heading="Reports",
            **_reports_context("Reports", "reports", None),
        ),
    )


@app.get("/reports/{farm}", response_class=HTMLResponse)
def reports_farm_page(request: Request, farm: str):
    if denied := _page_guard(request, PAGE_REPORTS):
        return denied
    from app.services.farm_schedule import FARM_LABELS, normalize_farm

    try:
        farm_key = normalize_farm(farm)
    except ValueError:
        return RedirectResponse(url="/reports", status_code=302)
    return templates.TemplateResponse(
        request,
        "reports/farm.html",
        _template_ctx(
            request,
            page_heading=f"Reports · {FARM_LABELS[farm_key]}",
            farm=farm_key,
            **_reports_context("Reports", "reports", FARM_LABELS[farm_key]),
        ),
    )


@app.get("/benchmarking/forecasts", response_class=HTMLResponse)
def benchmarking_forecasts_hub_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    return templates.TemplateResponse(
        request,
        "benchmarking/forecasts/index.html",
        _template_ctx(
            request,
            page_heading="Budgets",
            **_benchmarking_context("Budgets", "forecasts", None),
        ),
    )


@app.get("/benchmarking/forecasts/livestock", response_class=HTMLResponse)
def benchmarking_livestock_forecasts_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.services.benchmarking import available_fiscal_years

    return templates.TemplateResponse(
        request,
        "benchmarking/forecasts/livestock.html",
        _template_ctx(
            request,
            page_heading="Livestock Forecasts",
            can_edit=has_action(request.state.user, ACTION_BENCHMARKING_EDIT),
            fiscal_year_options=available_fiscal_years(),
            **_benchmarking_context(
                "Livestock Forecasts",
                "forecasts-livestock",
                "Livestock Forecasts",
            ),
        ),
    )


@app.get("/benchmarking/forecasts/financial", response_class=HTMLResponse)
def benchmarking_financial_forecasts_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.services.benchmarking import available_fiscal_years

    return templates.TemplateResponse(
        request,
        "benchmarking/forecasts/financial.html",
        _template_ctx(
            request,
            page_heading="Financial Forecasts",
            can_edit=has_action(request.state.user, ACTION_BENCHMARKING_EDIT),
            fiscal_year_options=available_fiscal_years(),
            **_benchmarking_context(
                "Financial Forecasts",
                "forecasts-financial",
                "Financial Forecasts",
            ),
        ),
    )


@app.get("/benchmarking/stock-forecasts", response_class=HTMLResponse)
def benchmarking_stock_forecasts_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.models import HERD_FARM_OPTIONS
    from app.services.benchmarking import available_fiscal_years

    return templates.TemplateResponse(
        request,
        "benchmarking/stock_forecasts.html",
        _template_ctx(
            request,
            page_heading="Stock Forecasts",
            farm_options=list(HERD_FARM_OPTIONS),
            fiscal_year_options=available_fiscal_years(),
            **_benchmarking_context("Stock Forecasts", "stock-forecasts", "Stock Forecasts"),
        ),
    )


@app.get("/benchmarking/hp-schedules", response_class=HTMLResponse)
def benchmarking_hp_schedules_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "benchmarking/hp_schedules.html",
        _template_ctx(
            request,
            page_heading="HP Schedules",
            can_edit=has_action(request.state.user, ACTION_BENCHMARKING_EDIT),
            farm_options=list(HERD_FARM_OPTIONS),
            **_benchmarking_context("HP Schedules", "hp-schedules", "HP Schedules"),
        ),
    )


@app.get("/benchmarking/standing-orders", response_class=HTMLResponse)
def benchmarking_standing_orders_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "benchmarking/standing_orders.html",
        _template_ctx(
            request,
            page_heading="Standing Orders",
            can_edit=has_action(request.state.user, ACTION_BENCHMARKING_EDIT),
            farm_options=list(HERD_FARM_OPTIONS),
            **_benchmarking_context("Standing Orders", "standing-orders", "Standing Orders"),
        ),
    )


@app.get("/benchmarking/rental-agreements", response_class=HTMLResponse)
def benchmarking_rental_agreements_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.services.benchmarking import available_fiscal_years
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "benchmarking/rental_agreements.html",
        _template_ctx(
            request,
            page_heading="Rental Agreements",
            can_edit=has_action(request.state.user, ACTION_BENCHMARKING_EDIT),
            fiscal_year_options=available_fiscal_years(),
            farm_options=list(HERD_FARM_OPTIONS),
            **_benchmarking_context(
                "Rental Agreements",
                "rental-agreements",
                "Rental Agreements",
            ),
        ),
    )


@app.get("/benchmarking/cash-requirements", response_class=HTMLResponse)
def benchmarking_cash_requirements_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "benchmarking/cash_requirements.html",
        _template_ctx(
            request,
            page_heading="Cash Requirements",
            farm_options=list(HERD_FARM_OPTIONS),
            **_benchmarking_context(
                "Cash Requirements",
                "cash-requirements",
                "Cash Requirements",
            ),
        ),
    )


@app.get("/benchmarking/feed-purchase-forecasts", response_class=HTMLResponse)
def benchmarking_feed_purchase_forecasts_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.services.benchmarking import available_fiscal_years
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "benchmarking/feed_purchase_forecasts.html",
        _template_ctx(
            request,
            page_heading="Feed Purchase Forecasts",
            fiscal_year_options=available_fiscal_years(),
            farm_options=list(HERD_FARM_OPTIONS),
            **_benchmarking_context(
                "Feed Purchase Forecasts",
                "feed-purchase-forecasts",
                "Feed Purchase Forecasts",
            ),
        ),
    )


@app.get("/benchmarking/milk-sales-forecasts", response_class=HTMLResponse)
def benchmarking_milk_sales_forecasts_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.services.benchmarking import available_fiscal_years
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "benchmarking/milk_sales_forecasts.html",
        _template_ctx(
            request,
            page_heading="Milk Sales Forecast",
            fiscal_year_options=available_fiscal_years(),
            farm_options=list(HERD_FARM_OPTIONS),
            **_benchmarking_context(
                "Milk Sales Forecast",
                "milk-sales-forecasts",
                "Milk Sales Forecast",
            ),
        ),
    )


@app.get("/benchmarking/stock-sales-purchases-forecasts", response_class=HTMLResponse)
def benchmarking_stock_sales_purchases_forecasts_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    from app.services.benchmarking import available_fiscal_years
    from app.models import HERD_FARM_OPTIONS

    return templates.TemplateResponse(
        request,
        "benchmarking/stock_sales_purchases_forecasts.html",
        _template_ctx(
            request,
            page_heading="Stock Sales / Purchases Forecast",
            fiscal_year_options=available_fiscal_years(),
            farm_options=list(HERD_FARM_OPTIONS),
            **_benchmarking_context(
                "Stock Sales / Purchases Forecast",
                "stock-sales-purchases-forecasts",
                "Stock Sales / Purchases Forecast",
            ),
        ),
    )


@app.get("/benchmarking/rations", response_class=HTMLResponse)
def benchmarking_rations_hub_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    return templates.TemplateResponse(
        request,
        "benchmarking/rations/index.html",
        _template_ctx(
            request,
            page_heading="Rations",
            **_benchmarking_context("Rations", "rations", None),
        ),
    )


@app.get("/benchmarking/rations/ingredients", response_class=HTMLResponse)
def benchmarking_rations_ingredients_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    return templates.TemplateResponse(
        request,
        "benchmarking/rations/ingredients.html",
        _template_ctx(
            request,
            page_heading="Ingredients",
            can_edit=has_action(request.state.user, ACTION_BENCHMARKING_EDIT),
            **_benchmarking_context("Rations", "rations-ingredients", "Ingredients"),
        ),
    )


@app.get("/benchmarking/rations/cm", response_class=HTMLResponse)
def benchmarking_rations_cm_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    return templates.TemplateResponse(
        request,
        "benchmarking/rations/farm_rations.html",
        _template_ctx(
            request,
            page_heading="CM Rations",
            farm_label="CM",
            farm_slug="cm",
            can_edit=has_action(request.state.user, ACTION_BENCHMARKING_EDIT),
            **_benchmarking_context("Rations", "rations-cm", "CM Rations"),
        ),
    )


@app.get("/benchmarking/rations/gad", response_class=HTMLResponse)
def benchmarking_rations_gad_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    return templates.TemplateResponse(
        request,
        "benchmarking/rations/farm_rations.html",
        _template_ctx(
            request,
            page_heading="GAD Rations",
            farm_label="GAD",
            farm_slug="gad",
            can_edit=has_action(request.state.user, ACTION_BENCHMARKING_EDIT),
            **_benchmarking_context("Rations", "rations-gad", "GAD Rations"),
        ),
    )


@app.get("/benchmarking/rations/cost-comparison", response_class=HTMLResponse)
def benchmarking_rations_cost_comparison_page(request: Request):
    if denied := _page_guard(request, PAGE_BENCHMARKING):
        return denied
    return templates.TemplateResponse(
        request,
        "benchmarking/rations/cost_comparison.html",
        _template_ctx(
            request,
            page_heading="Ration Cost Comparison",
            **_benchmarking_context(
                "Rations",
                "rations-comparison",
                "Ration Cost Comparison",
            ),
        ),
    )


@app.get("/hr/staff", response_class=HTMLResponse)
def hr_staff_directory_page(request: Request):
    if denied := _page_guard(request, PAGE_HR):
        return denied
    from app.models import HR_BUSINESS_OPTIONS

    return templates.TemplateResponse(
        request,
        "hr/directory.html",
        _template_ctx(
            request,
            page_heading="Staff Directory",
            can_enroll=has_action(request.state.user, ACTION_HR_ENROLL),
            business_options=list(HR_BUSINESS_OPTIONS),
            **_hr_context("Staff Directory", "staff-directory", "Directory"),
        ),
    )


@app.get("/hr/staff/{employee_id}", response_class=HTMLResponse)
def hr_staff_detail_page(request: Request, employee_id: int):
    if denied := _page_guard(request, PAGE_HR):
        return denied
    from app.models import DOCUMENT_TYPE_OPTIONS

    return templates.TemplateResponse(
        request,
        "hr/staff_detail.html",
        _template_ctx(
            request,
            page_heading="Staff Profile",
            employee_id=employee_id,
            can_view_sensitive=has_action(request.state.user, ACTION_HR_VIEW_SENSITIVE),
            can_enroll=has_action(request.state.user, ACTION_HR_ENROLL),
            document_types=list(DOCUMENT_TYPE_OPTIONS),
            **_hr_context("Staff Profile", "staff-directory", "Profile"),
        ),
    )


@app.get("/hr/enroll", response_class=HTMLResponse)
def hr_enroll_page(request: Request):
    if denied := _page_guard(request, PAGE_HR):
        return denied
    user = getattr(request.state, "user", None)
    if not has_action(user, ACTION_HR_ENROLL):
        return templates.TemplateResponse(
            request,
            "forbidden.html",
            _template_ctx(
                request,
                title="Access denied",
                active_nav=None,
                active_nav_group=None,
                active_section=None,
                breadcrumb=None,
            ),
            status_code=403,
        )
    from app.db import SessionLocal
    from app.models import HR_BUSINESS_OPTIONS, TITLE_OPTIONS
    from app.services.hr_service import list_job_titles

    with SessionLocal() as db:
        job_titles = list_job_titles(db)

    return templates.TemplateResponse(
        request,
        "hr/enroll.html",
        _template_ctx(
            request,
            page_heading="Enroll New Staff",
            business_options=list(HR_BUSINESS_OPTIONS),
            title_options=list(TITLE_OPTIONS),
            job_title_options=job_titles,
            edit_employee_id=None,
            can_view_sensitive=has_action(request.state.user, ACTION_HR_VIEW_SENSITIVE),
            **_hr_context("Enroll New Staff", "enroll", "Enroll"),
        ),
    )


@app.get("/hr/staff/{employee_id}/edit", response_class=HTMLResponse)
def hr_edit_staff_page(request: Request, employee_id: int):
    if denied := _page_guard(request, PAGE_HR):
        return denied
    user = getattr(request.state, "user", None)
    if not has_action(user, ACTION_HR_ENROLL):
        return templates.TemplateResponse(
            request,
            "forbidden.html",
            _template_ctx(
                request,
                title="Access denied",
                active_nav=None,
                active_nav_group=None,
                active_section=None,
                breadcrumb=None,
            ),
            status_code=403,
        )

    from app.db import SessionLocal
    from app.models import HR_BUSINESS_OPTIONS, TITLE_OPTIONS
    from app.services.hr_service import list_job_titles

    with SessionLocal() as db:
        job_titles = list_job_titles(db)

    return templates.TemplateResponse(
        request,
        "hr/enroll.html",
        _template_ctx(
            request,
            page_heading="Edit Draft Staff",
            business_options=list(HR_BUSINESS_OPTIONS),
            title_options=list(TITLE_OPTIONS),
            job_title_options=job_titles,
            edit_employee_id=employee_id,
            can_view_sensitive=has_action(user, ACTION_HR_VIEW_SENSITIVE),
            **_hr_context("Edit Draft Staff", "staff-directory", "Edit"),
        ),
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
