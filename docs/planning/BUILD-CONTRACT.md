# StockLens (Track B) — LOCKED BUILD CONTRACT

> **Status:** LOCKED. This is the single source of truth. Every downstream build agent
> (seed, modules, SQL, shims, CLI, tests, app) MUST follow the names, schemas, signatures,
> config keys, and seed magnitudes defined here **exactly**. If something here is wrong,
> change THIS document first (and bump the version), then rebuild — do not diverge silently.
>
> **Repo:** `stocklens` — public portfolio showcase.
> **Origin:** sanitized clean-room reconstruction of two real production purchasing/aging
> pipelines. The originals (which ran on a Redshift + Airflow + Tableau + Google Sheets stack)
> are quarantined privately and are **never** published.
>
> **Contract version:** 1.1.0 · **Target Python:** 3.10+ (dev box 3.10.6) · **Manager:** `uv`
>
> **v1.1.0 (additive):** adds a read-only analytics layer and a multi-page viewer **on top of** the
> locked artifacts. The pipeline math, the `Rules`/config keys, and the 44-column consolidated schema
> (§3.4) are **unchanged**. See §9.

---

## 0. NON-NEGOTIABLE GUARDRAILS (read before writing any file)

This is a **PUBLIC** repo. A single leak fails the entire task. The build agents inherit these rules.

### 0.1 Design hygiene

All committed files (`src/`, `seed/`, `shims/`, `data/`, `config/`, `tests/`, `app/`, `cli.py`,
`README.md`, `ORIGIN.md`, and any committed output) use **only the synthetic names, ids, and
values defined in this contract**. No real schema names, internal module names, business-rule ids,
brand/codename tokens, document ids, infrastructure paths, service accounts, credentials, person
names, or email addresses may appear anywhere. The synthetic vocabulary below is the complete and
only allowed vocabulary.

### 0.2 Mandatory synthetic vocabulary

| Concept | Synthetic value to use |
|---|---|
| Premium product tag | tag name `"Premium"`, `product_tag_id = 1` |
| PIC (person-in-charge) | `"Buyer-A"`, `"Buyer-B"` (and `"Unassigned"` fallback) |
| Report recipients | `["purchasing-lead@example.com", "ops@example.com"]` |
| Report sender | `"reports@example.com"` |
| Warehouses | `"North DC"`, `"South DC"`, `"Central DC"` (+ special/excluded ones, §1.4) |
| SKUs | `"SKU-0001"`, `"SKU-0002"`, … |
| Suppliers | `"Supplier Alpha"`, `"Supplier Beta"`, `"Supplier Gamma"` |
| Brands | `"BrandOne"`, `"BrandTwo"`, `"BrandThree"` |
| Categories | `"Staples"`, `"Beverages"`, `"Personal Care"`, `"Home"`, `"Snacks"` |

### 0.3 No live side-effects — EVER

- **No SMTP / no email send.** The aging alert renders `out/aging_report.html` + `out/aging_report.md`.
- **No Google Sheets read/write.** Replaced by committed local CSVs in `data/` and `out/`.
- **No object-store (S3) writes.** `write_parquet` writes to local `out/`.
- **No BI/Tableau publish.** Optional `publish_stub()` only logs `"would publish ..."`.
- **No network at all** in any module, shim, seed, or test.

### 0.4 SQL engine rule

- Use **DuckDB directly** (`shims/data_io.get_data`). Register pandas frames or query the seeded
  `stocklens.duckdb`.
- **Do NOT use `pandasql`** (fragile dependency — explicitly excluded from deps).
- All table names are **unqualified synthetic names** (no schema prefix). The aging job's
  `sqldf(...)` group-bys from the originals are re-expressed either as DuckDB SQL over registered
  frames or as pandas `groupby` — implementer's choice, but no `pandasql`.

### 0.5 Numeric fidelity

- Keep numeric math in **pandas / numpy** (this is the standalone showcase, not the ERP TS port).
- Match the pandas defaults the originals relied on: `quantile` linear interpolation (type 7),
  `std()` = sample std (`ddof=1`), `round()` = banker's rounding as in numpy/pandas.
- Target py3.10: add `from __future__ import annotations` to every module; avoid 3.11+-only syntax
  (no `tomllib`-only assumptions without fallback — see §6.1; no `Self`, no `X | Y` in runtime
  `isinstance`, no PEP 695 generics).

---

## 1. SYNTHETIC RELATIONAL SCHEMA (DuckDB)

The seeder `seed/generate.py` builds `stocklens.duckdb` containing the tables below. These replace
the originals' internal source tables. **All names are generic and synthetic.** Grain key used
throughout the pipeline: `(warehouse_id, product_id, product_attribute_id)`.

### 1.1 Scale targets (so every stage yields non-empty, sensible output)

- **3 warehouses** that flow through the pipeline (`North DC`, `South DC`, `Central DC`) **plus**
  1 excluded warehouse (`Consignment DC`, id below) **plus** 1 "special-handling" warehouse
  (`RTP DC`) → **5 warehouses total**.
- **~36 products** (ids `101..136`), each with **1 product_attribute** (so ~36 grains/warehouse;
  not every product stocked in every warehouse — see seed rules).
- **Order history over the last ~60 days** (the pipeline windows to last 30; 60d gives headroom and
  realistic L30D buckets), **≥31 days of turnover history**, **last 7 days** of stock requests and
  aging sell-out.
- Seed RNG **fixed** (`numpy.random.default_rng(42)`) so output is deterministic and tests are stable.

### 1.2 Warehouse id constants (LOCKED)

| Role | name | id |
|---|---|---|
| Normal | North DC | `1` |
| Normal | South DC | `2` |
| Normal | Central DC | `3` |
| Excluded (the "demand-exclusion" rule) | Consignment DC | `9` |
| Special-handling (forces status, lead-time, PIC) | RTP DC | `7` |

> `EXCLUDED_WAREHOUSE_ID = 9` and `SPECIAL_WAREHOUSE_IDS = [7]` are config keys (§2). `Consignment DC`
> also exercises the aging job's `NOT LIKE '%Consignment%'` filter. (The excluded warehouse models a
> consignment warehouse that is excluded from demand; the special-handling warehouse models warehouses
> forced active with overridden status/lead-time/PIC — see §2.)

