# ORIGIN — from production to portfolio

StockLens is a **clean-room reconstruction** of a production data pipeline I designed, built, and
operated. The original ran on a scheduled orchestration stack (Airflow on AWS) against a cloud data
warehouse, published an analytical extract to a BI server, and read/wrote a couple of supporting
spreadsheets. It executed daily against a live multi-tenant e-commerce dataset to drive purchasing
and replenishment decisions across a multi-warehouse network.

This public repository contains **none** of that. It is a fresh implementation written against
**synthetic data only**, built specifically to demonstrate the *algorithmic design* of the original
without exposing anything proprietary.

## What is preserved

The portfolio value is the engineering and the statistics, and those are reproduced faithfully:

- **Demand classification** — the weighted sales/order-frequency score and the per-segment
  mean + standard-deviation classification limit (including the wide-variance damping branch).
- **Outlier cleaning** — per-window IQR bounds with the single-sample special case, emitting both
  include- and exclude-outlier totals.
- **Stock-position assembly** — the layered multi-source stock CTE (unreleased / released / booking /
  incoming), lead-time and cycle-time computation, and the product-status merge.
- **Turnover & margin** — rolling L7–L30-day turnover ratios with caps and a recurrence fallback
  ladder, plus GMV, gross margin, and gross-margin rate.
- **Aging-stock alerting** — the category-based aged-stock split, the warehouse/status filters, the
  short-window sell-out join, and the stakeholder report.

Every threshold, weight, and window from the original is externalized into `config/rules.toml`, so
the heuristics read as *tunable policy* rather than buried constants.

## What was substituted (open replacements)

| Original layer | Open substitute in this repo |
|---|---|
| Cloud data warehouse (SQL over remote tables) | **DuckDB** in-process, querying seeded local tables |
| Object-store extract write (S3) + BI-server publish | **local Parquet** under `out/` (+ an optional no-op publish stub) |
| BI-server view feeding the aging cohort | a committed synthetic **CSV** (`data/aging_cohort.csv`) |
| Spreadsheet read/write for product status | a committed synthetic **CSV** (`data/product_status.csv`) |
| Scheduled email blast to stakeholders | a rendered **HTML / Markdown report** written to `out/` — nothing is sent |
| Internal credential / service-account modules | a small open `shims/` package hitting local files only |

There is **no network access** anywhere in the codebase — no warehouse connection string, no
object-store client, no SMTP login, no spreadsheet or BI-server API calls. The seeder uses a fixed
random seed, so the whole pipeline is deterministic and reproducible from a clean checkout.

## What is deliberately absent

To be explicit: this repository contains **no proprietary data, credentials, schema or table names,
internal identifiers, business-entity names, document IDs, infrastructure paths, or colleague
information** from the original system. Table names are generic (`orders`, `inventories`,
`turnover_history`, …), identifiers are obviously synthetic (`SKU-0001`, `North DC`, `Supplier
Alpha`, `Buyer-A`), recipients are placeholder addresses (`purchasing-lead@example.com`), and all
magic numbers have been replaced with synthetic, configurable values.

The goal is to show **how I approach inventory data engineering** — the modeling, the statistics, the
orchestration shape, and the testing discipline — not to reproduce any employer's system.
