"""StockLens read-only JSON API (FastAPI) over the shared analytics engine.

Same numbers, a different delivery surface: every endpoint reuses the pure
:mod:`stocklens.analytics` functions and the locked pipeline's artifacts, so the API,
the CLI, and the Streamlit viewer can never disagree. Nothing here writes, seeds, or
reaches a network — it reads the consolidated Parquet and the seeded DuckDB
(``read_only=True``) and serializes the result. Auto-generated OpenAPI docs live at
``/docs``.

Run::

    python cli.py all                       # build the artifacts first
    uv sync --extra api
    uv run uvicorn api.main:app --reload     # http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from stocklens import Rules, load_rules
from stocklens import analytics as A

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _REPO_ROOT / "config" / "rules.toml"

# Stocking policy per ABC-XYZ cell (mirrors the viewer's matrix legend).
_POLICY = {
    "AX": "Automate & always stock", "AY": "Stock with buffer", "AZ": "Tight review, hold safety",
    "BX": "Periodic auto-order", "BY": "Standard review", "BZ": "Cautious, smaller lots",
    "CX": "Min-stock / bulk-buy", "CY": "Make-to-order leaning", "CZ": "Make-to-order / delist",
}  # fmt: skip

app = FastAPI(
    title="StockLens API",
    version="1.1.0",
    summary="Read-only JSON access to the StockLens inventory-analytics pipeline.",
    description=(
        "A thin, read-only layer over the same `stocklens.analytics` engine that powers the CLI and "
        "the Streamlit viewer. All data is synthetic and fully reproducible (fixed RNG seed)."
    ),
)


# ── Data access (cached, read-only) ───────────────────────────────────────────


@lru_cache(maxsize=1)
def _rules() -> Rules:
    return load_rules(str(_CONFIG))


def _out_dir() -> Path:
    out = Path(_rules().report.get("output_dir", "out"))
    return out if out.is_absolute() else _REPO_ROOT / out


def _duckdb_path() -> Path:
    db = Path(_rules().paths["duckdb_path"])
    return db if db.is_absolute() else _REPO_ROOT / db


@lru_cache(maxsize=1)
def _consolidated() -> pd.DataFrame:
    from shims import data_io

    parquet = _out_dir() / "consolidate_purchasing_agg.parquet"
    if not parquet.is_file():
        raise HTTPException(503, "consolidated artifact missing — run `python cli.py all` first")
    # Read via DuckDB (no pyarrow dependency); the writer emits both .parquet and .csv.
    return data_io.read_table(str(parquet))


def _con() -> duckdb.DuckDBPyConnection:
    db = _duckdb_path()
    if not db.is_file():
        raise HTTPException(503, "seeded DuckDB missing — run `python cli.py all` first")
    return duckdb.connect(str(db), read_only=True)


def _as_of(df: pd.DataFrame) -> date:
    stamps = pd.to_datetime(df["running_datetime"], errors="coerce")
    return stamps.max().date() if stamps.notna().any() else date.today()


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame → JSON-safe records (NaN/Inf → null, dates → ISO strings)."""
    safe = df.copy()
    for col in safe.columns:
        if safe[col].dtype == object:
            safe[col] = safe[col].map(
                lambda v: v.isoformat() if isinstance(v, (date, datetime)) else v
            )
    return json.loads(safe.to_json(orient="records", date_format="iso"))


@lru_cache(maxsize=1)
def _aging_frames() -> dict[str, pd.DataFrame]:
    from stocklens.aging_alert import categorize_and_filter, join_sell_out, load_cohort

    df = _consolidated()
    con = _con()
    try:
        cohort = load_cohort(_rules())
        aged = categorize_and_filter(cohort, con, _rules())
        return join_sell_out(aged, con, _rules(), now=_as_of(df))
    finally:
        con.close()


# ── Response models ───────────────────────────────────────────────────────────


class Health(BaseModel):
    status: str
    as_of: date | None
    grains: int
    warehouses: int
    rows: int


class KPIs(BaseModel):
    grains: int
    skus: int
    warehouses: int
    gmv: float
    total_margin: float
    gm_rate: float
    on_hand_units: float
    inventory_value_at_cost: float
    dead_stock_value: float
    dead_stock_skus: int
    days_inventory_out: float
    aged_value_at_risk: float
    aged_skus: int