### 1.3 Table definitions

> Types are DuckDB types. `approx rows` assumes the §1.1 scale. "Role" = what this synthetic table
> represents in the pipeline (the originals read internal source tables with the same shape).

#### Core product / location dimension

| Synthetic table | Columns (DuckDB types) | Approx rows | Role |
|---|---|---|---|
| **products** | `id BIGINT, sku VARCHAR, name VARCHAR, category_id BIGINT, brand_id BIGINT, type INTEGER, status INTEGER` | 36 | product master |
| **product_attributes** | `id BIGINT, product_id BIGINT, unit VARCHAR, position INTEGER, status INTEGER` | 36 | per-product unit/attribute rows |
| **warehouses** | `id BIGINT, name VARCHAR, type VARCHAR` | 5 | location dimension |
| **categories** | `id BIGINT, name VARCHAR` | 5 | category lookup |
| **brands** | `id BIGINT, name VARCHAR` | 3 | brand lookup |
| **product_tags** | `id BIGINT, name VARCHAR` | 2 | tag lookup |
| **product_tag_relations** | `product_id BIGINT, product_tag_id BIGINT` | ~8 | product↔tag map |
| **suppliers** | `id BIGINT, name VARCHAR` | 3 | supplier lookup |

> `products.type ∈ {1,2}` — `type=2` exercises the RTP branch. ~8 products tagged `product_tag_id=1`
> ("Premium") via `product_tag_relations`.

#### Sales / order fact

| Synthetic table | Columns (DuckDB types) | Approx rows | Role |
|---|---|---|---|
| **orders** | `id BIGINT, created_at TIMESTAMP, invoice VARCHAR, warehouse_id BIGINT, superagent_id BIGINT, status INTEGER` | ~1500 | internal orders table |
| **order_items** | `id BIGINT, order_id BIGINT, product_id BIGINT, product_attribute_id BIGINT, product_name VARCHAR, unit VARCHAR, quantity INTEGER, deleted_at TIMESTAMP, deleted_by BIGINT` | ~2500 | order line items |
| **order_logs** | `order_id BIGINT, order_item_id BIGINT, quantity INTEGER, status INTEGER, type VARCHAR, inventory_publish_id BIGINT` | ~2500 | order movement log (signed) |

> `order_logs.quantity` is **signed** (OUT/booking rows negative, matching `ol.quantity < 0`).
> `order_logs.type = 'order'` for sales lines. `orders.status` spans `-1..3`; the pipeline keeps
> `status > 1`. `superagent_id = 0` ⇒ "Include" (first-party/own-channel) per the original filter;
> seed a mix so the `filter_mandiri` rule keeps a healthy majority.

#### Inventory / stock position

| Synthetic table | Columns (DuckDB types) | Approx rows | Role |
|---|---|---|---|
| **inventories** | `id BIGINT, product_id BIGINT, product_attribute_id BIGINT, warehouse_id BIGINT, remaining_quantity INTEGER, created_at TIMESTAMP, new_purchase_order_id BIGINT, production_order_id BIGINT, purchase_order_id BIGINT, inventory_vendor_id BIGINT` | ~300 | per-lot inventory positions |
| **inventory_published** | `id BIGINT, inventory_id BIGINT, product_attribute_id BIGINT, warehouse_id BIGINT, remaining_quantity INTEGER, publish_type VARCHAR, group_type VARCHAR, quantity INTEGER, created_at TIMESTAMP` | ~400 | published/available stock rows |
| **margin_costs** | `inventory_id BIGINT, purchase_price_inc_ppn DOUBLE` | ~300 | internal margin/cost layer (per-lot cost) |

> `inventory_published.publish_type ∈ {regular, flashsale, reward, rtp}`, `group_type = 'grosir'`.
> `inventory_vendor_id = 0` for own-stock (exercises cycle-time CTE). `margin_costs` carries the
> per-lot purchase price the stocks/margin queries multiply by `remaining_quantity` / `quantity_out`.

#### Purchasing / receiving

| Synthetic table | Columns (DuckDB types) | Approx rows | Role |
|---|---|---|---|
| **purchase_orders** | `id BIGINT, warehouse_id BIGINT, status INTEGER, created_at TIMESTAMP, po_code VARCHAR, po_date DATE, supplier_id BIGINT, company_type VARCHAR, purchase_order_payment_id BIGINT` | ~120 | purchase order header |
| **purchase_order_items** | `id BIGINT, purchase_order_id BIGINT, product_id BIGINT, product_attribute_id BIGINT, quantity INTEGER, remaining_quantity INTEGER, deleted_at TIMESTAMP, deleted_by BIGINT` | ~250 | purchase order lines |
| **purchase_order_status_logs** | `purchase_order_id BIGINT, current_status INTEGER` | ~120 | PO status history |
| **production_orders** | `id BIGINT, pro_code VARCHAR, pro_date DATE, supplier_id BIGINT` | ~10 | production order header |
| **production_order_items** | `id BIGINT, production_order_id BIGINT, product_id BIGINT, quantity INTEGER` | ~15 | production order lines |

> `purchase_orders.status` and `purchase_order_status_logs.current_status` span the values the
> lead-time CTE filters on (`po_status in (1,2)`, `status > 0 and < 2` for incoming). `po_date` seeded
> within the last 6 months. `production_orders/_items` are minimal — they satisfy the margin
> `UNION ALL` branch only.

#### Stock-request / pricing

| Synthetic table | Columns (DuckDB types) | Approx rows | Role |
|---|---|---|---|
| **stock_requests** | `id BIGINT, product_id BIGINT, customer_id BIGINT, product_attribute_id BIGINT, warehouse_id BIGINT, quantity INTEGER, created_at TIMESTAMP` | ~60 | stock-request source |
| **product_stocks** | `id BIGINT, product_attribute_id BIGINT, warehouse_id BIGINT` | ~150 | priced stock rows |
| **product_selling_prices** | `id BIGINT, product_stock_id BIGINT, selling_price DOUBLE, minimum_quantity INTEGER` | ~150 | tiered selling prices |

> `stock_requests.created_at` within last 7 days; `product_selling_prices.minimum_quantity = 1` for the
> rows the request query keeps.

#### RTP / segmentation source

