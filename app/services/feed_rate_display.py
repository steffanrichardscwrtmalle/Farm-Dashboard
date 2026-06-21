"""Transform raw feed rate rows into aggregated display tables (Streamlit parity)."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _empty_report() -> dict[str, Any]:
    return {
        "ration_names": [],
        "ration_tables": {},
        "summary_rows": [],
    }


def _as_int(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return int(round(float(value)))


def build_feed_rate_display(records: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Aggregate duplicate ration/group rows and build per-ration tables plus summary.

    Mirrors the legacy Streamlit feed dashboard logic.
    """
    if not records:
        return _empty_report()

    df = pd.DataFrame(records)
    if df.empty:
        return _empty_report()

    calves_mask = df["group_name"].astype(str).str.strip().str.lower() == "calves"
    df.loc[calves_mask, "ration_name"] = "Calves"

    numeric_cols = [
        "cow_count",
        "feed_percent",
        "total_fresh",
        "total_dm",
        "dm_kg_per_cow",
        "cost",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    deduped = df.drop_duplicates(subset=["ration_name", "group_name"])[
        ["ration_name", "group_name", "cow_count"]
    ]
    agg = df.groupby(["ration_name", "group_name"], as_index=False).agg(
        {
            "feed_percent": "sum",
            "total_fresh": "sum",
            "total_dm": "sum",
            "dm_kg_per_cow": "sum",
            "cost": "sum",
        }
    )
    merged = pd.merge(agg, deduped, on=["ration_name", "group_name"], how="left")
    merged["portions"] = (merged["cow_count"].fillna(0) * merged["feed_percent"].fillna(0)) / 100

    merged["feed_percent"] = merged["feed_percent"].round(0)
    merged["total_fresh"] = merged["total_fresh"].round(0)
    merged["total_dm"] = merged["total_dm"].round(0)
    merged["cost"] = merged["cost"].round(0)
    merged["cow_count"] = merged["cow_count"].round(0)
    merged["portions"] = merged["portions"].round(0)
    merged["dm_kg_per_cow"] = merged["dm_kg_per_cow"].round(1)

    ration_tables: dict[str, list[dict[str, Any]]] = {}
    summary_rows: list[dict[str, Any]] = []

    for ration, group in merged.groupby("ration_name"):
        data = group[
            [
                "group_name",
                "portions",
                "cow_count",
                "feed_percent",
                "dm_kg_per_cow",
                "total_dm",
                "cost",
            ]
        ].copy()

        rows: list[dict[str, Any]] = []
        for _, row in data.iterrows():
            rows.append(
                {
                    "group_name": row["group_name"],
                    "portions": _as_int(row["portions"]),
                    "cow_count": _as_int(row["cow_count"]),
                    "feed_percent": _as_int(row["feed_percent"]),
                    "dm_kg_per_cow": float(row["dm_kg_per_cow"]),
                    "total_dm": _as_int(row["total_dm"]),
                    "cost": _as_int(row["cost"]),
                    "cost_per_cow": None,
                    "is_total": False,
                }
            )

        totals = data[["total_dm", "cost", "cow_count", "portions"]].sum(numeric_only=True)
        cow_total = float(totals["cow_count"]) if totals["cow_count"] else 0.0

        total_feed_pct = (
            round((float(totals["portions"]) / cow_total) * 100, 1) if cow_total else 0.0
        )
        total_dm_per_cow = round(float(totals["total_dm"]) / cow_total, 1) if cow_total else 0.0
        cost_per_cow = round(float(totals["cost"]) / cow_total, 2) if cow_total else 0.0

        rows.append(
            {
                "group_name": "Total",
                "portions": _as_int(totals["portions"]),
                "cow_count": _as_int(totals["cow_count"]),
                "feed_percent": total_feed_pct,
                "dm_kg_per_cow": total_dm_per_cow,
                "total_dm": _as_int(totals["total_dm"]),
                "cost": _as_int(totals["cost"]),
                "cost_per_cow": cost_per_cow,
                "is_total": True,
            }
        )

        ration_tables[str(ration)] = rows
        summary_rows.append(
            {
                "ration_name": str(ration),
                "portions": _as_int(totals["portions"]),
                "cow_count": _as_int(totals["cow_count"]),
                "feed_percent": total_feed_pct,
                "dm_kg_per_cow": total_dm_per_cow,
                "total_dm": _as_int(totals["total_dm"]),
                "cost": _as_int(totals["cost"]),
                "cost_per_cow": cost_per_cow,
            }
        )

    summary_rows.sort(key=lambda row: row["ration_name"].lower())
    ration_names = sorted(ration_tables.keys(), key=str.lower)

    return {
        "ration_names": ration_names,
        "ration_tables": ration_tables,
        "summary_rows": summary_rows,
    }
