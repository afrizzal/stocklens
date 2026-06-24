"""Margin / GMV / gross-margin-rate and inventory-turnover analytics.

Ports PART 3 of the original ``consolidate_purchasing.py`` pipeline
(``In[92]``-``In[110]``), sanitized for the public showcase:

* :func:`load_margin` — GMV, total gross margin and ``gm_rate`` per
  ``(product_id, unit, warehouse_id)`` over the sales-lookback window
  (orig ``In[92]``-``In[97]``), via ``sql/margin.sql``.
* :func:`load_turnover` — the L7/L14/L21/L30 turnover ratios plus the
  ``recur_tor`` fallback ladder (orig ``In[103]``-``In[110]``), via
  ``sql/turnover.sql``.
* :func:`load_tag_relations` — the ``status_wl`` flag per product
  (orig ``In[99]``-``In[101]``), an inline query.

All functions are pure: they take a DuckDB connection and a :class:`Rules`
object and return pandas DataFrames. No network, email, spreadsheet, object-store
or BI-publish I/O happens anywhere in this module — only local DuckDB reads.
Every division is divide-by-zero guarded.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from shims import data_io
from stocklens import Rules

# Directory holding the .sql files shipped alongside this module.
_SQL_DIR = Path(__file__).resolve().parent / "sql"


def _read_sql(name: str) -> str:
    """Return the text of ``sql/<name>``.

    Prefers the shim's :func:`shims.data_io.read_sql_file` so the loading path
    matches the rest of the pipeline, but falls back to a direct read keyed off
    this module's location (the shim resolves paths relative to the package).
    """
    try:
        return data_io.read_sql_file(name)
    except (FileNotFoundError, OSError):
        return (_SQL_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Margin / GMV / gm_rate  (orig In[92]-In[97])
# ---------------------------------------------------------------------------
def load_margin(
    con: duckdb.DuckDBPyConnection,
    rules: Rules,
    *,
    now: date,
) -> pd.DataFrame:
    """Compute GMV, total gross margin and margin rate per grain.

    Runs ``sql/margin.sql`` over the last ``windows.sales_lookback_days`` days
    (window ``[now - lookback, now]``), then aggregates each OUT line to
    ``(product_id, unit, warehouse_id)``:

    * ``gmv``          = Σ(``selling_price`` × ``quantity_out``)
    * ``total_margin`` = Σ((``selling_price`` − ``purchase_price``) × ``quantity_out``)
    * ``gm_rate``      = ``total_margin`` / ``gmv`` (``0`` when ``gmv == 0``)

    Args:
        con: Open DuckDB connection to the seeded database.
        rules: Loaded tunables; ``windows.sales_lookback_days`` sets the window.
        now: Reference "today" (the window's inclusive upper bound).

    Returns:
        DataFrame with columns ``product_id, unit, warehouse_id, gmv,
        total_margin, gm_rate`` — one row per grain (always those six columns,
        even when the source window is empty).
    """
    lookback = int(rules.windows["sales_lookback_days"])
    start = now - timedelta(days=lookback)
    end = now

    sql = _read_sql("margin.sql")
    df = con.execute(sql, [start, end]).df()

    cols = ["product_id", "unit", "warehouse_id", "gmv", "total_margin", "gm_rate"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    # Per-line economics (orig In[94]-In[95]). Missing prices contribute zero.
    selling = df["selling_price"].fillna(0.0)
    purchase = df["purchase_price"].fillna(0.0)
    qty_out = df["quantity_out"].fillna(0.0)
    df = df.assign(
        gmv=selling * qty_out,
        total_margin=(selling - purchase) * qty_out,
    )

    # Aggregate to the grain (orig In[96]).
    agg = (
        df.groupby(["product_id", "unit", "warehouse_id"], as_index=False)[
            ["gmv", "total_margin"]
        ]
        .sum()
    )

    # gm_rate with divide-by-zero guard (orig In[97], guarded per contract §3.3).
    agg["gm_rate"] = np.where(
        agg["gmv"] != 0,
        agg["total_margin"] / agg["gmv"].replace(0, np.nan),
        0.0,
    )
    agg["gm_rate"] = agg["gm_rate"].fillna(0.0)

    return agg[cols]


# ---------------------------------------------------------------------------
# Turnover ratios + recur_tor ladder  (orig In[103]-In[109])
# ---------------------------------------------------------------------------
def _tor(
    inv: pd.Series,
    inc: pd.Series,
    final: pd.Series,
) -> pd.Series:
    """Vectorised turnover ratio with a divide-by-zero guard.

    ``tor = (inv + inc - final) / ((inv + final) / 2)`` rounded to 2 dp; the
    denominator being zero (a never-stocked grain) yields ``0`` rather than an
    inf/NaN (mirrors the original ``df_tor.fillna(0)`` at ``In[105]``).
    """
    denom = (inv + final) / 2.0
    numer = inv + inc - final
    raw = np.where(denom != 0, numer / denom.replace(0, np.nan), 0.0)
    return pd.Series(raw, index=inv.index).fillna(0.0).round(2)


def _cap(tor: pd.Series, threshold: int, cap_value: int) -> pd.Series:
    """Cap a turnover column: ``tor >= threshold`` collapses to ``cap_value``.

    Ports orig ``In[107]`` (``np.where(tor < 30, tor, cap)``) expressed as a
    ``>=`` cap so the threshold value itself is capped.
    """
    return np.where(tor < threshold, tor, float(cap_value))


def load_turnover(
    con: duckdb.DuckDBPyConnection,
    rules: Rules,
    *,
    now: date,
) -> pd.DataFrame:
    """Compute per-window turnover ratios and the ``recur_tor`` fallback.

    Runs ``sql/turnover.sql`` (snapshots at ``asof - {1,8,15,22,31}`` and
    incoming sums over the rolling L7/L14/L21/L30 windows, ``asof = now``), then:

    * ``l*d_tor`` = ``(inv + inc - final) / ((inv + final) / 2)`` rounded to 2 dp,
      divide-by-zero guarded to ``0``.
    * Cap: for L7/L14/L21, ``tor >= turnover.tor_cap_threshold`` →
      ``turnover.tor_cap_value_default`` (14). For L30, → ``turnover.tor_cap_value_l30``
      (30). (orig ``In[107]``.)
    * ``recur_tor`` = the first strictly-positive window in the ladder
      L7 → L14 → L21 → L30, else ``turnover.recur_fallback`` (14). (orig ``In[108]``.)

    Args:
        con: Open DuckDB connection to the seeded database.
        rules: Loaded tunables; ``turnover.*`` keys set thresholds / caps.
        now: Reference "today"; used as the ``asof`` anchor for the snapshots.

    Returns:
        DataFrame with columns ``product_id, warehouse_id, l7d_tor, l14d_tor,
        l21d_tor, l30d_tor, recur_tor`` — one row per ``(product_id,
        warehouse_id)`` (the intermediate ``l7d/l14d/l21d`` tors are retained
        alongside the contract-required ``l30d_tor`` / ``recur_tor``).
    """
    asof = now
    sql = _read_sql("turnover.sql")
    # The query references the single :asof DATE once per positional ?
    # placeholder (5 snapshot CTEs + 4 incoming CTEs × 2 bounds = 13), all bound
    # to the same asof anchor. Count them so the binding can never drift from the
    # file.
    n_params = sql.count("?")
    df = con.execute(sql, [asof] * n_params).df()

    cols = [
        "product_id",
        "warehouse_id",
        "l7d_tor",
        "l14d_tor",
        "l21d_tor",
        "l30d_tor",
        "recur_tor",
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)

    # fillna(0) before the tor math (orig In[105]).
    df = df.fillna(0)

    # Per-window turnover ratios (orig In[106]).
    df["l7d_tor"] = _tor(df["l7d_inv"], df["l7d_inc"], df["final_inv"])
    df["l14d_tor"] = _tor(df["l14d_inv"], df["l14d_inc"], df["final_inv"])
    df["l21d_tor"] = _tor(df["l21d_inv"], df["l21d_inc"], df["final_inv"])
    df["l30d_tor"] = _tor(df["l30d_inv"], df["l30d_inc"], df["final_inv"])

    # Caps (orig In[107]). L7/L14/L21 share the default cap value; L30 has its own.
    threshold = int(rules.turnover["tor_cap_threshold"])
    cap_default = int(rules.turnover["tor_cap_value_default"])
    cap_l30 = int(rules.turnover["tor_cap_value_l30"])
    df["l7d_tor"] = _cap(df["l7d_tor"], threshold, cap_default)
    df["l14d_tor"] = _cap(df["l14d_tor"], threshold, cap_default)
    df["l21d_tor"] = _cap(df["l21d_tor"], threshold, cap_default)
    df["l30d_tor"] = _cap(df["l30d_tor"], threshold, cap_l30)

    # recur_tor ladder (orig In[108]): first positive window, else fallback.
    fallback = float(rules.turnover["recur_fallback"])
    df["recur_tor"] = _recur_tor(
        df["l7d_tor"], df["l14d_tor"], df["l21d_tor"], df["l30d_tor"], fallback
    )

    return df[cols]


def _recur_tor(
    t7: pd.Series,
    t14: pd.Series,
    t21: pd.Series,
    t30: pd.Series,
    fallback: float,
) -> pd.Series:
    """Resolve ``recur_tor`` per row via the original fallback ladder.

    Faithful port of orig ``In[108]``::

        if   i > 0:                t = i
        elif i == 0 and j > 0:     t = j
        elif i + j == 0 and k > 0: t = k
        elif i+j+k == 0 and l > 0: t = l
        elif i+j+k+l == 0:         t = 14

    The original could leave ``t`` undefined when none of these branches
    matched (e.g. mixed negative tors that never sum to exactly 0); here that
    residual case falls through to the configured ``recur_fallback`` so the
    function is always total.
    """
    out = np.full(len(t7), fallback, dtype=float)
    # i/j/k/m mirror the original ladder's i/j/k/l (renamed off the ambiguous "l").
    i = t7.to_numpy(dtype=float)
    j = t14.to_numpy(dtype=float)
    k = t21.to_numpy(dtype=float)
    m = t30.to_numpy(dtype=float)

    # Evaluate the ladder from the lowest-priority branch upward so that the
    # higher-priority assignments overwrite, reproducing the if/elif order.
    out = np.where((i + j + k + m) == 0, fallback, out)
    out = np.where((i + j + k) == 0, np.where(m > 0, m, out), out)
    out = np.where((i + j) == 0, np.where(k > 0, k, out), out)
    out = np.where(i == 0, np.where(j > 0, j, out), out)
    out = np.where(i > 0, i, out)
    return pd.Series(out, index=t7.index)


# ---------------------------------------------------------------------------
# Tag relations / status_wl flag  (orig In[99]-In[101])
# ---------------------------------------------------------------------------
def load_tag_relations(
    con: duckdb.DuckDBPyConnection,
    rules: Rules,
) -> pd.DataFrame:
    """Return the ``status_wl`` flag per product (synthetic tokens).

    Inline port of orig ``In[99]``: ``type=1`` products carry their product-tag
    name (no ``status_wl``); ``type=2`` products carry the max ``status_wl`` from
    ``product_rtp``. The original kept a hand-picked set of tag names and emitted
    only ``(product_id, status_wl)`` downstream — we keep the same projection.

    Args:
        con: Open DuckDB connection to the seeded database.
        rules: Loaded tunables (unused beyond signature parity; kept for a
            consistent module interface).

    Returns:
        DataFrame with columns ``product_id, status_wl`` — one row per product
        (``status_wl`` is the synthetic WL token, or ``None`` for non-RTP rows).
    """
    sql = """
        SELECT
            ptr.product_tag_id,
            p.id           AS product_id,
            pt.name        AS tag_name,
            CAST(NULL AS VARCHAR) AS status_wl
        FROM products p
        LEFT JOIN product_tag_relations ptr ON p.id = ptr.product_id
        JOIN product_tags pt ON pt.id = ptr.product_tag_id
        WHERE p.type = 1
        UNION ALL
        SELECT
            CAST(NULL AS BIGINT) AS product_tag_id,
            p.id           AS product_id,
            CASE WHEN p.type = 2 THEN 'rtp' END AS tag_name,
            pr.status_wl
        FROM products p
        LEFT JOIN (
            SELECT product_id, max(status_wl) AS status_wl
            FROM product_rtp
            GROUP BY 1
        ) pr ON pr.product_id = p.id
        WHERE p.type = 2
    """
    df = data_io.get_data(sql, con)

    cols = ["product_id", "status_wl"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    return df[cols].drop_duplicates().reset_index(drop=True)
