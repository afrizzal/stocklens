"""Aging & Dead Stock — capital tied up past its age and demand thresholds.

Joins the aged WL cohort (Daily-Needs ≥15d / Lifestyle ≥31d) with its 7-day sell-out,
quantifies the value at risk in currency, charts the age distribution against the
thresholds, and cross-references the slow-moving "dead capital" from the demand tiering.
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
        "Aging & Dead Stock",
        subtitle="Aged WL stock and slow-moving capital — quantified in currency.",
        icon="⏳",
    )

    rules = data.get_rules()
    data.require_consolidated(rules)  # ensures artifacts exist / stops with a prompt
    frames = data.load_aging_frames(rules)
    data.caption_synthetic()

    value_col = "total_purchase_stok_tanpa_booking"

    if frames is None:
        st.warning(f"Aging tables need the seeded database. Run **`{data.RUN_HINT}`**.")
        st.stop()

    all_aged = frames.get("all")
    aged_value = (
        float(all_aged[value_col].sum()) if all_aged is not None and not all_aged.empty else 0.0
    )
    aged_skus = 0 if all_aged is None else len(all_aged)
    sold = (
        float(all_aged["qty_sell_out"].sum())
        if all_aged is not None and "qty_sell_out" in all_aged
        else 0.0
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Aged value at risk", data.compact(aged_value))
    c2.metric("Aged WL SKUs", f"{aged_skus:,}")
    c3.metric("7-day sell-out (units)", f"{int(sold):,}",
              help="Units of the aged cohort that still sold in the last 7 days.")  # fmt: skip

    # ── Value at risk by warehouse ────────────────────────────────────────────
    if all_aged is not None and not all_aged.empty:
        st.subheader("Value at risk by warehouse & category")
        bar = (
            alt.Chart(all_aged)
            .mark_bar()
            .encode(
                x=alt.X(f"sum({value_col}):Q", title="Tied-up purchase value"),
                y=alt.Y("warehouse_name:N", sort="-x", title=None),
                color=alt.Color("Category:N", title="Category"),
                tooltip=[
                    "warehouse_name:N",
                    "Category:N",
                    alt.Tooltip(f"sum({value_col}):Q", format=",.0f"),
                ],  # fmt: skip
            )
            .properties(height=240)
        )
        st.altair_chart(bar, use_container_width=True)

    # ── Age distribution vs thresholds ────────────────────────────────────────
    st.subheader("Age distribution vs thresholds")
    from stocklens.aging_alert import load_cohort

    cohort = load_cohort(rules)
    daily_days = int(rules.aging["daily_needs_days"])
    life_days = int(rules.aging["lifestyle_days"])
    hist = (
        alt.Chart(cohort)
        .mark_bar(opacity=0.8)
        .encode(
            x=alt.X("diff_days_inhouse:Q", bin=alt.Bin(maxbins=20), title="Days in house"),
            y=alt.Y("count():Q", title="Cohort rows"),
            tooltip=[alt.Tooltip("count():Q")],
        )
        .properties(height=260)
    )
    rules_df = cohort.assign(_dn=daily_days, _ls=life_days)
    dn_line = alt.Chart(rules_df).mark_rule(color="orange", strokeDash=[4, 4]).encode(x="_dn:Q")
    ls_line = alt.Chart(rules_df).mark_rule(color="red", strokeDash=[4, 4]).encode(x="_ls:Q")
    st.altair_chart(hist + dn_line + ls_line, use_container_width=True)
    st.caption(f"Dashed lines: Daily-Needs threshold ({daily_days}d, orange) and Lifestyle "
               f"threshold ({life_days}d, red).")  # fmt: skip

    # ── Per-category tables ───────────────────────────────────────────────────
    st.subheader("Aged cohort detail")
    tab_dn, tab_ls, tab_all = st.tabs(["Daily Needs", "Lifestyle", "All"])
    for tab, key in ((tab_dn, "daily_needs"), (tab_ls, "lifestyle"), (tab_all, "all")):
        with tab:
            frame = frames.get(key)
            if frame is None or frame.empty:
                st.write("_No aged stock in this category for the current run._")
            else:
                st.dataframe(frame, hide_index=True, use_container_width=True)

    # ── Dead capital (slow movers) ────────────────────────────────────────────
    st.subheader("Dead capital — on-hand slow movers")
    voc = data.load_value_at_cost(rules)
    grain = data.load_grain(rules).merge(voc, on=A.GRAIN, how="left")
    grain["value_at_cost"] = grain["value_at_cost"].fillna(0.0)
    dead = grain[(grain["cat_flow"].astype(str) == "Slow Moving") & (grain["stok_gudang"] > 0)]
    st.metric("Slow-moving capital on hand", data.compact(float(dead["value_at_cost"].sum())),
              f"{len(dead)} SKUs")  # fmt: skip
    show = ["warehouse_name", "sku", "product_name", "category_name", "stok_gudang",
            "value_at_cost", "recur_tor"]  # fmt: skip
    present = [c for c in show if c in dead.columns]
    st.dataframe(dead[present].sort_values("value_at_cost", ascending=False).head(50),
                 hide_index=True, use_container_width=True)  # fmt: skip

    # ── Download the rendered report ──────────────────────────────────────────
    report_md = data.resolve_out_dir(rules) / "aging_report.md"
    if report_md.is_file():
        st.download_button("⬇️ Download aging report (Markdown)",
                           report_md.read_text(encoding="utf-8"),
                           file_name="aging_report.md", mime="text/markdown")  # fmt: skip


if __name__ == "__main__":
    main()
