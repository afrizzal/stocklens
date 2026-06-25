"""Aging-stock detection + alert report (sanitized port of ``aging_stock_alert_nz.py``).

The original production job read an aging extract from a BI view, joined a
product-segmentation lookup for the RTP category, applied category-differentiated
age thresholds (Daily Needs >= 15d / Lifestyle >= 31d), joined the last-7-day
sell-out from a warehouse sales fact, rendered an HTML table and **emailed** it to
a hardcoded recipient list, then wrote the full table back to a shared spreadsheet.

This standalone showcase reproduces the *logic* with every live side-effect
removed (see ``docs/planning/BUILD-CONTRACT.md`` §0):

* The BI aging extract becomes the committed, synthetic ``data/aging_cohort.csv``.
* The product-segmentation lookup becomes the synthetic ``product_rtp`` table.
* The warehouse sales fact becomes the synthetic ``sales_history`` DuckDB table,
  queried via DuckDB directly (never ``pandasql``).
* The email blast and the spreadsheet writes become a local HTML + MD report
  written to ``out/`` via :mod:`shims.report` (no network, no mail send, no
  spreadsheet writes, no live links, generic "Dear Purchasing Team" greeting).

The category split uses the synthetic tunables ``daily_needs_category`` /
``daily_needs_subcategory_like`` (default ``"Staples"`` / ``"Flour"``) instead of
the original domain-specific category tokens.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from shims import data_io, report

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import duckdb

    from stocklens import Rules


__all__ = [
    "load_cohort",
    "categorize_and_filter",
    "join_sell_out",
    "run_aging_alert",
    "run_aging",
]


# Grouping grain for the aged-cohort aggregation (mirrors the original
# ``group by 1,2,3,4`` over product / unit / warehouse / category).
_COHORT_GROUP_KEYS = ["product_id", "product_unit", "warehouse_name", "Category"]

# Column order of the per-category display tables in the rendered report.
_REPORT_COLUMNS = [
    "warehouse_name",
    "product_unit",
    "Category",
    "stok_gudang_tanpa_booking",
    "total_purchase_stok_tanpa_booking",
    "qty_sell_out",
    "gmv",
]


def load_cohort(rules: Rules) -> pd.DataFrame:
    """Read ``data/aging_cohort.csv`` (the committed BI-view replacement).

    Columns (per BUILD-CONTRACT §1.4)::

        product_id, product_unit, warehouse_name, diff_days_inhouse,
        stok_gudang_tanpa_booking, total_purchase_stok_tanpa_booking, status_wl

    Returns a typed DataFrame with numeric columns coerced and string columns
    left as-is. ``status_wl`` NaN is normalised to the empty string so the
    ``LIKE 'WL%'`` filter never trips on a missing value.
    """
    csv_path = Path(rules.paths["aging_cohort_csv"])
    df = pd.read_csv(csv_path)

    df["product_id"] = df["product_id"].astype("int64")
    df["diff_days_inhouse"] = df["diff_days_inhouse"].astype("int64")
    df["stok_gudang_tanpa_booking"] = df["stok_gudang_tanpa_booking"].astype("float64")
    df["total_purchase_stok_tanpa_booking"] = df["total_purchase_stok_tanpa_booking"].astype(
        "float64"
    )
    df["product_unit"] = df["product_unit"].astype("string").fillna("")
    df["warehouse_name"] = df["warehouse_name"].astype("string").fillna("")
    df["status_wl"] = df["status_wl"].astype("string").fillna("")

    return df


def categorize_and_filter(
    df_cohort: pd.DataFrame,
    con: duckdb.DuckDBPyConnection,
    rules: Rules,
) -> pd.DataFrame:
    """Join ``product_rtp`` for the RTP category, classify, threshold, aggregate.

    Mirrors the original In-cells that:

    1. join the ``product_rtp`` segmentation table on ``product_id`` for
       ``rtp_category`` / ``rtp_sub_category``;
    2. derive ``Category`` = ``"Daily Needs"`` when
       ``rtp_category == daily_needs_category`` **or**
       ``rtp_sub_category`` contains ``daily_needs_subcategory_like`` (synthetic
       category tokens, default ``"Staples"`` / ``"Flour"``), else ``"Lifestyle"``;
    3. keep rows aged past their category threshold
       (Daily Needs ``>= daily_needs_days``, Lifestyle ``>= lifestyle_days``);
    4. drop the excluded (consignment) warehouses and keep only
       ``status_wl LIKE 'WL%'``;
    5. aggregate ``stok``/``total_purchase`` per
       ``(product_id, product_unit, warehouse_name, Category)``.

    The original's ``sqldf`` group-by is re-expressed as a pandas ``groupby`` (no
    ``pandasql``); the ``product_rtp`` lookup uses DuckDB directly.
    """
    aging_cfg = rules.aging

    # --- 1. RTP-category lookup (DuckDB, not pandasql) -----------------------
    product_ids = sorted({int(pid) for pid in df_cohort["product_id"].tolist()})
    rtp = _load_product_rtp(con, product_ids)

    # Keep one category row per product (the original joins on product_id only).
    rtp = rtp.drop_duplicates(subset=["product_id"], keep="first")

    df = df_cohort.merge(
        rtp[["product_id", "rtp_category", "rtp_sub_category"]],
        on="product_id",
        how="left",
    )

    # --- 2. Category split ---------------------------------------------------
    daily_category = str(aging_cfg["daily_needs_category"])
    daily_sub_like = str(aging_cfg["daily_needs_subcategory_like"])

    rtp_category = df["rtp_category"].astype("string").fillna("")
    rtp_sub = df["rtp_sub_category"].astype("string").fillna("")
    is_daily = (rtp_category == daily_category) | rtp_sub.str.contains(
        daily_sub_like, case=False, na=False
    )
    df["Category"] = pd.Series(
        ["Daily Needs" if flag else "Lifestyle" for flag in is_daily],
        index=df.index,
        dtype="object",
    )

    # --- 3. Category-differentiated age threshold ----------------------------
    daily_days = int(aging_cfg["daily_needs_days"])
    lifestyle_days = int(aging_cfg["lifestyle_days"])
    aged_mask = ((df["Category"] == "Daily Needs") & (df["diff_days_inhouse"] >= daily_days)) | (
        (df["Category"] == "Lifestyle") & (df["diff_days_inhouse"] >= lifestyle_days)
    )
    df = df[aged_mask].copy()

    # --- 4. Warehouse exclusion + WL filter (the original's sqldf WHERE) ------
    exclude_like = str(aging_cfg["exclude_warehouse_name_like"])
    wl_prefix = str(aging_cfg["status_wl_prefix"])

    keep_wh = ~df["warehouse_name"].astype("string").str.contains(
        exclude_like, case=False, na=False
    )
    keep_wl = df["status_wl"].astype("string").str.startswith(wl_prefix, na=False)
    df = df[keep_wh & keep_wl].copy()

    # --- 5. Aggregate per (product, unit, warehouse, category) ---------------
    if df.empty:
        return pd.DataFrame(
            columns=[
                *_COHORT_GROUP_KEYS,
                "stok_gudang_tanpa_booking",
                "total_purchase_stok_tanpa_booking",
            ]
        )

    df_aged = df.groupby(_COHORT_GROUP_KEYS, as_index=False, sort=True).agg(
        stok_gudang_tanpa_booking=("stok_gudang_tanpa_booking", "sum"),
        total_purchase_stok_tanpa_booking=(
            "total_purchase_stok_tanpa_booking",
            "sum",
        ),
    )
    return df_aged


def join_sell_out(
    df_aged: pd.DataFrame,
    con: duckdb.DuckDBPyConnection,
    rules: Rules,
    *,
    now: date,
) -> dict[str, pd.DataFrame]:
    """Left-join the last-``sell_out_lookback_days`` sell-out and build the tables.

    Queries the synthetic ``sales_history`` fact via DuckDB (excluding
    ``order_item_type = 'reward'``, matching the original
    ``order_item_type <> 'reward'``), aggregates ``qty_sell_out`` and ``gmv`` per
    ``(product_id, warehouse_name)``, left-joins onto the aged cohort, coalesces
    the sell-out columns to 0, then materialises three display frames:

    * ``daily_needs`` — ``Category == 'Daily Needs'``
    * ``lifestyle``   — ``Category == 'Lifestyle'``
    * ``all``         — every aged row

    Each frame is rounded and integer-cast exactly as the original ``sqldf``
    blocks did (``round(...)`` then ``astype('int64')``).
    """
    lookback_days = int(rules.windows["sell_out_lookback_days"])
    product_ids = (
        sorted({int(pid) for pid in df_aged["product_id"].tolist()}) if not df_aged.empty else []
    )
    df_revenue = _load_sell_out(con, product_ids, now=now, lookback_days=lookback_days)

    merged = df_aged.merge(
        df_revenue,
        on=["product_id", "warehouse_name"],
        how="left",
    )

    daily_needs = _build_display_table(merged, category="Daily Needs")
    lifestyle = _build_display_table(merged, category="Lifestyle")
    all_data = _build_display_table(merged, category=None)

    return {"daily_needs": daily_needs, "lifestyle": lifestyle, "all": all_data}


def run_aging_alert(
    config: Rules,
    con: duckdb.DuckDBPyConnection,
    *,
    now: date | None = None,
) -> dict[str, pd.DataFrame]:
    """Orchestrate the aging pipeline and render the local HTML + MD report.

    This is the callable named in the BUILD-CONTRACT (``run_aging_alert(config, con)``).
    It runs :func:`load_cohort` -> :func:`categorize_and_filter` ->
    :func:`join_sell_out`, then renders ``out/aging_report.html`` and
    ``out/aging_report.md`` via :func:`shims.report.save_report` and writes
    ``out/last_refreshed.csv`` metadata. **No email is sent and no Google Sheet
    is written** — the recipients/sender from config appear only as displayed
    report metadata.

    Returns the dict of display frames (``daily_needs`` / ``lifestyle`` / ``all``).
    """
    run_now = now or date.today()

    df_cohort = load_cohort(config)
    df_aged = categorize_and_filter(df_cohort, con, config)
    frames = join_sell_out(df_aged, con, config, now=run_now)

    _render_report(frames, config, now=run_now)
    _write_last_refreshed(config)

    return frames


# Backwards-compatible alias matching BUILD-CONTRACT §3.5's ``run_aging`` name.
def run_aging(
    con: duckdb.DuckDBPyConnection,
    rules: Rules,
    *,
    now: date | None = None,
) -> dict[str, pd.DataFrame]:
    """Alias for :func:`run_aging_alert` with the ``(con, rules)`` argument order.

    The contract lists both ``run_aging_alert(config, con)`` (the required
    callable) and ``run_aging(con, rules, *, now)`` (the §3.5 orchestrator
    signature). Both drive the identical pipeline.
    """
    return run_aging_alert(rules, con, now=now)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_product_rtp(con: duckdb.DuckDBPyConnection, product_ids: list[int]) -> pd.DataFrame:
    """Fetch ``product_rtp`` rows for the cohort's products via DuckDB."""
    columns = ["product_id", "rtp_category", "rtp_sub_category", "status_wl"]
    if not product_ids:
        return pd.DataFrame(columns=columns)

    id_list = ", ".join(str(pid) for pid in product_ids)
    sql = (
        "select distinct product_id, rtp_category, rtp_sub_category, status_wl "
        "from product_rtp "
        f"where product_id in ({id_list})"
    )
    df = data_io.get_data(sql, con)
    # Guarantee the expected columns even if the seed yielded no matches.
    for col in columns:
        if col not in df.columns:
            df[col] = pd.Series(dtype="object")
    return df[columns]