| Synthetic table | Columns (DuckDB types) | Approx rows | Role |
|---|---|---|---|
| **product_rtp** | `product_id BIGINT, rtp_category VARCHAR, rtp_sub_category VARCHAR, status_wl VARCHAR, start_date DATE, end_date DATE` | ~12 | RTP/segmentation source |

> `status_wl` values like `'WL'`, `'WL-A'`, `''`/NULL — exercises `status_wl LIKE '%WL%'` / `LIKE 'WL%'`.
> `rtp_category ∈ {'Staples','Lifestyle'}`, `rtp_sub_category` includes some `'Flour'` (a generic
> staples sub-category split). Use `'Staples'` + sub `'Flour'` — no localized brand/category tokens.
> `end_date` NULL for some rows (open-ended).

#### Turnover snapshot + commercial sales fact

| Synthetic table | Columns (DuckDB types) | Approx rows | Role |
|---|---|---|---|
| **turnover_history** | `product_id BIGINT, warehouse_id BIGINT, period DATE, stock_value DOUBLE, sum_value_po DOUBLE, sum_value_retur DOUBLE, sum_value_retur_vendor DOUBLE, sum_value_transfer DOUBLE, sum_value_po_vendor DOUBLE` | ~36×3×32 ≈ 3500 | internal turnover snapshot view |
| **sales_history** | `product_id BIGINT, product_name VARCHAR, warehouse_name VARCHAR, order_date DATE, quantity INTEGER, gmv DOUBLE, order_item_type VARCHAR` | ~400 | internal sales fact |

> `turnover_history` must have rows for **`period = asof-1, asof-8, asof-15, asof-22, asof-31`** and a
> continuous daily range covering the L7/L14/L21/L30 incoming windows, so the `final/l7d/.../l30d`
> joins all resolve and the recur-ladder runs. `sales_history.order_date` within last 7 days for the
> aging sell-out join; `order_item_type ∈ {'regular','reward'}` (reward excluded by the aging query).

### 1.4 Committed seed CSVs (clearly synthetic, checked into git)

These two are **committed** (the rest live only in the generated `stocklens.duckdb`). They replace the
two external sources (a Google Sheet and a BI view) that the originals read.

#### `data/product_status.csv` — replaces the original "Product Status" Google Sheet

Columns (header row, exact order):

```
product_id,product_attribute_id,warehouse_id,status,adj_lead_time,PIC,label_priority,ragu_nonaktif
```

| Column | Type | Notes |
|---|---|---|
| `product_id` | int | FK → products.id |
| `product_attribute_id` | int | FK → product_attributes.id |
| `warehouse_id` | int | FK → warehouses.id (normal whs only: 1,2,3) |
| `status` | int | 0/1 approval status |
| `adj_lead_time` | int | manual lead-time override, days (e.g. 2..7) |
| `PIC` | string | `Buyer-A` / `Buyer-B` only |
| `label_priority` | string | `High` / `Medium` / `Low` / empty |
| `ragu_nonaktif` | int | 0/1 (a "doubtful — deactivate? (1: yes)" review flag) |

> ~20 rows covering a subset of grains in warehouses 1/2/3. Provides the columns the pipeline expects:
> `status`, `adj_lead_time` (manual lead-time override, days), `PIC`, `label_priority`, and
> `ragu_nonaktif` (the deactivation-review flag).

#### `data/aging_cohort.csv` — replaces the original BI aging view

Columns (header row, exact order):

```
product_id,product_unit,warehouse_name,diff_days_inhouse,stok_gudang_tanpa_booking,total_purchase_stok_tanpa_booking,status_wl
```

| Column | Type | Notes |
|---|---|---|
| `product_id` | int | FK → products.id (a `type=2`/RTP-ish cohort) |
| `product_unit` | string | display label, e.g. `"SKU-0007 (pcs)"` (a "Product (Unit)" label) |
| `warehouse_name` | string | one of North DC / South DC / Central DC (+ a couple `Consignment DC` rows to exercise the `NOT LIKE '%Consignment%'` filter) |
| `diff_days_inhouse` | int | age in days; seed a spread crossing 15 and 31 so both Daily-Needs and Lifestyle thresholds trigger |
| `stok_gudang_tanpa_booking` | float | on-hand-without-booking qty |
| `total_purchase_stok_tanpa_booking` | float | tied-up purchase value |
| `status_wl` | string | mostly `'WL'`/`'WL-A'` (the aging query keeps `LIKE 'WL%'`); a few non-WL to prove the filter |

> ~18 rows. Each `product_id` here MUST also exist in `product_rtp` (for the category join) and have
> matching `sales_history` rows for some, to make the sell-out merge non-empty.

### 1.5 Internal-consistency rules the seeder MUST enforce

1. Every `order_items.product_id/product_attribute_id` exists in `products`/`product_attributes`.
2. Every grain that appears in sales also appears in the stock universe (so the outer-merge in
   `consolidate` yields rows with both sales and stock for most grains, and zero-sales-but-stocked
   grains for some — mirroring the original `_wp cross join` + outer merge).
3. `order_logs` has a matching signed-negative OUT row for each sold `order_item` (so booking/margin
   queries resolve).
4. `margin_costs.inventory_id` covers every `inventories.id` referenced by published/booking joins.
5. `turnover_history` covers every `(product_id, warehouse_id)` that has turnover-relevant activity on
   the required `period` dates.
6. `product_status.csv` and `aging_cohort.csv` reference only ids the seeder actually creates.
7. Output of EACH pipeline stage is **non-empty** for at least warehouses 1, 2, 3.

---

## 2. CONFIG KEYS — `config/rules.toml`

Every value the originals hardcoded becomes a tunable here. Loaded once into a `Rules` dataclass
(see §3.0). **Exact key names and default values are LOCKED.**

