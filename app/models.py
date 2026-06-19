from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


BUSINESS_OPTIONS: tuple[str, ...] = ("Cwrt Malle", "Green Acre Dairy", "H&S Forage")
DEFAULT_BUSINESS = "Cwrt Malle"

SUPPLIER_WYNNSTAY = "wynnstay"
SUPPLIER_PROSTOCK = "prostock"
PROSTOCK_BUSINESS_OPTIONS: tuple[str, ...] = ("Cwrt Malle", "Green Acre Dairy")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier: Mapped[str] = mapped_column(String(32), default=SUPPLIER_WYNNSTAY, index=True)
    source_filename: Mapped[str] = mapped_column(String(255), default="")
    invoice_date: Mapped[datetime.date] = mapped_column(Date)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_dropped: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    invoice_lines: Mapped[list[InvoiceLine]] = relationship(back_populates="import_batch")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)
    supplier: Mapped[str] = mapped_column(String(32), default=SUPPLIER_WYNNSTAY, index=True)

    business: Mapped[str | None] = mapped_column(String(100), nullable=True)
    date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    farm_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    goods_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    vat: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    date_added: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    invoice_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    recent: Mapped[str | None] = mapped_column(String(10), nullable=True)
    credit: Mapped[str | None] = mapped_column(String(10), nullable=True)

    import_batch: Mapped[ImportBatch | None] = relationship(back_populates="invoice_lines")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "business": self.business,
            "date": self.date.isoformat() if self.date else None,
            "reference": self.reference,
            "product_code": self.product_code,
            "category": self.category,
            "product_description": self.product_description,
            "farm_description": self.farm_description,
            "quantity": self.quantity,
            "unit": self.unit,
            "price": self.price,
            "goods_value": self.goods_value,
            "vat": self.vat,
            "total": self.total,
            "date_added": self.date_added.isoformat() if self.date_added else None,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "recent": self.recent,
            "credit": self.credit,
        }

    @classmethod
    def from_row_dict(
        cls,
        row: dict[str, Any],
        import_batch_id: int | None = None,
        *,
        supplier: str = SUPPLIER_WYNNSTAY,
    ) -> InvoiceLine:
        return cls(
            import_batch_id=import_batch_id,
            supplier=supplier,
            business=_str_or_none(row.get("business")),
            date=row.get("date") if isinstance(row.get("date"), datetime.date) else None,
            reference=_str_or_none(row.get("reference")),
            product_code=_str_or_none(row.get("product_code")),
            category=_str_or_none(row.get("category")),
            product_description=_str_or_none(row.get("product_description")),
            farm_description=_str_or_none(row.get("farm_description")),
            quantity=_float_or_none(row.get("quantity")),
            unit=_str_or_none(row.get("unit")),
            price=_float_or_none(row.get("price")),
            goods_value=_float_or_none(row.get("goods_value")),
            vat=_float_or_none(row.get("vat")),
            total=_float_or_none(row.get("total")),
            date_added=row.get("date_added") if isinstance(row.get("date_added"), datetime.date) else None,
            invoice_date=row.get("invoice_date")
            if isinstance(row.get("invoice_date"), datetime.date)
            else None,
            recent=_str_or_none(row.get("recent")),
            credit=_str_or_none(row.get("credit")),
        )

    def apply_dict(self, row: dict[str, Any]) -> None:
        if "business" in row:
            self.business = _str_or_none(row.get("business"))
        self.date = row.get("date") if isinstance(row.get("date"), datetime.date) else self.date
        self.reference = _str_or_none(row.get("reference"))
        self.product_code = _str_or_none(row.get("product_code"))
        self.category = _str_or_none(row.get("category"))
        self.product_description = _str_or_none(row.get("product_description"))
        self.farm_description = _str_or_none(row.get("farm_description"))
        self.quantity = _float_or_none(row.get("quantity"))
        self.unit = _str_or_none(row.get("unit"))
        self.price = _float_or_none(row.get("price"))
        self.goods_value = _float_or_none(row.get("goods_value"))
        self.vat = _float_or_none(row.get("vat"))
        self.total = _float_or_none(row.get("total"))
        self.date_added = (
            row.get("date_added") if isinstance(row.get("date_added"), datetime.date) else self.date_added
        )
        self.invoice_date = (
            row.get("invoice_date")
            if isinstance(row.get("invoice_date"), datetime.date)
            else self.invoice_date
        )
        self.recent = _str_or_none(row.get("recent"))
        self.credit = _str_or_none(row.get("credit"))


class ProductMappingRule(Base):
    __tablename__ = "product_mapping_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier: Mapped[str] = mapped_column(String(32), default=SUPPLIER_WYNNSTAY, index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(255), default="")
    farm_description: Mapped[str] = mapped_column(String(255), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "keyword": self.keyword,
            "category": self.category,
            "farm_description": self.farm_description,
            "sort_order": self.sort_order,
        }


class MappingOption(Base):
    __tablename__ = "mapping_options"
    __table_args__ = (
        UniqueConstraint("supplier", "option_type", "value", name="uq_mapping_option_supplier_type_value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier: Mapped[str] = mapped_column(String(32), default=SUPPLIER_WYNNSTAY, index=True)
    option_type: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(255))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "option_type": self.option_type,
            "value": self.value,
        }


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CowEvent(Base):
    """Cow events from DCEXPORT CMEVENTS / GADEVENTS files."""

    __tablename__ = "cow_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cow_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bdat: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    fdat: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    lact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gndr: Mapped[str | None] = mapped_column(String(8), nullable=True)
    edat: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    event: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dim: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True, index=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    r: Mapped[str | None] = mapped_column(String(64), nullable=True)
    t: Mapped[str | None] = mapped_column(String(64), nullable=True)
    b: Mapped[str | None] = mapped_column(String(64), nullable=True)
    protocols: Mapped[str | None] = mapped_column(String(255), nullable=True)
    technician: Mapped[str | None] = mapped_column(String(128), nullable=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    month_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_key: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cbrd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    import_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
