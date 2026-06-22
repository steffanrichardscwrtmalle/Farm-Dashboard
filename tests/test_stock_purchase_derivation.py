"""Tests for purchase stock-group classification."""

from __future__ import annotations

import datetime as dt

from app.models import STOCK_GROUP_BEEF, STOCK_GROUP_COWS, STOCK_GROUP_YOUNGSTOCK
from app.services.stock_purchase_derivation import classify_purchase_stock_group


def test_purchased_heifer_fresh_after_arrival_is_youngstock() -> None:
    assert (
        classify_purchase_stock_group(
            1,
            10,
            "F",
            edat=dt.date(2024, 12, 27),
            fdat=dt.date(2024, 12, 31),
            min_lact=1,
        )
        == STOCK_GROUP_YOUNGSTOCK
    )


def test_purchased_milking_cow_stays_cows() -> None:
    assert (
        classify_purchase_stock_group(
            3,
            10,
            "F",
            edat=dt.date(2020, 12, 23),
            fdat=dt.date(2021, 12, 30),
            min_lact=3,
        )
        == STOCK_GROUP_COWS
    )


def test_purchased_first_lact_cow_with_fdat_before_arrival_stays_cows() -> None:
    assert (
        classify_purchase_stock_group(
            1,
            10,
            "F",
            edat=dt.date(2024, 6, 1),
            fdat=dt.date(2024, 2, 1),
            min_lact=1,
        )
        == STOCK_GROUP_COWS
    )


def test_purchased_heifer_with_lact_zero_history_is_youngstock() -> None:
    assert (
        classify_purchase_stock_group(
            1,
            10,
            "F",
            edat=dt.date(2024, 12, 27),
            fdat=dt.date(2024, 12, 31),
            min_lact=0,
        )
        == STOCK_GROUP_YOUNGSTOCK
    )


def test_beef_purchase_ignores_fdat() -> None:
    assert (
        classify_purchase_stock_group(
            1,
            102,
            "F",
            edat=dt.date(2024, 12, 27),
            fdat=dt.date(2025, 1, 1),
        )
        == STOCK_GROUP_BEEF
    )
