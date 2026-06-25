"""Demand classification: rolling windows, weighted score, and IQR outlier cleaning.

Clean-room port of ``consolidate_purchasing.py`` PART 1 (notebook cells In[9]-In[38]):

* :func:`load_orders` pulls the last ``sales_lookback_days`` of sales lines via
  ``sql/orders.sql``, applies the mandiri / status / excluded-warehouse filters,
  and tags each line with its age (``diff_days``), the cumulative ``L7D``/``L14D``/
  ``L21D``/``L30D`` flags, and its exclusive ``days`` bucket.
* :func:`classify_demand` computes a weighted velocity score per grain
  (``0.8 * qty + 0.2 * order_count``), benchmarks it against the per-warehouse(,tag)
  mean + std limit (with a wide-std damp rule), and assigns Super Fast / Fast /
  Slow Moving.
* :func:`remove_outliers` strips statistical outliers per ``(warehouse, window,
  product_attribute)`` with an IQR rule (plus the single-sample special branch),
  exposes both include-outliers and exclude-outliers totals, and derives the
  ``qty_per_day`` demand rate.

All numeric math stays in pandas/numpy and matches the pandas defaults the original
relied on (``quantile`` linear interpolation, ``std`` with ``ddof=1``). No network or
external-service I/O happens here -- the functions are pure over DataFrames given a
:class:`~stocklens.Rules` config object.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    import duckdb

    from stocklens import Rules

__all__ = ["load_orders", "classify_demand", "remove_outliers"]


# Window label -> divider used for the qty/day demand rate.
_WINDOW_DIVIDERS: dict[str, int] = {"L7D": 7, "L14D": 14, "L21D": 21, "L30D": 30}


def load_orders(
    con: duckdb.DuckDBPyConnection,
    rules: Rules,
    *,
    now: date,
) -> pd.DataFrame:
    """Load and window the sales lines that feed demand classification.

    Runs ``sql/orders.sql`` over the last ``windows.sales_lookback_days`` days,
    applies the original In[11] filters (kept ``status > 1``, excluded warehouse
    dropped, ``filter_mandiri == 'Include'`` only), then derives the age columns.

    The ``days`` bucket is the *exclusive* window a line falls into, while the
    ``L7D``..``L30D`` flags are *cumulative* (a line aged 9 days has ``days='L14D'``
    but both ``L7D='Y'`` and ``L14D='Y'``), mirroring the original cells In[23]-In[25].

    Parameters
    ----------
    con:
        Open DuckDB connection to the seeded ``stocklens.duckdb``.
    rules:
        Loaded configuration; uses ``windows.sales_lookback_days``,
        ``classification.premium_tag_id`` and ``stock.excluded_warehouse_id`` /
        ``stock.include_mandiri_only``.
    now:
        "As of" date; the lookback window ends here and ages are measured from it.

    Returns
    -------
    pandas.DataFrame
        Columns: ``order_date, order_id, order_item_id, product_name, product_tag,
        product_id, product_attribute_id, unit, qty_sales, warehouse_id,
        warehouse_name, diff_days, days``.
    """
    # Imported lazily so the module loads even before the shims package is built,
    # and so the heavy DuckDB dependency is only required at call time.
    from shims import data_io  # noqa: PLC0415

    lookback = int(rules.windows["sales_lookback_days"])
    start = now - timedelta(days=lookback)
    premium_tag_id = int(rules.classification["premium_tag_id"])

    # sql/orders.sql carries named placeholders ($start, $end, $premium_tag_id);
    # DuckDB binds them from this dict. read_sql_file resolves the path under sql/.
    sql = data_io.read_sql_file("orders.sql")
    params = {
        "start": start.isoformat(),
        "end": now.isoformat(),
        "premium_tag_id": premium_tag_id,
    }
    df = con.execute(sql, params).df()

    return _window_orders(df, rules, now=now)


def _window_orders(df: pd.DataFrame, rules: Rules, *, now: date) -> pd.DataFrame:
    """Apply the In[11] filters and the age/window tagging (In[20]-In[25]).

    Split out from :func:`load_orders` so the windowing logic is testable without
    a live DuckDB connection.
    """
    excluded_wh = int(rules.stock["excluded_warehouse_id"])
    include_only = bool(rules.stock["include_mandiri_only"])

    df = df.copy()

    # In[11]: keep delivered+ lines, drop the excluded (consignment) warehouse,
    # and -- when configured -- keep only mandiri ("Include") lines.
    df = df[df["status"] > 1]
    df = df[df["warehouse_id"] != excluded_wh]
    if include_only:
        df = df[df["filter_mandiri"] == "Include"]
    df = df.drop_duplicates(keep="last").reset_index(drop=True)

    # In[21]-In[22]: integer age in days from `now`.
    order_dates = pd.to_datetime(df["order_date"])
    now_ts = pd.Timestamp(now)
    diff_days = (now_ts - order_dates).dt.days.astype("int64")
    df["diff_days"] = diff_days

    # In[24]: exclusive window bucket. Anything older than 21d folds into L30D
    # (the pipeline only looks back 30 days, enforced by load_orders' SQL window).
    conditions = [
        diff_days <= 7,
        (diff_days > 7) & (diff_days <= 14),
        (diff_days > 14) & (diff_days <= 21),
        diff_days > 21,
    ]
    df["days"] = np.select(conditions, ["L7D", "L14D", "L21D", "L30D"], default="L30D")

    keep = [
        "order_date",
        "order_id",
        "order_item_id",
        "product_name",
        "product_tag",
        "product_id",
        "product_attribute_id",
        "unit",
        "qty_sales",
        "warehouse_id",
        "warehouse_name",
        "diff_days",
        "days",
    ]
    return df[keep].reset_index(drop=True)


def classify_demand(df_orders: pd.DataFrame, rules: Rules) -> pd.DataFrame:
    """Classify each grain as Super Fast / Fast / Slow Moving (In[12]-In[19]).

    The weighted velocity score is ``weight_qty * Σqty + weight_orders * #invoices``
    summed over all windows (the original pivots the full 30-day frame). Within each
    ``(warehouse_id, product_tag)`` group it computes the mean and **sample** std
    (``ddof=1``) of that score; the classification ``limit`` is ``mean + std`` unless
    the std is "wide" (``> std_damp_threshold``), in which case it is damped to
    ``mean + std_damp_factor * std``. A grain is Super Fast when its score reaches the
    limit, Fast when it reaches the mean, else Slow.

    Parameters
    ----------
    df_orders:
        Output of :func:`load_orders` (needs ``product_id``, ``product_attribute_id``,
        ``warehouse_id``, ``product_tag``, ``order_id``, ``qty_sales``).
    rules:
        Uses ``classification.weight_qty``, ``weight_orders``, ``std_damp_threshold``
        and ``std_damp_factor``.

    Returns
    -------
    pandas.DataFrame
        Columns: ``product_id, product_attribute_id, warehouse_id, weighted,
        avg_score, std_score, limit, cat_flow``.
    """
    weight_qty = float(rules.classification["weight_qty"])
    weight_orders = float(rules.classification["weight_orders"])
    damp_threshold = float(rules.classification["std_damp_threshold"])
    damp_factor = float(rules.classification["std_damp_factor"])

    grain = ["product_id", "product_attribute_id", "warehouse_id", "product_tag"]

    if df_orders.empty:
        cols = [
            "product_id",
            "product_attribute_id",
            "warehouse_id",
            "weighted",
            "avg_score",
            "std_score",
            "limit",
            "cat_flow",
        ]
        return pd.DataFrame(columns=cols)

    # In[12]-In[14]: per-grain Σqty + #invoices -> weighted score.
    agg = df_orders.groupby(grain, as_index=False).agg(
        sum_qty=("qty_sales", "sum"), count_invoice=("order_id", "count")
    )
    agg["weighted"] = weight_qty * agg["sum_qty"] + weight_orders * agg["count_invoice"]

    # In[15]: per-(warehouse, tag) mean & sample std of the weighted score.
    group_keys = ["warehouse_id", "product_tag"]
    agg["avg_score"] = agg.groupby(group_keys)["weighted"].transform("mean")
    # pandas .std() uses ddof=1; a single-element group yields NaN (matches the original).
    agg["std_score"] = agg.groupby(group_keys)["weighted"].transform("std")

    # In[16]-In[17]: limit rule with the wide-std damp branch. NaN std (n==1 group)
    # leaves limit == NaN, exactly as the original notebook propagated it.
    agg["limit"] = np.where(
        agg["std_score"] > damp_threshold,
        agg["avg_score"] + damp_factor * agg["std_score"],
        agg["avg_score"] + agg["std_score"],
    )

    # In[18]: 3-way classification. `weighted >= limit` -> Super Fast (NaN limit
    # comparison is False, so such grains fall through to the mean test).
    agg["cat_flow"] = np.where(
        agg["weighted"] >= agg["limit"],
        "Super Fast Moving",
        np.where(agg["weighted"] >= agg["avg_score"], "Fast Moving", "Slow Moving"),
    )

    return agg[
        [
            "product_id",
            "product_attribute_id",
            "warehouse_id",
            "weighted",
            "avg_score",
            "std_score",
            "limit",
            "cat_flow",
        ]
    ].reset_index(drop=True)


def _iqr_bounds(qty: pd.Series, *, single_factor: float, iqr_factor: float) -> tuple[float, float]:
    """Compute the (upper, lower) outlier bounds for one grain/window sample set.

    * ``len == 1`` -> ``upper = qty * single_factor``, ``lower = 0`` (a lone sample is
      never an outlier).
    * ``len > 1``  -> Tukey IQR fences ``q3 + iqr_factor*iqr`` / ``q1 - iqr_factor*iqr``,
      rounded; quantiles use the pandas default (linear interpolation, type 7).

    The lower fence is clamped to ``>= 0`` here (the original also clamps again at the
    concatenated-frame level in In[36]).
    """
    n = len(qty)
    if n == 1:
        upper = float(qty.iloc[0]) * single_factor
        lower = 0.0
    else:
        q1 = qty.quantile(0.25)
        q3 = qty.quantile(0.75)
        iqr = q3 - q1
        upper = round(q3 + iqr_factor * iqr)
        lower = round(q1 - iqr_factor * iqr)
    if lower < 0:
        lower = 0
    return upper, lower


def remove_outliers(df_orders: pd.DataFrame, rules: Rules) -> pd.DataFrame:
    """Clean per-window demand outliers and derive the qty/day demand rate.

    Ports In[29]-In[38]. For every ``(warehouse_id, days, product_attribute_id)``
    sample set it derives IQR fences (single-sample special case included), flags
    samples outside the fences, and aggregates the per-grain total **twice**: once
    keeping every line (``status_outliers='include outliers'``) and once dropping the
    flagged lines (``status_outliers='exclude outliers'``). Each total is divided by
    its window length to give ``qty_per_day``, floored to ``demand.qty_per_day_min``.

    Parameters
    ----------
    df_orders:
        Output of :func:`load_orders`.
    rules:
        Uses ``demand.outlier_single_row_factor``, ``demand.iqr_factor`` and
        ``demand.qty_per_day_min``.

    Returns
    -------
    pandas.DataFrame
        Columns: ``warehouse_id, warehouse_name, product_id, product_attribute_id,
        product_name, unit, total_quantity, upper_bound, lower_bound, days,
        status_outliers, days_divider, qty_per_day``.
    """
    single_factor = float(rules.demand["outlier_single_row_factor"])
    iqr_factor = float(rules.demand["iqr_factor"])
    qty_per_day_min = int(rules.demand["qty_per_day_min"])

    out_cols = [
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
    if df_orders.empty:
        return pd.DataFrame(columns=out_cols)

    grain_cols = [
        "warehouse_id",
        "warehouse_name",
        "product_id",
        "product_attribute_id",
        "upper_bound",
        "lower_bound",
    ]
    agg_spec = {"product_name": "min", "unit": "min", "qty_sales": "sum"}

    incl_frames: list[pd.DataFrame] = []
    excl_frames: list[pd.DataFrame] = []

    # In[29]-In[30]: bounds + outlier flags per (warehouse, window, product_attribute).
    for (_warehouse_id, period), grp in df_orders.groupby(["warehouse_id", "days"], sort=False):
        tagged: list[pd.DataFrame] = []
        for _, sub in grp.groupby("product_attribute_id", sort=False):
            upper, lower = _iqr_bounds(
                sub["qty_sales"], single_factor=single_factor, iqr_factor=iqr_factor
            )
            sub = sub.copy()
            sub["upper_bound"] = upper
            sub["lower_bound"] = lower
            sub["status_outliers_flag"] = np.where(
                (sub["qty_sales"] < lower) | (sub["qty_sales"] > upper), 1, 0
            )
            tagged.append(sub)

        clean = pd.concat(tagged, ignore_index=True)

        # In[33]/In[35]: include-outliers total (all lines).
        incl = clean.groupby(grain_cols, as_index=False).agg(agg_spec)
        incl["days"] = period
        incl_frames.append(incl)

        # In[31]/In[34]: exclude-outliers total (flagged lines dropped).
        kept = clean[clean["status_outliers_flag"] == 0]
        if not kept.empty:
            excl = kept.groupby(grain_cols, as_index=False).agg(agg_spec)
            excl["days"] = period
            excl_frames.append(excl)

    df_incl = _finalize_outlier_frame(incl_frames, "include outliers")
    df_excl = _finalize_outlier_frame(excl_frames, "exclude outliers")

    # In[36]: stack both variants; re-clamp the lower bound defensively.
    df = pd.concat([df_excl, df_incl], ignore_index=True)
    df["lower_bound"] = np.where(df["lower_bound"] < 0, 0, df["lower_bound"])

    # In[37]: window length divider.
    df["days_divider"] = df["days"].map(_WINDOW_DIVIDERS).astype("int64")

    # In[38]: integer demand rate, floored to the configured minimum.
    qty_per_day = (df["total_quantity"] / df["days_divider"]).astype("int64")
    df["qty_per_day"] = np.where(qty_per_day < qty_per_day_min, qty_per_day_min, qty_per_day)

    return df[out_cols].reset_index(drop=True)


def _finalize_outlier_frame(frames: list[pd.DataFrame], status: str) -> pd.DataFrame:
    """Concatenate per-window aggregates and apply the In[34]/In[35] shaping."""
    cols = [
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
    ]
    if not frames:
        return pd.DataFrame(columns=cols)

    df = pd.concat(frames, ignore_index=True)
    df = df.rename(columns={"qty_sales": "total_quantity"})
    df["status_outliers"] = status
    df = df.sort_values(by=["warehouse_id", "product_id", "product_attribute_id", "total_quantity"])
    return df[cols].reset_index(drop=True)