class Page(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[dict[str, Any]]


class Items(BaseModel):
    count: int
    items: list[dict[str, Any]]


class MatrixCell(BaseModel):
    abc_class: str
    xyz_class: str
    skus: int
    value: float
    policy: str


class ABCXYZ(BaseModel):
    abc_counts: dict[str, int]
    xyz_counts: dict[str, int]
    cells: list[MatrixCell]


class SimulateRequest(BaseModel):
    weight_qty: float = Field(
        0.8, ge=0.0, le=1.0, description="Quantity weight (orders weight = 1 − this)."
    )
    std_damp_threshold: float = Field(1000.0, ge=0.0)
    std_damp_factor: float = Field(0.25, ge=0.0, le=1.0)
    iqr_factor: float = Field(1.5, ge=1.0)


class SimulateResult(BaseModel):
    grains: int
    changed: int
    changed_share: float
    mix_before: dict[str, int]
    mix_after: dict[str, int]
    transitions: list[dict[str, Any]]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/", tags=["meta"], summary="API index")
def root() -> dict[str, Any]:
    return {
        "name": "StockLens API",
        "version": app.version,
        "docs": "/docs",
        "endpoints": [
            "/healthz",
            "/kpis",
            "/grains",
            "/demand/classification",
            "/stock/reorder",
            "/aging",
            "/margin/gmroi",
            "/abc-xyz",
            "/simulate",
        ],  # fmt: skip
    }


@app.get("/healthz", response_model=Health, tags=["meta"], summary="Liveness & freshness")
def healthz() -> Health:
    df = _consolidated()
    grain = A.to_grain(df)
    return Health(
        status="ok",
        as_of=_as_of(df),
        grains=len(grain),
        warehouses=int(grain["warehouse_name"].nunique()),
        rows=len(df),
    )


@app.get("/kpis", response_model=KPIs, tags=["analytics"], summary="Executive headline KPIs")
def kpis() -> KPIs:
    df = _consolidated()
    aging_all = _aging_frames().get("all")
    con = _con()
    try:
        values = A.headline_kpis(df, con, aging_all=aging_all)
    finally:
        con.close()
    return KPIs(
        grains=int(values["grains"]),
        skus=int(values["skus"]),
        warehouses=int(values["warehouses"]),
        gmv=values["gmv"],
        total_margin=values["total_margin"],
        gm_rate=values["gm_rate"],
        on_hand_units=values["on_hand_units"],
        inventory_value_at_cost=values["inventory_value_at_cost"],
        dead_stock_value=values["dead_stock_value"],
        dead_stock_skus=int(values["dead_stock_skus"]),
        days_inventory_out=values["days_inventory_out"],
        aged_value_at_risk=values["aged_value_at_risk"],
        aged_skus=int(values["aged_skus"]),
    )


@app.get("/grains", response_model=Page, tags=["analytics"], summary="Paginated per-grain rows")
def grains(
    warehouse: str | None = Query(None, description="Filter by warehouse_name."),
    cat_flow: str | None = Query(None, description="Filter by demand class."),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page:
    grain = A.to_grain(_consolidated())
    if warehouse:
        grain = grain[grain["warehouse_name"].astype(str) == warehouse]
    if cat_flow:
        grain = grain[grain["cat_flow"].astype(str) == cat_flow]
    cols = [
        "warehouse_name", "sku", "product_name", "category_name", "brand_name", "cat_flow",
        "stok_gudang", "qty_per_day", "gmv", "total_margin", "gm_rate", "recur_tor",
    ]  # fmt: skip
    present = [c for c in cols if c in grain.columns]
    page = grain[present].iloc[offset : offset + limit]
    return Page(total=len(grain), limit=limit, offset=offset, items=_records(page))


@app.get("/demand/classification", tags=["analytics"], summary="Demand-class distribution")
def demand_classification() -> dict[str, Any]:
    grain = A.to_grain(_consolidated())
    distribution = grain["cat_flow"].astype(str).value_counts().to_dict()
    by_wh = (
        grain.groupby(["warehouse_name", "cat_flow"], as_index=False)
        .size()
        .rename(columns={"size": "grains"})
    )
    return {"distribution": {k: int(v) for k, v in distribution.items()},
            "by_warehouse": _records(by_wh)}  # fmt: skip


@app.get("/stock/reorder", response_model=Items, tags=["analytics"], summary="Reorder worklist")
def stock_reorder(review_days: int = Query(7, ge=0, le=30)) -> Items:
    df = _consolidated()
    cfg = A.AnalyticsConfig(reorder_review_days=review_days)
    work = A.reorder_worklist(df, cfg=cfg, now=_as_of(df))
    return Items(count=len(work), items=_records(work))


@app.get("/aging", tags=["analytics"], summary="Aged cohort & value at risk")
def aging() -> dict[str, Any]:
    frames = _aging_frames()
    all_aged = frames.get("all", pd.DataFrame())
    col = "total_purchase_stok_tanpa_booking"
    value_at_risk = float(all_aged[col].sum()) if not all_aged.empty and col in all_aged else 0.0
    return {
        "value_at_risk": value_at_risk,
        "skus": int(len(all_aged)),
        "daily_needs": _records(frames.get("daily_needs", pd.DataFrame())),
        "lifestyle": _records(frames.get("lifestyle", pd.DataFrame())),
    }


@app.get("/margin/gmroi", response_model=Items, tags=["analytics"], summary="GMROI ranking")
def margin_gmroi(limit: int = Query(25, ge=1, le=500)) -> Items:
    df = _consolidated()
    con = _con()
    try:
        ranked = A.gmroi(df, con)
    finally:
        con.close()
    return Items(count=len(ranked), items=_records(ranked.head(limit)))


@app.get("/abc-xyz", response_model=ABCXYZ, tags=["analytics"], summary="ABC-XYZ matrix")
def abc_xyz(value_col: str = Query("gmv", pattern="^(gmv|total_margin)$")) -> ABCXYZ:
    df = _consolidated()
    abc = A.abc_classification(df, value_col=value_col)
    con = _con()
    try:
        xyz = A.xyz_classification(con, now=_as_of(df))
    finally:
        con.close()
    matrix = A.abc_xyz_matrix(abc, xyz)
    cells = [
        MatrixCell(
            abc_class=row["abc_class"],
            xyz_class=row["xyz_class"],
            skus=int(row["skus"]),
            value=float(row["value"]),
            policy=_POLICY.get(f"{row['abc_class']}{row['xyz_class']}", ""),
        )
        for _, row in matrix.iterrows()
    ]
    return ABCXYZ(
        abc_counts={k: int(v) for k, v in abc["abc_class"].value_counts().to_dict().items()},
        xyz_counts={k: int(v) for k, v in xyz["xyz_class"].value_counts().to_dict().items()},
        cells=cells,
    )


@app.post("/simulate", response_model=SimulateResult, tags=["analytics"],
          summary="Re-run demand classification under a modified policy")  # fmt: skip
def simulate(req: SimulateRequest) -> SimulateResult:
    from stocklens.demand_classify import classify_demand, load_orders

    df = _consolidated()
    rules = _rules()
    con = _con()
    try:
        orders = load_orders(con, rules, now=_as_of(df))
    finally:
        con.close()

    modified = dataclasses.replace(
        rules,
        classification={
            **rules.classification,
            "weight_qty": req.weight_qty,
            "weight_orders": round(1 - req.weight_qty, 4),
            "std_damp_threshold": req.std_damp_threshold,
            "std_damp_factor": req.std_damp_factor,
        },  # fmt: skip
        demand={**rules.demand, "iqr_factor": req.iqr_factor},
    )
    base = classify_demand(orders, rules)[[*A.GRAIN, "cat_flow"]].rename(
        columns={"cat_flow": "base"}
    )
    tuned = classify_demand(orders, modified)[[*A.GRAIN, "cat_flow"]].rename(
        columns={"cat_flow": "tuned"}
    )
    compare = base.merge(tuned, on=A.GRAIN, how="outer")
    changed = compare[compare["base"].astype(str) != compare["tuned"].astype(str)]
    transitions = (
        changed.assign(
            transition=changed["base"].astype(str) + " → " + changed["tuned"].astype(str)
        )
        .groupby("transition", as_index=False)
        .size()
        .rename(columns={"size": "grains"})
    )
    return SimulateResult(
        grains=len(compare),
        changed=len(changed),
        changed_share=round(len(changed) / len(compare), 4) if len(compare) else 0.0,
        mix_before={k: int(v) for k, v in compare["base"].value_counts().to_dict().items()},
        mix_after={k: int(v) for k, v in compare["tuned"].value_counts().to_dict().items()},
        transitions=_records(transitions),
    )
