"""Data Quality — the consolidated frame's self-defending contract.

Runs the StockLens data-quality contract over the consolidated artifact and renders the
full pass/fail checklist. The same checks back the ``stocklens validate`` CLI gate that
fails CI, so a broken pipeline cannot ship silently.
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
    import pandas as pd
    import streamlit as st

    from stocklens import analytics as A

    data.setup_page(
        "Data Quality",
        subtitle="A data contract that fails the build when the pipeline drifts.",
        icon="✅",
    )

    rules = data.get_rules()
    df = data.require_consolidated(rules)
    data.caption_synthetic()

    ok, results = A.validate_consolidated(df)
    passed = sum(1 for r in results if r.passed)

    c1, c2, c3 = st.columns(3)
    c1.metric("Checks passed", f"{passed}/{len(results)}")
    c2.metric("Hard-gate status", "PASS ✅" if ok else "FAIL ❌")
    c3.metric("Rows validated", f"{len(df):,}")

    if ok:
        st.success("All hard data-quality checks pass — the artifact honours its contract.")
    else:
        st.error("One or more hard checks failed — `stocklens validate` would fail CI.")

    st.subheader("Contract checklist")
    table = pd.DataFrame([
        {
            "Check": r.name,
            "Result": "✅ pass" if r.passed else "❌ fail",
            "Severity": "hard" if r.hard else "advisory",
            "Detail": r.detail,
        }
        for r in results
    ])  # fmt: skip
    st.dataframe(table, hide_index=True, use_container_width=True)

    st.subheader("What this guarantees")
    st.markdown(
        """
        - **Schema lock** — all 44 contract columns are present (BUILD-CONTRACT §3.4).
        - **Natural key uniqueness** — exactly one row per *(grain × window × outlier-treatment)*.
        - **Referential sanity** — grain keys are never null; categoricals stay in their vocabulary.
        - **Value bounds** — stock is non-negative, the demand rate is floored, and the
          gross-margin rate never exceeds 100% (it *may* be negative — loss-making lines are real).

        Run it yourself: `python cli.py validate` (exits non-zero on any hard failure).
        """
    )


if __name__ == "__main__":
    main()
