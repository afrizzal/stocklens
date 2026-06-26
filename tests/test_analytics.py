"""Unit tests for the derived analytics layer (:mod:`stocklens.analytics`).

These assert the analytics *contracts* the multi-page viewer depends on — most
importantly that value roll-ups collapse the 8-rows-per-grain consolidated frame to
one row per grain before summing (the headline double-counting trap), plus the
ABC/XYZ/GMROI/days-of-cover maths and the data-quality gate. Everything is small and
deterministic; the DuckDB-backed cases use a fresh in-memory connection.
"""

from __future__ import annotations

from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest

from stocklens import analytics as A

_RUN_STAMP = pd.Timestamp("2026-06-25 12:00:00")


def _row(**over: object) -> dict[str, object]:
    """One consolidated row with every locked column defaulted; override by kwarg."""
    base: dict[str, object] = {
        "warehouse_id": 1, "warehouse_name": "North DC", "product_id": 101,
        "product_attribute_id": 101, "product_name": "Product 101", "unit": "pcs",
        "total_quantity": 30, "upper_bound": 0, "lower_bound": 0, "days": "L30D",
        "status_outliers": "exclude outliers", "days_divider": 30, "qty_per_day": 1,
        "sku": "SKU-0101", "category_id": 1, "category_name": "Staples", "brand_id": 1,
        "brand_name": "BrandOne", "position": 1, "product_status": 1,
        "product_attribute_status": 1, "divider": "General Product", "avg_lead_time": 1,
        "cycle_time": 0, "stok_belum_rilis": 0, "stok_rilis": 0, "stok_booking": 0,
        "stok_incoming": 0, "stok_gudang": 100, "status_final": 1, "status": 1,
        "adj_lead_time": 3, "PIC": "Buyer-A", "label_priority": "", "ragu_nonaktif": 0,
        "cat_flow": "Fast Moving", "qty_req": 0, "gmv": 1000.0, "total_margin": 200.0,
        "gm_rate": 0.2, "status_wl": "", "l30d_tor": 1.0, "recur_tor": 1.0,
        "running_datetime": _RUN_STAMP,
    }  # fmt: skip
    base.update(over)
    return base


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)[A.CONSOLIDATED_COLUMNS]


# ── to_grain & headline KPIs (the double-counting trap) ───────────────────────


@pytest.mark.unit
def test_to_grain_collapses_window_and_outlier_rows() -> None:
    """Eight (window × outlier) rows for one grain collapse to a single grain row."""
    rows = []
    for window in ("L7D", "L14D", "L21D", "L30D"):
        for status in ("include outliers", "exclude outliers"):
            rows.append(_row(days=window, status_outliers=status, gmv=1000.0))
    df = _frame(rows)
    assert len(df) == 8

    grain = A.to_grain(df)
    assert len(grain) == 1
    # Anchored to the preferred treatment/window.
    assert grain.iloc[0]["status_outliers"] == "exclude outliers"
    assert grain.iloc[0]["days"] == "L30D"


@pytest.mark.duckdb
def test_headline_kpis_do_not_double_count_value(mini_con: duckdb.DuckDBPyConnection) -> None:
    """GMV / margin are summed once per grain, not once per window×outlier row."""
    rows = []
    # Grain A: 4 repeated rows, gmv 1000 each (must count as 1000, not 4000).
    for window in ("L7D", "L14D", "L21D", "L30D"):
        rows.append(_row(product_id=101, product_attribute_id=101, gmv=1000.0,
                         total_margin=200.0, days=window, stok_gudang=100))  # fmt: skip
    # Grain B: 2 repeated rows, gmv 500 each.
    for window in ("L7D", "L30D"):
        rows.append(_row(product_id=102, product_attribute_id=102, sku="SKU-0102",
                         gmv=500.0, total_margin=100.0, days=window, stok_gudang=40))  # fmt: skip
    df = _frame(rows)

    kpis = A.headline_kpis(df, mini_con)
    assert kpis["grains"] == 2
    assert kpis["gmv"] == pytest.approx(1500.0)  # 1000 + 500, not 4000 + 1000
    assert kpis["total_margin"] == pytest.approx(300.0)
    assert kpis["on_hand_units"] == pytest.approx(140.0)