```toml
# config/rules.toml — StockLens tunables (all values illustrative/synthetic)

[aging]
daily_needs_days = 15          # Daily-Needs aged threshold (orig: >=15)
lifestyle_days   = 31          # Lifestyle aged threshold (orig: >=31)
exclude_warehouse_name_like = "Consignment"   # orig: NOT LIKE '%Consignment%'
status_wl_prefix = "WL"        # orig: status_wl LIKE 'WL%'
daily_needs_category = "Staples"     # synthetic staples-category split
daily_needs_subcategory_like = "Flour"

[turnover]
tor_cap_threshold = 30         # if rounded tor >= 30 -> capped
tor_cap_value_default = 14     # cap value for L7/L14/L21 windows
tor_cap_value_l30 = 30         # cap value specifically for L30D window
recur_fallback = 14            # recur_tor when all windows are zero

[classification]
weight_qty = 0.8               # weighted = 0.8*qty + 0.2*orderCount
weight_orders = 0.2
std_damp_threshold = 1000      # std > 1000 -> limit = mean + 0.25*std
std_damp_factor = 0.25
premium_tag_id = 1             # synthetic premium-tag id
premium_tag_name = "Premium"

[stock]
excluded_warehouse_id = 9      # consignment warehouse excluded from demand
special_warehouse_ids = [7]    # special-handling warehouses (forced active)
lead_time_fallback = 3         # orig fillna(3) on adj. lead time
avg_lead_time_fallback = 1     # orig fillna(1) on avg_lead_time
include_mandiri_only = true    # filter_mandiri == 'Include'

[windows]
rolling_days = [7, 14, 21, 30] # L7D/L14D/L21D/L30D cumulative buckets
sales_lookback_days = 30       # orders pulled over last 30 days
stock_request_lookback_days = 7
sell_out_lookback_days = 7     # aging sell-out window
po_lookback_months = 6

[demand]
qty_per_day_min = 1            # demand rate floored to >=1 (orig qty/day==0 -> 1)
outlier_single_row_factor = 1.5  # single-sample upper = qty * 1.5
iqr_factor = 1.5              # q3 + 1.5*iqr / q1 - 1.5*iqr

[report]
recipients = ["purchasing-lead@example.com", "ops@example.com"]  # NO send; metadata only
sender = "reports@example.com"
team_greeting = "Dear Purchasing Team"
signature = "Regards, Analytics"
output_dir = "out"

[paths]
duckdb_path = "stocklens.duckdb"
product_status_csv = "data/product_status.csv"
aging_cohort_csv = "data/aging_cohort.csv"
```

> Build agents read config via `load_rules()` (§3.0). No module may re-hardcode any of these values.

---

## 3. MODULE INTERFACES (`src/stocklens/`)

All modules begin with `from __future__ import annotations`. Numeric math stays in pandas/numpy.
Functions are pure where practical: they take a DuckDB connection and/or DataFrames + a `Rules` object,
and return DataFrames. **No module performs network I/O, SMTP, Sheets, object-store, or BI calls.**

Grain key everywhere: `["warehouse_id", "product_id", "product_attribute_id"]`.

### 3.0 Shared config loader (lives in `src/stocklens/__init__.py`)

```python
@dataclass(frozen=True)
class Rules:
    aging: dict; turnover: dict; classification: dict; stock: dict
    windows: dict; demand: dict; report: dict; paths: dict

def load_rules(path: str = "config/rules.toml") -> Rules: ...
```

> TOML loading: prefer stdlib `tomllib` (py3.11+); on 3.10 fall back to `tomli`. To avoid adding a dep,
> the contract permits a tiny try/except: `try: import tomllib except ImportError: import tomli as tomllib`.
> If `tomli` is not desired, the loader MAY parse the small flat TOML with a minimal hand-rolled reader,
> but `tomllib`/`tomli` is the default. (Decision: add `tomli` to deps for py3.10 — see §6.1.)

### 3.1 `demand_classify.py` — weighted score + Super/Fast/Slow + IQR outliers + L7/14/21/30

Ports the consolidation pipeline's orders/classification stage: orders pull, mandiri/status/
warehouse filters, weighted score, per-(warehouse, tag) mean+std limit, 3-way classification, day
bucketing, per-(grain, window) IQR outlier cleaning, `qty/day`.

**Reads:** `orders`, `order_items`, `order_logs`, `warehouses`, `products`, `product_tag_relations`,
`product_rtp` (via `sql/orders.sql`).
**Writes:** returns DataFrames (no disk).

```python
def load_orders(con: duckdb.DuckDBPyConnection, rules: Rules, *, now: date) -> pd.DataFrame:
    """Run sql/orders.sql, apply Include/status>1/exclude-warehouse filters,
       add diff_days + L7D/L14D/L21D/L30D flags + `days` bucket.
       Columns: order_date, order_id, order_item_id, product_name, product_tag,
                product_id, product_attribute_id, unit, qty_sales, warehouse_id,
                warehouse_name, diff_days, days (L7D|L14D|L21D|L30D)."""

def classify_demand(df_orders: pd.DataFrame, rules: Rules) -> pd.DataFrame:
    """Weighted score = weight_qty*sum_qty + weight_orders*count_invoice, grouped by
       (warehouse_id, product_attribute_id, warehouse_id, product_tag).
       Per (warehouse_id, product_tag): mean+std; limit = mean + (std>thr ? damp*std : std).
       cat_flow ∈ {Super Fast Moving, Fast Moving, Slow Moving}.
       Returns columns: product_id, product_attribute_id, warehouse_id, weighted,
                        avg_score, std_score, limit, cat_flow."""

def remove_outliers(df_orders: pd.DataFrame, rules: Rules) -> pd.DataFrame:
    """Per (warehouse_id, period, product_attribute_id) compute IQR bounds.
       len==1 -> upper = qty*single_row_factor, lower = 0 (never an outlier).
       len>1  -> upper = round(q3 + iqr_factor*iqr), lower = round(q1 - iqr_factor*iqr).
       lower clamped to >=0. Emits both include-outliers and exclude-outliers totals.
       Returns df_orders_in_ex columns: warehouse_id, warehouse_name, product_id,
         product_attribute_id, product_name, unit, total_quantity, upper_bound,
         lower_bound, days, status_outliers (include outliers|exclude outliers),
         days_divider (7|14|21|30), qty_per_day (>=1)."""
```

> `quantile` MUST use pandas default (`Series.quantile(0.25/0.75)`, linear interp). `std` MUST be
> pandas `.std()` (ddof=1). Single-sample grains never flagged outliers. `qty_per_day = int(total/divider)`,
> bumped to `qty_per_day_min` when 0.

### 3.2 `stock_position.py` — multi-source stock CTE + lead/cycle time + product-status merge

