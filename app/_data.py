"""Shared data-access + UI helpers for the StockLens multi-page viewer.

This is the one place the Streamlit pages get their data and their common chrome.
It wraps the locked pipeline's artifacts (the consolidated Parquet + the seeded
DuckDB) and the pure :mod:`stocklens.analytics` layer in cached, page-friendly
loaders, so each page under ``app/pages/`` stays a thin presentation script.

Design notes
------------
* **Caching.** Heavy frames are memoised with ``st.cache_data`` and the DuckDB
  connection with ``st.cache_resource``. The :class:`~stocklens.Rules` argument is
  passed as ``_rules`` so Streamlit skips hashing the (unhashable, dict-bearing)
  dataclass — there is effectively a single dataset per session.
* **Cold start.** :func:`ensure_artifacts` rebuilds the Parquet + DuckDB on first
  boot if they are missing (they are git-ignored), so a freshly deployed app
  reproduces the exact local numbers from the fixed RNG seed — no manual step.
* **No side-effects beyond that.** Everything else is read-only; the DuckDB file is
  opened ``read_only=True``.

The module assumes its importer has already put ``src`` / repo-root on ``sys.path``
(every page calls its tiny ``_bootstrap`` first), so the top-level imports resolve.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import streamlit as st

from stocklens import Rules, load_rules
from stocklens import analytics as A

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "rules.toml"
RUN_HINT = "python cli.py all"


# ── Config / paths ────────────────────────────────────────────────────────────


@st.cache_data(show_spinner=False)
def get_rules() -> Rules:
    """Load the locked tunables once per session."""
    return load_rules(str(_DEFAULT_CONFIG))


def resolve_out_dir(rules: Rules) -> Path:
    """Resolve the configured output directory against the repo root."""
    out_dir = Path(rules.report.get("output_dir", "out"))
    return out_dir if out_dir.is_absolute() else _REPO_ROOT / out_dir


def duckdb_path(rules: Rules) -> Path:
    """Resolve the seeded DuckDB path against the repo root."""
    db = Path(rules.paths["duckdb_path"])
    return db if db.is_absolute() else _REPO_ROOT / db


# ── Cold-start seeding (deployment) ───────────────────────────────────────────


@st.cache_resource(show_spinner="First boot: building the synthetic dataset…")
def ensure_artifacts(_rules: Rules) -> bool:
    """Build the Parquet + DuckDB on first boot if they are missing.

    The artifacts are git-ignored, so a freshly cloned/deployed app has none. This
    runs the same ``seed → consolidate → aging`` the CLI does (deterministic RNG), so
    the hosted app reproduces local numbers. Runs at most once per session.

    Returns:
        ``True`` if a build was triggered, ``False`` if the artifacts already existed.
    """
    parquet = resolve_out_dir(_rules) / "consolidate_purchasing_agg.parquet"
    if parquet.is_file() and duckdb_path(_rules).is_file():
        return False

    from shims import data_io
    from stocklens.aging_alert import run_aging_alert
    from stocklens.consolidate import run_consolidate

    con = data_io.connect(str(duckdb_path(_rules)))
    try:
        run_consolidate(con, _rules)
        run_aging_alert(_rules, con)
    finally:
        con.close()
    return True


def open_con(rules: Rules) -> duckdb.DuckDBPyConnection:
    """Open a fresh **read-only** DuckDB connection (caller closes it).

    Used by interactive pages (forecasting, what-if) that issue parameterised queries.
    """
    import duckdb

    return duckdb.connect(str(duckdb_path(rules)), read_only=True)


# ── Cached frame loaders ──────────────────────────────────────────────────────


@st.cache_data(show_spinner=False)
def load_consolidated(_rules: Rules) -> pd.DataFrame | None:
    """The consolidated artifact (read via DuckDB; CSV fallback), or ``None`` if absent."""
    from shims import data_io

    parquet = resolve_out_dir(_rules) / "consolidate_purchasing_agg.parquet"
    if not parquet.is_file() and not parquet.with_suffix(".csv").is_file():
        return None
    return data_io.read_table(str(parquet))


@st.cache_data(show_spinner=False)
def load_grain(_rules: Rules) -> pd.DataFrame:
    """One row per grain (value columns de-duplicated)."""
    df = load_consolidated(_rules)
    return A.to_grain(df) if df is not None else pd.DataFrame()


@st.cache_data(show_spinner=False)
def load_aging_frames(_rules: Rules) -> dict[str, pd.DataFrame] | None:
    """Recompute the aging cohort frames from the seeded DuckDB (read-only)."""
    if not duckdb_path(_rules).is_file():
        return None
    from stocklens.aging_alert import categorize_and_filter, join_sell_out, load_cohort

    con = open_con(_rules)
    try:
        cohort = load_cohort(_rules)
        aged = categorize_and_filter(cohort, con, _rules)
        return join_sell_out(aged, con, _rules, now=as_of(load_consolidated(_rules)))
    finally:
        con.close()


@st.cache_data(show_spinner=False)
def load_value_at_cost(_rules: Rules) -> pd.DataFrame:
    """Per-grain inventory value at purchase cost."""
    con = open_con(_rules)
    try:
        return A.value_at_cost(con)
    finally:
        con.close()


@st.cache_data(show_spinner=False)
def load_kpis(_rules: Rules) -> dict[str, float]:
    """The executive headline KPIs (value-at-risk uses the aging frames)."""
    df = load_consolidated(_rules)
    if df is None:
        return {}
    frames = load_aging_frames(_rules)
    aging_all = frames.get("all") if frames else None
    con = open_con(_rules)
    try:
        return A.headline_kpis(df, con, aging_all=aging_all)
    finally:
        con.close()


@st.cache_data(show_spinner=False)
def load_classification(_rules: Rules) -> pd.DataFrame:
    """Per-grain weighted score, per-warehouse mean/limit, and ``cat_flow``.

    Re-derived from the seeded DuckDB so the viewer can show the *internals* of the
    demand tiering (the weighted score vs. the mean+std limit) that the consolidated
    frame only exposes as the final ``cat_flow`` label.
    """
    df = load_consolidated(_rules)
    from stocklens.demand_classify import classify_demand, load_orders

    con = open_con(_rules)
    try:
        orders = load_orders(con, _rules, now=as_of(df))
        return classify_demand(orders, _rules)
    finally:
        con.close()


@st.cache_data(show_spinner=False)
def load_xyz(_rules: Rules) -> pd.DataFrame:
    """Per-grain XYZ variability classification."""
    df = load_consolidated(_rules)
    con = open_con(_rules)
    try:
        return A.xyz_classification(con, now=as_of(df))
    finally:
        con.close()


@st.cache_data(show_spinner=False)
def load_gmroi(_rules: Rules) -> pd.DataFrame:
    """Per-grain GMROI ranking."""
    df = load_consolidated(_rules)
    if df is None:
        return pd.DataFrame()
    con = open_con(_rules)
    try:
        return A.gmroi(df, con)
    finally:
        con.close()


# ── Small helpers ─────────────────────────────────────────────────────────────


def as_of(df: pd.DataFrame | None) -> date:
    """The dataset "as of" date (max ``running_datetime``), else today."""
    if df is None or df.empty or "running_datetime" not in df.columns:
        return date.today()
    stamps = pd.to_datetime(df["running_datetime"], errors="coerce")
    return stamps.max().date() if stamps.notna().any() else date.today()


def compact(value: float) -> str:
    """Human-compact number: ``280626000 → '280.6M'`` (neutral synthetic units)."""
    number = float(value)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= divisor:
            return f"{number / divisor:,.1f}{suffix}"
    return f"{number:,.0f}"


def pct(value: float, digits: int = 1) -> str:
    """Format a 0–1 ratio as a percentage string."""
    return f"{float(value) * 100:.{digits}f}%"


# ── UI chrome ─────────────────────────────────────────────────────────────────

ICON = "📦"


def setup_page(title: str, *, subtitle: str = "", icon: str = "📊", wide: bool = True) -> None:
    """Standard page header: page config, title, optional caption."""
    st.set_page_config(page_title=f"StockLens · {title}", page_icon=ICON,
                       layout="wide" if wide else "centered")  # fmt: skip
    st.title(f"{icon} {title}")
    if subtitle:
        st.caption(subtitle)


def require_consolidated(rules: Rules) -> pd.DataFrame:
    """Return the consolidated frame or stop the page with a friendly prompt."""
    ensure_artifacts(rules)
    df = load_consolidated(rules)
    if df is None:
        st.warning(
            f"No consolidated output found. Run **`{RUN_HINT}`** to generate the "
            "artifacts, then reload."
        )
        st.stop()
    return df


def sidebar_filters(df: pd.DataFrame, *, key: str) -> pd.DataFrame:
    """Render the shared warehouse / demand-class sidebar filters and apply them."""
    st.sidebar.header("Filters")
    filtered = df

    if "warehouse_name" in df.columns:
        options = sorted(str(w) for w in df["warehouse_name"].dropna().unique())
        picked = st.sidebar.multiselect("Warehouse", options, default=options, key=f"{key}_wh")
        if picked:
            filtered = filtered[filtered["warehouse_name"].astype(str).isin(picked)]

    if "cat_flow" in df.columns:
        options = [c for c in A.CAT_FLOW_ORDER if c in set(df["cat_flow"].astype(str))]
        picked = st.sidebar.multiselect("Demand class", options, default=options, key=f"{key}_flow")
        if picked:
            filtered = filtered[filtered["cat_flow"].astype(str).isin(picked)]

    return filtered


def caption_synthetic() -> None:
    """The standard synthetic-data disclaimer shown in the sidebar footer."""
    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Synthetic data, fixed RNG seed. No live systems are touched. "
        "Currency figures are illustrative units."
    )
