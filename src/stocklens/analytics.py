"""Derived inventory analytics over the consolidated demand/stock frame.

This module is the **read-only analytics layer** that the multi-page viewer
(:mod:`app`) and any future API are built on. It takes the artifacts the locked
pipeline already produces — the consolidated per-grain Parquet
(:func:`stocklens.consolidate.run_consolidate`) and the seeded ``stocklens.duckdb`` —
and derives the operational and financial metrics a buyer / planner actually acts
on, **without modifying the pipeline or its LOCKED output schema** (BUILD-CONTRACT
§3.4). Everything here is pure pandas/numpy over DataFrames plus read-only DuckDB
queries; no value is hard-coded that the pipeline already exposes, and there is no
network, write, or other side-effect.

The single most important fact about the consolidated frame is its **grain**: it
holds up to eight rows per ``(warehouse_id, product_id, product_attribute_id)`` —
the cross product of the four rolling windows (``L7D``/``L14D``/``L21D``/``L30D``)
and the two outlier treatments (``include outliers`` / ``exclude outliers``). The
stock, margin, classification and turnover columns are **repeated** across those
rows, so any value/stock roll-up MUST first collapse to one row per grain
(:func:`to_grain`) or it will multiply totals ~8×. Every function here that reports
a value KPI goes through :func:`to_grain` first.

Function groups
---------------
* **Grain & value** — :func:`to_grain`, :func:`value_at_cost`,
  :func:`attach_value_at_cost`, :func:`headline_kpis`.
* **Operational** — :func:`days_of_cover`, :func:`reorder_worklist`.
* **Segmentation** — :func:`abc_classification`, :func:`weekly_demand`,
  :func:`xyz_classification`, :func:`abc_xyz_matrix`.
* **Financial** — :func:`gmroi`.
* **Forecasting** — :func:`daily_demand_series`, :func:`forecast`,
  :func:`backtest`, :func:`safety_stock`, :func:`reorder_point`.
* **Data contract** — :func:`data_quality_checks`, :func:`validate_consolidated`,
  :func:`raise_for_quality`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

__all__ = [
    "GRAIN",
    "CAT_FLOW_ORDER",
    "CheckResult",
    "AnalyticsConfig",
    "to_grain",
    "value_at_cost",
    "attach_value_at_cost",
    "headline_kpis",
    "days_of_cover",
    "reorder_worklist",
    "abc_classification",
    "weekly_demand",
    "xyz_classification",
    "abc_xyz_matrix",
    "gmroi",
    "daily_demand_series",
    "forecast",
    "backtest",
    "safety_stock",
    "reorder_point",
    "data_quality_checks",
    "validate_consolidated",
    "raise_for_quality",
]

# The grain key used everywhere in the pipeline (BUILD-CONTRACT §3).
GRAIN = ["warehouse_id", "product_id", "product_attribute_id"]

# Demand velocity tiers, ordered slowest→fastest for stable categorical display.
CAT_FLOW_ORDER = ["Slow Moving", "Fast Moving", "Super Fast Moving"]

# Allowed enum values of the consolidated frame (used by the data-quality contract).
_DAYS_VALUES = {"L7D", "L14D", "L21D", "L30D"}
_STATUS_OUTLIER_VALUES = {"include outliers", "exclude outliers"}
_WINDOW_RANK = {"L30D": 0, "L21D": 1, "L14D": 2, "L7D": 3}

# The 44 LOCKED columns of consolidate_purchasing_agg (BUILD-CONTRACT §3.4).
CONSOLIDATED_COLUMNS = [
    "warehouse_id", "warehouse_name", "product_id", "product_attribute_id", "product_name",
    "unit", "total_quantity", "upper_bound", "lower_bound", "days", "status_outliers",
    "days_divider", "qty_per_day", "sku", "category_id", "category_name", "brand_id",
    "brand_name", "position", "product_status", "product_attribute_status", "divider",
    "avg_lead_time", "cycle_time", "stok_belum_rilis", "stok_rilis", "stok_booking",
    "stok_incoming", "stok_gudang", "status_final", "status", "adj_lead_time", "PIC",
    "label_priority", "ragu_nonaktif", "cat_flow", "qty_req", "gmv", "total_margin",
    "gm_rate", "status_wl", "l30d_tor", "recur_tor", "running_datetime",
]  # fmt: skip


@dataclass(frozen=True)
class AnalyticsConfig:
    """App-layer policy parameters that do not belong to the locked pipeline.

    These are deliberately kept **out** of ``config/rules.toml`` / :class:`Rules`,
    which the BUILD-CONTRACT locks. They are illustrative planning policies (ABC
    Pareto cut-points, XYZ coefficient-of-variation bands, target service level,
    safety-stock horizon) that the viewer surfaces as live, tunable controls.

    Attributes:
        abc_a_cut: Cumulative value share (0–1) up to which a SKU is class **A**.
        abc_b_cut: Cumulative value share up to which a SKU is class **B** (rest = C).
        xyz_x_max: Upper coefficient-of-variation bound for the **X** (stable) band.
        xyz_y_max: Upper coefficient-of-variation bound for the **Y** (variable) band.
        service_level_z: Safety-factor *z* (standard normal quantile of the target
            cycle-service level, e.g. ``1.645`` ≈ 95%).
        reorder_review_days: Stockout-risk amber threshold — days of cover below which
            a grain is flagged for review even when above its lead time.
    """

    abc_a_cut: float = 0.80
    abc_b_cut: float = 0.95
    xyz_x_max: float = 0.5
    xyz_y_max: float = 1.0
    service_level_z: float = 1.645
    reorder_review_days: int = 7


@dataclass(frozen=True)
class CheckResult:
    """One row of the data-quality contract.

    Attributes:
        name: Short identifier of the check.
        passed: Whether the assertion held.
        detail: Human-readable explanation (counts, offending values).
        hard: ``True`` for contract-breaking checks (these gate
            :func:`raise_for_quality`); ``False`` for advisory checks.
    """

    name: str
    passed: bool
    detail: str
    hard: bool = True


# ── Grain & value ─────────────────────────────────────────────────────────────


def to_grain(
    df: pd.DataFrame,
    *,
    status: str = "exclude outliers",
    window: str = "L30D",
) -> pd.DataFrame:
    """Collapse the consolidated frame to exactly one row per grain.

    The consolidated frame repeats every stock/margin/classification column across
    its (window × outlier-treatment) rows, so summing those columns directly
    over-counts. This anchors each grain to a single, deterministic row — preferring
    the cleaned ``exclude outliers`` treatment and the full ``L30D`` window — so that
    the demand-window columns (``total_quantity``, ``qty_per_day``, ``days_divider``)
    are well defined and the value columns are counted once.

    Args:
        df: The consolidated frame (or any subset with the grain + ``days`` +
            ``status_outliers`` columns).
        status: Preferred ``status_outliers`` treatment to anchor on.
        window: Preferred ``days`` window to anchor on.

    Returns:
        One row per grain. Falls back gracefully when a grain lacks the preferred
        window/status combination (it keeps that grain's closest-to-``L30D`` row).
    """
    if df.empty:
        return df.copy()

    ranked = df.copy()
    ranked["_status_rank"] = (ranked["status_outliers"].astype(str) != status).astype(int)
    ranked["_window_rank"] = ranked["days"].astype(str).map(_WINDOW_RANK).fillna(9)
    # Prefer the requested window first, then the global L30D→L7D ordering.
    ranked["_pref"] = (ranked["days"].astype(str) != window).astype(int)
    ranked = ranked.sort_values([*GRAIN, "_status_rank", "_pref", "_window_rank"])
    ranked = ranked.drop_duplicates(GRAIN, keep="first")
    return ranked.drop(columns=["_status_rank", "_window_rank", "_pref"]).reset_index(drop=True)


def value_at_cost(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Per-grain on-hand units and inventory value **at purchase cost**.

    Reconstructs the per-lot purchase value that ``sql/stocks.sql`` computes but the
    locked consolidated schema drops, by joining the synthetic ``inventories`` lots to
    their ``margin_costs`` purchase price. Read-only; no writes.

    Args:
        con: Open DuckDB connection to the seeded database.

    Returns:
        Columns ``warehouse_id, product_id, product_attribute_id, on_hand_lots,
        value_at_cost`` — one row per grain that has at least one inventory lot.
    """
    sql = """
        SELECT
            i.warehouse_id,
            i.product_id,
            i.product_attribute_id,
            SUM(i.remaining_quantity)                              AS on_hand_lots,
            SUM(i.remaining_quantity * mc.purchase_price_inc_ppn)  AS value_at_cost
        FROM inventories i
        JOIN margin_costs mc ON mc.inventory_id = i.id
        GROUP BY 1, 2, 3
    """
    return con.execute(sql).df()


def attach_value_at_cost(df_grain: pd.DataFrame, con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Left-join :func:`value_at_cost` onto a grain frame (missing → 0).

    Args:
        df_grain: One row per grain (typically :func:`to_grain` output).
        con: Open read-only DuckDB connection.

    Returns:
        ``df_grain`` plus ``on_hand_lots`` and ``value_at_cost`` (0-filled for grains
        without inventory lots).
    """
    voc = value_at_cost(con)
    out = df_grain.merge(voc, on=GRAIN, how="left")
    out["on_hand_lots"] = out["on_hand_lots"].fillna(0.0)
    out["value_at_cost"] = out["value_at_cost"].fillna(0.0)
    return out


def headline_kpis(
    df: pd.DataFrame,
    con: duckdb.DuckDBPyConnection,
    *,
    aging_all: pd.DataFrame | None = None,
) -> dict[str, float]:
    """Compute the executive headline KPIs from the consolidated frame.

    All value figures are computed on the grain-deduped frame, so GMV / margin /
    on-hand / inventory-value are counted once per grain rather than once per
    window×outlier row.

    Args:
        df: The full consolidated frame.
        con: Read-only DuckDB connection (for inventory value at cost).
        aging_all: Optional ``all`` aging frame (from
            :func:`stocklens.aging_alert.join_sell_out`) carrying
            ``total_purchase_stok_tanpa_booking`` — drives the "value at risk" KPI.

    Returns:
        A flat ``dict`` of named KPI values (counts as ``float`` for a uniform type):
        ``grains, warehouses, skus, gmv, total_margin, gm_rate, on_hand_units,
        inventory_value_at_cost, dead_stock_value, dead_stock_skus, days_inventory_out,
        aged_value_at_risk, aged_skus``.
    """
    g = attach_value_at_cost(to_grain(df), con)

    gmv = float(g["gmv"].sum())
    total_margin = float(g["total_margin"].sum())
    cogs = gmv - total_margin
    inventory_value = float(g["value_at_cost"].sum())

    dead_mask = g["cat_flow"].astype(str).eq("Slow Moving") & (g["stok_gudang"] > 0)
    dead_value = float(g.loc[dead_mask, "value_at_cost"].sum())

    # Days-inventory-outstanding proxy: inventory at cost ÷ average daily COGS (30d).
    daily_cogs = cogs / 30.0
    dio = float(inventory_value / daily_cogs) if daily_cogs > 0 else 0.0

    kpis: dict[str, float] = {
        "grains": float(len(g)),
        "warehouses": float(g["warehouse_name"].nunique()),
        "skus": float(g["sku"].nunique()),
        "gmv": gmv,
        "total_margin": total_margin,
        "gm_rate": float(total_margin / gmv) if gmv > 0 else 0.0,
        "on_hand_units": float(g["stok_gudang"].sum()),
        "inventory_value_at_cost": inventory_value,
        "dead_stock_value": dead_value,
        "dead_stock_skus": float(int(dead_mask.sum())),
        "days_inventory_out": dio,
    }

    if aging_all is not None and not aging_all.empty:
        col = "total_purchase_stok_tanpa_booking"
        kpis["aged_value_at_risk"] = float(aging_all[col].sum()) if col in aging_all else 0.0
        kpis["aged_skus"] = float(len(aging_all))
    else:
        kpis["aged_value_at_risk"] = 0.0
        kpis["aged_skus"] = 0.0

    return kpis


# ── Operational: days-of-cover & reorder ──────────────────────────────────────


def days_of_cover(
    df: pd.DataFrame,
    *,
    cfg: AnalyticsConfig | None = None,
    now: date | None = None,
) -> pd.DataFrame:
    """Per-grain days-of-cover, reorder flag, and stockout-risk rating.

    Working from the grain-deduped frame, ``days_of_cover`` is on-hand divided by the
    average daily demand (``qty_per_day`` of the anchored window). A grain ``needs``
    reorder when its cover is below its lead time net of incoming/booking pipeline; a
    Red/Amber/Green rating buckets stockout urgency.

    Args:
        df: The full consolidated frame.
        cfg: Analytics policy (uses ``reorder_review_days`` for the amber band).
        now: "As of" date for the projected stockout date; defaults to the frame's
            ``running_datetime`` date, else today.

    Returns:
        One row per grain with ``avg_daily_demand, days_of_cover, net_position,
        needs_reorder, stockout_risk`` (Red/Amber/Green) and ``projected_stockout``.
    """
    cfg = cfg or AnalyticsConfig()
    g = to_grain(df).copy()
    if g.empty:
        return g

    as_of = now or _frame_as_of(df)

    demand = g["qty_per_day"].clip(lower=0).astype(float)
    on_hand = g["stok_gudang"].astype(float)
    # Net available = on-hand + already-incoming − reserved bookings.
    net = on_hand + g.get("stok_incoming", 0).astype(float) - g.get("stok_booking", 0).astype(float)
    g["avg_daily_demand"] = demand
    g["net_position"] = net

    cover = np.where(demand > 0, on_hand / demand.replace(0, np.nan), np.inf)
    g["days_of_cover"] = np.where(np.isfinite(cover), np.round(cover, 1), np.inf)

    lead = g["adj_lead_time"].astype(float).clip(lower=0)
    net_cover = np.where(demand > 0, net / demand.replace(0, np.nan), np.inf)
    g["needs_reorder"] = (net_cover < lead) & (demand > 0)

    review = float(cfg.reorder_review_days)
    risk = np.where(
        ~(demand > 0),
        "Green",
        np.where(
            net_cover < lead,
            "Red",
            np.where(net_cover < lead + review, "Amber", "Green"),
        ),
    )
    g["stockout_risk"] = risk

    # Projected stockout date = as_of + whole days of cover (None when never).
    days_left = pd.Series(
        np.where(np.isfinite(cover), np.floor(cover), np.nan), index=g.index, dtype="float64"
    )
    as_of_ts = pd.Timestamp(as_of)
    g["projected_stockout"] = [
        (as_of_ts + pd.Timedelta(days=int(d))).date() if pd.notna(d) else None for d in days_left
    ]

    return g.reset_index(drop=True)


def reorder_worklist(
    df: pd.DataFrame,
    *,
    cfg: AnalyticsConfig | None = None,
    now: date | None = None,
) -> pd.DataFrame:
    """The buyer-facing reorder worklist: grains that need replenishment, ranked.

    Filters :func:`days_of_cover` to ``needs_reorder`` and orders by stockout urgency
    (Red before Amber) then ascending days-of-cover, projecting a suggested order
    quantity that refills to the lead-time-plus-review target.

    Returns:
        A compact, display-ready frame with the key replenishment columns and a
        ``suggested_order_qty``.
    """
    cfg = cfg or AnalyticsConfig()
    cover = days_of_cover(df, cfg=cfg, now=now)
    if cover.empty:
        return cover

    work = cover[cover["needs_reorder"]].copy()
    if work.empty:
        return work

    target_days = work["adj_lead_time"].astype(float) + float(cfg.reorder_review_days)
    target_units = (work["avg_daily_demand"] * target_days).round()
    work["suggested_order_qty"] = (
        (target_units - work["net_position"]).clip(lower=0).astype("int64")
    )

    risk_rank = {"Red": 0, "Amber": 1, "Green": 2}
    work["_risk_rank"] = work["stockout_risk"].map(risk_rank).fillna(3)
    work = work.sort_values(["_risk_rank", "days_of_cover"]).drop(columns="_risk_rank")

    cols = [
        "warehouse_name", "sku", "product_name", "unit", "cat_flow", "stok_gudang",
        "stok_incoming", "stok_booking", "avg_daily_demand", "adj_lead_time",
        "days_of_cover", "stockout_risk", "projected_stockout", "suggested_order_qty", "PIC",
    ]  # fmt: skip
    present = [c for c in cols if c in work.columns]
    return work[present].reset_index(drop=True)


# ── Segmentation: ABC / XYZ ───────────────────────────────────────────────────


def abc_classification(
    df: pd.DataFrame,
    *,
    value_col: str = "gmv",
    cfg: AnalyticsConfig | None = None,
) -> pd.DataFrame:
    """ABC (Pareto) classification of grains by contribution to a value column.

    Sorts grains by ``value_col`` descending, computes the cumulative value share, and
    cuts at ``abc_a_cut`` / ``abc_b_cut`` into classes **A** (the vital few), **B**, **C**.

    Args:
        df: The full consolidated frame.
        value_col: Value to rank on (``gmv`` or ``total_margin``).
        cfg: Analytics policy (Pareto cut-points).

    Returns:
        One row per grain with ``value, value_share, cum_share, abc_class``,
        sorted by value descending.
    """
    cfg = cfg or AnalyticsConfig()
    g = to_grain(df).copy()
    if g.empty:
        g["value"] = []
        g["value_share"] = []
        g["cum_share"] = []
        g["abc_class"] = []
        return g

    g["value"] = g[value_col].astype(float).clip(lower=0)
    g = g.sort_values("value", ascending=False).reset_index(drop=True)
    total = g["value"].sum()
    g["value_share"] = g["value"] / total if total > 0 else 0.0
    g["cum_share"] = g["value_share"].cumsum()
    # Classify by the cumulative share reached *before* each item, so the SKU that
    # straddles a cut-point lands in the higher (more important) class — the standard
    # ABC convention. The single largest SKU is always class A even if it alone
    # exceeds the A threshold.
    prev_cum = g["cum_share"] - g["value_share"]
    g["abc_class"] = np.where(
        prev_cum < cfg.abc_a_cut, "A", np.where(prev_cum < cfg.abc_b_cut, "B", "C")
    )
    return g


def weekly_demand(
    con: duckdb.DuckDBPyConnection,
    *,
    now: date | None = None,
    lookback_days: int = 35,
) -> pd.DataFrame:
    """Per-grain **weekly** demand series over the recent window (for XYZ).

    Buckets sold quantity into 7-day periods anchored on ``now``, restricted to
    delivered orders (``status > 1``) over the last ``lookback_days``. Weekly buckets
    are far more stable than daily ones over the ~30-day synthetic history.

    Returns:
        Columns ``warehouse_id, product_id, product_attribute_id, week, qty``.
    """
    as_of = now or date.today()
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=lookback_days)).date()
    sql = """
        SELECT
            o.warehouse_id,
            oi.product_id,
            oi.product_attribute_id,
            CAST(floor(date_diff('day', date($start), date(o.created_at)) / 7) AS INTEGER) AS week,
            SUM(oi.quantity) AS qty
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE o.status > 1
          AND date(o.created_at) > date($start)
          AND date(o.created_at) <= date($end)
        GROUP BY 1, 2, 3, 4
    """
    return con.execute(sql, {"start": start.isoformat(), "end": as_of.isoformat()}).df()


def xyz_classification(
    con: duckdb.DuckDBPyConnection,
    *,
    now: date | None = None,
    cfg: AnalyticsConfig | None = None,
) -> pd.DataFrame:
    """XYZ classification by demand variability (coefficient of variation).

    For each grain computes the coefficient of variation (``std / mean``) of its
    weekly demand and bands it: **X** stable (``cv ≤ xyz_x_max``), **Y** variable
    (``≤ xyz_y_max``), **Z** erratic (above). Grains with a single observed week are
    treated as **Z** (insufficient evidence of stability).

    Returns:
        Columns ``warehouse_id, product_id, product_attribute_id, weeks, mean_demand,
        cv, xyz_class``.
    """
    cfg = cfg or AnalyticsConfig()
    wk = weekly_demand(con, now=now)
    if wk.empty:
        return pd.DataFrame(columns=[*GRAIN, "weeks", "mean_demand", "cv", "xyz_class"])

    agg = wk.groupby(GRAIN, as_index=False).agg(
        weeks=("qty", "count"), mean_demand=("qty", "mean"), std_demand=("qty", "std")
    )
    agg["std_demand"] = agg["std_demand"].fillna(0.0)
    agg["cv"] = np.where(agg["mean_demand"] > 0, agg["std_demand"] / agg["mean_demand"], 0.0)
    single = agg["weeks"] <= 1
    agg["xyz_class"] = np.where(
        agg["cv"] <= cfg.xyz_x_max, "X", np.where(agg["cv"] <= cfg.xyz_y_max, "Y", "Z")
    )
    agg.loc[single, "xyz_class"] = "Z"
    return agg.drop(columns="std_demand")


def abc_xyz_matrix(abc: pd.DataFrame, xyz: pd.DataFrame) -> pd.DataFrame:
    """Join ABC × XYZ to a 3×3 policy matrix with counts and value.

    Args:
        abc: :func:`abc_classification` output.
        xyz: :func:`xyz_classification` output.

    Returns:
        One row per ``(abc_class, xyz_class)`` cell with ``skus`` and ``value`` (sum
        of the ABC value column), suitable for a heatmap. Grains missing an XYZ class
        (no recent demand) are labelled ``Z``.
    """
    merged = abc.merge(xyz[[*GRAIN, "xyz_class"]], on=GRAIN, how="left")
    merged["xyz_class"] = merged["xyz_class"].fillna("Z")
    cell = merged.groupby(["abc_class", "xyz_class"], as_index=False).agg(
        skus=("value", "size"), value=("value", "sum")
    )
    return cell


# ── Financial: GMROI ──────────────────────────────────────────────────────────


def gmroi(df: pd.DataFrame, con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Gross-Margin Return On Inventory investment, per grain.

    ``GMROI = gross margin ÷ average inventory at cost``. With a single inventory
    snapshot the denominator is point-in-time (not a period average); callers should
    caption it as such. A GMROI below 1.0 means the inventory is not earning back its
    own carrying cost — the "dead capital" signal.

    Returns:
        One row per grain with ``gmv, total_margin, gm_rate, value_at_cost, gmroi``,
        sorted by ``gmroi`` descending.
    """
    g = attach_value_at_cost(to_grain(df), con)
    g["gmroi"] = np.where(g["value_at_cost"] > 0, g["total_margin"] / g["value_at_cost"], np.nan)
    cols = [
        "warehouse_name", "sku", "product_name", "brand_name", "category_name",
        "cat_flow", "gmv", "total_margin", "gm_rate", "value_at_cost", "gmroi",
    ]  # fmt: skip
    present = [c for c in cols if c in g.columns]
    return g.sort_values("gmroi", ascending=False, na_position="last")[present].reset_index(
        drop=True
    )


# ── Forecasting ───────────────────────────────────────────────────────────────


def daily_demand_series(
    con: duckdb.DuckDBPyConnection,
    *,
    grain: tuple[int, int, int] | None = None,
    warehouse_id: int | None = None,
    now: date | None = None,
    lookback_days: int = 60,
) -> pd.DataFrame:
    """Daily sold-quantity series, optionally scoped to one grain or warehouse.

    Builds a dense, gap-filled daily series (missing days → 0) from delivered orders,
    so downstream smoothing and backtests see an unbroken calendar.

    Args:
        con: Read-only DuckDB connection.
        grain: Optional ``(warehouse_id, product_id, product_attribute_id)`` filter.
        warehouse_id: Optional warehouse filter (ignored when ``grain`` is given).
        now: End of the window (defaults to today).
        lookback_days: Length of the window in days.

    Returns:
        Columns ``order_date`` (``datetime64``) and ``qty`` (``float``), one row per day.
    """
    as_of = now or date.today()
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=lookback_days)).date()
    where = [
        "o.status > 1",
        "date(o.created_at) > date($start)",
        "date(o.created_at) <= date($end)",
    ]
    params: dict[str, object] = {"start": start.isoformat(), "end": as_of.isoformat()}
    if grain is not None:
        where.append(
            "o.warehouse_id = $wh AND oi.product_id = $pid AND oi.product_attribute_id = $paid"
        )
        params.update({"wh": grain[0], "pid": grain[1], "paid": grain[2]})
    elif warehouse_id is not None:
        where.append("o.warehouse_id = $wh")
        params["wh"] = warehouse_id

    sql = f"""
        SELECT date(o.created_at) AS order_date, SUM(oi.quantity) AS qty
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE {" AND ".join(where)}
        GROUP BY 1
        ORDER BY 1
    """  # noqa: S608 - identifiers are static; values are bound parameters
    raw = con.execute(sql, params).df()

    idx = pd.date_range(
        start=pd.Timestamp(start) + pd.Timedelta(days=1), end=pd.Timestamp(as_of), freq="D"
    )
    series = pd.DataFrame({"order_date": idx})
    if raw.empty:
        series["qty"] = 0.0
        return series
    raw["order_date"] = pd.to_datetime(raw["order_date"])
    series = series.merge(raw, on="order_date", how="left")
    series["qty"] = series["qty"].fillna(0.0).astype(float)
    return series