Ports the consolidation pipeline's stock-position stage: the big `stocks` CTE
(`stok_belum_rilis`/`stok_rilis_*`/`stok_booking`/`stok_incoming`/`cycle_time`/`lead_time`/`divider`),
the `data/product_status.csv` merge (formerly the Google Sheet), the orders⨝stocks merge, the
special-handling-warehouse overrides, and the stock-request merge.

**Reads:** `warehouses`, `products`, `product_attributes`, `categories`, `brands`, `inventories`,
`inventory_published`, `margin_costs`, `purchase_orders`, `purchase_order_items`,
`purchase_order_status_logs`, `product_rtp`, `product_tag_relations` (via `sql/stocks.sql`);
`stock_requests`, `product_stocks`, `product_selling_prices` (inline query); `data/product_status.csv`.
**Writes:** returns DataFrame.

```python
def load_stocks(con, rules: Rules) -> pd.DataFrame:
    """Run sql/stocks.sql -> per-grain stock position.
       Columns: warehouse_id, warehouse_name, product_id, product_attribute_id, sku,
         product_name, category_id, category_name, brand_id, brand_name, unit, position,
         product_status, product_attribute_status, divider, avg_lead_time, cycle_time,
         stok_belum_rilis, stok_rilis, stok_booking, stok_incoming, stok_gudang, status_final."""

def load_product_status(rules: Rules) -> pd.DataFrame:
    """Read data/product_status.csv (cols per §1.4) -> typed DataFrame."""

def load_stock_requests(con, rules: Rules) -> pd.DataFrame:
    """Aggregate qty_req per grain over last `stock_request_lookback_days`. Cols:
       product_id, product_attribute_id, warehouse_id, qty_req."""

def assemble_position(df_orders_in_ex, df_stocks, df_status, df_flow, df_req,
                      rules: Rules) -> pd.DataFrame:
    """Outer-merge orders⨝stocks on grain+unit+warehouse_name; left-merge product_status,
       classification (cat_flow), stock_requests. Apply special_warehouse_ids overrides
       (status=1, adj_lead_time=avg_lead_time, PIC='Buyer-A', label_priority=None).
       Apply fillna defaults (adj_lead_time->lead_time_fallback, cat_flow->'Slow Moving',
       PIC->'Unassigned', qty_req->0). Returns the assembled per-grain frame."""
```

> The special-handling override block forces the special warehouses active using
> `rules.stock["special_warehouse_ids"]` and synthetic PIC. `avg_lead_time` fillna uses
> `avg_lead_time_fallback`.

### 3.3 `margin_turnover.py` — GMV / gross-margin / gm_rate + L*d turnover + recur_tor

Ports the consolidation pipeline's margin + turnover stage: margin pull + aggregation
(`gmv`, `total_margin`, `gm_rate`), tag-relations join, turnover history math (`l7d..l30d_tor`,
caps, `recur_tor` ladder).

**Reads:** `orders`, `order_items`, `order_logs`, `inventory_published`, `inventories`,
`purchase_orders`, `purchase_order_items`, `suppliers`, `margin_costs`, `production_orders`,
`production_order_items` (via `sql/margin.sql`); `turnover_history` (via `sql/turnover.sql`);
`product_tags`, `product_tag_relations`, `product_rtp` (inline for tag-relations).
**Writes:** returns DataFrames.

```python
def load_margin(con, rules: Rules, *, now: date) -> pd.DataFrame:
    """Run sql/margin.sql over last `sales_lookback_days`. Per (product_id, unit, warehouse_id):
       gmv = Σ(selling_price*quantity_out); total_margin = Σ((selling_price-purchase_price)*quantity_out);
       gm_rate = total_margin/gmv (0 when gmv==0). Cols: product_id, unit, warehouse_id, gmv,
       total_margin, gm_rate."""

def load_turnover(con, rules: Rules, *, now: date) -> pd.DataFrame:
    """Run sql/turnover.sql -> per (product_id, warehouse_id) inventory/incoming snapshots at
       final/l7d/l14d/l21d/l30d. Compute tor per window = (inv+inc-final)/((inv+final)/2), round 2;
       cap: window<L30 tor>=cap_threshold -> cap_value_default(14); L30 -> cap_value_l30(30).
       recur_tor = first positive in ladder L7->L14->L21->L30 else recur_fallback(14).
       Cols: product_id, warehouse_id, l30d_tor, recur_tor (+ intermediate l*d_tor)."""

def load_tag_relations(con, rules: Rules) -> pd.DataFrame:
    """status_wl flag per product (synthetic tokens). Cols: product_id, status_wl."""
```

> Turnover divide-by-zero guarded: denom 0 -> tor 0. `df_tor.fillna(0)` before tor math.
> recur ladder is exactly the original `i->j->k->l->14`.

### 3.4 `consolidate.py` — ORCHESTRATOR

Ports the consolidation pipeline's final-merge stage plus the whole DAG wiring.
Replaces the object-store/BI/Sheets sinks with a single local Parquet write.

**Reads:** everything above (calls the other modules).
**Writes:** `out/consolidate_purchasing_agg.parquet` (+ optional `out/consolidate_purchasing_agg.csv`).

```python
def run_consolidate(con, rules: Rules, *, now: date | None = None) -> pd.DataFrame:
    """Full pipeline: load_orders -> classify_demand + remove_outliers ->
       load_stocks/load_product_status/load_stock_requests -> assemble_position ->
       load_margin + load_turnover + load_tag_relations -> final merge + fillna + dtype cast +
       drop_duplicates. Writes out/consolidate_purchasing_agg.parquet via shims.report/data_io.
       Returns the final consolidated DataFrame (one row per grain)."""
```

**Final consolidated columns (LOCKED ORDER):**

```
warehouse_id, warehouse_name, product_id, product_attribute_id, product_name, unit,
total_quantity, upper_bound, lower_bound, days, status_outliers, days_divider, qty_per_day,
sku, category_id, category_name, brand_id, brand_name, position, product_status,
product_attribute_status, divider, avg_lead_time, cycle_time, stok_belum_rilis, stok_rilis,
stok_booking, stok_incoming, stok_gudang, status_final, status, adj_lead_time, PIC,
label_priority, ragu_nonaktif, cat_flow, qty_req, gmv, total_margin, gm_rate, status_wl,
l30d_tor, recur_tor, running_datetime
```

