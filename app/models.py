from __future__ import annotations

import datetime
import re
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, Time, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


BUSINESS_OPTIONS: tuple[str, ...] = ("Cwrt Malle", "Green Acre Dairy", "H&S Forage")
DEFAULT_BUSINESS = "Cwrt Malle"
# Named groups for consolidated Actual Data / reporting views.
BUSINESS_GROUP_OPTIONS: dict[str, tuple[str, ...]] = {
    "Cwrt Malle + H&S Forage": ("Cwrt Malle", "H&S Forage"),
}

SUPPLIER_WYNNSTAY = "wynnstay"
SUPPLIER_PROSTOCK = "prostock"
PROSTOCK_BUSINESS_OPTIONS: tuple[str, ...] = ("Cwrt Malle", "Green Acre Dairy")
HERD_FARM_OPTIONS: tuple[str, ...] = ("CM", "GAD")


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
    role: Mapped[str] = mapped_column(String(32), default="user")
    permissions: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    def to_dict(self) -> dict[str, Any]:
        from app.auth.permissions import parse_permissions

        return {
            "id": self.id,
            "email": self.email,
            "role": self.role,
            "permissions": parse_permissions(self.permissions),
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BreedingSireClassification(Base):
    """Manual beef/dairy classification for breeding sires without .b/.s suffix."""

    __tablename__ = "breeding_sire_classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sire_code: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    semen_type: Mapped[str] = mapped_column(String(16))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sire_code": self.sire_code,
            "semen_type": self.semen_type,
        }


