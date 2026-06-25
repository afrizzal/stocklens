"""Purchasing-consolidation orchestrator — the full demand/stock/margin DAG.

Sanitized clean-room port of ``consolidate_purchasing.py`` PART 4 (notebook cells
In[111]-In[117]) plus the DAG wiring that threads the three analytic modules together.
It reproduces the original ``df_consolidate_purchasing`` row set — one row per grain ×
demand-window combination — and persists it locally.

Pipeline (mirrors the original notebook order):

1. ensure the synthetic DuckDB is seeded;
2. demand: :func:`~stocklens.demand_classify.load_orders` ->
   :func:`~stocklens.demand_classify.classify_demand` +
   :func:`~stocklens.demand_classify.remove_outliers`;
3. stock position: :func:`~stocklens.stock_position.load_stocks` /
   :func:`~stocklens.stock_position.load_product_status` /
   :func:`~stocklens.stock_position.load_stock_requests` ->
   :func:`~stocklens.stock_position.assemble_position`;
4. margin + turnover: :func:`~stocklens.margin_turnover.load_margin` ->
   :func:`~stocklens.margin_turnover.load_turnover` +
   :func:`~stocklens.margin_turnover.load_tag_relations`;
5. final merge on the grain key, the In[111]-In[113] fillna / dtype shaping, the
   ``running_datetime`` stamp, the LOCKED column projection, and ``drop_duplicates``;
6. write ``out/consolidate_purchasing_agg.parquet`` (+ a ``.csv`` sibling).

The original's object-store write and BI-server publish (In[117]-In[120]) collapse to a
single local Parquet write plus an informational ``publish_stub`` log line. No network,
SMTP, spreadsheet, object-store, or BI-publish I/O happens anywhere.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from shims import data_io
from stocklens import Rules
from stocklens.demand_classify import classify_demand, load_orders, remove_outliers
from stocklens.margin_turnover import load_margin, load_tag_relations, load_turnover
from stocklens.stock_position import (
    assemble_position,
    load_product_status,
    load_stock_requests,
    load_stocks,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

__all__ = ["run_consolidate"]

logger = logging.getLogger("stocklens.consolidate")

# Repo root = three levels up (src/stocklens/consolidate.py -> repo/).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SEED_SCRIPT = _REPO_ROOT / "seed" / "generate.py"

# Output artifact name (matches the original ``consolidate_purchasing_agg.csv`` sink).
_OUTPUT_PARQUET = "out/consolidate_purchasing_agg.parquet"

# Final consolidated column order — LOCKED by BUILD-CONTRACT §3.4. The names already
# carry the sanitized renames (qty/day -> qty_per_day, adj. lead time (hari) ->
# adj_lead_time, Label Priority -> label_priority, Ragu Dinonaktifkan (1: yes) ->
# ragu_nonaktif, Running Datetime -> running_datetime).
_FINAL_COLUMNS = [
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
    "sku",
    "category_id",
    "category_name",
    "brand_id",
    "brand_name",
    "position",
    "product_status",
    "product_attribute_status",
    "divider",
    "avg_lead_time",
    "cycle_time",
    "stok_belum_rilis",
    "stok_rilis",
    "stok_booking",
    "stok_incoming",
    "stok_gudang",
    "status_final",
    "status",
    "adj_lead_time",
    "PIC",
    "label_priority",
    "ragu_nonaktif",
    "cat_flow",
    "qty_req",
    "gmv",
    "total_margin",
    "gm_rate",
    "status_wl",
    "l30d_tor",
    "recur_tor",
    "running_datetime",
]

# Integer-typed columns of the final frame (orig In[113]). ``status_final`` /
# ``ragu_nonaktif`` / ``avg_lead_time`` etc. were already coerced upstream, but the
# outer merges can reintroduce floats (NaN-backed), so every integer column is
# re-coerced here after the fillna pass.
_INT_COLUMNS = (
    "warehouse_id",
    "product_id",
    "product_attribute_id",
    "total_quantity",
    "upper_bound",
    "lower_bound",
    "days_divider",
    "qty_per_day",
    "category_id",
    "brand_id",
    "position",
    "product_status",
    "product_attribute_status",
    "avg_lead_time",
    "cycle_time",
    "stok_belum_rilis",
    "stok_rilis",
    "stok_booking",
    "stok_incoming",
    "stok_gudang",
    "status_final",
    "status",
    "adj_lead_time",
    "ragu_nonaktif",
    "qty_req",
    "total_margin",
)

# Float-typed columns of the final frame (orig In[113]).
_FLOAT_COLUMNS = ("gmv", "gm_rate", "l30d_tor", "recur_tor")


def run_consolidate(
    con: duckdb.DuckDBPyConnection,
    rules: Rules,
    *,
    now: date | None = None,
) -> pd.DataFrame:
    """Run the full purchasing-consolidation pipeline and persist the result.

    Threads the demand, stock-position, and margin/turnover modules together exactly
    as the original notebook did, applies the In[111]-In[113] fillna / dtype shaping,
    stamps a ``running_datetime``, projects to the LOCKED column order, drops duplicate
    rows, and writes ``out/consolidate_purchasing_agg.parquet`` (plus a ``.csv``
    sibling) via :mod:`shims.data_io`.

    Args:
        con: Open DuckDB connection to the seeded database. The database is seeded
            on demand if it is missing or empty.
        rules: Loaded tunables (see :func:`stocklens.load_rules`).
        now: "As of" date driving every lookback window. Defaults to today; pass an
            explicit date for deterministic runs / tests.

    Returns:
        The final consolidated DataFrame — one row per grain × demand-window
        combination, columns in the LOCKED order of BUILD-CONTRACT §3.4.
    """
    as_of = now or date.today()
    run_stamp = datetime.now()

    _ensure_seeded(con, rules)

    # --- Stage 1: demand classification + outlier cleaning (In[9]-In[38]) ----------
    df_orders = load_orders(con, rules, now=as_of)
    df_flow = classify_demand(df_orders, rules)
    df_orders_in_ex = remove_outliers(df_orders, rules)

    # --- Stage 2: stock position + product status + requests (In[39]-In[91]) -------
    df_stocks = load_stocks(con, rules)
    df_status = load_product_status(rules)
    df_req = load_stock_requests(con, rules)
    df_position = assemble_position(df_orders_in_ex, df_stocks, df_status, df_flow, df_req, rules)

    # --- Stage 3: margin / turnover / tag relations (In[92]-In[109]) ---------------
    df_margin = load_margin(con, rules, now=as_of)
    df_tor = load_turnover(con, rules, now=as_of)
    df_tags = load_tag_relations(con, rules)

    # --- Stage 4: final merge on the grain key (In[98]-In[110]) --------------------
    # Margin joins on (product_id, unit, warehouse_id); tags on product_id; turnover
    # on (product_id, warehouse_id) — exactly the original merge sequence.
    df = df_position.merge(df_margin, how="left", on=["product_id", "unit", "warehouse_id"])
    df = df.merge(df_tags[["product_id", "status_wl"]], how="left", on=["product_id"])
    df = df.merge(
        df_tor[["product_id", "warehouse_id", "l30d_tor", "recur_tor"]],
        how="left",
        on=["product_id", "warehouse_id"],
    )

    # --- Stage 5: fillna defaults + dtype shaping + stamp (In[110]-In[114]) ---------
    df = _apply_fillna_defaults(df, rules)
    df["running_datetime"] = run_stamp
    df = _coerce_dtypes(df)

    # --- Stage 6: LOCKED projection + dedupe (In[115]-In[116]) ----------------------
    df = df[_FINAL_COLUMNS].drop_duplicates().reset_index(drop=True)

    # --- Persist (In[117]; the S3 write + BI publish collapse to a local write) -----
    out_path = data_io.write_parquet(df, _OUTPUT_PARQUET)
    data_io.publish_stub("consolidate_purchasing_aggregation")
    logger.info("consolidate: wrote %d rows -> %s", len(df), out_path)

    return df


def _apply_fillna_defaults(df: pd.DataFrame, rules: Rules) -> pd.DataFrame:
    """Apply the In[110]-In[112] no-sales / no-stock fillna defaults.

    Most grain-side defaults were already applied by ``assemble_position`` (so this is
    idempotent there); the margin / turnover / tag columns introduced by the final
    merges are defaulted here for the first time:

    * ``cat_flow`` -> ``"Slow Moving"``; ``status`` -> 0;
    * ``adj_lead_time`` -> ``stock.lead_time_fallback`` (3);
    * ``label_priority`` -> ``""``; ``PIC`` -> ``"Unassigned"`` (empty / ``"nan"`` too);
    * ``gmv`` / ``total_margin`` / ``gm_rate`` -> 0; ``status_wl`` -> ``""``;
    * ``recur_tor`` -> ``turnover.recur_fallback`` (14); ``l30d_tor`` -> 0;
    * the demand-window columns -> their no-sales defaults (``days`` /
      ``status_outliers`` -> ``"No Sales"``, numeric -> 0).
    """
    lead_time_fallback = rules.stock["lead_time_fallback"]
    recur_fallback = rules.turnover["recur_fallback"]

    df = df.copy()

    # Demand-side no-sales defaults (idempotent with assemble_position; reasserted in
    # case the margin/turnover merges reintroduced NaNs on outer-merge rows).
    df["total_quantity"] = df["total_quantity"].fillna(0)
    df["upper_bound"] = df["upper_bound"].fillna(0)
    df["lower_bound"] = df["lower_bound"].fillna(0)
    df["days"] = df["days"].fillna("No Sales")
    df["status_outliers"] = df["status_outliers"].fillna("No Sales")
    df["days_divider"] = df["days_divider"].fillna(0)
    df["qty_per_day"] = df["qty_per_day"].fillna(0)
    df["qty_req"] = df["qty_req"].fillna(0)

    # Classification / status / lead-time / label defaults (orig In[111]).
    df["cat_flow"] = df["cat_flow"].fillna("Slow Moving")
    df["status"] = df["status"].fillna(0)
    df["adj_lead_time"] = df["adj_lead_time"].fillna(lead_time_fallback)
    df["avg_lead_time"] = df["avg_lead_time"].fillna(rules.stock["avg_lead_time_fallback"])
    df["cycle_time"] = df["cycle_time"].fillna(0)
    df["ragu_nonaktif"] = df["ragu_nonaktif"].replace("", 0).fillna(0)
    df["label_priority"] = df["label_priority"].fillna("")

    # PIC normalisation (orig In[110]): empty / "nan" / NaN -> "Unassigned".
    pic = df["PIC"].astype("object")
    pic = pic.where(~pic.isin(["", "nan"]), "Unassigned")
    df["PIC"] = pic.fillna("Unassigned")

    # Margin defaults (orig In[111]).
    df["gmv"] = df["gmv"].fillna(0)
    df["total_margin"] = df["total_margin"].fillna(0)
    df["gm_rate"] = df["gm_rate"].fillna(0)

    # status_wl default (orig In[112]).
    df["status_wl"] = df["status_wl"].fillna("")

    # Turnover defaults (orig In[110] left-merge fills): no turnover history -> the
    # recur fallback (14) and a zero L30 ratio.
    df["l30d_tor"] = df["l30d_tor"].fillna(0)
    df["recur_tor"] = df["recur_tor"].fillna(recur_fallback)

    return df


def _coerce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Cast the integer / float columns to their final types (orig In[113]).

    Integer columns are coerced via ``int64`` (NaNs are already filled, so the cast is
    lossless); float columns to ``float64``. Columns absent from a given frame are
    skipped defensively, though the LOCKED schema always supplies them.
    """
    df = df.copy()
    for col in _INT_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    for col in _FLOAT_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype("float64")
    return df