> Note column renames from the original's Sheet-derived headers: `qty/day`→`qty_per_day`,
> `adj. lead time`→`adj_lead_time`, `Label Priority`→`label_priority`,
> deactivation-review flag→`ragu_nonaktif`, `Running Datetime`→`running_datetime`.
> The original BI/extract-publish block is **dropped**; replaced by an optional
> `publish_stub()` log line.

### 3.5 `aging_alert.py` — aging cohort + thresholds + sell-out join + HTML/MD report

Ports the aging-stock-alert pipeline (entire flow), minus all live I/O. Reads the committed
`data/aging_cohort.csv` (was the BI view), joins `product_rtp` for category, applies
Daily-Needs(≥`daily_needs_days`)/Lifestyle(≥`lifestyle_days`) thresholds, joins last-7-day `sales_history`
sell-out, renders report to disk.

**Reads:** `data/aging_cohort.csv`, `product_rtp` (DuckDB), `sales_history` (DuckDB).
**Writes:** `out/aging_report.html`, `out/aging_report.md` (+ `out/last_refreshed.csv` metadata).

```python
def load_cohort(rules: Rules) -> pd.DataFrame:
    """Read data/aging_cohort.csv (cols per §1.4)."""

def categorize_and_filter(df_cohort: pd.DataFrame, con, rules: Rules) -> pd.DataFrame:
    """Join product_rtp -> Category = 'Daily Needs' if rtp_category==daily_needs_category
       or rtp_sub_category LIKE daily_needs_subcategory_like else 'Lifestyle'.
       Keep rows where (Daily Needs & diff_days_inhouse>=daily_needs_days)
                    or (Lifestyle & diff_days_inhouse>=lifestyle_days).
       Drop warehouse_name LIKE exclude_warehouse_name_like; keep status_wl LIKE 'WL%'.
       Aggregate stok/total_purchase per (product_id, product_unit, warehouse_name, Category)."""

def join_sell_out(df_aged: pd.DataFrame, con, rules: Rules, *, now: date) -> pd.DataFrame:
    """Left-join last `sell_out_lookback_days` sales_history (exclude order_item_type='reward')
       -> qty_sell_out, gmv (coalesce 0). Returns daily_needs / lifestyle / all_data frames."""

def run_aging(con, rules: Rules, *, now: date | None = None) -> dict[str, pd.DataFrame]:
    """Orchestrate the above; render report via shims.report.save_report ->
       out/aging_report.html + out/aging_report.md. NO email, NO sheets. Returns the frames."""
```

> The category split uses `daily_needs_category='Staples'` + `daily_needs_subcategory_like='Flour'`
> (generic staples tokens, no localized brand/category names). Recipients/sender from config are
> metadata only (printed in the report header), never used to send.

---

## 4. SHIM CONTRACT (`shims/`)

Open replacements for the originals' internal infra module. **No external services.**

### 4.1 `shims/__init__.py`
Empty package marker (may re-export `data_io` and `report` symbols for convenience).

### 4.2 `shims/data_io.py`

```python
from __future__ import annotations
import duckdb, pandas as pd
from pathlib import Path

def connect(db_path: str = "stocklens.duckdb") -> duckdb.DuckDBPyConnection:
    """Open (or create) the DuckDB database file. Read-only callers pass the seeded db."""

def get_data(sql_or_name: str, con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Backed by DuckDB. If `sql_or_name` looks like SQL (contains whitespace/'select'),
       execute it; otherwise treat it as a table name and `SELECT * FROM <name>`.
       Returns a pandas DataFrame (con.execute(sql).df()). This is the open analogue of the
       originals' internal data-warehouse fetch helper (no network)."""

def read_sql_file(path: str) -> str:
    """Load a .sql file from src/stocklens/sql/ and return its text (prefixes already stripped)."""

def write_parquet(df: pd.DataFrame, path: str) -> str:
    """Write df to local Parquet under out/ (mkdir -p). Returns the path. Also writes a .csv
       sibling for easy inspection. The open analogue of the originals' object-store writer (no S3)."""

def publish_stub(name: str) -> None:
    """No-op. Logs 'would publish <name> (skipped: showcase build)'. Replaces the BI publish step."""
```

> `get_data` must NEVER reach a network. SQL is run against the local DuckDB connection only.

### 4.3 `shims/report.py`

```python
from __future__ import annotations
import pandas as pd
from pathlib import Path

def render_html(context: dict) -> str:
    """Render an HTML report from a Jinja2 template (inline or templates/). `context` carries
       greeting, generated_at, recipients (metadata only), and named DataFrames already turned to
       HTML tables (df.to_html(classes='table table-striped', index=False))."""

def render_md(context: dict) -> str:
    """Render the same report as Markdown (df.to_markdown(index=False))."""

def save_report(context: dict, *, html_path: str, md_path: str) -> tuple[str, str]:
    """render_html + render_md, write both under out/ (mkdir -p). Returns (html_path, md_path).
       NO smtplib, NO network. Replaces the entire email-blast block of the original."""
```

> The report header includes `sender`/`recipients` from config purely as displayed metadata
> (e.g. "Would be sent to: purchasing-lead@example.com") — never an actual send.

---

## 5. SQL FILE CONTRACT (`src/stocklens/sql/`)

Each file holds one big query, ported from the originals' CTEs, **schema-prefixes stripped**, tables
**unqualified synthetic names** (§1). No schema prefixes of any kind on table names.
DuckDB dialect (use DuckDB date functions: `current_date`, `date_diff`, `date '...' - interval`).
Parameterize date windows via Python f-string/`?` params passed from the module (no live `getdate()`).

### 5.1 `sql/orders.sql`
Ports the orders query. Joins `order_items ⨝ orders ⨝ warehouses ⨝ products`,
left-joins `product_tag_relations` (premium tag), `product_rtp` (status_wl window), `order_logs`.
Emits: `order_date, order_id, invoice, order_item_id, product_name, product_tag, filter_mandiri,
superagent_id, product_id, product_attribute_id, unit, qty_sales, status, warehouse_id, warehouse_name`.
`product_tag` CASE: `product_tag_id = :premium_tag_id` → `'Premium'`; `status_wl LIKE '%WL%'` → `'RTP'`;
else `'Reguler'`. `filter_mandiri` CASE per the original (Premium/RTP/superagent_id=0 → Include).
Window: `order_date BETWEEN :start AND :end` (last `sales_lookback_days`).

