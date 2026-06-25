"""Worked-example tests for the IQR outlier logic in :mod:`stocklens.demand_classify`.

Covers ``STOCKLENS_PLAN`` §2 / ``BUILD-CONTRACT`` §6.4:

* IQR on ``[2, 3, 3, 4, 50]`` → q1=3, q3=4, iqr=1, upper=round(4+1.5)=6,
  lower=round(3-1.5)=2; the 50 is flagged → include-total 62, exclude-total 12.
* ``len == 1`` branch → upper = qty*single_factor, lower = 0, sample always kept
  (include-total == exclude-total).
* ``lower < 0`` → clamped to 0.
* ``len == 0`` → an empty result frame.

Both the pure ``_iqr_bounds`` helper and the end-to-end ``remove_outliers``
aggregation (qty_per_day floor included) are exercised.
"""

from __future__ import annotations

import pandas as pd
import pytest

from stocklens.demand_classify import _iqr_bounds, remove_outliers


def _outlier_lines(
    qtys: list[int], *, warehouse_id: int = 1, product_attribute_id: int = 1, days: str = "L7D"
) -> pd.DataFrame:
    """Build a one-grain/one-window sample frame from a list of per-line quantities."""
    rows = [
        {
            "warehouse_id": warehouse_id,
            "warehouse_name": "North DC",
            "product_id": 1,
            "product_attribute_id": product_attribute_id,
            "product_name": "SKU-0001",
            "unit": "pcs",
            "qty_sales": q,
            "days": days,
            "order_id": i,
        }
        for i, q in enumerate(qtys)
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Pure IQR bound helper.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_iqr_bounds_five_samples():
    """[2,3,3,4,50] → upper 6, lower 2 (pandas linear-interpolation quantiles)."""
    upper, lower = _iqr_bounds(pd.Series([2, 3, 3, 4, 50]), single_factor=1.5, iqr_factor=1.5)
    assert upper == 6
    assert lower == 2


@pytest.mark.unit
def test_iqr_bounds_single_sample():
    """len==1 → upper = qty*1.5 = 12, lower = 0 (never an outlier)."""
    upper, lower = _iqr_bounds(pd.Series([8]), single_factor=1.5, iqr_factor=1.5)
    assert upper == 12.0
    assert lower == 0.0


@pytest.mark.unit
def test_iqr_bounds_lower_clamped_to_zero():
    """A negative lower fence is clamped to 0 ([1,2] would give lower = round(1.25-0.75)=0)."""
    # [1, 5]: q1=2, q3=4, iqr=2 → lower = round(2 - 3) = -1 → clamped to 0.
    upper, lower = _iqr_bounds(pd.Series([1, 5]), single_factor=1.5, iqr_factor=1.5)
    assert lower == 0
    assert upper >= lower


# ---------------------------------------------------------------------------
# End-to-end remove_outliers (include / exclude totals + qty_per_day floor).
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_remove_outliers_totals_drop_the_fifty(rules):
    """[2,3,3,4,50] → include-total 62, exclude-total 12; bounds 6 / 2."""
    df = _outlier_lines([2, 3, 3, 4, 50])
    result = remove_outliers(df, rules)

    incl = result[result["status_outliers"] == "include outliers"].iloc[0]
    excl = result[result["status_outliers"] == "exclude outliers"].iloc[0]

    assert incl["total_quantity"] == 62
    assert excl["total_quantity"] == 12
    assert incl["upper_bound"] == 6
    assert incl["lower_bound"] == 2
    assert excl["upper_bound"] == 6
    assert excl["lower_bound"] == 2

    # days_divider is the window length; qty_per_day = int(total/divider), floored to min 1.
    assert int(incl["days_divider"]) == 7
    assert int(incl["qty_per_day"]) == 62 // 7  # = 8
    assert int(excl["qty_per_day"]) == max(12 // 7, 1)  # = 1 (floor bumps 1 → 1)


@pytest.mark.unit
def test_remove_outliers_single_sample_kept(rules):
    """A lone sample is never flagged: include-total == exclude-total == qty."""
    df = _outlier_lines([8])
    result = remove_outliers(df, rules)

    incl = result[result["status_outliers"] == "include outliers"].iloc[0]
    excl = result[result["status_outliers"] == "exclude outliers"].iloc[0]

    assert incl["total_quantity"] == 8
    assert excl["total_quantity"] == 8
    assert incl["upper_bound"] == 12.0
    assert incl["lower_bound"] == 0.0
    # qty_per_day floored to the configured minimum (8/7 == 1, already ≥ floor).
    assert int(excl["qty_per_day"]) == int(rules.demand["qty_per_day_min"])


@pytest.mark.unit
def test_remove_outliers_qty_per_day_floor(rules):
    """A grain whose total < its divider still gets qty_per_day == the floor (1)."""
    # 3 units across the L30D window (divider 30) → int(3/30) == 0 → floored to 1.
    df = _outlier_lines([3], days="L30D")
    result = remove_outliers(df, rules)

    excl = result[result["status_outliers"] == "exclude outliers"].iloc[0]
    assert int(excl["days_divider"]) == 30
    assert int(excl["qty_per_day"]) == int(rules.demand["qty_per_day_min"]) == 1


@pytest.mark.unit
def test_remove_outliers_empty_frame(rules):
    """An empty input yields an empty frame with the locked output columns."""
    empty = _outlier_lines([]).iloc[0:0]
    result = remove_outliers(empty, rules)

    assert result.empty
    assert list(result.columns) == [
        "warehouse_id",
        "warehouse_name",
        "product_id",
        "product_attribute_id",
        "product_name",
        "unit",
        "total_quantity",
        "upper_bound",
        "lower_bound",
        "days",
        "status_outliers",
        "days_divider",
        "qty_per_day",
    ]