class SalesPaymentRecord(Base):
    """Tracks confirmed payment for sold animals; survives herd event reimports."""

    __tablename__ = "sales_payment_records"
    __table_args__ = (
        UniqueConstraint(
            "farm",
            "cow_id",
            "etag",
            "event_date",
            name="uq_sales_payment_natural_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    cow_id: Mapped[str] = mapped_column(String(64), index=True)
    etag: Mapped[str] = mapped_column(String(64), index=True)
    event_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    paid_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    unarchived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "farm": self.farm,
            "cow_id": self.cow_id,
            "etag": self.etag,
            "event_date": self.event_date.isoformat(),
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "unarchived_at": self.unarchived_at.isoformat() if self.unarchived_at else None,
            "confirmed_by_user_id": self.confirmed_by_user_id,
        }


class FallenStockRecord(Base):
    """Tracks confirmed collection for dead animals; survives herd event reimports."""

    __tablename__ = "fallen_stock_records"
    __table_args__ = (
        UniqueConstraint(
            "farm",
            "cow_id",
            "etag",
            "event_date",
            name="uq_fallen_stock_natural_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    cow_id: Mapped[str] = mapped_column(String(64), index=True)
    etag: Mapped[str] = mapped_column(String(64), index=True)
    event_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    collected_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    unarchived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "farm": self.farm,
            "cow_id": self.cow_id,
            "etag": self.etag,
            "event_date": self.event_date.isoformat(),
            "collected_at": self.collected_at.isoformat() if self.collected_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "unarchived_at": self.unarchived_at.isoformat() if self.unarchived_at else None,
            "confirmed_by_user_id": self.confirmed_by_user_id,
        }


STOCK_GROUP_COWS = "cows"
STOCK_GROUP_YOUNGSTOCK = "youngstock"
STOCK_GROUP_BEEF = "beef"
STOCK_GROUP_OPTIONS: tuple[str, ...] = (
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    STOCK_GROUP_BEEF,
)
PURCHASE_STOCK_GROUP_OPTIONS: tuple[str, ...] = (
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    STOCK_GROUP_BEEF,
)


class StockOpeningBaseline(Base):
    """Opening stock count for a farm/group from a specific month onward."""

    __tablename__ = "stock_opening_baselines"
    __table_args__ = (
        UniqueConstraint(
            "farm",
            "stock_group",
            name="uq_stock_opening_baseline_farm_group",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    stock_group: Mapped[str] = mapped_column(String(16), index=True)
    month_start: Mapped[datetime.date] = mapped_column(Date, index=True)
    opening_count: Mapped[int] = mapped_column(Integer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "farm": self.farm,
            "stock_group": self.stock_group,
            "month_start": self.month_start.isoformat(),
            "opening_count": self.opening_count,
        }


class StockValuationSnapshot(Base):
    """Pre-computed month-end stock valuations per farm (rebuilt on herd import)."""

    __tablename__ = "stock_valuation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "anchor_import_timestamp",
            "farm",
            "month_start",
            name="uq_stock_valuation_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    anchor_import_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, index=True
    )
    farm: Mapped[str] = mapped_column(String(8), index=True)
    month_start: Mapped[datetime.date] = mapped_column(Date, index=True)
    close_date: Mapped[datetime.date] = mapped_column(Date)
    dairy_cows: Mapped[int] = mapped_column(Integer, default=0)
    beef_count: Mapped[int] = mapped_column(Integer, default=0)
    beef_value_gbp: Mapped[float] = mapped_column(Float, default=0)
    beef_aged_sum: Mapped[int] = mapped_column(Integer, default=0)
    beef_lact_sum: Mapped[float] = mapped_column(Float, default=0)
    beef_lact_count: Mapped[int] = mapped_column(Integer, default=0)
    dairy_count: Mapped[int] = mapped_column(Integer, default=0)
    dairy_value_gbp: Mapped[float] = mapped_column(Float, default=0)
    dairy_aged_sum: Mapped[int] = mapped_column(Integer, default=0)
    dairy_lact_sum: Mapped[float] = mapped_column(Float, default=0)
    dairy_lact_count: Mapped[int] = mapped_column(Integer, default=0)
    youngstock_count: Mapped[int] = mapped_column(Integer, default=0)
    youngstock_value_gbp: Mapped[float] = mapped_column(Float, default=0)
    youngstock_aged_sum: Mapped[int] = mapped_column(Integer, default=0)
    youngstock_lact_sum: Mapped[float] = mapped_column(Float, default=0)
    youngstock_lact_count: Mapped[int] = mapped_column(Integer, default=0)


class StockAccrualSnapshot(Base):
    """Pre-computed monthly stock accrual rows per farm (rebuilt on herd import).

    Actual movement months only; projected stock forecast rows always use live
    manual forecast data at read time.
    """

    __tablename__ = "stock_accrual_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "anchor_import_timestamp",
            "farm",
            "stock_group",
            "month_start",
            name="uq_stock_accrual_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    anchor_import_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, index=True
    )
    farm: Mapped[str] = mapped_column(String(8), index=True)
    stock_group: Mapped[str] = mapped_column(String(16), index=True)
    month_start: Mapped[datetime.date] = mapped_column(Date, index=True)
    opening_count: Mapped[int] = mapped_column(Integer, default=0)
    sales: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    sales_total: Mapped[int] = mapped_column(Integer, default=0)
    deaths: Mapped[int] = mapped_column(Integer, default=0)
    births: Mapped[int] = mapped_column(Integer, default=0)
    calvings: Mapped[int] = mapped_column(Integer, default=0)
    purchases: Mapped[int] = mapped_column(Integer, default=0)
    closing_count: Mapped[int] = mapped_column(Integer, default=0)
    warning: Mapped[bool] = mapped_column(Boolean, default=False)


class StockPurchaseAnimal(Base):
    """Purchased animals derived from cow events (EDAT != BDAT), rebuilt on herd import."""

    __tablename__ = "stock_purchase_animals"
    __table_args__ = (
        UniqueConstraint("farm", "etag", name="uq_stock_purchase_animal_farm_etag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    etag: Mapped[str] = mapped_column(String(64), index=True)
    edat: Mapped[datetime.date] = mapped_column(Date, index=True)
    bdat: Mapped[datetime.date] = mapped_column(Date)
    lact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cbrd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gndr: Mapped[str | None] = mapped_column(String(8), nullable=True)
    stock_group: Mapped[str] = mapped_column(String(16), index=True)
    import_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "farm": self.farm,
            "etag": self.etag,
            "edat": self.edat.isoformat(),
            "bdat": self.bdat.isoformat(),
            "lact": self.lact,
            "cbrd": self.cbrd,
            "gndr": self.gndr,
            "stock_group": self.stock_group,
            "import_timestamp": self.import_timestamp.isoformat()
            if self.import_timestamp
            else None,
        }


class FeedRateRecord(Base):
    """Latest feed ration snapshot imported from Feedlync."""

    __tablename__ = "feed_rate_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ration_name: Mapped[str] = mapped_column(String(255), index=True)
    group_name: Mapped[str] = mapped_column(String(255), index=True)
    cow_count: Mapped[float | None] = mapped_column(Float, nullable=True)
    feed_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_fresh: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_dm: Mapped[float | None] = mapped_column(Float, nullable=True)
    dm_kg_per_cow: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    scraped_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    import_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ration_name": self.ration_name,
            "group_name": self.group_name,
            "cow_count": self.cow_count,
            "feed_percent": self.feed_percent,
            "total_fresh": self.total_fresh,
            "total_dm": self.total_dm,
            "dm_kg_per_cow": self.dm_kg_per_cow,
            "cost": self.cost,
            "scraped_date": self.scraped_date.isoformat() if self.scraped_date else None,
            "import_timestamp": (
                self.import_timestamp.isoformat() if self.import_timestamp else None
            ),
        }


class SenseHubReportSnapshot(Base):
    """Latest SenseHub report snapshot imported from st.scrdairy.com."""

    __tablename__ = "sensehub_report_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_key: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    report_name: Mapped[str] = mapped_column(String(128), index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(128))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    fetched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "report_key": self.report_key,
            "report_name": self.report_name,
            "category": self.category,
            "title": self.title,
            "row_count": self.row_count,
            "payload": self.payload or {},
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


class SenseHubYoungstockHealth(Base):
    """Health-index snapshot for one animal at one SenseHub sample slot."""

    __tablename__ = "sensehub_youngstock_health"
    __table_args__ = (
        UniqueConstraint("animal_id", "sampled_at", name="uq_sensehub_ys_animal_sampled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    animal_id: Mapped[str] = mapped_column(String(16), index=True)
    raw_animal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sampled_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    slot: Mapped[str] = mapped_column(String(16), index=True)
    health_index: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rumination: Mapped[float | None] = mapped_column(Float, nullable=True)
    eating: Mapped[float | None] = mapped_column(Float, nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "animal_id": self.animal_id,
            "raw_animal_id": self.raw_animal_id,
            "sampled_at": self.sampled_at.isoformat() if self.sampled_at else None,
            "slot": self.slot,
            "health_index": self.health_index,
            "age_days": self.age_days,
            "rumination": self.rumination,
            "eating": self.eating,
            "group_name": self.group_name,
        }


class SenseHubCalfAssignment(Base):
    """Saved SCR / SenseHub tag for a DairyComp calf, ready for a later send."""

    __tablename__ = "sensehub_calf_assignments"
    __table_args__ = (
        UniqueConstraint("row_key", name="uq_sensehub_calf_assignment_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    row_key: Mapped[str] = mapped_column(String(128), index=True)
    farm: Mapped[str | None] = mapped_column(String(8), nullable=True)
    cow_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scr_tag: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class FeedContract(Base):
    """Purchased feed contract / delivery agreement."""

    __tablename__ = "feed_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    delivery_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    product: Mapped[str] = mapped_column(String(128), index=True)
    product_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tonnage: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    supplier: Mapped[str] = mapped_column(String(128), index=True)
    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "purchase_date": self.purchase_date.isoformat(),
            "delivery_date": self.delivery_date.isoformat(),
            "product": self.product,
            "product_type": self.product_type,
            "tonnage": self.tonnage,
            "price": self.price,
            "supplier": self.supplier,
            "source_file": self.source_file,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class FeedlyncAuth(Base):
    """Stored Feedlync OAuth refresh token (singleton row id=1)."""

    __tablename__ = "feedlync_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    refresh_token: Mapped[str] = mapped_column(Text)
    connected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    connected_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )


class XeroAuth(Base):
    """Stored Xero OAuth tokens (singleton row id=1)."""

    __tablename__ = "xero_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    refresh_token: Mapped[str] = mapped_column(Text)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    connected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    connected_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )


class XeroOrganisation(Base):
    """Xero tenant (organisation) linked after OAuth, mapped to a dashboard business."""

    __tablename__ = "xero_organisations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), default="")
    tenant_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dashboard_business: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    connected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class XeroInvoice(Base):
    """Xero sales invoice (ACCREC) or bill (ACCPAY) header."""

    __tablename__ = "xero_invoices"
    __table_args__ = (UniqueConstraint("tenant_id", "invoice_id", name="uq_xero_invoice"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    invoice_id: Mapped[str] = mapped_column(String(64), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_type: Mapped[str] = mapped_column(String(16), index=True)  # ACCREC / ACCPAY
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Exclusive / Inclusive / NoTax — Inclusive LineAmount includes VAT.
    line_amount_types: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    invoice_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True, index=True)
    due_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    sub_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_tax: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_due: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_paid: Mapped[float | None] = mapped_column(Float, nullable=True)
    dashboard_business: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    xero_updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    lines: Mapped[list[XeroInvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class XeroInvoiceLine(Base):
    """Line item from a Xero invoice/bill — account codes drive budget vs actual."""

    __tablename__ = "xero_invoice_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "line_item_id", name="uq_xero_invoice_line"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_pk: Mapped[int] = mapped_column(
        ForeignKey("xero_invoices.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    line_item_id: Mapped[str] = mapped_column(String(64), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    line_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    account_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tax_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    invoice: Mapped[XeroInvoice] = relationship(back_populates="lines")


class XeroAccount(Base):
    """Xero chart of accounts — maps account codes to category names."""

    __tablename__ = "xero_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", name="uq_xero_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    account_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class XeroAccountBudgetMapping(Base):
    """Maps a Xero account (per tenant) to a financial budget heading."""

    __tablename__ = "xero_account_budget_mappings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            name="uq_xero_account_budget_mapping",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    account_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("financial_forecast_mappings.id", ondelete="CASCADE"),
        index=True,
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class XeroManualJournal(Base):
    """Xero manual journal header (posted journals feed Actual Data)."""

    __tablename__ = "xero_manual_journals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "manual_journal_id", name="uq_xero_manual_journal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    manual_journal_id: Mapped[str] = mapped_column(String(64), index=True)
    narration: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    journal_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True, index=True)
    dashboard_business: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    xero_updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    lines: Mapped[list[XeroManualJournalLine]] = relationship(
        back_populates="journal", cascade="all, delete-orphan"
    )


class XeroManualJournalLine(Base):
    """Line on a Xero manual journal."""

    __tablename__ = "xero_manual_journal_lines"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "manual_journal_id",
            "line_index",
            name="uq_xero_manual_journal_line",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    journal_pk: Mapped[int] = mapped_column(
        ForeignKey("xero_manual_journals.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    manual_journal_id: Mapped[str] = mapped_column(String(64), index=True)
    line_index: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    line_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    account_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tax_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    journal: Mapped[XeroManualJournal] = relationship(back_populates="lines")


class XeroBankTransaction(Base):
    """Xero Spend Money / Receive Money (bank transactions) — fills P&L gaps bills miss."""

    __tablename__ = "xero_bank_transactions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "bank_transaction_id",
            name="uq_xero_bank_transaction",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    bank_transaction_id: Mapped[str] = mapped_column(String(64), index=True)
    transaction_type: Mapped[str] = mapped_column(String(32), index=True)  # SPEND / RECEIVE / …
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Exclusive / Inclusive / NoTax — Inclusive LineAmount includes VAT.
    line_amount_types: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    transaction_date: Mapped[datetime.date | None] = mapped_column(
        Date, nullable=True, index=True
    )
    sub_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_tax: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_reconciled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dashboard_business: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    xero_updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    lines: Mapped[list[XeroBankTransactionLine]] = relationship(
        back_populates="bank_transaction", cascade="all, delete-orphan"
    )


class XeroBankTransactionLine(Base):
    """Line on a Xero bank transaction (coded to chart accounts)."""

    __tablename__ = "xero_bank_transaction_lines"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "bank_transaction_id",
            "line_index",
            name="uq_xero_bank_transaction_line",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bank_transaction_pk: Mapped[int] = mapped_column(
        ForeignKey("xero_bank_transactions.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    bank_transaction_id: Mapped[str] = mapped_column(String(64), index=True)
    line_index: Mapped[int] = mapped_column(Integer)
    line_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    line_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    account_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tax_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    bank_transaction: Mapped[XeroBankTransaction] = relationship(back_populates="lines")


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
    dest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    month_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_key: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cbrd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    import_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class HerdInventory(Base):
    """Current herd inventory from DCEXPORT CMINV / GADINV files."""

    __tablename__ = "herd_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cow_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bdat: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    edat: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    cbrd: Mapped[float | None] = mapped_column(Float, nullable=True)
    sbrd: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fdat: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    dim: Mapped[float | None] = mapped_column(Float, nullable=True)
    lact: Mapped[float | None] = mapped_column(Float, nullable=True)
    hdat: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    dslh: Mapped[float | None] = mapped_column(Float, nullable=True)
    rc: Mapped[float | None] = mapped_column(Float, nullable=True)
    rpro: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pen: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tbrd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ewgt: Mapped[float | None] = mapped_column(Float, nullable=True)
    httag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rum: Mapped[float | None] = mapped_column(Float, nullable=True)
    dcc: Mapped[float | None] = mapped_column(Float, nullable=True)
    due: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    lsir: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sirc: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lsbrd: Mapped[str | None] = mapped_column(String(16), nullable=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    aged: Mapped[int | None] = mapped_column(Integer, nullable=True)
    months_old: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    expected_due: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    fiscal_year_due: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_key: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_month: Mapped[str | None] = mapped_column(String(16), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    ped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dreg: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sreg: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gtest: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    subd: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    import_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class PedigreeRegistrationRecord(Base):
    """Persistent pedigree flags per animal; survives herd_inventory full-replace imports."""

    __tablename__ = "pedigree_registration_records"
    __table_args__ = (
        UniqueConstraint("farm", "etag", name="uq_pedigree_registration_farm_etag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    etag: Mapped[str] = mapped_column(String(64), index=True)
    cow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dreg: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sreg: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    registered_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    registered_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    emailed_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    emailed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AppSetting(Base):
    """Simple key-value application settings."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class GenomicResult(Base):
    """Genomic evaluation traits from DCEXPORTCM/genomicresults.xlsx (keyed by HBN)."""

    __tablename__ = "genomic_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hbn: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    eartag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sire_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sire_reg: Mapped[str | None] = mapped_column(String(64), nullable=True)
    milk_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    pli: Mapped[float | None] = mapped_column(Float, nullable=True)
    cci: Mapped[float | None] = mapped_column(Float, nullable=True)
    fertility_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    scc: Mapped[float | None] = mapped_column(Float, nullable=True)
    life_span: Mapped[float | None] = mapped_column(Float, nullable=True)
    mastitis: Mapped[float | None] = mapped_column(Float, nullable=True)
    milking_speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    type_merit: Mapped[float | None] = mapped_column(Float, nullable=True)
    mammary: Mapped[float | None] = mapped_column(Float, nullable=True)
    legs_and_feet: Mapped[float | None] = mapped_column(Float, nullable=True)
    stature: Mapped[float | None] = mapped_column(Float, nullable=True)
    chest_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    body_depth: Mapped[float | None] = mapped_column(Float, nullable=True)
    mature_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AhdbBull(Base):
    """AHDB Holstein bull proofs (genomic, marketed proven, and top international)."""

    __tablename__ = "ahdb_bulls"
    __table_args__ = (
        UniqueConstraint("hbn", name="uq_ahdb_bull_hbn"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    list_type: Mapped[str] = mapped_column(String(16), index=True)
    hbn: Mapped[str] = mapped_column(String(32), index=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bull_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bull_name_full: Mapped[str | None] = mapped_column(String(512), nullable=True)
    breed_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pli: Mapped[float | None] = mapped_column(Float, nullable=True)
    pli_reliability: Mapped[float | None] = mapped_column(Float, nullable=True)
    milk_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    healthycow: Mapped[float | None] = mapped_column(Float, nullable=True)
    envirocow: Mapped[float | None] = mapped_column(Float, nullable=True)
    fertility_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    calf_survival: Mapped[float | None] = mapped_column(Float, nullable=True)
    lifespan_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    scc: Mapped[float | None] = mapped_column(Float, nullable=True)
    mastitis: Mapped[float | None] = mapped_column(Float, nullable=True)
    lameness: Mapped[float | None] = mapped_column(Float, nullable=True)
    digital_dermatitis: Mapped[float | None] = mapped_column(Float, nullable=True)
    gestation_length: Mapped[float | None] = mapped_column(Float, nullable=True)
    dairy_carcass_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    maintenance: Mapped[float | None] = mapped_column(Float, nullable=True)
    feed_advantage: Mapped[float | None] = mapped_column(Float, nullable=True)
    direct_ce: Mapped[float | None] = mapped_column(Float, nullable=True)
    maternal_ce: Mapped[float | None] = mapped_column(Float, nullable=True)
    tb_advantage: Mapped[float | None] = mapped_column(Float, nullable=True)
    legs: Mapped[float | None] = mapped_column(Float, nullable=True)
    udder: Mapped[float | None] = mapped_column(Float, nullable=True)
    type_merit: Mapped[float | None] = mapped_column(Float, nullable=True)
    supplier_gb: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supplier_ni: Mapped[str | None] = mapped_column(String(64), nullable=True)
    genomic_indicator: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sexed_gb: Mapped[str | None] = mapped_column(String(16), nullable=True)
    uk_proven: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sire_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    grandsire_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supplier_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "list_type": "proven" if self.list_type != "genomic" else "genomic",
            "proof": "G" if self.list_type == "genomic" else "P",
            "list_label": "G" if self.list_type == "genomic" else "P",
            "hbn": self.hbn,
            "rank": self.rank,
            "bull_name": _clean_bull_name(self.bull_name),
            "bull_name_full": self.bull_name_full,
            "breed_code": self.breed_code,
            "pli": self.pli,
            "pli_reliability": self.pli_reliability,
            "milk_kg": self.milk_kg,
            "fat_kg": self.fat_kg,
            "protein_kg": self.protein_kg,
            "fat_pct": self.fat_pct,
            "protein_pct": self.protein_pct,
            "healthycow": self.healthycow,
            "envirocow": self.envirocow,
            "fertility_index": self.fertility_index,
            "calf_survival": self.calf_survival,
            "lifespan_days": self.lifespan_days,
            "scc": self.scc,
            "mastitis": self.mastitis,
            "lameness": self.lameness,
            "digital_dermatitis": self.digital_dermatitis,
            "gestation_length": self.gestation_length,
            "dairy_carcass_index": self.dairy_carcass_index,
            "maintenance": self.maintenance,
            "feed_advantage": self.feed_advantage,
            "direct_ce": self.direct_ce,
            "maternal_ce": self.maternal_ce,
            "tb_advantage": self.tb_advantage,
            "legs": self.legs,
            "udder": self.udder,
            "type_merit": self.type_merit,
            "supplier_gb": self.supplier_gb,
            "supplier_ni": self.supplier_ni,
            "genomic_indicator": self.genomic_indicator,
            "sexed_gb": self.sexed_gb,
            "uk_proven": self.uk_proven,
            "sire_name": self.sire_name,
            "grandsire_name": self.grandsire_name,
            "supplier_url": self.supplier_url,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


class NmlMilkResult(Base):
    """Per-collection milk quality results from NML report PDFs (emailed daily).

    Keyed by (producer_ref, sample_date, sample_id). The sample_id is stored as
    text to preserve leading zeros (e.g. '003'); it links to the milk haulier
    database (volumes, collection times) on sample_id + sample_date (+/- 1 day).
    """

    __tablename__ = "nml_milk_results"
    __table_args__ = (
        UniqueConstraint(
            "producer_ref", "sample_date", "sample_id", name="uq_nml_producer_sample"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    producer_ref: Mapped[str] = mapped_column(String(32), index=True)
    milk_buyer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    report_month: Mapped[str | None] = mapped_column(String(16), nullable=True)
    report_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    sample_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    sample_id: Mapped[str] = mapped_column(String(16))
    butterfat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    scc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bactoscan: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fpd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    antibiotic_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    urea_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(256), nullable=True)
    imported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MilkCollection(Base):
    """Per-load milk collection records from the haulier's emailed XLSX report.

    Keyed by (farm, collection_date, sample_id). The sample_id matches the NML
    sample number for the same load, linking collection logistics (volume, times,
    temperature) to milk quality on sample_id + date (+/- 1 day). Sample IDs are
    stored as text exactly as the haulier writes them (e.g. '026'); matching to
    NML normalises leading zeros.
    """

    __tablename__ = "milk_collections"
    __table_args__ = (
        UniqueConstraint(
            "farm", "collection_date", "sample_id", name="uq_collection_farm_date_sample"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    collection_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    # Blank when the haulier omits a sample number; stored as NULL so several
    # sample-less loads can share a day (NULLs are distinct in the constraint).
    sample_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    driver: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vehicle_reg: Mapped[str | None] = mapped_column(String(16), nullable=True)
    arrival_time: Mapped[datetime.time | None] = mapped_column(Time, nullable=True)
    depart_time: Mapped[datetime.time | None] = mapped_column(Time, nullable=True)
    volume_litres: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_raw: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Day-level herd size; stored on each load row for that collection day.
    cows_in_milk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # When the source email was received; used to keep the newest email's data
    # when the haulier re-dates a load across reports.
    source_received: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    imported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CattleSaleLine(Base):
    """Per-animal line from cattle-sale remittance PDFs (Eurofarm / Pathway / Buitelaar / Game Changer)."""

    __tablename__ = "cattle_sale_lines"
    __table_args__ = (
        UniqueConstraint(
            "farm",
            "etag",
            "sale_date",
            name="uq_cattle_sale_farm_etag_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    etag: Mapped[str] = mapped_column(String(64), index=True)
    sale_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    cold_weight_kg: Mapped[float] = mapped_column(Float)
    reject_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    kill_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    amount_gbp: Mapped[float] = mapped_column(Float)
    buyer: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_received: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    imported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MilkStatement(Base):
    """Confirmed monthly milk sales from buyer payment statements (emailed PDFs).

    One row per farm per calendar month. Figures are the buyer's final statement
    values (litres sold, quality averages, milk price). CM price is stored net
    of haulage.
    """

    __tablename__ = "milk_statements"
    __table_args__ = (
        UniqueConstraint(
            "farm", "statement_month", name="uq_milk_statement_farm_month"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    statement_month: Mapped[datetime.date] = mapped_column(Date, index=True)
    supplier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    litres_sold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    milk_price_ppl: Mapped[float | None] = mapped_column(Float, nullable=True)
    haulage_ppl: Mapped[float | None] = mapped_column(Float, nullable=True)
    butterfat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    scc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bactoscan: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thermoduric: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fpd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_received: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    imported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ParlourMilkFlowImport(Base):
    """One imported milk-flow shift report (usually emailed after each milking)."""

    __tablename__ = "parlour_milk_flow_imports"
    __table_args__ = (
        UniqueConstraint(
            "farm",
            "milking_date",
            "shift",
            name="uq_parlour_milk_flow_import_farm_date_shift",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    milking_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    shift: Mapped[str] = mapped_column(String(32), index=True)
    source_filename: Mapped[str] = mapped_column(String(255), default="")
    source_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_received: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    rows: Mapped[list[ParlourMilkFlowRow]] = relationship(
        back_populates="import_batch", cascade="all, delete-orphan"
    )


class ParlourMilkFlowRow(Base):
    """One cow milking from a parlour milk-flow report."""

    __tablename__ = "parlour_milk_flow_rows"
    __table_args__ = (
        UniqueConstraint(
            "import_id",
            "cow_id",
            "milking_point",
            "start_seconds",
            name="uq_parlour_milk_flow_row_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("parlour_milk_flow_imports.id", ondelete="CASCADE"), index=True
    )
    farm: Mapped[str] = mapped_column(String(8), index=True)
    milking_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    shift: Mapped[str] = mapped_column(String(32), index=True)
    cow_id: Mapped[str] = mapped_column(String(64), index=True)
    pen: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    milking_point: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yield_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_to_peak_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flow_15s: Mapped[float | None] = mapped_column(Float, nullable=True)
    flow_30s: Mapped[float | None] = mapped_column(Float, nullable=True)
    flow_60s: Mapped[float | None] = mapped_column(Float, nullable=True)
    flow_120s: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_2_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    milk_yield_2_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    flow_rate_at_removal: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    identification_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lag_phase_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    identified_at_milking: Mapped[str | None] = mapped_column(String(16), nullable=True)
    final_detaching: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra_attachments: Mapped[str | None] = mapped_column(String(16), nullable=True)

    import_batch: Mapped[ParlourMilkFlowImport] = relationship(back_populates="rows")


class ParlourRotaryEntryIdImport(Base):
    """One imported Rotary Entry ID attachment batch."""

    __tablename__ = "parlour_rotary_entry_id_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    source_filename: Mapped[str] = mapped_column(String(255), default="")
    source_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_received: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    events: Mapped[list[ParlourRotaryEntryIdEvent]] = relationship(
        back_populates="import_batch", cascade="all, delete-orphan"
    )


class ParlourRotaryEntryIdEvent(Base):
    """One cow identification / prep timestamp from a Rotary Entry ID report."""

    __tablename__ = "parlour_rotary_entry_id_events"
    __table_args__ = (
        UniqueConstraint(
            "farm",
            "cow_id",
            "identified_at",
            name="uq_parlour_rotary_entry_id_farm_cow_time",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("parlour_rotary_entry_id_imports.id", ondelete="CASCADE"),
        index=True,
    )
    farm: Mapped[str] = mapped_column(String(8), index=True)
    cow_id: Mapped[str] = mapped_column(String(64), index=True)
    identified_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    id_seconds: Mapped[int] = mapped_column(Integer, default=0)

    import_batch: Mapped[ParlourRotaryEntryIdImport] = relationship(
        back_populates="events"
    )


class BenchmarkForecastLine(Base):
    """Manual monthly forecast/budget figures for benchmarking (per farm, per metric)."""

    __tablename__ = "benchmark_forecast_lines"
    __table_args__ = (
        UniqueConstraint(
            "fiscal_year",
            "forecast_month",
            "metric",
            "farm",
            name="uq_benchmark_forecast_line",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fiscal_year: Mapped[int] = mapped_column(Integer, index=True)
    forecast_month: Mapped[datetime.date] = mapped_column(Date, index=True)
    metric: Mapped[str] = mapped_column(String(32), index=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


FINANCIAL_OPTION_ITEM_TYPE = "item_type"
FINANCIAL_OPTION_BAND = "band"
FINANCIAL_OPTION_GROUP = "group"
FINANCIAL_OPTION_HEADING = "heading"
FINANCIAL_OPTION_TYPES: tuple[str, ...] = (
    FINANCIAL_OPTION_ITEM_TYPE,
    FINANCIAL_OPTION_BAND,
    FINANCIAL_OPTION_GROUP,
    FINANCIAL_OPTION_HEADING,
)


class FinancialForecastOption(Base):
    """Allowed values for financial forecast category hierarchy."""

    __tablename__ = "financial_forecast_options"
    __table_args__ = (
        UniqueConstraint("option_type", "value", name="uq_financial_forecast_option_type_value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    option_type: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[str] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class FinancialForecastMapping(Base):
    """Maps each heading to its item type, band and group."""

    __tablename__ = "financial_forecast_mappings"
    __table_args__ = (
        UniqueConstraint(
            "item_type",
            "band",
            "group",
            "heading",
            name="uq_financial_forecast_mapping",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    heading: Mapped[str] = mapped_column(String(255), index=True)
    item_type: Mapped[str] = mapped_column(String(64))
    band: Mapped[str] = mapped_column(String(128))
    group: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class FinancialForecastMappingSource(Base):
    """Links a financial heading mapping to one or more benchmarking data sources."""

    __tablename__ = "financial_forecast_mapping_sources"
    __table_args__ = (
        UniqueConstraint(
            "mapping_id",
            "source_key",
            name="uq_financial_forecast_mapping_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("financial_forecast_mappings.id", ondelete="CASCADE"),
        index=True,
    )
    source_key: Mapped[str] = mapped_column(String(64), index=True)


class FinancialForecastLine(Base):
    """Manual monthly financial forecast amounts (per farm, per mapping)."""

    __tablename__ = "financial_forecast_lines"
    __table_args__ = (
        UniqueConstraint(
            "fiscal_year",
            "forecast_month",
            "mapping_id",
            "farm",
            name="uq_financial_forecast_line",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fiscal_year: Mapped[int] = mapped_column(Integer, index=True)
    forecast_month: Mapped[datetime.date] = mapped_column(Date, index=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("financial_forecast_mappings.id"), index=True
    )
    farm: Mapped[str] = mapped_column(String(8), index=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class HpSchedule(Base):
    """Hire purchase agreement for benchmarking HP Schedules."""

    __tablename__ = "hp_schedules"
    __table_args__ = (
        UniqueConstraint("business", "name", name="uq_hp_schedule_business_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business: Mapped[str] = mapped_column(String(8), index=True, default="CM")
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(255), default="")
    monthly_capital: Mapped[float] = mapped_column(Float)
    monthly_interest: Mapped[float] = mapped_column(Float)
    months: Mapped[int] = mapped_column(Integer)
    payment_day: Mapped[int] = mapped_column(Integer)  # 1–31, same day each month
    start_month: Mapped[datetime.date] = mapped_column(Date, index=True)  # first payment month
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class StandingOrder(Base):
    """Recurring standing-order payment for budgeting cash requirements."""

    __tablename__ = "standing_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business: Mapped[str] = mapped_column(String(8), index=True, default="CM")
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(255), default="")
    amount: Mapped[float] = mapped_column(Float)  # total paid each installment
    months: Mapped[int] = mapped_column(Integer)
    frequency: Mapped[str] = mapped_column(String(16), default="monthly")
    interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_day: Mapped[int] = mapped_column(Integer)  # 1–31, first / monthly payment day
    start_month: Mapped[datetime.date] = mapped_column(Date, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class RentalAgreement(Base):
    """Land rental agreement for benchmarking rental schedules."""

    __tablename__ = "rental_agreements"
    __table_args__ = (
        UniqueConstraint("business", "farm_name", name="uq_rental_agreement_business_farm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business: Mapped[str] = mapped_column(String(8), index=True, default="CM")
    farm_name: Mapped[str] = mapped_column(String(128))
    farm_size: Mapped[float] = mapped_column(Float)  # acres
    payment_day: Mapped[int] = mapped_column(Integer, default=1)  # 1–31, same day each month
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    payments: Mapped[list[RentalAgreementPayment]] = relationship(
        back_populates="agreement", cascade="all, delete-orphan"
    )


class RentalAgreementPayment(Base):
    """Monthly rent amount due for a rental agreement."""

    __tablename__ = "rental_agreement_payments"
    __table_args__ = (
        UniqueConstraint(
            "agreement_id",
            "payment_month",
            name="uq_rental_agreement_payment_month",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agreement_id: Mapped[int] = mapped_column(
        ForeignKey("rental_agreements.id", ondelete="CASCADE"), index=True
    )
    payment_month: Mapped[datetime.date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    agreement: Mapped[RentalAgreement] = relationship(back_populates="payments")


RATION_INGREDIENT_CATEGORIES: tuple[str, ...] = ("concentrate", "forage", "straw")


class RationIngredient(Base):
    """Feed ingredients for benchmarking rations (monthly cost entry)."""

    __tablename__ = "ration_ingredients"
    __table_args__ = (
        UniqueConstraint("name", name="uq_ration_ingredient_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(16), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class RationIngredientCost(Base):
    """Monthly cost per tonne for a ration ingredient."""

    __tablename__ = "ration_ingredient_costs"
    __table_args__ = (
        UniqueConstraint(
            "fiscal_year",
            "cost_month",
            "ingredient_id",
            name="uq_ration_ingredient_cost",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fiscal_year: Mapped[int] = mapped_column(Integer, index=True)
    cost_month: Mapped[datetime.date] = mapped_column(Date, index=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ration_ingredients.id"), index=True
    )
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class FarmRation(Base):
    """A feed ration recipe for CM or GAD benchmarking."""

    __tablename__ = "farm_rations"
    __table_args__ = (
        UniqueConstraint("farm", "name", name="uq_farm_ration_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    name: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class FarmRationIngredient(Base):
    """Ingredients included in a farm ration recipe."""

    __tablename__ = "farm_ration_ingredients"
    __table_args__ = (
        UniqueConstraint(
            "ration_id",
            "ingredient_id",
            name="uq_farm_ration_ingredient",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ration_id: Mapped[int] = mapped_column(
        ForeignKey("farm_rations.id"), index=True
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ration_ingredients.id"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class FarmRationInclusion(Base):
    """Daily kg/head inclusion per ingredient for a farm ration (stored per month)."""

    __tablename__ = "farm_ration_inclusions"
    __table_args__ = (
        UniqueConstraint(
            "fiscal_year",
            "inclusion_month",
            "ration_id",
            "ingredient_id",
            name="uq_farm_ration_inclusion",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fiscal_year: Mapped[int] = mapped_column(Integer, index=True)
    inclusion_month: Mapped[datetime.date] = mapped_column(Date, index=True)
    ration_id: Mapped[int] = mapped_column(ForeignKey("farm_rations.id"), index=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ration_ingredients.id"), index=True
    )
    kg_per_head: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class HerdBirth(Base):
    """Birth records from DCEXPORT CMBORN / GADBORN files."""

    __tablename__ = "herd_births"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cow_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bdat: Mapped[datetime.date | None] = mapped_column(Date, nullable=True, index=True)
    cbrd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gndr: Mapped[str | None] = mapped_column(String(8), nullable=True)
    category: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    event: Mapped[str | None] = mapped_column(String(64), nullable=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    import_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


# --- HR / Staff management ---

EMPLOYEE_STATUS_ONBOARDING = "onboarding"
EMPLOYEE_STATUS_PENDING_SIGNATURE = "pending_signature"
EMPLOYEE_STATUS_ACTIVE = "active"
EMPLOYEE_STATUS_ARCHIVED = "archived"
EMPLOYEE_STATUSES: tuple[str, ...] = (
    EMPLOYEE_STATUS_ONBOARDING,
    EMPLOYEE_STATUS_PENDING_SIGNATURE,
    EMPLOYEE_STATUS_ACTIVE,
    EMPLOYEE_STATUS_ARCHIVED,
)

CONTRACT_STATUS_PENDING = "pending"
CONTRACT_STATUS_COMPLETED = "completed"
CONTRACT_STATUS_DECLINED = "declined"
CONTRACT_STATUSES: tuple[str, ...] = (
    CONTRACT_STATUS_PENDING,
    CONTRACT_STATUS_COMPLETED,
    CONTRACT_STATUS_DECLINED,
)

PAY_TYPE_HOURLY = "hourly"
PAY_TYPE_SALARY = "salary"
PAY_TYPES: tuple[str, ...] = (PAY_TYPE_HOURLY, PAY_TYPE_SALARY)

EMPLOYMENT_TYPE_EMPLOYED = "employed"
EMPLOYMENT_TYPE_SELF_EMPLOYED = "self_employed"
EMPLOYMENT_TYPES: tuple[str, ...] = (
    EMPLOYMENT_TYPE_EMPLOYED,
    EMPLOYMENT_TYPE_SELF_EMPLOYED,
)
EMPLOYMENT_TYPE_LABELS: dict[str, str] = {
    EMPLOYMENT_TYPE_EMPLOYED: "Employed",
    EMPLOYMENT_TYPE_SELF_EMPLOYED: "Self-employed",
}

# Legal entities staff can be employed by (full registered names).
HR_BUSINESS_OPTIONS: tuple[str, ...] = ("Cwrt Malle Ltd", "Green Acre Dairy Ltd")
# Personal title options for the new-starter form.
TITLE_OPTIONS: tuple[str, ...] = ("Mr", "Mrs", "Miss", "Ms", "Dr")
# Job titles: defaults seeded into AppSetting; manage via Enroll page Settings.
JOB_TITLE_OPTIONS: tuple[str, ...] = ("Farm Worker",)
HR_JOB_TITLES_SETTING_KEY = "hr.job_titles"
# Feed contract lookup lists (manage via Contracts gear).
FEED_PRODUCT_TYPES_DEFAULT: tuple[str, ...] = ("Cereal", "Fibre", "Protein")
FEED_PRODUCT_TYPES_SETTING_KEY = "feed.product_types"
FEED_PRODUCTS_SETTING_KEY = "feed.products"
FEED_SUPPLIERS_SETTING_KEY = "feed.suppliers"
# Document categories that can be attached to a staff profile.
DOCUMENT_TYPE_OPTIONS: tuple[str, ...] = (
    "Passport",
    "Driving Licence",
    "Right to Work",
    "Visa / BRP",
    "Proof of Address",
    "Qualification",
    "Other",
)


class ContractTemplate(Base):
    """DocuSeal template metadata (template lives in DocuSeal)."""

    __tablename__ = "contract_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    docuseal_template_id: Mapped[int] = mapped_column(Integer, index=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    employees: Mapped[list[Employee]] = relationship(back_populates="template")
    contracts: Mapped[list[EmployeeContract]] = relationship(back_populates="template")


class Employee(Base):
    """Staff member enrolled through HR."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    employment_type: Mapped[str] = mapped_column(
        String(32), default=EMPLOYMENT_TYPE_EMPLOYED, index=True
    )
    title: Mapped[str | None] = mapped_column(String(16), nullable=True)
    employee_number: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    full_name: Mapped[str] = mapped_column(String(255), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dob: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    ni_number_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    pay_type: Mapped[str] = mapped_column(String(16), default=PAY_TYPE_HOURLY)
    pay_rate_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    role_title: Mapped[str] = mapped_column(String(128), index=True)
    start_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    working_days_per_week: Mapped[float | None] = mapped_column(Float, nullable=True)
    working_hours_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    driving_license_number_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_points: Mapped[str | None] = mapped_column(String(255), nullable=True)
    right_to_work_share_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_holder_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sort_code_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_number_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_of_kin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    next_of_kin_relationship: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_of_kin_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=EMPLOYEE_STATUS_ONBOARDING, index=True
    )
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract_templates.id"), nullable=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    template: Mapped[ContractTemplate | None] = relationship(back_populates="employees")
    contracts: Mapped[list[EmployeeContract]] = relationship(
        back_populates="employee", order_by="EmployeeContract.created_at.desc()"
    )
    documents: Mapped[list[EmployeeDocument]] = relationship(
        back_populates="employee",
        order_by="EmployeeDocument.created_at.desc()",
        cascade="all, delete-orphan",
    )


class EmployeeContract(Base):
    """DocuSeal submission linked to an employee."""

    __tablename__ = "employee_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract_templates.id"), nullable=True
    )
    docuseal_submission_id: Mapped[int | None] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=CONTRACT_STATUS_PENDING, index=True
    )
    signed_pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signed_pdf_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    employee: Mapped[Employee] = relationship(back_populates="contracts")
    template: Mapped[ContractTemplate | None] = relationship(back_populates="contracts")


class EmployeeDocument(Base):
    """An uploaded document attached to a staff profile (passport, licence, etc.)."""

    __tablename__ = "employee_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id"), index=True
    )
    doc_type: Mapped[str] = mapped_column(String(64), default="Other")
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stored_path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    employee: Mapped[Employee] = relationship(back_populates="documents")


class CtsSyncRun(Base):
    """One CTS GetHolding sync attempt for a farm."""

    __tablename__ = "cts_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    animal_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )


class CtsOnHolding(Base):
    """Latest CTS cattle-on-holding snapshot row for a farm."""

    __tablename__ = "cts_on_holding"
    __table_args__ = (
        UniqueConstraint("farm", "etag", name="uq_cts_on_holding_farm_etag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    etag: Mapped[str] = mapped_column(String(64), index=True)
    breed: Mapped[str] = mapped_column(String(16), default="")
    sex: Mapped[str] = mapped_column(String(8), default="")
    dob: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    on_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    sync_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("cts_sync_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class CtsReportedMovement(Base):
    """Movements/births already submitted (or acknowledged) to BCMS."""

    __tablename__ = "cts_reported_movements"
    __table_args__ = (
        UniqueConstraint(
            "farm",
            "movement_type",
            "etag",
            "event_date",
            name="uq_cts_reported_movement",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    movement_type: Mapped[str] = mapped_column(String(16), index=True)
    etag: Mapped[str] = mapped_column(String(64), index=True)
    event_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(32), default="sent", index=True)
    receipt: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


FARM_JOB_STATUS_PENDING = "pending"
FARM_JOB_STATUS_ARCHIVED = "archived"
FARM_JOB_STATUSES: tuple[str, ...] = (
    FARM_JOB_STATUS_PENDING,
    FARM_JOB_STATUS_ARCHIVED,
)


class FarmJobTemplate(Base):
    """Recurring farm job (e.g. liner change every 42 days)."""

    __tablename__ = "farm_job_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm: Mapped[str] = mapped_column(String(8), index=True)
    name: Mapped[str] = mapped_column(String(255))
    interval_days: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(String(500), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    occurrences: Mapped[list[FarmJobOccurrence]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class FarmJobOccurrence(Base):
    """One due instance of a farm job; archived after it is marked done."""

    __tablename__ = "farm_job_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("farm_job_templates.id"), index=True
    )
    farm: Mapped[str] = mapped_column(String(8), index=True)
    due_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default=FARM_JOB_STATUS_PENDING, index=True
    )
    completed_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    completed_by: Mapped[str] = mapped_column(String(255), default="")
    completed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    template: Mapped[FarmJobTemplate] = relationship(back_populates="occurrences")


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


_BULL_NAME_CODES = re.compile(r"\s+A[12]A[12]\b.*$", re.IGNORECASE)


def _clean_bull_name(name: str | None) -> str | None:
    cleaned = _str_or_none(name)
    if cleaned is None:
        return None
    stripped = _BULL_NAME_CODES.sub("", cleaned).strip()
    return stripped or cleaned


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