# ── value at cost ─────────────────────────────────────────────────────────────


@pytest.mark.duckdb
def test_value_at_cost_sums_lots_times_purchase_price(mini_con: duckdb.DuckDBPyConnection) -> None:
    """value_at_cost = Σ(remaining_quantity × purchase_price) per grain."""
    voc = A.value_at_cost(mini_con)
    grain_a = voc[(voc["product_id"] == 101) & (voc["warehouse_id"] == 1)]
    # Lots: 100 @ 7,000 and 50 @ 8,000 → 1,100,000 on 150 units.
    assert float(grain_a["on_hand_lots"].iloc[0]) == 150.0
    assert float(grain_a["value_at_cost"].iloc[0]) == pytest.approx(1_100_000.0)


# ── ABC ───────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_abc_classification_cuts_on_cumulative_share() -> None:
    """The vital-few high-GMV grain is A; the long-tail grain is C."""
    rows = [
        _row(product_id=1, product_attribute_id=1, gmv=900.0),
        _row(product_id=2, product_attribute_id=2, gmv=80.0),
        _row(product_id=3, product_attribute_id=3, gmv=20.0),
    ]
    abc = A.abc_classification(_frame(rows), cfg=A.AnalyticsConfig(abc_a_cut=0.8, abc_b_cut=0.95))
    by_pid = abc.set_index("product_id")["abc_class"].to_dict()
    assert by_pid[1] == "A"  # 90% of value
    assert by_pid[3] == "C"  # bottom of the tail


# ── days of cover / reorder ───────────────────────────────────────────────────


@pytest.mark.unit
def test_days_of_cover_rates_red_amber_green() -> None:
    """Stockout risk buckets by cover vs lead time (+ review band)."""
    rows = [
        _row(product_id=1, product_attribute_id=1, stok_gudang=5, qty_per_day=10, adj_lead_time=3),
        _row(product_id=2, product_attribute_id=2, stok_gudang=80, qty_per_day=10, adj_lead_time=3),
        _row(product_id=3, product_attribute_id=3, stok_gudang=500, qty_per_day=1, adj_lead_time=3),
    ]
    cover = A.days_of_cover(_frame(rows), now=date(2026, 6, 25)).set_index("product_id")
    assert cover.loc[1, "stockout_risk"] == "Red"  # 0.5 days < 3-day lead
    assert bool(cover.loc[1, "needs_reorder"]) is True
    assert cover.loc[2, "stockout_risk"] == "Amber"  # 8 days < lead + review(10)
    assert cover.loc[3, "stockout_risk"] == "Green"  # 500 days cover


@pytest.mark.unit
def test_reorder_worklist_suggests_refill_quantity() -> None:
    """The worklist suggests bringing the net position up to the lead+review target."""
    rows = [
        _row(product_id=1, product_attribute_id=1, stok_gudang=5, qty_per_day=10, adj_lead_time=3)
    ]
    work = A.reorder_worklist(_frame(rows), now=date(2026, 6, 25))
    assert len(work) == 1
    # target = 10/day × (3 + 7) = 100 units; net position 5 → suggest 95.
    assert int(work.iloc[0]["suggested_order_qty"]) == 95


# ── XYZ (DuckDB) ──────────────────────────────────────────────────────────────


