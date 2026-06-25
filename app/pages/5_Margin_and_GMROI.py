"""Margin & GMROI — profitability per unit of inventory invested.

Pairs gross-margin rate with turnover and GMROI (gross margin ÷ inventory at cost) to
separate the lines that earn their shelf space from the dead capital — the inventory
returning less than it costs to hold.
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
    import numpy as np
    import pandas as pd
    import streamlit as st

    from stocklens import analytics as A

    data.setup_page(
        "Margin & GMROI",
        subtitle="Gross-margin return on inventory — which stock earns its keep.",
        icon="💰",
    )

    rules = data.get_rules()
    data.require_consolidated(rules)  # ensures artifacts exist / stops with a prompt
    grain = data.sidebar_filters(data.load_grain(rules), key="margin")
    data.caption_synthetic()

    if grain.empty:
        st.info("No grains match the current filters.")
        st.stop()

    voc = data.load_value_at_cost(rules)
    g = grain.merge(voc, on=A.GRAIN, how="left")
    g["value_at_cost"] = g["value_at_cost"].fillna(0.0)
    g["gmroi"] = np.where(g["value_at_cost"] > 0, g["total_margin"] / g["value_at_cost"], np.nan)

    gmv = float(g["gmv"].sum())
    margin = float(g["total_margin"].sum())
    cogs = gmv - margin
    dead = int((g["gmroi"] < 1).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("GMV", data.compact(gmv))
    c2.metric("Gross margin", data.compact(margin), data.pct(margin / gmv if gmv else 0))
    c3.metric("Median GMROI", f"{np.nanmedian(g['gmroi']):.2f}")
    c4.metric("Dead-capital SKUs", f"{dead:,}", help="GMROI < 1.0 (margin under inventory cost)")

    # ── Margin waterfall ──────────────────────────────────────────────────────
    st.subheader("Margin bridge")
    waterfall = pd.DataFrame({
        "step": ["GMV", "COGS", "Gross margin"],
        "value": [gmv, -cogs, margin],
    })  # fmt: skip
    bridge = (
        alt.Chart(waterfall)
        .mark_bar()
        .encode(
            x=alt.X("step:N", sort=["GMV", "COGS", "Gross margin"], title=None),
            y=alt.Y("value:Q", title="Value"),
            color=alt.Color("step:N", legend=None),
            tooltip=["step:N", alt.Tooltip("value:Q", format=",.0f")],
        )
        .properties(height=260)
    )
    st.altair_chart(bridge, use_container_width=True)

    # ── GMROI vs turnover bubble ──────────────────────────────────────────────
    st.subheader("Margin rate vs turnover")
    st.caption(
        "Bubble size = GMV. Bottom-left (low margin, low turnover) is where capital goes to die."
    )
    bubble_df = g.dropna(subset=["gm_rate", "recur_tor"]).copy()
    bubble = (
        alt.Chart(bubble_df)
        .mark_circle(opacity=0.6)
        .encode(
            x=alt.X("recur_tor:Q", title="Recurring turnover (recur_tor)"),
            y=alt.Y("gm_rate:Q", axis=alt.Axis(format="%"), title="Gross-margin rate"),
            size=alt.Size("gmv:Q", title="GMV", scale=alt.Scale(range=[20, 600])),
            color=alt.Color("cat_flow:N", sort=A.CAT_FLOW_ORDER, title="Demand class"),
            tooltip=[
                "sku:N",
                "warehouse_name:N",
                alt.Tooltip("gm_rate:Q", format=".1%"),
                alt.Tooltip("recur_tor:Q", format=".2f"),
                alt.Tooltip("gmroi:Q", format=".2f"),
            ],  # fmt: skip
        )
        .properties(height=340)
    )
    st.altair_chart(bubble, use_container_width=True)

    # ── GMROI ranking ─────────────────────────────────────────────────────────
    st.subheader("GMROI ranking")
    cols = ["warehouse_name", "sku", "product_name", "category_name", "cat_flow",
            "gmv", "total_margin", "gm_rate", "value_at_cost", "gmroi"]  # fmt: skip
    present = [c for c in cols if c in g.columns]
    ranked = g[present].sort_values("gmroi", ascending=False, na_position="last")
    top, bottom = st.tabs(["🏆 Top earners", "⚠️ Dead capital"])
    with top:
        st.dataframe(ranked.head(25), hide_index=True, use_container_width=True)
    with bottom:
        st.dataframe(
            ranked[ranked["gmroi"] < 1].sort_values("value_at_cost", ascending=False).head(25),
            hide_index=True,
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
