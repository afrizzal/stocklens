# StockLens

[![CI](https://github.com/afrizzal/stocklens/actions/workflows/ci.yml/badge.svg)](https://github.com/afrizzal/stocklens/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Inventory demand-classification & aging-stock alerting pipeline, rebuilt standalone on synthetic data.

StockLens is a clean-room, fully runnable reconstruction of a production inventory-intelligence
pipeline. It seeds a synthetic relational dataset into DuckDB, runs three statistical transform
modules over it, writes a consolidated per-grain Parquet table, and renders an aging-stock alert as a
local HTML/Markdown report. No external services, no real data — the **algorithms** are the portfolio.

See [`ORIGIN.md`](ORIGIN.md) for the "from production to portfolio" honesty note.

---

## What it demonstrates

- **Statistical demand tiering** — a weighted sales/order-frequency score
  (`0.8 * qty + 0.2 * orderCount`) benchmarked against a per-warehouse mean + standard-deviation
  limit (with a `std > 1000 → mean + 0.25·std` damping branch) to bucket every
  warehouse/product grain into *Super Fast / Fast / Slow Moving*.
- **IQR outlier handling** — per `(grain, window)` outlier removal (`q3 + 1.5·IQR` /
  `q1 − 1.5·IQR`, with a single-sample fallback of `qty · 1.5`) so demand isn't inflated by one
  freak bulk order. Both *include-outliers* and *exclude-outliers* totals are emitted.
- **Multi-source stock-position assembly** — a layered stock CTE
  (unreleased / released / booking / incoming), lead-time and cycle-time, and a product-status merge,
  combined into one row per grain.
- **Inventory-turnover & margin math** — rolling L7/L14/L21/L30-day turnover ratios with caps and a
  `recur_tor` fallback ladder, plus GMV, gross margin, and gross-margin rate (`gm_rate`).
- **Automated aging alerts** — a Daily-Needs (≥15-day) vs Lifestyle (≥31-day) aged-stock split joined
  to a 7-day sell-out window, rendered to a report instead of emailed.

All thresholds, weights, windows, and warehouse rules are externalized to
[`config/rules.toml`](config/rules.toml) so they read as *tunable*, not hard-coded.

---

## Architecture

```
                ┌──────────────────┐
 seed/generate  │  stocklens.duckdb│   (synthetic relational tables, RNG-seeded)
   ───────────► │   + data/*.csv   │
                └────────┬─────────┘
                         │  shims/data_io.get_data()  (DuckDB, no network)
                         ▼
        ┌────────────────────────────────────────────┐
        │            3 transform modules              │
        │  demand_classify  ·  stock_position  ·      │
        │            margin_turnover                  │
        └────────────────────┬───────────────────────┘
                             │  consolidate.py (orchestrator)
                             ▼
            out/consolidate_purchasing_agg.parquet  (+ .csv)
                             │
              ┌──────────────┴───────────────┐
              ▼                              ▼
   app/viewer.py (Streamlit)     aging_alert.py → out/aging_report.{html,md}
```

This mirrors the shape of the original scheduled DAG: extract → transform → load to an analytical
store → surface to stakeholders. The storage and delivery layers are open substitutes
(DuckDB for the warehouse, Parquet for the extract, a local HTML/MD report for the email blast).

---

## Quickstart

Requires Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                                  # install runtime + dev deps into .venv
uv run python cli.py all                 # seed → consolidate → aging (end-to-end)

# optional: the Streamlit viewer (pulls in the `viz` extra)
uv sync --extra viz
uv run streamlit run app/viewer.py
```

Run individual stages:

```bash
uv run python cli.py seed          # build stocklens.duckdb from seed/generate.py
uv run python cli.py consolidate   # write out/consolidate_purchasing_agg.parquet
uv run python cli.py aging         # write out/aging_report.html + .md
```

Useful flags (every subcommand): `--now 2026-06-25` pins the "as of" date for deterministic
output, and `--config path/to/rules.toml` overrides the config location.

Outputs land in `./out/` (git-ignored). The seeded `stocklens.duckdb` is also git-ignored —
it is regenerated from `seed/generate.py`, so the repo stays free of binary artifacts.

---

## Configuration

All tunables live in [`config/rules.toml`](config/rules.toml), grouped by concern:

| Section | What it controls |
|---|---|
| `[aging]` | Daily-Needs / Lifestyle aged thresholds, excluded-warehouse filter, category split |
| `[turnover]` | TOR cap threshold and cap values, `recur_tor` fallback |
| `[classification]` | weighted-score weights, the std-damp threshold/factor, the premium tag |
| `[stock]` | excluded & special-handling warehouse ids, lead-time fallbacks, mandiri filter |
| `[windows]` | rolling-window days and lookback periods |
| `[demand]` | demand-rate floor, IQR/single-row outlier factors |
| `[report]` | report recipients/sender (metadata only — **nothing is sent**) and output dir |
| `[paths]` | DuckDB and seed-CSV paths |

A near-empty [`.env.example`](.env.example) documents the only environment overrides
(`STOCKLENS_DB`, `STOCKLENS_OUT`). There are no secrets.

---

## Algorithms explained

**Demand classification.** For each grain, `weighted = weight_qty·qty + weight_orders·orderCount`
(default `0.8 / 0.2`). Within each warehouse, compute the mean and sample standard deviation
(ddof = 1) of `weighted`. The classification limit is `mean + std`, damped to `mean + 0.25·std`
when `std > std_damp_threshold` (default 1000) so a few outsized grains don't pull the bar out of
reach. Then: `weighted ≥ limit → Super Fast`, `weighted ≥ mean → Fast`, else `Slow`.

**IQR outlier removal.** Per `(warehouse, window, grain)` sample set: with one sample, the upper
bound is `qty · 1.5` and the lower bound is `0` (a lone sample is never an outlier); with more, the
bounds are `round(q3 + 1.5·IQR)` and `round(q1 − 1.5·IQR)`, the lower clamped to ≥0. Quantiles use
pandas' default linear interpolation. Both include- and exclude-outlier totals are reported.

**Rolling windows.** L7/L14/L21/L30-day buckets are *cumulative* — L14D includes L7D — matching the
source pipeline. `qty_per_day = floor(total / days_divider)`, floored to a minimum of `1` so even
slow movers get a token reorder signal.

**Turnover (TOR).** Per window, `tor = (inv_start + incoming − final) / ((inv_start + final) / 2)`,
rounded to 2 dp, with a divide-by-zero guard returning 0. A `tor ≥ 30` is capped to `14` for
L7/L14/L21 and `30` for L30D. `recur_tor` walks the ladder L7 → L14 → L21 → L30 and takes the first
positive window, falling back to `14` when all are zero.

**Margin.** `gmv = Σ(selling_price · qty_out)`, `total_margin = Σ((selling_price − purchase_price) ·
qty_out)`, and `gm_rate = total_margin / gmv` (guarded to 0 when `gmv == 0`). For example,
`gmv = 86,000`, COGS `= 59,000` ⟹ `total_margin = 27,000`, `gm_rate ≈ 0.3140`.

**Aging alert.** Cohort rows are categorized Daily-Needs (category `Staples` or sub-category like
`Flour`) vs Lifestyle, kept when `diff_days_inhouse ≥ 15` (Daily-Needs) or `≥ 31` (Lifestyle),
filtered to exclude consignment warehouses and to keep `status_wl LIKE 'WL%'`, then left-joined to a
7-day sell-out window (reward lines excluded). The result renders to `out/aging_report.{html,md}`.

---

## Project layout

```
config/rules.toml                 all tunable thresholds
data/                             committed synthetic seed inputs (CSV)
seed/generate.py                  builds stocklens.duckdb (RNG-seeded)
shims/                            open replacements for the internal infra (DuckDB / local report)
src/stocklens/                    the importable package (transform modules + SQL)
app/viewer.py                     Streamlit viewer (optional `viz` extra)
cli.py                            Typer CLI: seed | consolidate | aging | all
tests/                           worked-example unit tests
```

---

## License

MIT — see [`LICENSE`](LICENSE).
