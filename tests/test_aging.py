"""Worked-example tests for :mod:`stocklens.aging_alert`.

Covers ``STOCKLENS_PLAN`` §2 (aging) + ``BUILD-CONTRACT`` §6.4:

* **Category-differentiated thresholds** — a Daily-Needs row (``rtp_category`` is
  ``Staples`` or sub LIKE ``Flour``) is kept at ``diff_days == 15`` but dropped at
  14; a Lifestyle row is kept at 31 but dropped at 30.
* **Sub-category Daily-Needs** — a row whose ``rtp_category`` is *Lifestyle* but whose
  ``rtp_sub_category`` matches the Flour token is still classified Daily Needs.
* **Warehouse exclusion** — ``Consignment`` warehouses are dropped.
* **WL filter** — only ``status_wl LIKE 'WL%'`` rows survive.
* **Sell-out join** — last-7-day sell-out is attached, ``reward`` rows excluded.

The aging logic is driven against an in-memory DuckDB (``aging_db`` fixture) for
``product_rtp`` / ``sales_history``; the cohort itself is a tiny synthetic frame.
"""

from __future__ import annotations

import pandas as pd
import pytest

from stocklens.aging_alert import categorize_and_filter, join_sell_out


def _cohort_row(
    *,
    product_id: int,
    warehouse_name: str,
    diff_days: int,
    status_wl: str = "WL",
    stok: float = 10.0,
    purchase: float = 1000.0,
    unit_suffix: str = "kg",
) -> dict:
    """One synthetic aging-cohort row (matches ``data/aging_cohort.csv`` columns)."""
    return {
        "product_id": product_id,
        "product_unit": f"SKU-{product_id:04d} ({unit_suffix})",
        "warehouse_name": warehouse_name,
        "diff_days_inhouse": diff_days,
        "stok_gudang_tanpa_booking": stok,
        "total_purchase_stok_tanpa_booking": purchase,
        "status_wl": status_wl,
    }


@pytest.mark.duckdb
def test_threshold_split_daily_needs_vs_lifestyle(aging_db, rules):
    """Daily-Needs kept at ≥15 / dropped at 14; Lifestyle kept at ≥31 / dropped at 30."""
    cohort = pd.DataFrame(
        [
            # 201 = Staples/Flour → Daily Needs (threshold 15).
            _cohort_row(product_id=201, warehouse_name="North DC", diff_days=15),  # kept
            _cohort_row(product_id=201, warehouse_name="South DC", diff_days=14),  # dropped
            # 202 = Lifestyle/Apparel → Lifestyle (threshold 31).
            _cohort_row(product_id=202, warehouse_name="North DC", diff_days=31),  # kept
            _cohort_row(product_id=202, warehouse_name="South DC", diff_days=30),  # dropped
        ]
    )

    aged = categorize_and_filter(cohort, aging_db, rules)

    surviving = {(row.product_id, row.warehouse_name, row.Category) for row in aged.itertuples()}
    assert surviving == {
        (201, "North DC", "Daily Needs"),
        (202, "North DC", "Lifestyle"),
    }


@pytest.mark.duckdb
def test_sub_category_flour_is_daily_needs(aging_db, rules):
    """A ``Lifestyle`` rtp_category with a ``Flour`` sub-category → Daily Needs."""
    # 203 has rtp_category='Lifestyle' but rtp_sub_category='Flour' in the fixture.
    cohort = pd.DataFrame([_cohort_row(product_id=203, warehouse_name="North DC", diff_days=20)])

    aged = categorize_and_filter(cohort, aging_db, rules)

    assert len(aged) == 1
    assert aged.iloc[0]["Category"] == "Daily Needs"


@pytest.mark.duckdb
def test_consignment_warehouse_excluded(aging_db, rules):
    """A ``Consignment`` warehouse row is dropped even when well past threshold."""
    cohort = pd.DataFrame(
        [
            _cohort_row(product_id=201, warehouse_name="North DC", diff_days=40),
            _cohort_row(product_id=201, warehouse_name="Consignment DC", diff_days=40),
        ]
    )

    aged = categorize_and_filter(cohort, aging_db, rules)

    assert aged["warehouse_name"].tolist() == ["North DC"]


@pytest.mark.duckdb
def test_non_wl_status_dropped(aging_db, rules):
    """Only ``status_wl LIKE 'WL%'`` survives; a ``Reguler`` row is filtered out."""
    cohort = pd.DataFrame(
        [
            _cohort_row(product_id=201, warehouse_name="North DC", diff_days=40, status_wl="WL"),
            _cohort_row(
                product_id=201, warehouse_name="Central DC", diff_days=40, status_wl="Reguler"
            ),
        ]
    )

    aged = categorize_and_filter(cohort, aging_db, rules)

    assert aged["warehouse_name"].tolist() == ["North DC"]


@pytest.mark.duckdb
def test_sell_out_excludes_reward(aging_db, rules, now):
    """The last-7-day sell-out join keeps ``regular`` (7) and excludes ``reward`` (100)."""
    cohort = pd.DataFrame([_cohort_row(product_id=203, warehouse_name="North DC", diff_days=20)])
    aged = categorize_and_filter(cohort, aging_db, rules)

    frames = join_sell_out(aged, aging_db, rules, now=now)
    daily = frames["daily_needs"]

    assert len(daily) == 1
    row = daily.iloc[0]
    # Only the regular sale (7 units / 70,000 gmv) is counted; the reward row is excluded.
    assert int(row["qty_sell_out"]) == 7
    assert int(row["gmv"]) == 70_000
    assert frames["lifestyle"].empty
    assert len(frames["all"]) == 1