@pytest.mark.duckdb
def test_xyz_classification_bands_by_coefficient_of_variation(
    mini_con: duckdb.DuckDBPyConnection,
) -> None:
    """Stable weekly demand → X; erratic or single-week → Z."""
    xyz = A.xyz_classification(mini_con, now=date(2026, 6, 25)).set_index("product_id")
    assert xyz.loc[101, "xyz_class"] == "X"  # 10/10/10/10 → cv 0
    assert xyz.loc[102, "xyz_class"] == "Z"  # 30 vs 2 → cv > 1
    assert xyz.loc[103, "xyz_class"] == "Z"  # single observed week


# ── GMROI (DuckDB) ────────────────────────────────────────────────────────────


@pytest.mark.duckdb
def test_gmroi_is_margin_over_inventory_value(mini_con: duckdb.DuckDBPyConnection) -> None:
    """GMROI = total_margin ÷ value_at_cost (per grain)."""
    rows = [_row(product_id=101, product_attribute_id=101, total_margin=110_000.0)]
    gm = A.gmroi(_frame(rows), mini_con)
    # Grain 101 inventory at cost = 1,100,000 → GMROI = 0.1.
    assert float(gm.iloc[0]["gmroi"]) == pytest.approx(0.1)


# ── forecasting maths ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_forecast_methods() -> None:
    s = pd.Series([2.0, 4.0, 6.0, 8.0])
    assert list(A.forecast(s, horizon=3, method="naive")) == [8.0, 8.0, 8.0]
    assert list(A.forecast(s, horizon=2, method="ma")) == [5.0, 5.0]  # mean of all (<7)
    ses = A.forecast(s, horizon=1, method="ses", alpha=0.5)
    assert ses[0] == pytest.approx(6.25)  # 2→3→4.5→6.25


@pytest.mark.unit
def test_backtest_reports_wape_against_seasonal_naive() -> None:
    rng = np.arange(1, 22, dtype=float)  # 21 points, clean trend
    bt = A.backtest(pd.Series(rng), holdout=7)
    assert set(bt["method"]) == {"seasonal_naive", "naive", "ma", "ses"}
    assert (bt["wape"] >= 0).all()


@pytest.mark.unit
def test_reorder_point_decomposition() -> None:
    rp = A.reorder_point(avg_daily_demand=10.0, lead_time_days=4.0, sigma_daily=5.0, z=1.645)
    assert rp["cycle_stock"] == pytest.approx(40.0)
    assert rp["safety_stock"] == pytest.approx(1.645 * 5.0 * 2.0)  # √4 = 2
    assert rp["reorder_point"] == pytest.approx(40.0 + 16.45)


# ── data-quality contract ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_data_quality_passes_on_clean_frame() -> None:
    df = _frame([
        _row(product_id=1, product_attribute_id=1),
        _row(product_id=2, product_attribute_id=2),
    ])  # fmt: skip
    ok, results = A.validate_consolidated(df)
    assert ok
    assert all(r.passed for r in results)


@pytest.mark.unit
def test_data_quality_flags_and_raises_on_corruption() -> None:
    df = _frame([
        _row(product_id=1, product_attribute_id=1, stok_gudang=-5),
        _row(product_id=2, product_attribute_id=2, cat_flow="Teleporting", gm_rate=2.5),
    ])  # fmt: skip
    ok, results = A.validate_consolidated(df)
    assert not ok
    failed = {r.name for r in results if not r.passed}
    assert {"stok_gudang_non_negative", "cat_flow_in_vocabulary", "gm_rate_at_most_one"} <= failed
    with pytest.raises(ValueError, match="data-quality contract failed"):
        A.raise_for_quality(df)


@pytest.mark.unit
def test_data_quality_flags_duplicate_natural_key() -> None:
    """Two identical (grain, window, outlier) rows break the natural-key contract."""
    df = _frame([
        _row(product_id=1, product_attribute_id=1),
        _row(product_id=1, product_attribute_id=1),
    ])  # fmt: skip
    ok, results = A.validate_consolidated(df)
    assert not ok
    assert any(r.name == "natural_key_unique" and not r.passed for r in results)