### 5.2 `sql/stocks.sql`
Ports the stocks query: the `_warehouses/_products/_wp` cross join + the five
`_stok_*` CTEs (belum_rilis, rilis_regular/fs/reward/rtp), `_stok_booking`, `_incoming_goods`,
`_cycle_time`, `_lead_time`, and the `divider` CASE. `divider` CASE rewritten with synthetic tokens:
`product_rtp.rtp_sub_category NOTNULL → 'Private Label'`; `product_tag_id = :premium_tag_id → 'Premium'`;
`warehouse_name LIKE '%RTP DC%' → 'Exclusivity'` (synthetic special-handling rule);
else `'General Product'`. Lead-time/cycle-time use DuckDB `date_diff('day', a, b)` and `ceil(avg(...))`.
Emits the per-grain stock-position row (cols per §3.2 `load_stocks`).

### 5.3 `sql/margin.sql`
Ports the margin query: `orders ⨝ order_items ⨝ order_logs ⨝ inventory_published`,
a `pur` subquery `UNION ALL` over `purchase_orders/_items + suppliers + margin_costs` and
`production_orders/_items + suppliers + margin_costs`. Emits per OUT line:
`order_id, warehouse_id, created_at, invoice, product_name, product_id, unit, selling_price,
quantity_out, purchase_price` (the module aggregates to gmv/total_margin/gm_rate).
Window: `date(created_at) BETWEEN :start AND :end`.

### 5.4 `sql/turnover.sql`
Ports the turnover query: CTEs `fins, s7, s14, s21, s30` (stock_value at
`period = :asof - {1,8,15,22,31}`) and `in7, in14, in21, in30` (Σ incoming over the rolling window),
left-joined back per `(product_id, warehouse_id)`. Emits:
`product_id, warehouse_id, final_inv, l7d_inv, l14d_inv, l21d_inv, l30d_inv, l7d_inc, l14d_inc,
l21d_inc, l30d_inc` (the module computes the tors). Uses `turnover_history` (synthetic),
DuckDB date arithmetic, parameterized `:asof`.

> The stock-request query and tag-relations query are small enough to stay
> inline in their modules (`stock_position.py` / `margin_turnover.py`) — they are NOT separate .sql
> files (only the four big queries get files, matching the required tree).

---

## 6. CLI + TESTS

### 6.1 `pyproject.toml` (deps — LOCKED)

- **Build/runtime deps:** `pandas`, `numpy`, `duckdb`, `jinja2`, `typer`, `tomli; python_version < "3.11"`.
- **Optional extra `viz`:** `streamlit`.
- **Dev deps:** `pytest`, `ruff`, `mypy`.
- `requires-python = ">=3.10"`. `[tool.ruff] target-version = "py310"`. `[tool.mypy] python_version = "3.10"`.
- **Excluded:** `pandasql` (banned), `gspread`, `oauth2client`, `tableauhyperapi`, `tableau_api_lib`,
  `smtplib`-based mail libs, `boto3` — none of these may appear.
- `to_markdown` needs `tabulate`; add `tabulate` to runtime deps (or render MD by hand). **Decision:**
  add `tabulate` to runtime deps.

### 6.2 `cli.py` (Typer)

```
python cli.py seed          # run seed/generate.py -> stocklens.duckdb
python cli.py consolidate   # run_consolidate -> out/consolidate_purchasing_agg.parquet
python cli.py aging         # run_aging -> out/aging_report.html + .md
python cli.py all           # seed -> consolidate -> aging
```

Each subcommand: load `Rules` via `load_rules()`, open DuckDB via `shims.data_io.connect()`, call the
orchestrator, print a one-line summary (row counts, output paths). `--now` optional ISO date override
for deterministic runs/tests. `--config` optional path override.

### 6.3 `app/viewer.py` (Streamlit, optional `viz` extra)
Loads `out/consolidate_purchasing_agg.parquet` and the aging frames; shows the demand table (filter by
warehouse / `cat_flow`) and the two aging tables. Pure read of local files. No network.

### 6.4 Tests (`tests/`) — each asserts the plan's WORKED EXAMPLES

`tests/conftest.py`: fixture building a tiny in-memory DuckDB (or seeded temp db) + a `Rules` loaded
from `config/rules.toml`; a fixed `now`. `tests/__init__.py` empty.

| Test file | Asserts (worked examples) |
|---|---|
| **test_demand_classify.py** | (1) Velocity cumulative windows: movements 5@2d, 3@9d, 4@25d → L7=5, L14=8, L21=8, L30=12; orderCount(L7)=1. (2) Weighted score: grains A(100,10)→82, B(20,4)→16.8, C(5,2)→4.4; warehouse mean=34.4, sample std≈41.69 (ddof=1), std≤1000 → limit≈76.09 → A=Super Fast, B=Slow, C=Slow. (3) std-damp branch: when std>1000, limit = mean + 0.25*std. |
| **test_outliers.py** | IQR on `[2,3,3,4,50]` (n=5): q1=3, q3=4, iqr=1, upper=round(4+1.5)=6, lower=round(3-1.5)=2; 50 flagged → totalIncl=62, totalExcl=12. Single-row `[8]`: upper=12, lower=0, kept (totalExcl=totalIncl=8). lower clamp <0→0. len==0 → all-zero. |
| **test_turnover.py** | TOR: invStart=1,000,000, incoming=200,000, final=600,000 → denom=800,000, raw=0.75 (<30, uncapped) → 0.75. Cap: raw≥30 → 14 (L7/L14/L21) / 30 (L30). recur ladder: t7=0,t14=0.75 → recur=0.75; all-zero → 14. denom 0 → 0. |
| **test_aging.py** | Daily-Needs(≥15)/Lifestyle(≥31) split: a Daily-Needs (`rtp_category='Staples'` or sub LIKE 'Flour') row with diff_days=15 kept, 14 dropped; a Lifestyle row diff_days=31 kept, 30 dropped. `Consignment` warehouse excluded; non-`WL%` status dropped. reward sell-out excluded. |

