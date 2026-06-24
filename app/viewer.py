"""StockLens portfolio viewer (Streamlit).

A small, read-only dashboard over the artifacts produced by ``python cli.py all``:
the consolidated demand/stock Parquet and the aging-stock cohort tables. It is the
"screenshot" surface of the showcase — it loads local files only and performs **no**
network, database-write, or any other side-effect.

Run it (after installing the optional ``viz`` extra) with::

    uv sync --extra viz
    streamlit run app/viewer.py

Streamlit is imported lazily and guarded, so importing this module (e.g. for linting
or for ``viewer.load_consolidated`` in tests) never requires Streamlit to be installed;
only :func:`main` needs it. If the expected outputs are missing, the app shows a friendly
prompt to run ``python cli.py all`` rather than crashing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from stocklens import Rules

# ── Path bootstrap (mirror cli.py so a bare `streamlit run app/viewer.py` works) ─
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
for _p in (str(_SRC_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DEFAULT_CONFIG = _REPO_ROOT / "config" / "rules.toml"
_RUN_HINT = "python cli.py all"


# ── Data loaders (pure, Streamlit-free) ──────────────────────────────────────


def _load_rules() -> Rules:
    """Load the rules TOML (kept local so the module imports without Streamlit)."""
    from stocklens import load_rules

    return load_rules(str(_DEFAULT_CONFIG))


def _resolve_out_dir(rules: Rules) -> Path:
    """Resolve the configured output dir against the repo root."""
    out_dir = Path(rules.report.get("output_dir", "out"))
    if not out_dir.is_absolute():
        out_dir = _REPO_ROOT / out_dir
    return out_dir


def load_consolidated(rules: Rules) -> pd.DataFrame | None:
    """Load ``out/consolidate_purchasing_agg.parquet`` if it exists, else ``None``.

    Returns ``None`` (rather than raising) when the Parquet is absent so the UI can
    prompt the user to run the pipeline. Falls back to the ``.csv`` sibling that the
    writer also emits, so the viewer still works even if a Parquet engine is unavailable.
    """
    out_dir = _resolve_out_dir(rules)
    parquet = out_dir / "consolidate_purchasing_agg.parquet"
    if parquet.is_file():
        try:
            return pd.read_parquet(parquet)
        except (ImportError, ValueError, OSError):
            pass  # fall through to the CSV sibling
    csv = parquet.with_suffix(".csv")
    if csv.is_file():
        return pd.read_csv(csv)
    return None


def load_aging_frames(rules: Rules) -> dict[str, pd.DataFrame] | None:
    """Recompute the aging cohort tables from the seeded DuckDB (read-only).

    The aging job renders HTML/MD to disk rather than persisting the frames, so the
    viewer re-derives the ``daily_needs`` / ``lifestyle`` / ``all`` tables directly from
    the seeded database via the same pure pipeline functions the CLI uses. The DuckDB
    file is opened read-only and the call has no side-effects beyond (idempotently)
    re-rendering the report. Returns ``None`` if the database is missing.
    """
    duckdb_path = Path(rules.paths["duckdb_path"])
    if not duckdb_path.is_absolute():
        duckdb_path = _REPO_ROOT / duckdb_path
    if not duckdb_path.is_file():
        return None

    from datetime import date

    import duckdb

    from stocklens.aging_alert import categorize_and_filter, join_sell_out, load_cohort

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        df_cohort = load_cohort(rules)
        df_aged = categorize_and_filter(df_cohort, con, rules)
        return join_sell_out(df_aged, con, rules, now=date.today())
    finally:
        con.close()


# ── Streamlit UI ─────────────────────────────────────────────────────────────


def _missing_streamlit_message() -> str:
    return (
        "Streamlit is not installed. Install the optional viewer extra with "
        "`uv sync --extra viz` (or `pip install streamlit`), then run "
        "`streamlit run app/viewer.py`."
    )


def main() -> None:
    """Render the Streamlit dashboard (the only Streamlit-dependent entry point)."""
    try:
        import streamlit as st
    except ImportError:  # pragma: no cover - exercised only without the viz extra
        print(_missing_streamlit_message(), file=sys.stderr)
        raise SystemExit(1) from None

    st.set_page_config(page_title="StockLens", page_icon="📦", layout="wide")
    st.title("📦 StockLens — Purchasing Consolidation & Aging Stock")
    st.caption(
        "Read-only view over `out/consolidate_purchasing_agg.parquet` and the aging "
        "cohort tables. Synthetic data; no live systems are touched."
    )

    rules = _load_rules()
    df = load_consolidated(rules)

    if df is None:
        st.warning(
            f"No consolidated output found. Run **`{_RUN_HINT}`** to generate "
            "`out/consolidate_purchasing_agg.parquet`, then reload this page."
        )
        st.stop()

    # ── Sidebar filters ──────────────────────────────────────────────────────
    st.sidebar.header("Filters")

    if "warehouse_name" in df.columns:
        warehouses = sorted(str(w) for w in df["warehouse_name"].dropna().unique())
        picked_wh = st.sidebar.multiselect("Warehouse", warehouses, default=warehouses)
    else:  # pragma: no cover - defensive; column is contract-locked
        picked_wh = []

    if "cat_flow" in df.columns:
        flows = sorted(str(c) for c in df["cat_flow"].dropna().unique())
        picked_flow = st.sidebar.multiselect("Demand class (cat_flow)", flows, default=flows)
    else:  # pragma: no cover
        picked_flow = []

    filtered = df
    if picked_wh and "warehouse_name" in filtered.columns:
        filtered = filtered[filtered["warehouse_name"].astype(str).isin(picked_wh)]
    if picked_flow and "cat_flow" in filtered.columns:
        filtered = filtered[filtered["cat_flow"].astype(str).isin(picked_flow)]

    # ── Headline metrics ─────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Grains", f"{len(filtered):,}")
    if "warehouse_name" in filtered.columns:
        c2.metric("Warehouses", filtered["warehouse_name"].nunique())
    if "gmv" in filtered.columns:
        c3.metric("Total GMV", f"{filtered['gmv'].sum():,.0f}")
    if "stok_gudang" in filtered.columns:
        c4.metric("On-hand (stok_gudang)", f"{int(filtered['stok_gudang'].sum()):,}")

    # ── Consolidated demand / stock table ────────────────────────────────────
    st.subheader("Consolidated demand & stock position")
    if "cat_flow" in filtered.columns and not filtered.empty:
        flow_counts = (
            filtered["cat_flow"].value_counts().rename_axis("cat_flow").reset_index(name="grains")
        )
        st.caption("Demand-class distribution (current filter):")
        st.dataframe(flow_counts, hide_index=True, use_container_width=True)
    st.dataframe(filtered, hide_index=True, use_container_width=True)

    # ── Aging stock tables ───────────────────────────────────────────────────
    st.subheader("Aging stock — WL cohort")
    frames = load_aging_frames(rules)
    if frames is None:
        st.info(
            f"Aging tables need the seeded database. Run **`{_RUN_HINT}`** to build "
            "`stocklens.duckdb`."
        )
    else:
        left, right = st.columns(2)
        with left:
            st.markdown("**Daily Needs** (aged ≥ daily-needs threshold)")
            dn = frames.get("daily_needs", pd.DataFrame())
            if dn.empty:
                st.write("_No aged Daily-Needs stock for the current run._")
            else:
                st.dataframe(dn, hide_index=True, use_container_width=True)
        with right:
            st.markdown("**Lifestyle** (aged ≥ lifestyle threshold)")
            ls = frames.get("lifestyle", pd.DataFrame())
            if ls.empty:
                st.write("_No aged Lifestyle stock for the current run._")
            else:
                st.dataframe(ls, hide_index=True, use_container_width=True)


if __name__ == "__main__":
    main()