def _load_sell_out(
    con: duckdb.DuckDBPyConnection,
    product_ids: list[int],
    *,
    now: date,
    lookback_days: int,
) -> pd.DataFrame:
    """Aggregate last-N-day sell-out from ``sales_history`` (reward excluded).

    Port of the original revenue query against the warehouse sales view:
    ``order_date > now - lookback_days`` and ``order_item_type <> 'reward'``,
    grouped to ``qty_sell_out`` / ``gmv`` per ``(product_id, warehouse_name)``.
    Run through DuckDB directly (never ``pandasql``).
    """
    out_columns = ["product_id", "warehouse_name", "qty_sell_out", "gmv"]
    if not product_ids:
        return pd.DataFrame(
            {
                "product_id": pd.Series(dtype="int64"),
                "warehouse_name": pd.Series(dtype="object"),
                "qty_sell_out": pd.Series(dtype="float64"),
                "gmv": pd.Series(dtype="float64"),
            }
        )

    id_list = ", ".join(str(pid) for pid in product_ids)
    start = (now - pd.Timedelta(days=lookback_days)).isoformat()
    end = now.isoformat()
    sql = (
        "select product_id, warehouse_name, "
        "sum(quantity) as qty_sell_out, sum(gmv) as gmv "
        "from sales_history "
        f"where order_date > date '{start}' and order_date <= date '{end}' "
        "and order_item_type <> 'reward' "
        f"and product_id in ({id_list}) "
        "group by product_id, warehouse_name"
    )
    df = data_io.get_data(sql, con)
    for col in out_columns:
        if col not in df.columns:
            df[col] = pd.Series(dtype="float64")
    return df[out_columns]