def forecast(
    series: pd.Series, *, horizon: int, method: str = "ses", alpha: float = 0.4
) -> np.ndarray:
    """Project ``horizon`` future points from a demand series.

    Methods:
        * ``naive`` — repeat the last observed value.
        * ``ma`` — repeat the trailing 7-point moving average.
        * ``ses`` — simple exponential smoothing level, repeated forward.

    Args:
        series: Historical demand (chronological).
        horizon: Number of future periods to project.
        method: One of ``naive`` / ``ma`` / ``ses``.
        alpha: Smoothing factor for ``ses`` (0–1).

    Returns:
        A length-``horizon`` array of forecast values (flat per method).
    """
    values = np.asarray(series, dtype=float)
    if values.size == 0:
        return np.zeros(horizon, dtype=float)

    if method == "naive":
        level = float(values[-1])
    elif method == "ma":
        window = values[-7:] if values.size >= 7 else values
        level = float(window.mean())
    elif method == "ses":
        level = float(values[0])
        for value in values[1:]:
            level = alpha * float(value) + (1 - alpha) * level
    else:  # pragma: no cover - guarded by the caller's method whitelist
        raise ValueError(f"unknown forecast method: {method!r}")

    return np.full(horizon, max(level, 0.0), dtype=float)


def _wape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Weighted Absolute Percentage Error = Σ|a−f| / Σ|a| (0 when actual sums to 0)."""
    denom = float(np.abs(actual).sum())
    if denom == 0:
        return 0.0
    return float(np.abs(actual - predicted).sum() / denom)


def backtest(series: pd.Series, *, holdout: int = 7) -> pd.DataFrame:
    """Holdout backtest of each method against a seasonal-naive baseline.

    Trains on all but the last ``holdout`` points, forecasts that horizon with each
    method, and reports WAPE. The **seasonal-naive** baseline (value from 7 days prior)
    is the honest bar a real forecast must beat.

    Returns:
        Columns ``method, wape, beats_baseline`` — one row per method, plus the
        ``seasonal_naive`` baseline itself.
    """
    values = np.asarray(series, dtype=float)
    if values.size <= holdout:
        return pd.DataFrame(columns=["method", "wape", "beats_baseline"])

    train, test = values[:-holdout], values[-holdout:]

    # Seasonal-naive baseline: each test point predicted by the value 7 positions back.
    season = 7
    baseline = np.array(
        [
            values[len(train) + i - season] if len(train) + i - season >= 0 else train[-1]
            for i in range(holdout)
        ],
        dtype=float,
    )
    baseline_wape = _wape(test, baseline)

    rows = [{"method": "seasonal_naive", "wape": baseline_wape, "beats_baseline": True}]
    for method in ("naive", "ma", "ses"):
        pred = forecast(pd.Series(train), horizon=holdout, method=method)
        wape = _wape(test, pred)
        rows.append({"method": method, "wape": wape, "beats_baseline": wape <= baseline_wape})
    return pd.DataFrame(rows).sort_values("wape").reset_index(drop=True)


def safety_stock(sigma_daily: float, lead_time_days: float, z: float) -> float:
    """Safety stock = ``z · σ_daily · √lead_time`` (demand-variability buffer)."""
    return float(z) * float(sigma_daily) * float(np.sqrt(max(lead_time_days, 0.0)))


def reorder_point(
    avg_daily_demand: float, lead_time_days: float, sigma_daily: float, z: float
) -> dict[str, float]:
    """Reorder point = cycle stock (demand over lead time) + safety stock.

    Returns:
        ``dict`` with ``cycle_stock``, ``safety_stock`` and their sum ``reorder_point``.
    """
    cycle = float(avg_daily_demand) * float(lead_time_days)
    ss = safety_stock(sigma_daily, lead_time_days, z)
    return {"cycle_stock": cycle, "safety_stock": ss, "reorder_point": cycle + ss}


# ── Data contract ─────────────────────────────────────────────────────────────


def data_quality_checks(df: pd.DataFrame) -> list[CheckResult]:
    """Run the StockLens data-quality contract over the consolidated frame.

    Encodes the guarantees a downstream consumer should be able to assume: the locked
    schema is present, the natural key is unique, grain keys are non-null, stock and
    demand are non-negative, the gross-margin rate cannot exceed 1, and the categorical
    columns stay inside their allowed vocabularies. Returns one :class:`CheckResult`
    per assertion (it never raises) so a UI can render the full pass/fail checklist.
    """
    results: list[CheckResult] = []

    missing = [c for c in CONSOLIDATED_COLUMNS if c not in df.columns]
    results.append(
        CheckResult(
            "schema_columns_present",
            not missing,
            "all 44 locked columns present" if not missing else f"missing: {missing}",
        )
    )
    # Every check below assumes the columns exist; bail early if the schema is broken.
    if missing:
        return results

    key = [*GRAIN, "days", "status_outliers"]
    dups = int(df.duplicated(key).sum())
    results.append(
        CheckResult(
            "natural_key_unique", dups == 0, f"{dups} duplicate (grain, window, outlier) rows"
        )
    )

    null_keys = int(df[GRAIN].isna().sum().sum())
    results.append(
        CheckResult("grain_keys_non_null", null_keys == 0, f"{null_keys} null grain-key cells")
    )

    neg_stock = int((df["stok_gudang"] < 0).sum())
    results.append(CheckResult("stok_gudang_non_negative", neg_stock == 0, f"{neg_stock} rows < 0"))

    bad_rate = int((df["gm_rate"] > 1.0).sum())
    results.append(
        CheckResult("gm_rate_at_most_one", bad_rate == 0, f"{bad_rate} rows with gm_rate > 1")
    )

    low_qpd = int((df["qty_per_day"] < 1).sum())
    results.append(
        CheckResult("qty_per_day_floored", low_qpd == 0, f"{low_qpd} rows below the qty/day floor")
    )

    neg_tor = int((df["recur_tor"] < 0).sum())
    results.append(CheckResult("recur_tor_non_negative", neg_tor == 0, f"{neg_tor} rows < 0"))

    bad_flow = sorted(set(df["cat_flow"].astype(str)) - set(CAT_FLOW_ORDER))
    results.append(
        CheckResult("cat_flow_in_vocabulary", not bad_flow, f"unexpected cat_flow: {bad_flow}")
    )

    bad_days = sorted(set(df["days"].astype(str)) - _DAYS_VALUES)
    results.append(CheckResult("days_in_vocabulary", not bad_days, f"unexpected days: {bad_days}"))

    bad_status = sorted(set(df["status_outliers"].astype(str)) - _STATUS_OUTLIER_VALUES)
    results.append(
        CheckResult("status_outliers_in_vocabulary", not bad_status, f"unexpected: {bad_status}")
    )

    fresh_nulls = int(df["running_datetime"].isna().sum())
    results.append(
        CheckResult(
            "running_datetime_present",
            fresh_nulls == 0,
            f"{fresh_nulls} null timestamps",
            hard=False,
        )
    )

    return results


def validate_consolidated(df: pd.DataFrame) -> tuple[bool, list[CheckResult]]:
    """Run :func:`data_quality_checks`; return ``(all_hard_checks_passed, results)``."""
    results = data_quality_checks(df)
    ok = all(r.passed for r in results if r.hard)
    return ok, results


def raise_for_quality(df: pd.DataFrame) -> list[CheckResult]:
    """Validate ``df`` and raise :class:`ValueError` if any hard check fails.

    The CI-facing gate (``stocklens validate``): returns the full result list on
    success, raises with the failing checks on violation.
    """
    ok, results = validate_consolidated(df)
    if not ok:
        failed = [r for r in results if r.hard and not r.passed]
        lines = "; ".join(f"{r.name}: {r.detail}" for r in failed)
        raise ValueError(f"consolidated data-quality contract failed — {lines}")
    return results


def _frame_as_of(df: pd.DataFrame) -> date:
    """Best-effort "as of" date from the frame's ``running_datetime`` (else today)."""
    if "running_datetime" in df.columns and not df["running_datetime"].isna().all():
        return pd.to_datetime(df["running_datetime"]).max().date()
    return date.today()
