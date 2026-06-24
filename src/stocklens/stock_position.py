"""Multi-source stock-position assembly, lead/cycle time, and product-status merge.

Sanitized clean-room port of ``consolidate_purchasing.py`` PART 2 (notebook cells
In[39]-In[91]): the big ``stocks`` CTE that assembles the five stock buckets
(``stok_belum_rilis`` / ``stok_rilis`` / ``stok_booking`` / ``stok_incoming`` /
``stok_gudang``) per grain, the lead-time / cycle-time math, the product-status merge
(formerly a Google Sheet, now ``data/product_status.csv``), the orders-stocks outer
merge, the special-handling-warehouse overrides, and the stock-request merge.

Grain key everywhere: ``(warehouse_id, product_id, product_attribute_id)``. All numeric
math stays in pandas/numpy. No network, SMTP, Sheets, S3, or Tableau I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from shims import data_io

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

    from . import Rules


# Stock buckets that fillna(0) before the stok_gudang / stok_rilis arithmetic (orig In[41]).
_STOCK_FILL_ZERO = (
    "stok_belum_rilis",
    "stok_rilis_regular",
    "stok_rilis_flashsale",
    "stok_rilis_reward",
    "stok_rilis_rtp",
    "stok_booking",
    "stok_incoming",
    "cycle_time",
)

# Columns carried straight through from the SQL into the per-grain stock frame (orig In[79]).
_STOCKS_PASSTHROUGH = [
    "warehouse_id",
    "warehouse_name",
    "product_id",
    "product_attribute_id",
    "sku",
    "product_name",
    "category_id",
    "category_name",
    "brand_id",
    "brand_name",
    "unit",
    "position",
    "product_status",
    "product_attribute_status",
    "divider",
    "avg_lead_time",
    "cycle_time",
]


def load_stocks(con: duckdb.DuckDBPyConnection, rules: Rules) -> pd.DataFrame:
    """Run ``sql/stocks.sql`` and reduce it to the per-grain stock position.

    Ports In[39]-In[41] + In[79]: assemble the raw multi-bucket stock frame, fill the
    bucket NULLs with 0, then fold the four release buckets into a single ``stok_rilis``
    and compute ``stok_gudang = belum_rilis + rilis + booking``. ``avg_lead_time`` falls
    back to ``avg_lead_time_fallback`` and ``cycle_time`` to 0. ``status_final`` is 1 only
    when both ``product_status`` and ``product_attribute_status`` equal 1.

    Returns one row per ``(warehouse_id, product_id, product_attribute_id)`` with columns:
    ``warehouse_id, warehouse_name, product_id, product_attribute_id, sku, product_name,
    category_id, category_name, brand_id, brand_name, unit, position, product_status,
    product_attribute_status, divider, avg_lead_time, cycle_time, stok_belum_rilis,
    stok_rilis, stok_booking, stok_incoming, stok_gudang, status_final``.
    """
    sql = data_io.read_sql_file("stocks.sql")
    params = {
        "premium_tag_id": int(rules.classification["premium_tag_id"]),
        "po_lookback": int(rules.windows["po_lookback_months"]),
        "special_like": f"%{_special_warehouse_token(rules)}%",
    }
    df_stocks = con.execute(sql, params).df()

    for col in _STOCK_FILL_ZERO:
        df_stocks[col] = df_stocks[col].fillna(0)
    df_stocks["stok_incoming"] = df_stocks["stok_incoming"].fillna(0)

    df = df_stocks[_STOCKS_PASSTHROUGH].copy()
    df["stok_belum_rilis"] = df_stocks["stok_belum_rilis"]
    df["stok_rilis"] = (
        df_stocks["stok_rilis_regular"]
        + df_stocks["stok_rilis_flashsale"]
        + df_stocks["stok_rilis_reward"]
        + df_stocks["stok_rilis_rtp"]
    )
    df["stok_booking"] = df_stocks["stok_booking"]
    df["stok_incoming"] = df_stocks["stok_incoming"]
    df["stok_gudang"] = df["stok_belum_rilis"] + df["stok_rilis"] + df["stok_booking"]
    df["avg_lead_time"] = df["avg_lead_time"].fillna(rules.stock["avg_lead_time_fallback"])
    df["cycle_time"] = df_stocks["cycle_time"]
    df["status_final"] = np.where(
        (df_stocks["product_status"] == 1) & (df_stocks["product_attribute_status"] == 1),
        1,
        0,
    )
    return df


def load_product_status(rules: Rules) -> pd.DataFrame:
    """Read ``data/product_status.csv`` (the synthetic "Product Status" sheet) -> typed frame.

    Ports In[80]-In[82]. The CSV already carries the sanitized, renamed headers
    (``adj_lead_time``, ``label_priority``, ``ragu_nonaktif``) and only the synthetic PIC
    tokens ``Buyer-A`` / ``Buyer-B``. Rows without a ``product_id`` are dropped; the four id /
    status columns are cast to ``int64`` and ``ragu_nonaktif`` NULLs default to 0.

    Returns columns: ``product_id, product_attribute_id, warehouse_id, status, adj_lead_time,
    PIC, label_priority, ragu_nonaktif``.
    """
    df = pd.read_csv(rules.paths["product_status_csv"], dtype=str)
    df = df[df["product_id"].notna() & (df["product_id"] != "")]

    for col in ("product_id", "product_attribute_id", "warehouse_id", "status"):
        df[col] = df[col].astype("int64")
    df["ragu_nonaktif"] = df["ragu_nonaktif"].fillna(0).replace("", 0).astype("int64")
    df["label_priority"] = df["label_priority"].fillna("")

    return df[
        [
            "product_id",
            "product_attribute_id",
            "warehouse_id",
            "status",
            "adj_lead_time",
            "PIC",
            "label_priority",
            "ragu_nonaktif",
        ]
    ].reset_index(drop=True)


def load_stock_requests(con: duckdb.DuckDBPyConnection, rules: Rules) -> pd.DataFrame:
    """Aggregate requested quantity per grain over the last ``stock_request_lookback_days``.

    Ports In[87]-In[88]: per ``(product_id, customer_id, product_attribute_id, warehouse_id)``
    take ``max(quantity)`` (matching the original ``group by`` + ``max``), keeping only
    selling-price rows with ``minimum_quantity = 1``, then pivot/sum to per-grain ``qty_req``.

    Returns columns: ``product_id, product_attribute_id, warehouse_id, qty_req``.
    """
    lookback = int(rules.windows["stock_request_lookback_days"])
    sql = """
        select
            sr.product_id,
            sr.customer_id,
            sr.product_attribute_id,
            sr.warehouse_id,
            max(sr.quantity) as qty_req
        from stock_requests sr
        left join product_stocks ps
            on ps.product_attribute_id = sr.product_attribute_id
            and ps.warehouse_id = sr.warehouse_id
        left join product_selling_prices psp on psp.product_stock_id = ps.id
        where date(sr.created_at) >= date_add(current_date, to_days(-$lookback))
            and psp.minimum_quantity = 1
        group by 1, 2, 3, 4
    """
    df_req = con.execute(sql, {"lookback": lookback}).df()

    if df_req.empty:
        return pd.DataFrame(
            columns=["product_id", "product_attribute_id", "warehouse_id", "qty_req"]
        )

    return (
        pd.pivot_table(
            data=df_req,
            values="qty_req",
            index=["product_id", "product_attribute_id", "warehouse_id"],
            aggfunc="sum",
        )
        .reset_index()
    )


def assemble_position(
    df_orders_in_ex: pd.DataFrame,
    df_stocks: pd.DataFrame,
    df_status: pd.DataFrame,
    df_flow: pd.DataFrame,
    df_req: pd.DataFrame,
    rules: Rules,
) -> pd.DataFrame:
    """Merge demand, stock position, product status, classification, and requests per grain.

    Ports In[83]-In[91]:

    * outer-merge ``df_orders_in_ex`` with ``df_stocks`` on grain + ``unit`` + ``warehouse_name``;
    * left-merge product status (``status``, ``adj_lead_time``, ``PIC``, ``label_priority``,
      ``ragu_nonaktif``) and classification (``cat_flow``) on the grain;
    * apply the special-handling-warehouse overrides (orig In[86], the ``==10/==8`` rule, now
      ``rules.stock["special_warehouse_ids"]``): force ``status=1``, ``adj_lead_time=avg_lead_time``,
      ``PIC='Buyer-A'``, ``label_priority=None``;
    * apply the no-sales fillna defaults for the demand columns;
    * left-merge stock requests -> ``qty_req`` (0 when absent).

    Returns the assembled per-grain frame (one row per grain x window combination, before the
    margin/turnover joins performed by the orchestrator).
    """
    grain = ["product_id", "product_attribute_id", "warehouse_id"]

    df_merge = df_orders_in_ex.merge(
        df_stocks,
        how="outer",
        on=["product_id", "product_attribute_id", "unit", "warehouse_id", "warehouse_name"],
    )
    # Reconcile the product_name carried by both sides (orig In[85]).
    if "product_name_x" in df_merge.columns or "product_name_y" in df_merge.columns:
        df_merge["product_name"] = df_merge.get("product_name_x")
        if "product_name_y" in df_merge.columns:
            df_merge["product_name"] = df_merge["product_name"].fillna(df_merge["product_name_y"])
        df_merge = df_merge.drop(
            columns=[c for c in ("product_name_x", "product_name_y") if c in df_merge.columns]
        )

    df = df_merge.merge(df_status, how="left", on=grain)
    df = df.merge(df_flow, how="left", on=grain)

    # Special-handling-warehouse overrides (orig In[86]).
    special_ids = list(rules.stock["special_warehouse_ids"])
    is_special = df["warehouse_id"].isin(special_ids)
    df["status"] = np.where(is_special, 1, df["status"])
    df["adj_lead_time"] = np.where(is_special, df["avg_lead_time"], df["adj_lead_time"])
    df["adj_lead_time"] = np.where(
        df["adj_lead_time"].isin(["", None]) | df["adj_lead_time"].isna(),
        df["avg_lead_time"],
        df["adj_lead_time"],
    )
    df["PIC"] = np.where(is_special, "Buyer-A", df["PIC"])
    df["label_priority"] = np.where(is_special, None, df["label_priority"])
    df["ragu_nonaktif"] = df["ragu_nonaktif"].replace("", 0).fillna(0)

    # No-sales fillna defaults for the demand-side columns (orig In[86]).
    df["total_quantity"] = df["total_quantity"].fillna(0)
    df["upper_bound"] = df["upper_bound"].fillna(0)
    df["lower_bound"] = df["lower_bound"].fillna(0)
    df["days"] = df["days"].fillna("No Sales")
    df["status_outliers"] = df["status_outliers"].fillna("No Sales")
    df["days_divider"] = df["days_divider"].fillna(0)
    df["qty_per_day"] = df["qty_per_day"].fillna(0)

    # Stock-request merge (orig In[89]-In[90]).
    df = df.merge(df_req, how="left", on=grain)
    if "qty_req" in df.columns:
        df["qty_req"] = np.where(df["qty_req"].notna(), df["qty_req"], 0)
    else:
        df["qty_req"] = 0

    # Classification / PIC fillna defaults applied here so downstream merges see clean values
    # (orig In[110]-In[111] handle these after the turnover join; we default early and the
    # orchestrator's final fillna is then idempotent).
    df["cat_flow"] = df["cat_flow"].fillna("Slow Moving")
    df["adj_lead_time"] = df["adj_lead_time"].fillna(rules.stock["lead_time_fallback"])
    pic = df["PIC"].astype("object")
    pic = pic.where(~pic.isin(["", "nan"]), "Unassigned")
    df["PIC"] = pic.fillna("Unassigned")

    return df


def _special_warehouse_token(rules: Rules) -> str:
    """Resolve the warehouse-name LIKE token for the "Exclusivity" divider rule.

    The seed names the special-handling warehouse ``RTP DC`` (BUILD-CONTRACT §1.2); the SQL's
    ``divider`` CASE flags any warehouse whose name contains this token as ``Exclusivity`` (the
    synthetic replacement for the original internal warehouse-brand rule).
    """
    return "RTP DC"
