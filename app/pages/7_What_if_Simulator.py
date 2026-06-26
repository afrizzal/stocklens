"""What-if Simulator — re-tune the classification policy and watch the mix move.

Every threshold in StockLens is externalised tunable policy. This page re-runs the
demand-classification logic live on a modified copy of the frozen ``Rules`` and shows
how the Super/Fast/Slow mix — and which specific grains — change, quantifying the cost
of a policy choice before it is committed.
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

    import dataclasses

    import _data as data
    import altair as alt
    import pandas as pd
    import streamlit as st

    from stocklens import analytics as A
    from stocklens.demand_classify import classify_demand, load_orders

    data.setup_page(
        "What-if Simulator",
        subtitle="Re-tune the demand-classification policy and see the mix shift, live.",
        icon="🎛️",
    )

    rules = data.get_rules()
    df = data.require_consolidated(rules)
    grain = data.load_grain(rules)
    as_of = data.as_of(df)
    base_cls = rules.classification
    base_demand = rules.demand

    st.sidebar.header("Policy controls")
    weight_qty = st.sidebar.slider(
        "Weight: quantity", 0.0, 1.0, float(base_cls["weight_qty"]), 0.05
    )
    st.sidebar.caption(f"Weight: orders = {1 - weight_qty:.2f} (complement)")
    damp_threshold = st.sidebar.slider(
        "Std-damp threshold", 100, 3000, int(base_cls["std_damp_threshold"]), 100
    )
    damp_factor = st.sidebar.slider(
        "Std-damp factor", 0.1, 1.0, float(base_cls["std_damp_factor"]), 0.05
    )
    iqr_factor = st.sidebar.slider("IQR factor", 1.0, 3.0, float(base_demand["iqr_factor"]), 0.1)
    if st.sidebar.button("↩️ Reset to config defaults"):
        st.rerun()
    data.caption_synthetic()

    modified = dataclasses.replace(
        rules,
        classification={
            **base_cls,
            "weight_qty": weight_qty,
            "weight_orders": round(1 - weight_qty, 4),
            "std_damp_threshold": damp_threshold,
            "std_damp_factor": damp_factor,
        },  # fmt: skip
        demand={**base_demand, "iqr_factor": iqr_factor},
    )

    con = data.open_con(rules)
    try:
        orders = load_orders(con, rules, now=as_of)
    finally:
        con.close()
    baseline = classify_demand(orders, rules)[[*A.GRAIN, "cat_flow"]].rename(
        columns={"cat_flow": "base"}
    )
    tuned = classify_demand(orders, modified)[[*A.GRAIN, "cat_flow"]].rename(
        columns={"cat_flow": "tuned"}
    )
    compare = baseline.merge(tuned, on=A.GRAIN, how="outer")

    # ── Mix shift ─────────────────────────────────────────────────────────────
    st.subheader("Demand-class mix: baseline → tuned")
    mix = pd.concat([
        compare["base"].value_counts().rename_axis("cat_flow").reset_index(name="grains").assign(scenario="baseline"),
        compare["tuned"].value_counts().rename_axis("cat_flow").reset_index(name="grains").assign(scenario="tuned"),
    ])  # fmt: skip
    chart = (
        alt.Chart(mix)
        .mark_bar()
        .encode(
            x=alt.X("scenario:N", title=None),
            y=alt.Y("grains:Q", title="Grains"),
            color=alt.Color("cat_flow:N", sort=A.CAT_FLOW_ORDER, title="Demand class"),
            column=alt.Column("cat_flow:N", sort=A.CAT_FLOW_ORDER, title=None),
            tooltip=["scenario:N", "cat_flow:N", "grains:Q"],
        )
        .properties(height=240, width=110)
    )
    st.altair_chart(chart, use_container_width=False)

    changed = compare[compare["base"].astype(str) != compare["tuned"].astype(str)]
    c1, c2 = st.columns(2)
    c1.metric("Grains that changed class", f"{len(changed):,}")
    c2.metric(
        "Share of grains reclassified", data.pct(len(changed) / len(compare) if len(compare) else 0)
    )

    # ── Which grains moved ────────────────────────────────────────────────────
    if not changed.empty:
        st.subheader("Reclassified grains")
        detail = changed.merge(
            grain[[*A.GRAIN, "warehouse_name", "sku", "product_name", "gmv"]],
            on=A.GRAIN,
            how="left",
        )
        detail = detail.assign(
            transition=detail["base"].astype(str) + " → " + detail["tuned"].astype(str)
        )
        show = ["warehouse_name", "sku", "product_name", "transition", "gmv"]
        st.dataframe(detail[show].sort_values("gmv", ascending=False),
                     hide_index=True, use_container_width=True)  # fmt: skip
        st.caption(
            "Each reclassified grain is working capital being re-prioritised — a downgrade to Slow "
            "frees buying budget; an upgrade to Super Fast pulls it forward."
        )
    else:
        st.info(
            "No grain changed class under this policy — try widening the weights or damp factor."
        )


if __name__ == "__main__":
    main()
