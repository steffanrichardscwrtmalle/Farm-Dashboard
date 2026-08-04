"""Tests for shared stock-group classification."""

from __future__ import annotations

from app.models import STOCK_GROUP_BEEF, STOCK_GROUP_COWS, STOCK_GROUP_YOUNGSTOCK
from app.services.stock_group import (
    stock_group_from_birth,
    stock_group_from_event_fields,
    stock_group_from_inventory,
    valuation_category_from_stock_group,
)


def test_stock_group_from_event_fields() -> None:
    assert stock_group_from_event_fields(2, 1, "F") == STOCK_GROUP_COWS
    assert stock_group_from_event_fields(0, 121, "M") == STOCK_GROUP_BEEF
    assert stock_group_from_event_fields(0, 1, "F") == STOCK_GROUP_YOUNGSTOCK


def test_stock_group_from_inventory() -> None:
    assert stock_group_from_inventory(1, "Holstein") == STOCK_GROUP_COWS
    assert stock_group_from_inventory(1, "HF") == STOCK_GROUP_COWS
    assert stock_group_from_inventory(0, "Beef") == STOCK_GROUP_BEEF
    assert stock_group_from_inventory(0, "AAX") == STOCK_GROUP_BEEF
    assert stock_group_from_inventory(0, "HEX") == STOCK_GROUP_BEEF
    assert stock_group_from_inventory(0, "Holstein") == STOCK_GROUP_YOUNGSTOCK
    assert stock_group_from_inventory(0, "HF") == STOCK_GROUP_YOUNGSTOCK


def test_stock_group_from_birth() -> None:
    assert stock_group_from_birth("Beef", 121, "M") == STOCK_GROUP_BEEF
    assert stock_group_from_birth("Dairy", 1, "F") == STOCK_GROUP_YOUNGSTOCK


def test_valuation_category_mapping() -> None:
    assert valuation_category_from_stock_group(STOCK_GROUP_COWS) == "Dairy"
    assert valuation_category_from_stock_group(STOCK_GROUP_YOUNGSTOCK) == "Youngstock"
    assert valuation_category_from_stock_group(STOCK_GROUP_BEEF) == "Beef"
