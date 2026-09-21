"""Staff time sheets: pay periods, hours, and holiday balances."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CM_TIMESHEET_PERIOD_START,
    EMPLOYEE_STATUS_ARCHIVED,
    GAD_TIMESHEET_PERIOD_START,
    PAY_TYPE_HOURLY,
    PAY_TYPE_LABELS,
    PAY_TYPE_SALARY,
    TIMESHEET_FARM_BUSINESS,
    TIMESHEET_FARM_CM,
    TIMESHEET_FARM_LABELS,
    TIMESHEET_FARM_OPTIONS,
    TIMESHEET_FORTNIGHT_DAYS,
    Employee,
    EmployeeTimesheetEntry,
)
from app.services.crypto_fields import decrypt_field

WEEKS_PER_YEAR = 52
PERIOD_HORIZON_MONTHS = 12


class TimesheetError(Exception):
    """Time sheet operation failed."""


def normalize_farm(farm: str | None) -> str:
    value = (farm or "").strip().upper()
    if value not in TIMESHEET_FARM_OPTIONS:
        raise TimesheetError("Farm must be CM or GAD.")
    return value


def farm_label(farm: str) -> str:
    return TIMESHEET_FARM_LABELS[normalize_farm(farm)]


def farm_business(farm: str) -> str:
    return TIMESHEET_FARM_BUSINESS[normalize_farm(farm)]


def is_fortnightly(farm: str) -> bool:
    return normalize_farm(farm) == TIMESHEET_FARM_CM


def pay_cycle_start(farm: str) -> dt.date:
    return (
        CM_TIMESHEET_PERIOD_START
        if is_fortnightly(farm)
        else GAD_TIMESHEET_PERIOD_START
    )


def period_containing(farm: str, target: dt.date) -> tuple[dt.date, dt.date]:
    farm_key = normalize_farm(farm)
    start_anchor = pay_cycle_start(farm_key)
    if farm_key == TIMESHEET_FARM_CM:
        if target < start_anchor:
            start = start_anchor
        else:
            weeks = (target - start_anchor).days // TIMESHEET_FORTNIGHT_DAYS
            start = start_anchor + dt.timedelta(days=weeks * TIMESHEET_FORTNIGHT_DAYS)
        end = start + dt.timedelta(days=TIMESHEET_FORTNIGHT_DAYS - 1)
        return start, end

    if target < start_anchor:
        start = start_anchor
    else:
        start = dt.date(target.year, target.month, 1)
        if start < start_anchor:
            start = start_anchor
    last_day = calendar.monthrange(start.year, start.month)[1]
    return start, dt.date(start.year, start.month, last_day)


def resolve_period(farm: str, period_start: dt.date) -> tuple[dt.date, dt.date]:
    start, end = period_containing(farm, period_start)
    if start != period_start:
        raise TimesheetError(
            "period_start must be the first day of a pay period for this farm."
        )
    return start, end


def _add_months(value: dt.date, months: int) -> dt.date:
    month_index = value.year * 12 + (value.month - 1) + months
    year, month = divmod(month_index, 12)
    last_day = calendar.monthrange(year, month + 1)[1]
    return dt.date(year, month + 1, min(value.day, last_day))


def list_periods(
    farm: str,
    *,
    as_of: dt.date | None = None,
) -> list[dict[str, Any]]:
    farm_key = normalize_farm(farm)
    as_of = as_of or dt.date.today()
    cursor = pay_cycle_start(farm_key)
    horizon = _add_months(max(as_of, cursor), PERIOD_HORIZON_MONTHS)
    periods: list[dict[str, Any]] = []
    while cursor <= horizon:
        start, end = period_containing(farm_key, cursor)
        periods.append(serialize_period(farm_key, start, end))
        cursor = end + dt.timedelta(days=1)
    return periods


def current_period_start(farm: str, *, as_of: dt.date | None = None) -> dt.date:
    as_of = as_of or dt.date.today()
    start, _end = period_containing(farm, as_of)
    return start


def serialize_period(
    farm: str, start: dt.date, end: dt.date
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    fortnightly = farm_key == TIMESHEET_FARM_CM
    if fortnightly:
        week_1_end = start + dt.timedelta(days=6)
        week_2_start = start + dt.timedelta(days=7)
        hours_columns = [
            {
                "key": "hours_week_1",
                "label": f"Hours {_format_range(start, week_1_end)}",
            },
            {
                "key": "hours_week_2",
                "label": f"Hours {_format_range(week_2_start, end)}",
            },
        ]
        cadence = "fortnightly"
        label = _format_range(start, end)
    else:
        hours_columns = [{"key": "hours_week_1", "label": "Hours"}]
        cadence = "monthly"
        label = start.strftime("%B %Y")
    return {
        "farm": farm_key,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "label": label,
        "cadence": cadence,
        "hours_columns": hours_columns,
    }


def list_timesheet(
    db: Session,
    farm: str,
    *,
    period_start: dt.date | None = None,
    as_of: dt.date | None = None,
    include_rate: bool = False,
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    as_of = as_of or dt.date.today()
    periods = list_periods(farm_key, as_of=as_of)
    if period_start is None:
        period_start = current_period_start(farm_key, as_of=as_of)
    start, end = resolve_period(farm_key, period_start)
    period = serialize_period(farm_key, start, end)
    if not any(item["start"] == period["start"] for item in periods):
        periods.append(period)
        periods.sort(key=lambda item: item["start"])

    staff = _active_staff_for_farm(db, farm_key)
    entries = {
        row.employee_id: row
        for row in db.scalars(
            select(EmployeeTimesheetEntry).where(
                EmployeeTimesheetEntry.farm == farm_key,
                EmployeeTimesheetEntry.period_start == start,
            )
        ).all()
    }
    holiday_hours_by_employee = _holiday_hours_by_employee(
        db, [row.id for row in staff]
    )
    return {
        "farm": farm_key,
        "farm_label": farm_label(farm_key),
        "business": farm_business(farm_key),
        "cadence": period["cadence"],
        "period": period,
        "periods": periods,
        "can_view_rate": include_rate,
        "rows": [
            _serialize_row(
                employee,
                entries.get(employee.id),
                holiday_hours_by_employee.get(employee.id, []),
                include_rate=include_rate,
            )
            for employee in staff
        ],
    }


def save_timesheet_row(
    db: Session,
    farm: str,
    payload: dict[str, Any],
    *,
    include_rate: bool = False,
) -> dict[str, Any]:
    farm_key = normalize_farm(farm)
    employee_id = int(payload["employee_id"])
    period_start = _parse_date(payload.get("period_start"), field="period_start")
    start, end = resolve_period(farm_key, period_start)
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise TimesheetError("Employee not found.")
    if employee.status == EMPLOYEE_STATUS_ARCHIVED:
        raise TimesheetError("Archived staff cannot be added to a time sheet.")
    if (employee.business or "") != farm_business(farm_key):
        raise TimesheetError("Staff member does not belong to this farm.")

    entry = db.scalar(
        select(EmployeeTimesheetEntry).where(
            EmployeeTimesheetEntry.employee_id == employee_id,
            EmployeeTimesheetEntry.period_start == start,
        )
    )
    if entry is None:
        entry = EmployeeTimesheetEntry(
            employee_id=employee_id,
            farm=farm_key,
            period_start=start,
            period_end=end,
        )
        db.add(entry)

    entry.farm = farm_key
    entry.period_end = end
    for key in (
        "hours_week_1",
        "holiday_hours",
        "dinner_break_hours",
        "mileage",
        "bonus",
        "loan_deduction",
        "other_deduction",
        "accommodation_deduction",
    ):
        if key in payload:
            setattr(entry, key, _optional_float(payload.get(key)))
    if is_fortnightly(farm_key):
        if "hours_week_2" in payload:
            entry.hours_week_2 = _optional_float(payload.get("hours_week_2"))
    else:
        entry.hours_week_2 = None
    if "other_remark" in payload:
        remark = payload.get("other_remark")
        entry.other_remark = (
            str(remark).strip() if remark is not None else ""
        ) or None

    if "holidays_remaining" in payload:
        employee.holidays_remaining = _optional_float(payload.get("holidays_remaining"))
    if "holidays_carry_forward" in payload:
        employee.holidays_carry_forward = _optional_float(
            payload.get("holidays_carry_forward")
        )
    if "holiday_year_end" in payload:
        raw_year_end = payload.get("holiday_year_end")
        employee.holiday_year_end = (
            None
            if raw_year_end in (None, "")
            else _parse_date(raw_year_end, field="holiday_year_end")
        )

    db.commit()
    db.refresh(employee)
    holiday_hours = _holiday_hours_by_employee(db, [employee.id]).get(employee.id, [])
    return _serialize_row(
        employee, entry, holiday_hours, include_rate=include_rate
    )


def _active_staff_for_farm(db: Session, farm: str) -> list[Employee]:
    business = farm_business(farm)
    return list(
        db.scalars(
            select(Employee)
            .where(
                Employee.status != EMPLOYEE_STATUS_ARCHIVED,
                Employee.business == business,
            )
            .order_by(Employee.full_name)
        ).all()
    )


def _holiday_hours_by_employee(
    db: Session, employee_ids: list[int]
) -> dict[int, list[EmployeeTimesheetEntry]]:
    if not employee_ids:
        return {}
    rows = db.scalars(
        select(EmployeeTimesheetEntry).where(
            EmployeeTimesheetEntry.employee_id.in_(employee_ids)
        )
    ).all()
    grouped: dict[int, list[EmployeeTimesheetEntry]] = {eid: [] for eid in employee_ids}
    for row in rows:
        grouped.setdefault(row.employee_id, []).append(row)
    return grouped


def _serialize_row(
    employee: Employee,
    entry: EmployeeTimesheetEntry | None,
    holiday_entries: list[EmployeeTimesheetEntry],
    *,
    include_rate: bool,
) -> dict[str, Any]:
    pay_type = employee.pay_type or PAY_TYPE_HOURLY
    rate_value = None
    rate_label = None
    if include_rate:
        rate_value, rate_label = _display_rate(pay_type, decrypt_field(employee.pay_rate_enc))
    holidays_taken = _holidays_taken_in_year(
        holiday_entries, employee.holiday_year_end
    )
    return {
        "employee_id": employee.id,
        "employee_number": employee.employee_number,
        "full_name": employee.full_name,
        "employment_type": employee.employment_type,
        "pay_type": pay_type,
        "pay_type_label": PAY_TYPE_LABELS.get(pay_type, "Hourly"),
        "rate": rate_value,
        "rate_label": rate_label,
        "hours_week_1": _num(entry.hours_week_1 if entry else None),
        "hours_week_2": _num(entry.hours_week_2 if entry else None),
        "holiday_hours": _num(entry.holiday_hours if entry else None),
        "dinner_break_hours": _num(entry.dinner_break_hours if entry else None),
        "mileage": _num(entry.mileage if entry else None),
        "bonus": _num(entry.bonus if entry else None),
        "loan_deduction": _num(entry.loan_deduction if entry else None),
        "other_deduction": _num(entry.other_deduction if entry else None),
        "accommodation_deduction": _num(
            entry.accommodation_deduction if entry else None
        ),
        "other_remark": (entry.other_remark if entry else None) or "",
        "holidays_remaining": _num(employee.holidays_remaining),
        "holidays_taken": holidays_taken,
        "holidays_carry_forward": _num(employee.holidays_carry_forward),
        "holiday_year_end": (
            employee.holiday_year_end.isoformat() if employee.holiday_year_end else None
        ),
    }


def _holidays_taken_in_year(
    entries: list[EmployeeTimesheetEntry],
    year_end: dt.date | None,
) -> float:
    year_start = None
    if year_end is not None:
        year_start = dt.date(year_end.year - 1, year_end.month, year_end.day) + dt.timedelta(
            days=1
        )
    total = 0.0
    for entry in entries:
        if year_start and (entry.period_start < year_start or entry.period_start > year_end):
            continue
        if entry.holiday_hours:
            total += float(entry.holiday_hours)
    return _num(total) or 0.0


def _display_rate(
    pay_type: str, raw: str | None
) -> tuple[float | None, str | None]:
    amount = _parse_money(raw)
    if amount is None:
        return None, None
    if pay_type == PAY_TYPE_SALARY:
        weekly = round(amount / WEEKS_PER_YEAR, 2)
        return weekly, f"£{weekly:,.2f} / wk"
    return amount, f"£{amount:,.2f} / hr"


def _parse_money(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = str(raw).replace("£", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TimesheetError("Numeric fields must be numbers.") from exc
    if number < 0:
        raise TimesheetError("Numeric fields cannot be negative.")
    return number


def _num(value: float | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _parse_date(value: dt.date | str | None, *, field: str) -> dt.date:
    if value is None or value == "":
        raise TimesheetError(f"{field} is required.")
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise TimesheetError(f"{field} must be a valid date.") from exc


def _format_range(start: dt.date, end: dt.date) -> str:
    if start.month == end.month and start.year == end.year:
        return f"{start.day}-{end.day} {start.strftime('%b %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%b')} - {end.day} {end.strftime('%b %Y')}"
    return f"{start.day} {start.strftime('%b %Y')} - {end.day} {end.strftime('%b %Y')}"
