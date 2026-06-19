"""Monthly spend (sum of goods value) by invoice month."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.product_month_matrix import get_product_month_matrix


def get_monthly_spend(
    db: Session,
    *,
    from_month: str,
    to_month: str,
    category: str | None = None,
    recent_only: bool = False,
    include_credit: bool = True,
) -> dict:
    return get_product_month_matrix(
        db,
        from_month=from_month,
        to_month=to_month,
        category=category,
        recent_only=recent_only,
        include_credit=include_credit,
        value_field="goods_value",
        values_key="spend",
    )