def _build_display_table(merged: pd.DataFrame, *, category: str | None) -> pd.DataFrame:
    """Round + integer-cast a per-category (or all) display table.

    Reproduces the original ``sqldf`` rounding blocks: coalesce sell-out to 0,
    round every numeric column to whole units, then cast to ``int64``. The
    grouping re-collapses to ``(warehouse_name, product_unit, Category)`` exactly
    as the original ``group by 1,2,3``.
    """
    df = merged if category is None else merged[merged["Category"] == category]

    if df.empty:
        empty = pd.DataFrame(columns=_REPORT_COLUMNS)
        for col in (
            "stok_gudang_tanpa_booking",
            "total_purchase_stok_tanpa_booking",
            "qty_sell_out",
            "gmv",
        ):
            empty[col] = empty[col].astype("int64")
        return empty

    grouped = df.groupby(
        ["warehouse_name", "product_unit", "Category"],
        as_index=False,
        sort=True,
    ).agg(
        stok_gudang_tanpa_booking=("stok_gudang_tanpa_booking", "sum"),
        total_purchase_stok_tanpa_booking=(
            "total_purchase_stok_tanpa_booking",
            "sum",
        ),
        qty_sell_out=("qty_sell_out", "sum"),
        gmv=("gmv", "sum"),
    )

    for col in (
        "stok_gudang_tanpa_booking",
        "total_purchase_stok_tanpa_booking",
        "qty_sell_out",
        "gmv",
    ):
        grouped[col] = grouped[col].fillna(0).round(0).astype("int64")

    return grouped[_REPORT_COLUMNS]


