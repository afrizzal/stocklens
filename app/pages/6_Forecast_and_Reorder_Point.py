"""Forecast & Reorder Point — backtested demand projection and safety stock.

Projects near-term demand with naive / moving-average / exponential-smoothing methods,
honestly backtests them against a seasonal-naive baseline (WAPE on a holdout), and turns
the demand statistics into a service-level-based reorder point and safety stock.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[2]
    for path in (str(root / "src"), str(root), str(root / "app")):
        if path not in sys.path:
            sys.path.insert(0, path)


_SERVICE_Z = {"90%": 1.2816, "95%": 1.6449, "97.5%": 1.9600, "99%": 2.3263}


def main() -> None:
    _bootstrap()

    import _data as data
    import altair as alt
    import pandas as pd
    import streamlit as st

    from stocklens import analytics as A

    data.setup_page(
        "Forecast & Reorder Point",
        subtitle="Short-horizon demand forecast (backtested) → safety stock & reorder point.",
        icon="🔮",
    )

    rules = data.get_rules()
    df = data.require_consolidated(rules)
    grain = data.load_grain(rules)
    as_of = data.as_of(df)
    data.caption_synthetic()

    st.warning(
        "Run-rate forecasting, not a trained seasonal model: the synthetic history is ~30 days, so "
        "this supports short moving-average / smoothing only. Forecast error is reported honestly "
        "(WAPE on a holdout) and must beat a seasonal-naive baseline to be trusted.",
        icon="ℹ️",
    )

    warehouses = sorted(grain["warehouse_name"].astype(str).unique())
    scope = st.selectbox("Warehouse", ["All warehouses", *warehouses])
    wh_id = None
    if scope != "All warehouses":
        wh_id = int(grain.loc[grain["warehouse_name"].astype(str) == scope, "warehouse_id"].iloc[0])

    con = data.open_con(rules)
    try:
        series = A.daily_demand_series(con, warehouse_id=wh_id, now=as_of, lookback_days=45)
    finally:
        con.close()

    # Trim leading zero-demand days for a cleaner trend view.
    nonzero = series[series["qty"] > 0]
    if not nonzero.empty:
        series = series[series["order_date"] >= nonzero["order_date"].min()].reset_index(drop=True)

    col_h, col_m = st.columns(2)
    horizon = col_h.slider("Forecast horizon (days)", 3, 14, 7)
    method = col_m.selectbox("Method", ["ses", "ma", "naive"],
                             format_func={"ses": "Exponential smoothing", "ma": "Moving average (7d)",
                                          "naive": "Naive (last value)"}.get)  # fmt: skip

    fc_values = A.forecast(series["qty"], horizon=horizon, method=method)
    future_dates = pd.date_range(series["order_date"].max() + pd.Timedelta(days=1), periods=horizon)
    hist = series.assign(kind="history").rename(columns={"order_date": "date"})
    fut = pd.DataFrame({"date": future_dates, "qty": fc_values, "kind": "forecast"})
    combined = pd.concat([hist[["date", "qty", "kind"]], fut], ignore_index=True)

    st.subheader("Daily demand & forecast")
    chart = (
        alt.Chart(combined)
        .mark_line(point=True)
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("qty:Q", title="Units / day"),
            color=alt.Color(
                "kind:N",
                title=None,
                scale=alt.Scale(domain=["history", "forecast"], range=["#4c78a8", "#e45756"]),
            ),  # fmt: skip
            strokeDash=alt.StrokeDash("kind:N", legend=None),
            tooltip=["date:T", alt.Tooltip("qty:Q", format=",.0f"), "kind:N"],
        )
        .properties(height=320)
    )
    st.altair_chart(chart, use_container_width=True)

    # ── Backtest ──────────────────────────────────────────────────────────────
    st.subheader("Backtest (WAPE on a 7-day holdout)")
    bt = A.backtest(series["qty"], holdout=7)
    if bt.empty:
        st.info("Not enough history to backtest at this scope.")
    else:
        bt_show = bt.assign(WAPE=(bt["wape"] * 100).round(1).astype(str) + "%")
        baseline = float(bt.loc[bt["method"] == "seasonal_naive", "wape"].iloc[0])
        best = bt.iloc[0]
        st.dataframe(bt_show[["method", "WAPE", "beats_baseline"]], hide_index=True,
                     use_container_width=True)  # fmt: skip
        verdict = (
            "beats"
            if best["method"] != "seasonal_naive" and best["wape"] <= baseline
            else "ties/loses to"
        )
        st.caption(f"Best method **{best['method']}** (WAPE {best['wape']:.1%}) {verdict} the "
                   f"seasonal-naive baseline (WAPE {baseline:.1%}).")  # fmt: skip

    # ── Reorder point (per SKU) ───────────────────────────────────────────────
    st.subheader("Reorder point & safety stock")
    scope_grain = grain if wh_id is None else grain[grain["warehouse_id"] == wh_id]
    skus = scope_grain.assign(
        label=scope_grain["sku"].astype(str) + " · " + scope_grain["warehouse_name"].astype(str)
    )
    pick = st.selectbox("SKU / warehouse", skus["label"].tolist())
    row = skus[skus["label"] == pick].iloc[0]

    s1, s2 = st.columns(2)
    service = s1.selectbox("Target service level", list(_SERVICE_Z.keys()), index=1)
    lead = s2.slider("Lead time (days)", 1, 21, int(row.get("adj_lead_time", 3)))
    z = _SERVICE_Z[service]

    con = data.open_con(rules)
    try:
        sku_series = A.daily_demand_series(
            con,
            grain=(int(row["warehouse_id"]), int(row["product_id"]), int(row["product_attribute_id"])),
            now=as_of, lookback_days=45,
        )  # fmt: skip
    finally:
        con.close()
    avg_daily = float(sku_series["qty"].mean())
    sigma_daily = float(sku_series["qty"].std(ddof=1)) if len(sku_series) > 1 else 0.0
    rp = A.reorder_point(avg_daily, lead, sigma_daily, z)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Avg daily demand", f"{avg_daily:.1f}")
    m2.metric("Cycle stock", f"{rp['cycle_stock']:.0f}")
    m3.metric(f"Safety stock ({service})", f"{rp['safety_stock']:.0f}")
    m4.metric("Reorder point", f"{rp['reorder_point']:.0f}",
              help="Reorder when on-hand crosses this level.")  # fmt: skip

    on_hand = float(row.get("stok_gudang", 0))
    if on_hand <= rp["reorder_point"]:
        st.error(f"On-hand {on_hand:,.0f} ≤ reorder point {rp['reorder_point']:.0f} — reorder now.")
    else:
        st.success(f"On-hand {on_hand:,.0f} is above the reorder point {rp['reorder_point']:.0f}.")


if __name__ == "__main__":
    main()
