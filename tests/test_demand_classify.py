"""Worked-example tests for :mod:`stocklens.demand_classify`.

Covers the three velocity/classification examples from ``STOCKLENS_PLAN`` §1
(velocity, classification) and ``BUILD-CONTRACT`` §6.4:

* **Cumulative L-window bucketing** — three movements (5@2d, 3@9d, 4@25d) roll up
  to L7=5, L14=8, L21=8, L30=12 with orderCount(L7)=1, and land in the correct
  *exclusive* ``days`` bucket.
* **Weighted score + Super/Fast/Slow boundary** — the canonical 3-grain warehouse
  (A=82, B=16.8, C=4.4 → mean 34.4, sample std ≈41.69, limit ≈76.09 → A Super
  Fast, B & C Slow).
* **Std-damp branch** — when the per-warehouse std exceeds the damp threshold the
  classification ``limit`` collapses to ``mean + std_damp_factor * std``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stocklens.demand_classify import _window_orders, classify_demand


# ---------------------------------------------------------------------------
# (1) Cumulative L7/L14/L21/L30 windows + exclusive `days` bucket.
# ---------------------------------------------------------------------------
def _order_line(*, now: date, age_days: int, qty: int, order_id: int) -> dict:
    """One synthetic, mandiri+delivered sales line aged ``age_days`` from ``now``."""
    return {
        "order_date": pd.Timestamp(now) - pd.Timedelta(days=age_days),
        "order_id": order_id,
        "order_item_id": order_id,
        "product_name": "SKU-0001",
        "product_tag": "Reguler",
        "filter_mandiri": "Include",
        "status": 2,
        "product_id": 1,
        "product_attribute_id": 1,
        "unit": "pcs",
        "qty_sales": qty,
        "warehouse_id": 1,
        "warehouse_name": "North DC",
    }


@pytest.mark.unit
def test_velocity_cumulative_windows(rules, now):
    """5@2d, 3@9d, 4@25d → cumulative L7=5, L14=8, L21=8, L30=12; orderCount(L7)=1."""
    df = pd.DataFrame(
        [
            _order_line(now=now, age_days=2, qty=5, order_id=1),
            _order_line(now=now, age_days=9, qty=3, order_id=2),
            _order_line(now=now, age_days=25, qty=4, order_id=3),
        ]
    )

    windowed = _window_orders(df, rules, now=now)

    # Exclusive `days` bucket per movement (2d→L7D, 9d→L14D, 25d→L30D).
    by_age = windowed.set_index("diff_days")["days"].to_dict()
    assert by_age == {2: "L7D", 9: "L14D", 25: "L30D"}

    # Cumulative window totals (a 9-day line still counts toward L14D, etc.).
    cumulative = {
        "L7D": int(windowed.loc[windowed["diff_days"] <= 7, "qty_sales"].sum()),
        "L14D": int(windowed.loc[windowed["diff_days"] <= 14, "qty_sales"].sum()),
        "L21D": int(windowed.loc[windowed["diff_days"] <= 21, "qty_sales"].sum()),
        "L30D": int(windowed.loc[windowed["diff_days"] <= 30, "qty_sales"].sum()),
    }
    assert cumulative == {"L7D": 5, "L14D": 8, "L21D": 8, "L30D": 12}

    # Distinct orders inside L7D feeds classification's order-count term.
    order_count_l7 = windowed.loc[windowed["diff_days"] <= 7, "order_id"].nunique()
    assert order_count_l7 == 1


@pytest.mark.unit
def test_window_filters_drop_non_include_and_low_status(rules, now):
    """The In[11] filters drop ``status<=1``, the excluded warehouse, and non-mandiri."""
    base = _order_line(now=now, age_days=3, qty=5, order_id=1)
    kept = dict(base)
    dropped_status = {**base, "order_id": 2, "status": 1}  # status not > 1
    dropped_excluded_wh = {
        **base,
        "order_id": 3,
        "warehouse_id": int(rules.stock["excluded_warehouse_id"]),
    }
    dropped_not_mandiri = {**base, "order_id": 4, "filter_mandiri": "Exclude"}

    df = pd.DataFrame([kept, dropped_status, dropped_excluded_wh, dropped_not_mandiri])
    windowed = _window_orders(df, rules, now=now)

    assert windowed["order_id"].tolist() == [1]


# ---------------------------------------------------------------------------
# (2) Weighted score (0.8/0.2) + Super/Fast/Slow boundary.
# ---------------------------------------------------------------------------
def _grain_lines(
    *, product_id: int, qty: int, n_orders: int, tag: str = "Reguler", warehouse_id: int = 1
) -> list[dict]:
    """Expand a grain into ``n_orders`` lines whose Σqty == ``qty``.

    ``classify_demand`` aggregates ``sum_qty`` and ``count_invoice`` per grain, so
    the per-line split is irrelevant to the score — only the totals matter.
    """
    base, remainder = divmod(qty, n_orders)
    lines: list[dict] = []
    for i in range(n_orders):
        line_qty = base + (remainder if i == 0 else 0)
        lines.append(
            {
                "product_id": product_id,
                "product_attribute_id": product_id,
                "warehouse_id": warehouse_id,
                "product_tag": tag,
                "order_id": product_id * 100 + i,
                "qty_sales": line_qty,
            }
        )
    return lines


@pytest.mark.unit
def test_weighted_score_and_classification_boundary(rules):
    """A(100,10)=82, B(20,4)=16.8, C(5,2)=4.4 → mean 34.4, std ≈41.69, limit ≈76.09."""
    rows: list[dict] = []
    rows += _grain_lines(product_id=1, qty=100, n_orders=10)  # A → weighted 82
    rows += _grain_lines(product_id=2, qty=20, n_orders=4)  # B → weighted 16.8
    rows += _grain_lines(product_id=3, qty=5, n_orders=2)  # C → weighted 4.4
    df = pd.DataFrame(rows)

    result = classify_demand(df, rules).set_index("product_id")

    # Weighted = 0.8*qty + 0.2*orderCount.
    assert result.loc[1, "weighted"] == pytest.approx(82.0)
    assert result.loc[2, "weighted"] == pytest.approx(16.8)
    assert result.loc[3, "weighted"] == pytest.approx(4.4)

    # Per-warehouse mean + sample std (ddof=1) → limit.
    assert result.loc[1, "avg_score"] == pytest.approx(34.4)
    assert result.loc[1, "std_score"] == pytest.approx(41.6864, abs=1e-4)
    assert result.loc[1, "limit"] == pytest.approx(76.0864, abs=1e-4)

    # 3-way bucket: A reaches the limit, B & C fall below the mean.
    assert result.loc[1, "cat_flow"] == "Super Fast Moving"
    assert result.loc[2, "cat_flow"] == "Slow Moving"
    assert result.loc[3, "cat_flow"] == "Slow Moving"


@pytest.mark.unit
def test_fast_moving_between_mean_and_limit(rules):
    """A grain scoring between the mean and the limit is ``Fast Moving``.

    Four grains in one warehouse with weighted scores well below an outsized top
    grain push the mean up so the second-highest sits in the (mean, limit) band.
    """
    rows: list[dict] = []
    rows += _grain_lines(product_id=1, qty=100, n_orders=1)  # weighted 80.2 (top)
    rows += _grain_lines(product_id=2, qty=50, n_orders=1)  # weighted 40.2 (mid)
    rows += _grain_lines(product_id=3, qty=10, n_orders=1)  # weighted 8.2
    rows += _grain_lines(product_id=4, qty=10, n_orders=1)  # weighted 8.2
    df = pd.DataFrame(rows)

    result = classify_demand(df, rules).set_index("product_id")
    mean = result.loc[2, "avg_score"]
    limit = result.loc[2, "limit"]

    # Sanity: the mid grain is genuinely in the Fast band.
    assert mean < result.loc[2, "weighted"] < limit
    assert result.loc[2, "cat_flow"] == "Fast Moving"
    assert result.loc[1, "cat_flow"] == "Super Fast Moving"


# ---------------------------------------------------------------------------
# (3) Wide-std damp branch: limit = mean + std_damp_factor * std.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_std_damp_branch(rules):
    """When std > std_damp_threshold the limit damps to mean + 0.25*std."""
    damp_threshold = float(rules.classification["std_damp_threshold"])
    damp_factor = float(rules.classification["std_damp_factor"])

    # Two grains with a huge spread → sample std comfortably above the threshold.
    rows: list[dict] = []
    rows += _grain_lines(product_id=1, qty=10_000, n_orders=1)  # weighted 8000.2
    rows += _grain_lines(product_id=2, qty=10, n_orders=1)  # weighted 8.2
    df = pd.DataFrame(rows)

    result = classify_demand(df, rules).set_index("product_id")
    mean = result.loc[1, "avg_score"]
    std = result.loc[1, "std_score"]
    limit = result.loc[1, "limit"]

    assert std > damp_threshold
    # Damped limit, NOT the plain mean + std.
    assert limit == pytest.approx(mean + damp_factor * std)
    assert limit != pytest.approx(mean + std)


@pytest.mark.unit
def test_classify_demand_empty_frame_returns_schema(rules):
    """An empty input yields the locked output columns and zero rows."""
    empty = pd.DataFrame(
        columns=[
            "product_id",
            "product_attribute_id",
            "warehouse_id",
            "product_tag",
            "order_id",
            "qty_sales",
        ]
    )
    result = classify_demand(empty, rules)

    assert result.empty
    assert list(result.columns) == [
        "product_id",
        "product_attribute_id",
        "warehouse_id",
        "weighted",
        "avg_score",
        "std_score",
        "limit",
        "cat_flow",
    ]


@pytest.mark.unit
def test_fixed_now_is_a_date(now):
    """The shared ``now`` fixture is the fixed reference date (sanity guard)."""
    assert isinstance(now, date)