> Additional asserts for completeness (the acceptance-gate analogue): reorder `qty/day` floor
> (total/divider, min 1) and margin `gm_rate` worked example (gmv=86,000, cogs=59,000 → gm_rate≈0.3140)
> may live in `test_demand_classify.py` / a small assertion in `test_turnover.py` or be folded into the
> nearest file. The four named test files above are the required minimum; do not skip the len==1 IQR
> branch, zero-sales velocity, the divide-by-zero turnover guard, or the threshold boundaries.

---

## 7. FILE TREE (build agents create exactly these; `.gitignore` already exists)

```
stocklens/
  README.md  ORIGIN.md  LICENSE  pyproject.toml  .env.example  cli.py
  config/rules.toml
  data/product_status.csv  data/aging_cohort.csv
  seed/generate.py
  shims/__init__.py  shims/data_io.py  shims/report.py
  src/stocklens/__init__.py
  src/stocklens/demand_classify.py  src/stocklens/stock_position.py  src/stocklens/margin_turnover.py
  src/stocklens/consolidate.py  src/stocklens/aging_alert.py
  src/stocklens/sql/orders.sql  src/stocklens/sql/stocks.sql  src/stocklens/sql/margin.sql  src/stocklens/sql/turnover.sql
  app/viewer.py
  docs/planning/BUILD-CONTRACT.md   (this file)
  tests/__init__.py  tests/conftest.py
  tests/test_demand_classify.py  tests/test_outliers.py  tests/test_turnover.py  tests/test_aging.py
```

> `README.md`, `ORIGIN.md` content follow the project plan's draft (one-liner,
> "what it demonstrates", architecture diagram seed→DuckDB→3 transforms→Parquet→report, quickstart
> `uv sync` / `python cli.py all`, config pointer, algorithm notes, ORIGIN honesty note). `.env.example`
> is intentionally near-empty (no secrets needed; document only optional `STOCKLENS_CONFIG` /
> `STOCKLENS_DUCKDB` overrides).

---

## 8. ACCEPTANCE GATE (a build is "done" only when all hold)

1. `python cli.py all` runs end-to-end on a clean checkout with `uv`, producing
   `out/consolidate_purchasing_agg.parquet`, `out/aging_report.html`, `out/aging_report.md` — all non-empty.
2. Every pipeline stage yields non-empty output for warehouses 1, 2, 3.
3. `pytest` green; all four worked-example tests pass.
4. `ruff` + `mypy` clean.
5. **No real schema names, internal module names, business-rule ids, brand/codename tokens, document
   ids, infra paths, service accounts, credentials, person names, or emails** anywhere in the committed
   tree (grep gate before commit) — only the synthetic vocabulary of this contract appears.
6. No network/SMTP/Sheets/object-store/BI call exists in any committed file.
7. Output column order of `consolidate_purchasing_agg` matches §3.4 exactly.

---

## 9. v1.1.0 — Analytics & multi-page app (ADDITIVE)

This release adds a read-only analytics/presentation layer **on top of** the locked pipeline. It is
strictly additive: the seed, the four transform modules, the SQL, the `Rules`/config keys (§2), and
the 44-column consolidated schema (§3.4) are **unchanged**, and no new live side-effect is introduced
(the guardrails of §0 still hold — the only writes are the same local `out/` artifacts).

### 9.1 New components

| Path | Role |
|---|---|
| `src/stocklens/analytics.py` | Pure, typed derived analytics over the consolidated Parquet + seeded DuckDB: grain de-duplication, inventory **value at cost** (reconstructed from `inventories × margin_costs`), headline KPIs, days-of-cover / reorder worklist, ABC + XYZ + the 3×3 matrix, GMROI, a daily-demand forecast + holdout backtest, reorder point / safety stock, and the data-quality contract. No writes, no network. |
| `app/_data.py` | Streamlit-cached loaders + UI helpers + a first-boot `ensure_artifacts` seeder (rebuilds the git-ignored artifacts on a cold deploy). |
| `app/viewer.py` | Repurposed as the multi-page **home** (executive overview); still the `streamlit run` entry point. |
| `app/pages/*.py` | The nine analytical pages (Demand, Stock & Reorder, Aging & Dead Stock, ABC-XYZ, Margin & GMROI, Forecast & Reorder Point, What-if Simulator, Data Quality, Methodology). |
| `cli.py validate` | Runs `analytics.validate_consolidated` over the artifact; exits non-zero on any hard-check failure (CI gate). |
| `tests/test_analytics.py` | Unit tests for the analytics core, incl. the grain double-counting trap and a corrupted-frame-must-fail contract test. |
| `tests/test_app_smoke.py` | Executes every page's `main()` against the real artifacts with `streamlit`/`altair` stubbed (no runtime / no extra). |
| `api/main.py` | A thin **FastAPI** JSON layer (`uvicorn api.main:app`) exposing the same analytics as REST: `/healthz`, `/kpis`, `/grains`, `/demand/classification`, `/stock/reorder`, `/aging`, `/margin/gmroi`, `/abc-xyz`, `POST /simulate`, with auto OpenAPI at `/docs`. Read-only; reuses the pure functions (`api` optional extra). |
| `tests/test_api.py` | `TestClient` coverage of every endpoint (skipped without the `api` extra). |
| `requirements.txt`, `.streamlit/config.toml` | Streamlit Community Cloud deploy. |

### 9.2 Rules that still hold

1. The locked schema (§3.4) and all `Rules` keys (§2) are read, never written or extended. App-only
   planning policy (ABC/XYZ cut-points, service-level *z*) lives in an `analytics.AnalyticsConfig`
   dataclass, **not** in `config/rules.toml`.
2. Any value roll-up first collapses the consolidated frame to one row per grain
   (`analytics.to_grain`) — the stock/margin/classification columns repeat across the
   `window × outlier-treatment` rows, so a naive sum over-counts ~8×.
3. `value_at_cost` is the only metric reconstructed from the raw DuckDB lots (the locked schema drops
   it); it is read-only and 0-filled for grains without lots.
4. mypy still passes on `src` (analytics is fully typed); `ruff` passes repo-wide; the app smoke test
   needs no optional extra so the default CI run stays green.