def _render_report(frames: dict[str, pd.DataFrame], rules: Rules, *, now: date) -> tuple[str, str]:
    """Render the HTML + MD aging report to ``out/`` (no email, no Sheets).

    Builds the :func:`shims.report.save_report` context: a generic greeting, the
    generation timestamp, the (display-only) recipient/sender metadata, and the
    two category tables already converted to HTML/MD by the report shim. The
    original's email body and Sheets push are entirely replaced by these files.
    """
    report_cfg = rules.report
    output_dir = Path(report_cfg.get("output_dir", "out"))
    html_path = str(output_dir / "aging_report.html")
    md_path = str(output_dir / "aging_report.md")

    context = {
        "title": "Aging Stock - WL Product",
        "greeting": report_cfg.get("team_greeting", "Dear Purchasing Team"),
        "signature": report_cfg.get("signature", "Regards, Analytics"),
        "generated_at": datetime.combine(now, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S"),
        "sender": report_cfg.get("sender", ""),
        "recipients": list(report_cfg.get("recipients", [])),
        "intro_daily_needs": (
            "Detail of aged WL Daily-Needs stock (in house beyond the "
            "Daily-Needs threshold), with the last 7 days of sell-out."
        ),
        "intro_lifestyle": (
            "Detail of aged WL Lifestyle stock (in house beyond the "
            "Lifestyle threshold), with the last 7 days of sell-out."
        ),
        "tables": {
            "daily_needs": frames["daily_needs"],
            "lifestyle": frames["lifestyle"],
        },
    }

    return report.save_report(context, html_path=html_path, md_path=md_path)


def _write_last_refreshed(rules: Rules) -> str:
    """Write ``out/last_refreshed.csv`` (the open analogue of the Sheets tab).

    The original wrote a ``last_refreshed`` timestamp to a Google Sheet worksheet;
    here it becomes a tiny local CSV so the showcase still records the run time
    without any Sheets call.
    """
    output_dir = Path(rules.report.get("output_dir", "out"))
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "last_refreshed.csv"
    last_refreshed = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame({"last_refreshed": [last_refreshed]}).to_csv(path, index=False)
    return str(path)
