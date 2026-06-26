"""ABC-XYZ Matrix — value × predictability segmentation and stocking policy.

Crosses an ABC Pareto ranking (contribution to value) with an XYZ classification
(demand variability) into a 3×3 matrix, each cell carrying a differentiated stocking
policy — the classic lever for cutting both stockouts on the vital few and dead
capital on the erratic tail.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[2]
    for path in (str(root / "src"), str(root), str(root / "app")):
        if path not in sys.path:
            sys.path.insert(0, path)


_POLICY = {
    "AX": "Automate & always stock", "AY": "Stock with buffer", "AZ": "Tight review, hold safety",
    "BX": "Periodic auto-order", "BY": "Standard review", "BZ": "Cautious, smaller lots",
    "CX": "Min-stock / bulk-buy", "CY": "Make-to-order leaning", "CZ": "Make-to-order / delist",
}  # fmt: skip


def main() -> None:
    _bootstrap()

    import _data as data
    import altair as alt
    import streamlit as st

    from stocklens import analytics as A

    data.setup_page(
        "ABC-XYZ Matrix",
        subtitle="Pareto value (A/B/C) × demand variability (X/Y/Z) → stocking policy.",
        icon="🔲",
    )

    rules = data.get_rules()
    df = data.require_consolidated(rules)

    st.sidebar.header("Segmentation policy")
    value_col = st.sidebar.selectbox("ABC value basis", ["gmv", "total_margin"], index=0)
    a_cut = st.sidebar.slider("A cut (cumulative value share)", 0.5, 0.95, 0.80, 0.05)
    b_cut = st.sidebar.slider("B cut (cumulative value share)", a_cut, 0.99, max(0.95, a_cut), 0.01)
    cfg = A.AnalyticsConfig(abc_a_cut=a_cut, abc_b_cut=b_cut)
    data.caption_synthetic()

    abc = A.abc_classification(df, value_col=value_col, cfg=cfg)
    xyz = data.load_xyz(rules)
    matrix = A.abc_xyz_matrix(abc, xyz)

    # ── Counts ────────────────────────────────────────────────────────────────
    a_counts = abc["abc_class"].value_counts()
    x_counts = xyz["xyz_class"].value_counts()
    cols = st.columns(6)
    for col, klass in zip(cols[:3], ["A", "B", "C"], strict=False):
        col.metric(f"Class {klass}", int(a_counts.get(klass, 0)))
    for col, klass in zip(cols[3:], ["X", "Y", "Z"], strict=False):
        col.metric(f"Class {klass}", int(x_counts.get(klass, 0)))

    # ── Pareto curve ──────────────────────────────────────────────────────────
    st.subheader("Pareto curve")
    pareto = abc.reset_index(drop=True).copy()
    pareto["rank"] = pareto.index + 1
    base = alt.Chart(pareto).encode(x=alt.X("rank:Q", title="SKU rank (by value)"))
    bars = base.mark_bar(opacity=0.4).encode(
        y=alt.Y("value:Q", title="Value"),
        color=alt.Color("abc_class:N", title="ABC"),
        tooltip=["sku:N", "abc_class:N", alt.Tooltip("value:Q", format=",.0f")],
    )
    cumline = base.mark_line(color="crimson").encode(
        y=alt.Y("cum_share:Q", axis=alt.Axis(format="%", title="Cumulative share")),
    )
    st.altair_chart(
        alt.layer(bars, cumline).resolve_scale(y="independent").properties(height=320),
        use_container_width=True,
    )

    # ── 3×3 heatmap ───────────────────────────────────────────────────────────
    st.subheader("ABC-XYZ matrix")
    matrix = matrix.copy()
    matrix["cell"] = matrix["abc_class"] + matrix["xyz_class"]
    matrix["policy"] = matrix["cell"].map(_POLICY).fillna("")
    heat = (
        alt.Chart(matrix)
        .mark_rect()
        .encode(
            x=alt.X("xyz_class:N", sort=["X", "Y", "Z"], title="Predictability (XYZ)"),
            y=alt.Y("abc_class:N", sort=["A", "B", "C"], title="Value (ABC)"),
            color=alt.Color("skus:Q", scale=alt.Scale(scheme="blues"), title="SKUs"),
            tooltip=["cell:N", "skus:Q", alt.Tooltip("value:Q", format=",.0f"), "policy:N"],
        )
        .properties(height=300)
    )
    text = (
        alt.Chart(matrix)
        .mark_text(baseline="middle", fontWeight="bold")
        .encode(
            x=alt.X("xyz_class:N", sort=["X", "Y", "Z"]),
            y=alt.Y("abc_class:N", sort=["A", "B", "C"]),
            text=alt.Text("skus:Q"),
            color=alt.value("black"),
        )
    )
    st.altair_chart(heat + text, use_container_width=True)

    # ── Policy legend + detail ────────────────────────────────────────────────
    st.subheader("Recommended policy per cell")
    legend = matrix[["cell", "skus", "value", "policy"]].sort_values("value", ascending=False)
    st.dataframe(legend, hide_index=True, use_container_width=True)

    st.caption(
        "XYZ uses the coefficient of variation of weekly demand over the recent window. With ~4–5 "
        "weeks of synthetic history this is a short-window estimate — directionally right, not a "
        "long-run seasonality read."
    )


if __name__ == "__main__":
    main()
