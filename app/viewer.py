"""StockLens — multi-page inventory-intelligence viewer (home / executive overview).

This is the Streamlit entry point (``streamlit run app/viewer.py``). It is the home
page of a multi-page app; the analytical pages live in ``app/pages/`` and are picked
up automatically by Streamlit's native multi-page routing. The home page is the
executive overview: headline KPIs, the capital-at-risk callout, and a few orienting
charts, all read-only over the artifacts ``python cli.py all`` produces.

Every page (this one included) is a thin presentation script over
:mod:`stocklens.analytics` and the shared :mod:`app._data` helpers — no business
logic is re-implemented here, and nothing touches a live system.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    """Put ``src`` / repo-root / ``app`` on ``sys.path`` for a bare ``streamlit run``."""
    root = Path(__file__).resolve().parents[1]
    for path in (str(root / "src"), str(root), str(root / "app")):
        if path not in sys.path:
            sys.path.insert(0, path)


def main() -> None:
    _bootstrap()

    import _data as data
    import altair as alt
    import streamlit as st

    from stocklens import analytics as A

    data.setup_page(
        "Inventory Intelligence",
        subtitle="Demand · stock position · margin · aging — one synthetic, fully reproducible pipeline.",
        icon="📦",
    )

    rules = data.get_rules()
    df = data.require_consolidated(rules)
    grain = data.load_grain(rules)
    kpis = data.load_kpis(rules)
    as_of = data.as_of(df)

    st.caption(f"Data as of **{as_of:%d %b %Y}** · {int(kpis['grains'])} grains · "
               f"{int(kpis['skus'])} SKUs · {int(kpis['warehouses'])} warehouses")  # fmt: skip

    # ── Operational scale ─────────────────────────────────────────────────────
    st.subheader("Operational scale")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Grains tracked", f"{int(kpis['grains']):,}")
    c2.metric("Active SKUs", f"{int(kpis['skus']):,}")
    c3.metric("Warehouses", f"{int(kpis['warehouses'])}")
    c4.metric("On-hand units", f"{int(kpis['on_hand_units']):,}")

    # ── Financial position ────────────────────────────────────────────────────
    st.subheader("Financial position (trailing 30 days)")
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("GMV", data.compact(kpis["gmv"]))
    f2.metric("Gross margin", data.compact(kpis["total_margin"]), data.pct(kpis["gm_rate"]),
              delta_color="normal")  # fmt: skip
    f3.metric("Inventory @ cost", data.compact(kpis["inventory_value_at_cost"]))
    f4.metric("Days inventory out", f"{kpis['days_inventory_out']:.0f} d",
              help="Inventory at cost ÷ average daily COGS")  # fmt: skip

    # ── Capital at risk (the headline story) ──────────────────────────────────
    st.subheader("⚠️ Capital at risk")
    r1, r2, r3 = st.columns(3)
    r1.metric("Dead-stock capital", data.compact(kpis["dead_stock_value"]),
              f"{int(kpis['dead_stock_skus'])} slow-moving SKUs", delta_color="inverse")  # fmt: skip
    r2.metric("Aged stock value-at-risk", data.compact(kpis["aged_value_at_risk"]),
              f"{int(kpis['aged_skus'])} aged WL SKUs", delta_color="inverse")  # fmt: skip
    share = (
        kpis["dead_stock_value"] / kpis["inventory_value_at_cost"]
        if kpis["inventory_value_at_cost"]
        else 0
    )
    r3.metric("Dead capital share", data.pct(share),
              help="Dead-stock value ÷ total inventory at cost")  # fmt: skip
    st.caption(
        "Dead-stock capital is the value-at-cost of on-hand **Slow Moving** grains — "
        "inventory tying up cash without earning its turns. See **Margin & GMROI** and "
        "**Aging & Dead Stock** to drill in."
    )

    st.divider()

    # ── Orienting charts ──────────────────────────────────────────────────────
    left, right = st.columns(2)

    with left:
        st.markdown("**Demand-class mix** (share of grains)")
        flow = (
            grain["cat_flow"]
            .astype(str)
            .value_counts()
            .rename_axis("cat_flow")
            .reset_index(name="grains")
        )
        donut = (
            alt.Chart(flow)
            .mark_arc(innerRadius=60)
            .encode(
                theta=alt.Theta("grains:Q"),
                color=alt.Color("cat_flow:N", sort=A.CAT_FLOW_ORDER, title="Demand class"),
                tooltip=["cat_flow:N", "grains:Q"],
            )
            .properties(height=280)
        )
        st.altair_chart(donut, use_container_width=True)

    with right:
        st.markdown("**GMV by warehouse**")
        by_wh = (
            grain.groupby("warehouse_name", as_index=False)["gmv"]
            .sum()
            .sort_values("gmv", ascending=False)
        )
        bars = (
            alt.Chart(by_wh)
            .mark_bar()
            .encode(
                x=alt.X("gmv:Q", title="GMV (units)"),
                y=alt.Y("warehouse_name:N", sort="-x", title=None),
                tooltip=["warehouse_name:N", alt.Tooltip("gmv:Q", format=",.0f")],
            )
            .properties(height=280)
        )
        st.altair_chart(bars, use_container_width=True)

    # Inventory value at cost by category.
    st.markdown("**Inventory value at cost, by category**")
    voc = data.load_value_at_cost(rules)
    g_voc = grain.merge(voc, on=A.GRAIN, how="left")
    g_voc["value_at_cost"] = g_voc["value_at_cost"].fillna(0.0)
    by_cat = (
        g_voc.groupby("category_name", as_index=False)["value_at_cost"]
        .sum()
        .sort_values("value_at_cost", ascending=False)
    )
    cat_bars = (
        alt.Chart(by_cat)
        .mark_bar()
        .encode(
            x=alt.X("value_at_cost:Q", title="Inventory at cost (units)"),
            y=alt.Y("category_name:N", sort="-x", title=None),
            tooltip=["category_name:N", alt.Tooltip("value_at_cost:Q", format=",.0f")],
        )
        .properties(height=240)
    )
    st.altair_chart(cat_bars, use_container_width=True)

    # ── Page directory ────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Explore")
    pages = [
        (
            "pages/1_Demand_Classification.py",
            "📈 Demand Classification",
            "Velocity tiering & the weighted-score logic",
        ),
        (
            "pages/2_Stock_and_Reorder.py",
            "🚚 Stock & Reorder",
            "Days-of-cover worklist & stockout risk",
        ),
        (
            "pages/3_Aging_and_Dead_Stock.py",
            "⏳ Aging & Dead Stock",
            "Capital tied up past age thresholds",
        ),
        (
            "pages/4_ABC_XYZ_Matrix.py",
            "🔲 ABC-XYZ Matrix",
            "Value × predictability stocking policy",
        ),
        ("pages/5_Margin_and_GMROI.py", "💰 Margin & GMROI", "Profitability per unit of inventory"),
        (
            "pages/6_Forecast_and_Reorder_Point.py",
            "🔮 Forecast & Reorder Point",
            "Backtested demand + safety stock",
        ),
        ("pages/7_What_if_Simulator.py", "🎛️ What-if Simulator", "Re-tune the policy live"),
        ("pages/8_Data_Quality.py", "✅ Data Quality", "The pipeline's self-defending contract"),
        ("pages/9_Methodology.py", "📖 Methodology", "Every formula & tunable, in plain language"),
    ]
    col_a, col_b = st.columns(2)
    for i, (path, label, blurb) in enumerate(pages):
        target = col_a if i % 2 == 0 else col_b
        with target:
            try:
                st.page_link(path, label=f"**{label}** — {blurb}")
            except Exception:  # pragma: no cover - older Streamlit fallback
                st.markdown(f"**{label}** — {blurb}")

    data.caption_synthetic()


if __name__ == "__main__":
    main()
