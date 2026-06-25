"""Demand Classification — velocity tiering and the weighted-score logic.

Surfaces the demand-classification stage: the Super / Fast / Slow Moving mix, the
weighted-score-versus-limit rule that drives it, and the rolling-window totals with
the IQR outlier treatment toggled on and off.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[2]
    for path in (str(root / "src"), str(root), str(root / "app")):
        if path not in sys.path:
            sys.path.insert(0, path)


def main() -> None:
    _bootstrap()

    import _data as data
    import altair as alt
    import pandas as pd
    import streamlit as st

    from stocklens import analytics as A

    data.setup_page(
        "Demand Classification",
        subtitle="Weighted velocity score → Super Fast / Fast / Slow Moving, per warehouse.",
        icon="📈",
    )

    rules = data.get_rules()
    df = data.require_consolidated(rules)
    grain = data.sidebar_filters(data.load_grain(rules), key="demand")
    data.caption_synthetic()

    if grain.empty:
        st.info("No grains match the current filters.")
        st.stop()

    # ── Class mix ─────────────────────────────────────────────────────────────
    counts = grain["cat_flow"].astype(str).value_counts()
    cols = st.columns(len(A.CAT_FLOW_ORDER))
    for col, klass in zip(cols, A.CAT_FLOW_ORDER, strict=False):
        col.metric(klass, f"{int(counts.get(klass, 0)):,}")

    st.subheader("Demand-class mix by warehouse")
    mix = (
        grain.groupby(["warehouse_name", "cat_flow"], as_index=False)
        .size()
        .rename(columns={"size": "grains"})
    )
    stacked = (
        alt.Chart(mix)
        .mark_bar()
        .encode(
            x=alt.X("warehouse_name:N", title=None),
            y=alt.Y("grains:Q", stack="normalize", title="Share of grains"),
            color=alt.Color("cat_flow:N", sort=A.CAT_FLOW_ORDER, title="Demand class"),
            tooltip=["warehouse_name:N", "cat_flow:N", "grains:Q"],
        )
        .properties(height=300)
    )
    st.altair_chart(stacked, use_container_width=True)

    # ── Weighted score vs limit (the rule, made visible) ──────────────────────
    st.subheader("The classification rule")
    st.caption(
        f"`weighted = {rules.classification['weight_qty']}·qty + "
        f"{rules.classification['weight_orders']}·orders`. A grain is **Super Fast** when its "
        "weighted score reaches its warehouse limit (mean + std, damped for wide variance), "
        "**Fast** when it reaches the mean, else **Slow**. Points on/right of the 45° line are Super Fast."
    )
    cls = data.load_classification(rules)
    keep_wh = set(grain["warehouse_name"].astype(str))
    cls = cls.merge(grain[[*A.GRAIN, "warehouse_name", "sku"]], on=A.GRAIN, how="inner")
    cls = cls[cls["warehouse_name"].astype(str).isin(keep_wh)]
    plot = cls.dropna(subset=["limit"])
    if not plot.empty:
        scatter = (
            alt.Chart(plot)
            .mark_circle(size=70, opacity=0.7)
            .encode(
                x=alt.X("limit:Q", title="Warehouse limit (mean + std)"),
                y=alt.Y("weighted:Q", title="Weighted velocity score"),
                color=alt.Color("cat_flow:N", sort=A.CAT_FLOW_ORDER, title="Demand class"),
                tooltip=[
                    "sku:N",
                    "warehouse_name:N",
                    alt.Tooltip("weighted:Q", format=",.1f"),
                    alt.Tooltip("avg_score:Q", format=",.1f"),
                    alt.Tooltip("limit:Q", format=",.1f"),
                    "cat_flow:N",
                ],  # fmt: skip
            )
            .properties(height=340)
        )
        lo = float(min(plot["limit"].min(), plot["weighted"].min()))
        hi = float(max(plot["limit"].max(), plot["weighted"].max()))
        line = (
            alt.Chart(pd.DataFrame({"x": [lo, hi], "y": [lo, hi]}))
            .mark_line(strokeDash=[5, 5], color="grey")
            .encode(x="x:Q", y="y:Q")
        )
        st.altair_chart(scatter + line, use_container_width=True)
    else:
        st.info("Not enough multi-grain warehouses to draw the limit reference.")

    # ── Rolling windows & the outlier treatment ───────────────────────────────
    st.subheader("Rolling-window demand & the IQR outlier treatment")
    st.caption(
        "Cumulative L7/L14/L21/L30-day totals. Toggle compares **include** vs **exclude** outliers — "
        "the gap is the demand the IQR rule attributes to freak bulk orders."
    )
    keep = df[df["warehouse_name"].astype(str).isin(keep_wh)]
    windowed = keep.groupby(["days", "status_outliers"], as_index=False)["total_quantity"].sum()
    order = ["L7D", "L14D", "L21D", "L30D"]
    win_chart = (
        alt.Chart(windowed)
        .mark_bar()
        .encode(
            x=alt.X("days:N", sort=order, title="Window"),
            y=alt.Y("total_quantity:Q", title="Total quantity"),
            color=alt.Color("status_outliers:N", title="Outlier treatment"),
            xOffset="status_outliers:N",
            tooltip=["days:N", "status_outliers:N", alt.Tooltip("total_quantity:Q", format=",.0f")],
        )
        .properties(height=300)
    )
    st.altair_chart(win_chart, use_container_width=True)

    # ── Detail table ──────────────────────────────────────────────────────────
    st.subheader("Grain detail")
    show_cols = ["warehouse_name", "sku", "product_name", "category_name", "cat_flow",
                 "qty_per_day", "stok_gudang", "gmv", "recur_tor"]  # fmt: skip
    present = [c for c in show_cols if c in grain.columns]
    st.dataframe(grain[present].sort_values("gmv", ascending=False),
                 hide_index=True, use_container_width=True)  # fmt: skip


if __name__ == "__main__":
    main()