def _ensure_seeded(con: duckdb.DuckDBPyConnection, rules: Rules) -> None:
    """Seed the synthetic tables into ``con`` if the database is empty.

    Population is checked through the *passed-in* connection (never a second
    ``duckdb.connect`` on the same file — DuckDB serialises separate write handles
    to one file and a parallel read-only handle would dead-lock against the
    already-open ``con``). When unseeded, the seed module's pure builder functions
    (which return DataFrames and never touch a connection) are invoked and their
    frames registered straight into ``con``, so seeding shares the one open handle
    and the committed CSV seeds are (re)written. This keeps
    ``run_consolidate(con, ...)`` self-contained on a clean checkout.

    Args:
        con: The open DuckDB connection the pipeline will read from.
        rules: Loaded tunables (``paths.product_status_csv`` / ``aging_cohort_csv``
            locate the committed CSV seeds).
    """
    if _is_populated(con):
        logger.debug("database already seeded")
        return

    logger.info("database empty, seeding synthetic tables into the open connection")
    seed = _load_seed_module()
    rng = seed.np.random.default_rng(seed.SEED)

    dims = seed.build_dimensions(rng)
    product_rtp = seed.build_product_rtp(rng)
    sales = seed.build_orders(rng, dims["products"], dims["product_attributes"])
    inventory = seed.build_inventory(rng, sales["order_logs"])
    purchasing = seed.build_purchasing(rng)
    requests = seed.build_stock_requests(rng)
    turnover_history = seed.build_turnover_history(rng)
    aging_rows, _ = seed.build_aging_cohort_rows(rng)
    sales_history = seed.build_sales_history(rng, aging_rows)

    tables: dict[str, pd.DataFrame] = {
        **dims,
        "product_rtp": product_rtp,
        **sales,
        **inventory,
        **purchasing,
        **requests,
        "turnover_history": turnover_history,
        "sales_history": sales_history,
    }
    for name, frame in tables.items():
        con.register("_seed_frame", frame)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _seed_frame")  # noqa: S608
        con.unregister("_seed_frame")

    # (Re)write the two committed CSV seeds if absent, so the product-status /
    # aging-cohort reads downstream resolve on a fresh checkout.
    data_dir = Path(rules.paths["product_status_csv"])
    data_dir = (
        (_REPO_ROOT / data_dir).resolve().parent if not data_dir.is_absolute() else data_dir.parent
    )
    if not (data_dir / "product_status.csv").is_file():
        seed.write_product_status_csv(seed.np.random.default_rng(seed.SEED), data_dir)
    if not (data_dir / "aging_cohort.csv").is_file():
        seed.write_aging_cohort_csv(aging_rows, data_dir)

    if not _is_populated(con):  # pragma: no cover - defensive
        raise RuntimeError("seed step completed but the connection still has no tables")


def _is_populated(con: duckdb.DuckDBPyConnection) -> bool:
    """Return ``True`` when ``con`` exposes at least one user table."""
    count = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchone()
    return bool(count and count[0] > 0)


def _load_seed_module():
    """Import ``seed/generate.py`` by file path and return the module object."""
    if not _SEED_SCRIPT.is_file():  # pragma: no cover - defensive
        raise FileNotFoundError(f"seed script not found: {_SEED_SCRIPT}")

    spec = importlib.util.spec_from_file_location("stocklens_seed_generate", _SEED_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"could not load seed module from {_SEED_SCRIPT}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
