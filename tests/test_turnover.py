"""Worked-example tests for :mod:`stocklens.margin_turnover`.

Covers ``STOCKLENS_PLAN`` §4 (turnover) + §7 (margin) and ``BUILD-CONTRACT`` §6.4:

* **TOR formula** — invStart=1,000,000 / incoming=200,000 / final=600,000 →
  denom 800,000, raw 0.75 (< cap threshold → uncapped).
* **Caps** — a rounded TOR ≥ 30 collapses to the default cap (14) for L7/L14/L21
  and to the L30 cap (30) for the L30D window.
* **recur ladder** — first strictly-positive window in L7→L14→L21→L30, else the
  configured fallback (14); an all-zero grain → 14.
* **Divide-by-zero guard** — a never-stocked grain (denom 0) → TOR 0.
* **End-to-end** — ``load_turnover`` over a tiny seeded ``turnover_history`` yields
  ``l7d_tor == 0.75`` and ``recur_tor == 0.75`` for the worked grain, and 14 for
  the all-zero grain.
* **gm_rate** — ``load_margin`` over the minimal margin seed: gmv 86,000 /
  cogs 59,000 → gm_rate ≈ 0.3140.
"""

from __future__ import annotations

import pandas as pd
import pytest

from stocklens.margin_turnover import (
    _cap,
    _recur_tor,
    _tor,
    load_margin,
    load_turnover,
)


# ---------------------------------------------------------------------------
# Pure TOR formula + cap + recur ladder.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_tor_formula_worked_example():
    """(1,000,000 + 200,000 - 600,000) / ((1,000,000 + 600,000)/2) = 0.75."""
    tor = _tor(
        pd.Series([1_000_000.0]),
        pd.Series([200_000.0]),
        pd.Series([600_000.0]),
    )
    assert tor.tolist() == [0.75]


@pytest.mark.unit
def test_tor_divide_by_zero_guard():
    """A never-stocked grain (inv + final == 0) → denom 0 → TOR 0 (not inf/NaN)."""
    tor = _tor(pd.Series([0.0]), pd.Series([0.0]), pd.Series([0.0]))
    assert tor.tolist() == [0.0]


@pytest.mark.unit
def test_tor_rounds_to_two_dp():
    """The ratio is rounded to 2 dp (matches the original ``.round(2)``)."""
    # (100 + 0 - 50) / ((100 + 50)/2) = 50/75 = 0.6666... → 0.67.
    tor = _tor(pd.Series([100.0]), pd.Series([0.0]), pd.Series([50.0]))
    assert tor.tolist() == [0.67]


@pytest.mark.unit
def test_cap_default_and_l30(rules):
    """TOR ≥ threshold → default cap (14); the L30 window uses its own cap (30)."""
    threshold = int(rules.turnover["tor_cap_threshold"])
    cap_default = int(rules.turnover["tor_cap_value_default"])
    cap_l30 = int(rules.turnover["tor_cap_value_l30"])

    # L7/L14/L21 share the default cap: 30 and 40 collapse to 14; 5 stays.
    capped = _cap(pd.Series([30.0, 40.0, 5.0]), threshold, cap_default)
    assert list(capped) == [14.0, 14.0, 5.0]

    # L30 window: 30 collapses to its own cap (30); a sub-threshold value is kept.
    capped_l30 = _cap(pd.Series([30.0, 29.0]), threshold, cap_l30)
    assert list(capped_l30) == [30.0, 29.0]


@pytest.mark.unit
def test_recur_ladder_first_positive():
    """recur_tor = first strictly-positive window in the L7→L14→L21→L30 ladder."""
    z = pd.Series([0.0])
    # t7 positive → recur = t7.
    assert _recur_tor(pd.Series([0.5]), pd.Series([0.75]), z, z, 14.0).tolist() == [0.5]
    # t7 == 0, t14 positive → recur = t14.
    assert _recur_tor(z, pd.Series([0.75]), z, z, 14.0).tolist() == [0.75]
    # t7 == t14 == 0, t21 positive → recur = t21.
    assert _recur_tor(z, z, pd.Series([0.4]), z, 14.0).tolist() == [0.4]
    # only t30 positive → recur = t30.
    assert _recur_tor(z, z, z, pd.Series([0.2]), 14.0).tolist() == [0.2]


@pytest.mark.unit
def test_recur_ladder_all_zero_fallback(rules):
    """An all-zero ladder falls through to the configured ``recur_fallback`` (14)."""
    fallback = float(rules.turnover["recur_fallback"])
    z = pd.Series([0.0])
    assert _recur_tor(z, z, z, z, fallback).tolist() == [14.0]


# ---------------------------------------------------------------------------
# End-to-end load_turnover over a tiny seeded turnover_history.
# ---------------------------------------------------------------------------
@pytest.mark.duckdb
def test_load_turnover_end_to_end(turnover_db, rules, now):
    """Grain 500 → l7d_tor 0.75 / recur_tor 0.75; grain 600 → recur_tor 14."""
    result = load_turnover(turnover_db, rules, now=now).set_index("product_id")

    assert result.loc[500, "l7d_tor"] == pytest.approx(0.75)
    assert result.loc[500, "recur_tor"] == pytest.approx(0.75)

    # The all-zero grain falls through to the fallback.
    assert result.loc[600, "recur_tor"] == pytest.approx(
        float(rules.turnover["recur_fallback"])
    )

    # Contract-required output columns are present.
    assert {"product_id", "warehouse_id", "l30d_tor", "recur_tor"}.issubset(
        load_turnover(turnover_db, rules, now=now).columns
    )


# ---------------------------------------------------------------------------
# Margin gm_rate worked example (folded here per BUILD-CONTRACT §6.4 note).
# ---------------------------------------------------------------------------
@pytest.mark.duckdb
def test_load_margin_gm_rate(margin_db, rules, now):
    """gmv 86,000 / total_margin 27,000 → gm_rate ≈ 0.3140 for the seeded grain."""
    result = load_margin(margin_db, rules, now=now)

    assert len(result) == 1
    row = result.iloc[0]
    assert row["product_id"] == 500
    assert row["gmv"] == pytest.approx(86_000.0)
    assert row["total_margin"] == pytest.approx(27_000.0)
    assert row["gm_rate"] == pytest.approx(0.3140, abs=1e-4)


@pytest.mark.duckdb
def test_load_margin_columns(margin_db, rules, now):
    """``load_margin`` emits exactly the contract's six columns."""
    result = load_margin(margin_db, rules, now=now)
    assert list(result.columns) == [
        "product_id",
        "unit",
        "warehouse_id",
        "gmv",
        "total_margin",
        "gm_rate",
    ]
