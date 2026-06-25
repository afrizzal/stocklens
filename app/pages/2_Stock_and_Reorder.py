"""Stock & Reorder — the multi-layer stock position and the buyer's reorder worklist.

Shows the five stock layers per warehouse, a Red/Amber/Green stockout-risk summary,
and the actionable reorder worklist (days-of-cover below lead time, with a suggested
order quantity) that a buyer would work down each morning.
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
    import streamlit as st

    from stocklens import analytics as A

    data.setup_page(
        "Stock & Reorder",
        subtitle="Days-of-cover vs lead time → a ranked replenishment worklist.",
        icon="🚚",
    )

    rules = data.get_rules()
    df = data.require_consolidated(rules)
    as_of = data.as_of(df)

    # Policy controls.
    st.sidebar.header("Reorder policy")
    review = st.sidebar.slider("Review buffer (days)", 0, 21, 7,
                               help="Amber when cover is below lead time + this buffer.")  # fmt: skip
    cfg = A.AnalyticsConfig(reorder_review_days=review)
    grain = data.sidebar_filters(data.load_grain(rules), key="stock")
    data.caption_synthetic()

    if grain.empty:
        st.info("No grains match the current filters.")
        st.stop()

    keep_wh = set(grain["warehouse_name"].astype(str))
    scoped = df[df["warehouse_name"].astype(str).isin(keep_wh)]
    cover = A.days_of_cover(scoped, cfg=cfg, now=as_of)

    # ── Risk summary ──────────────────────────────────────────────────────────
    risk_counts = cover["stockout_risk"].value_counts()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🔴 Red (reorder now)", int(risk_counts.get("Red", 0)))
    c2.metric("🟠 Amber (watch)", int(risk_counts.get("Amber", 0)))
    c3.metric("🟢 Green (healthy)", int(risk_counts.get("Green", 0)))
    c4.metric("On-hand units", f"{int(grain['stok_gudang'].sum()):,}")

    # ── Stock layers per warehouse ────────────────────────────────────────────
    st.subheader("Stock position by layer")
    layers = ["stok_belum_rilis", "stok_rilis", "stok_booking", "stok_incoming", "stok_gudang"]
    melt = (
        grain.groupby("warehouse_name", as_index=False)[layers]
        .sum()
        .melt(id_vars="warehouse_name", var_name="layer", value_name="units")
    )
    layer_chart = (
        alt.Chart(melt)
        .mark_bar()
        .encode(
            x=alt.X("warehouse_name:N", title=None),
            y=alt.Y("units:Q", title="Units"),
            color=alt.Color("layer:N", title="Stock layer"),
            tooltip=["warehouse_name:N", "layer:N", alt.Tooltip("units:Q", format=",.0f")],
        )
        .properties(height=300)
    )
    st.altair_chart(layer_chart, use_container_width=True)

    # ── Reorder worklist ──────────────────────────────────────────────────────
    st.subheader("Reorder worklist")
    work = A.reorder_worklist(scoped, cfg=cfg, now=as_of)
    if "PIC" in cover.columns:
        pics = sorted(cover["PIC"].astype(str).unique())
        chosen = st.multiselect("Filter by buyer (PIC)", pics, default=pics)
        if chosen and not work.empty and "PIC" in work.columns:
            work = work[work["PIC"].astype(str).isin(chosen)]

    if work.empty:
        st.success("No grains need reordering under the current policy — every line is covered "
                   "through its lead time.")  # fmt: skip
    else:
        st.caption(f"{len(work)} grain(s) below the reorder point, ranked by stockout urgency.")
        st.dataframe(work, hide_index=True, use_container_width=True)
        st.download_button(
            "⬇️ Download worklist (CSV)",
            work.to_csv(index=False).encode("utf-8"),
            file_name="reorder_worklist.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
