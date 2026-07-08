"""Benchmarking forecast tables available as financial forecast data sources."""

from __future__ import annotations

from typing import Any

from app.services.benchmarking import BENCHMARK_METRICS
from app.services.feed_purchase_forecasts import LINE_ITEM_KEYS
from app.services.stock_sales_purchases_forecasts import FORECAST_METRICS

FINANCIAL_DATA_SOURCE_KEYS: frozenset[str] = frozenset()  # populated below


def _metric_label(metric: str) -> str:
    return str(BENCHMARK_METRICS.get(metric, {}).get("label", metric))


def _build_source_registry() -> tuple[list[dict[str, Any]], frozenset[str]]:
    pages: list[dict[str, Any]] = []
    keys: set[str] = set()

    def add(page: str, page_key: str, sources: list[tuple[str, str]]) -> None:
        items = [{"key": key, "label": label} for key, label in sources]
        for item in items:
            keys.add(item["key"])
        pages.append({"page": page, "page_key": page_key, "sources": items})

    add(
        "Milk Sales Forecast",
        "milk_sales",
        [
            ("milk_sales.monthly_litres", "Monthly litres"),
            ("milk_sales.daily_litres", "Daily litres"),
            ("milk_sales.monthly_revenue", "Monthly revenue (£)"),
        ],
    )

    stock_sources: list[tuple[str, str]] = [
        (f"stock_sales_purchases.{metric}", _metric_label(metric))
        for metric in FORECAST_METRICS
    ]
    stock_sources.extend(
        [
            ("stock_sales_purchases.sales_total", "Sales total (£)"),
            ("stock_sales_purchases.purchases_total", "Purchases total (£)"),
        ]
    )
    add("Stock Sales / Purchases", "stock_sales_purchases", stock_sources)

    feed_sources: list[tuple[str, str]] = [
        ("feed_purchases.concentrate_dairy", "Concentrates — dairy"),
        ("feed_purchases.concentrate_youngstock", "Concentrates — youngstock"),
        ("feed_purchases.straw", "Straw"),
        ("feed_purchases.forage", "Forage"),
    ]
    detail_labels = {
        "milkers": "Milkers",
        "far_off": "Far off",
        "close_up": "Close up",
        "calf": "Calf",
        "pre_bullers": "Pre-bullers",
        "bullers": "Bullers",
        "pregnant_heifers": "Pregnant heifers",
    }
    for line_key in LINE_ITEM_KEYS:
        label = detail_labels.get(line_key, line_key.replace("_", " ").title())
        feed_sources.append((f"feed_purchases.detail.{line_key}", f"Line item — {label}"))
    add("Feed Purchase Forecasts", "feed_purchases", feed_sources)

    return pages, frozenset(keys)


FINANCIAL_DATA_SOURCE_PAGES, FINANCIAL_DATA_SOURCE_KEYS = _build_source_registry()


def list_financial_data_sources() -> dict[str, Any]:
    return {"pages": FINANCIAL_DATA_SOURCE_PAGES}


def validate_data_source_keys(source_keys: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in source_keys:
        key = raw.strip()
        if not key or key in seen:
            continue
        if key not in FINANCIAL_DATA_SOURCE_KEYS:
            raise ValueError(f"Unknown data source: {key}")
        seen.add(key)
        normalized.append(key)
    return normalized
