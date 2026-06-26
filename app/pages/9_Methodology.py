"""Methodology — every formula and tunable, in plain language.

The honest-engineering page: what each metric means, how it is computed, where the
synthetic data limits interpretation, and the clean-room origin of the project.
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
    import pandas as pd
    import streamlit as st

    from stocklens import analytics as A

    data.setup_page(
        "Methodology",
        subtitle="The formulas, the tunables, and the honest caveats.",
        icon="📖",
    )

    rules = data.get_rules()
    data.caption_synthetic()

    st.markdown(
        """
        StockLens is a clean-room reconstruction of a production inventory-intelligence
        pipeline. The pipeline (`seed → consolidate → aging`) is the locked core; this viewer adds
        a **read-only analytics layer** (`stocklens.analytics`) on top of its artifacts — it never
        changes the pipeline math or its output schema.

        ### Pipeline metrics
        - **Weighted demand score** — `weight_qty·Σqty + weight_orders·#orders`, benchmarked per
          warehouse against a `mean + std` limit (damped for wide variance) to tier each grain
          **Super Fast / Fast / Slow Moving**.
        - **IQR outliers** — per *(warehouse, window, grain)*, fences at `q3 + 1.5·IQR` /
          `q1 − 1.5·IQR` strip freak bulk orders; both include- and exclude-outlier totals are kept.
        - **Turnover (TOR)** — `(inv_start + incoming − final) / ((inv_start + final)/2)` per rolling
          window, with caps and a `recur_tor` fallback ladder.
        - **Margin** — `gmv = Σ(price·qty)`, `total_margin = Σ((price − cost)·qty)`,
          `gm_rate = margin / gmv`.

        ### Derived analytics (this viewer)
        - **Value at cost** — `Σ(remaining_qty · purchase_price)` from the inventory lots, the
          currency denominator the locked consolidated schema drops.
        - **Days of cover** — `on-hand ÷ avg daily demand`; a grain needs reorder when its net
          position (on-hand + incoming − booking) covers less than its lead time.
        - **ABC** — Pareto cut on cumulative value share; **XYZ** — coefficient of variation of
          weekly demand; the **3×3 matrix** assigns a stocking policy per cell.
        - **GMROI** — `gross margin ÷ inventory at cost`; below 1.0 the stock earns less than it costs
          to hold.
        - **Forecast & reorder point** — naive / moving-average / exponential-smoothing projection,
          backtested (WAPE) against a seasonal-naive baseline; reorder point =
          `demand·lead_time + z·σ·√lead_time`.
        """
    )

    # ── Pipeline tunables ─────────────────────────────────────────────────────
    st.subheader("Pipeline tunables (`config/rules.toml`)")
    rows = []
    for section in ("aging", "turnover", "classification", "stock", "windows", "demand"):
        for key, value in getattr(rules, section).items():
            rows.append({"Section": section, "Key": key, "Value": str(value)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    # ── Analytics policy ──────────────────────────────────────────────────────
    st.subheader("Analytics policy (`AnalyticsConfig` — app-layer, not locked)")
    cfg = A.AnalyticsConfig()
    cfg_rows = [{"Field": f.name, "Default": getattr(cfg, f.name)} for f in dataclasses.fields(cfg)]
    st.dataframe(pd.DataFrame(cfg_rows), hide_index=True, use_container_width=True)

    # ── Honest caveats ────────────────────────────────────────────────────────
    st.subheader("Honest caveats (synthetic-data limits)")
    st.markdown(
        """
        - **Short history (~30 days).** XYZ and forecasting are short-window estimates — directional,
          not a long-run seasonality read.
        - **Point-in-time inventory.** A single inventory snapshot means GMROI's "average inventory"
          is point-in-time, not a period average.
        - **Lead-time variability is weak.** The synthetic PO↔inventory links are randomised, so
          supplier lead-time σ is unreliable; safety stock here uses *demand* variability only.
        - **Loss-making lines are real.** `gm_rate` can be negative — this is a property of the
          seeded prices, surfaced rather than hidden.
        """
    )

    st.subheader("Origin")
    st.markdown(
        "StockLens is a sanitized, public reconstruction — synthetic data, fixed RNG seed, no live "
        "systems. See `ORIGIN.md` for the from-production-to-portfolio honesty note."
    )


if __name__ == "__main__":
    main()
