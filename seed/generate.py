"""Synthetic data generator for the StockLens (Track B) showcase.

Builds ``stocklens.duckdb`` with a fully synthetic relational schema that mirrors
the *shape* of the two production pipelines this repo reconstructs, without
reproducing any real identifier, brand, schema name, or volume. Every table is
internally consistent (foreign keys line up) so that each downstream pipeline
stage yields non-empty, sensible output for warehouses 1, 2 and 3.

Run as::

    python seed/generate.py                 # -> ./stocklens.duckdb
    python seed/generate.py --db custom.duckdb

The build is deterministic: all randomness flows through a single
``numpy.random.default_rng(42)`` instance (no global seeding) and every table is
created with ``CREATE OR REPLACE`` so re-running is idempotent.

All table/column names are generic synthetic names per the locked build
contract (``docs/planning/BUILD-CONTRACT.md`` section 1).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Configuration constants (mirror config/rules.toml; kept local so the seeder
# has no import dependency on the package being built in parallel).
# --------------------------------------------------------------------------- #

SEED = 42

# Warehouse id constants (LOCKED — contract section 1.2).
WH_NORTH = 1
WH_SOUTH = 2
WH_CENTRAL = 3
WH_CONSIGNMENT = 9  # consignment warehouse — excluded from replenishment demand
WH_RTP = 7  # special-handling warehouse — forced active, dedicated buyer

NORMAL_WAREHOUSES = (WH_NORTH, WH_SOUTH, WH_CENTRAL)

WAREHOUSES = [
    (WH_NORTH, "North DC", "normal"),
    (WH_SOUTH, "South DC", "normal"),
    (WH_CENTRAL, "Central DC", "normal"),
    (WH_RTP, "RTP DC", "special"),
    (WH_CONSIGNMENT, "Consignment DC", "consignment"),
]

PREMIUM_TAG_ID = 1  # synthetic replacement for the original proprietary tag id

CATEGORIES = [
    (1, "Staples"),
    (2, "Beverages"),
    (3, "Personal Care"),
    (4, "Home"),
    (5, "Snacks"),
]

BRANDS = [
    (1, "BrandOne"),
    (2, "BrandTwo"),
    (3, "BrandThree"),
]

SUPPLIERS = [
    (1, "Supplier Alpha"),
    (2, "Supplier Beta"),
    (3, "Supplier Gamma"),
]

PRODUCT_TAGS = [
    (1, "Premium"),
    (2, "Standard"),
]

# Product universe: ids 101..136 (36 products).
PRODUCT_ID_START = 101
N_PRODUCTS = 36
PRODUCT_IDS = list(range(PRODUCT_ID_START, PRODUCT_ID_START + N_PRODUCTS))

# Roughly the first ~8 products carry the "Premium" tag.
PREMIUM_PRODUCT_IDS = PRODUCT_IDS[:8]

# A subset of products are RTP (type == 2): these exercise the RTP / WL branch.
RTP_PRODUCT_IDS = PRODUCT_IDS[8:20]  # 12 products

UNITS = ["pcs", "box", "pack", "kg", "bottle"]

# Reference "now" for the seed. Using a fixed anchor keeps the generated history
# windows stable regardless of when the seeder runs (tests pass --now to match).
ASOF = date(2026, 6, 25)

SALES_LOOKBACK_DAYS = 30
HISTORY_DAYS = 60  # order history headroom beyond the 30d window
TURNOVER_HISTORY_DAYS = 40  # >= 31 days so L7/L14/L21/L30 windows resolve


def _sku(product_id: int) -> str:
    """SKU display token, e.g. SKU-0101 for product id 101."""
    return f"SKU-{product_id:04d}"


# --------------------------------------------------------------------------- #
# Dimension tables
# --------------------------------------------------------------------------- #


def build_dimensions(rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    """Build the product/location dimension frames."""
    warehouses = pd.DataFrame(WAREHOUSES, columns=["id", "name", "type"])
    categories = pd.DataFrame(CATEGORIES, columns=["id", "name"])
    brands = pd.DataFrame(BRANDS, columns=["id", "name"])
    suppliers = pd.DataFrame(SUPPLIERS, columns=["id", "name"])
    product_tags = pd.DataFrame(PRODUCT_TAGS, columns=["id", "name"])

    rtp_set = set(RTP_PRODUCT_IDS)
    products_rows = []
    attributes_rows = []
    for idx, pid in enumerate(PRODUCT_IDS):
        category_id = int(CATEGORIES[idx % len(CATEGORIES)][0])
        brand_id = int(BRANDS[idx % len(BRANDS)][0])
        ptype = 2 if pid in rtp_set else 1
        products_rows.append(
            {
                "id": pid,
                "sku": _sku(pid),
                "name": f"Product {pid}",
                "category_id": category_id,
                "brand_id": brand_id,
                "type": ptype,
                "status": 1,
            }
        )
        # Exactly one product_attribute per product; attribute id == product id
        # for an easy-to-follow 1:1 mapping in the synthetic data.
        attributes_rows.append(
            {
                "id": pid,
                "product_id": pid,
                "unit": UNITS[idx % len(UNITS)],
                "position": idx + 1,
                "status": 1,
            }
        )

    products = pd.DataFrame(products_rows)
    product_attributes = pd.DataFrame(attributes_rows)

    # Premium tag relations (~8 rows).
    tag_relations = pd.DataFrame(
        {"product_id": PREMIUM_PRODUCT_IDS, "product_tag_id": PREMIUM_TAG_ID}
    )

    return {
        "warehouses": warehouses,
        "categories": categories,
        "brands": brands,
        "suppliers": suppliers,
        "product_tags": product_tags,
        "products": products,
        "product_attributes": product_attributes,
        "product_tag_relations": tag_relations,
    }


def build_product_rtp(rng: np.random.Generator) -> pd.DataFrame:
    """RTP / segmentation table (replaces the Google-Sheet-backed product_rtp).

    Exercises ``status_wl LIKE 'WL%'`` / ``LIKE '%WL%'`` and the Daily-Needs
    (Staples / 'Flour') vs Lifestyle category split. ``end_date`` is NULL for
    some rows (open-ended).
    """
    rows = []
    # rtp_category is 'Staples' (Daily Needs side) or 'Lifestyle'.
    # rtp_sub_category includes some 'Flour' so the sub-category LIKE rule fires.
    for i, pid in enumerate(RTP_PRODUCT_IDS):
        if i % 3 == 0:
            category, sub = "Staples", "Flour"
        elif i % 3 == 1:
            category, sub = "Staples", "Grain"
        else:
            category, sub = "Lifestyle", "Apparel"

        # status_wl: mostly WL / WL-A, a couple blank to prove the filter.
        if i % 5 == 4:
            status_wl = ""
        elif i % 2 == 0:
            status_wl = "WL"
        else:
            status_wl = "WL-A"

        start = ASOF - timedelta(days=365)
        end = None if i % 4 == 0 else ASOF + timedelta(days=180)
        rows.append(
            {
                "product_id": pid,
                "rtp_category": category,
                "rtp_sub_category": sub,
                "status_wl": status_wl,
                "start_date": start,
                "end_date": end,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Sales / order fact
# --------------------------------------------------------------------------- #


def build_orders(
    rng: np.random.Generator,
    products: pd.DataFrame,
    attributes: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """Build orders, order_items and order_logs over the last ~60 days.

    Each sold order_item gets a matching signed-negative OUT row in order_logs
    (so booking / margin / cycle-time joins resolve). Order quantities are
    seeded so that, after the demand pipeline, warehouses 1/2/3 contain a mix of
    Super Fast / Fast / Slow movers, and a handful of grains have sales inside
    the last 7 days.
    """
    attr_by_product = {
        int(r.product_id): (int(r.id), str(r.unit))
        for r in attributes.itertuples(index=False)
    }
    product_name_by_id = {
        int(row.id): str(row.name) for row in products.itertuples(index=False)
    }

    orders_rows: list[dict] = []
    items_rows: list[dict] = []
    logs_rows: list[dict] = []

    order_id = 1
    order_item_id = 1
    publish_seq = 1

    # Each (warehouse, product) grain gets a deterministic activity profile so
    # the classification produces a sensible spread. We bias a few grains to be
    # high-volume (Super Fast), some mid (Fast), and most low (Slow).
    for wh in (*NORMAL_WAREHOUSES, WH_RTP, WH_CONSIGNMENT):
        # Not every product is stocked/sold in every warehouse. Normal
        # warehouses cover most of the universe; the special/excluded ones cover
        # a smaller slice (but still non-empty).
        if wh in NORMAL_WAREHOUSES:
            wh_products = PRODUCT_IDS
        elif wh == WH_RTP:
            wh_products = RTP_PRODUCT_IDS
        else:  # consignment / excluded
            wh_products = PRODUCT_IDS[:12]

        for rank, pid in enumerate(wh_products):
            pa_id, unit = attr_by_product[pid]
            pname = product_name_by_id[pid]

            # Volume tier by rank: a few hot grains, then a long tail.
            if rank < 3:
                base_qty, n_events = 40, 8  # high movers
            elif rank < 10:
                base_qty, n_events = 12, 5  # mid movers
            else:
                base_qty, n_events = 4, 3  # slow tail

            # Spread events across the last 30 days so L7/L14/L21/L30 buckets all
            # populate; guarantee at least one event inside the last 7 days for
            # the hot grains.
            ages = rng.integers(0, SALES_LOOKBACK_DAYS, size=n_events).tolist()
            if rank < 3:
                ages[0] = int(rng.integers(0, 7))  # ensure recent sale

            for ev_idx, age in enumerate(ages):
                created = datetime.combine(ASOF, datetime.min.time()) - timedelta(
                    days=int(age), hours=int(rng.integers(0, 12))
                )
                # quantity: base +/- jitter; inject one bulk outlier per hot grain
                # so the IQR cleaner has something to remove.
                qty = int(max(1, base_qty + rng.integers(-3, 4)))
                if rank < 3 and ev_idx == n_events - 1:
                    qty = base_qty * 6  # bulk outlier

                # superagent_id mix: mostly 0 ("Include" / mandiri), some not.
                superagent_id = 0 if rng.random() < 0.8 else int(rng.integers(1, 5))
                status = int(rng.choice([2, 3], p=[0.5, 0.5]))  # keep status > 1

                invoice = f"INV-{wh}-{order_id:06d}"
                orders_rows.append(
                    {
                        "id": order_id,
                        "created_at": created,
                        "invoice": invoice,
                        "warehouse_id": wh,
                        "superagent_id": superagent_id,
                        "status": status,
                    }
                )
                items_rows.append(
                    {
                        "id": order_item_id,
                        "order_id": order_id,
                        "product_id": pid,
                        "product_attribute_id": pa_id,
                        "product_name": pname,
                        "unit": unit,
                        "quantity": qty,
                        "deleted_at": pd.NaT,
                        "deleted_by": pd.NA,
                    }
                )
                # Matching signed-negative OUT log row (the booking / sale line).
                logs_rows.append(
                    {
                        "order_id": order_id,
                        "order_item_id": order_item_id,
                        "quantity": -qty,
                        "status": 1,
                        "type": "order",
                        "inventory_publish_id": publish_seq,
                    }
                )
                order_id += 1
                order_item_id += 1
                publish_seq += 1

    orders = pd.DataFrame(orders_rows)
    order_items = pd.DataFrame(items_rows)
    order_logs = pd.DataFrame(logs_rows)

    # A few cancelled / pre-sale orders (status <= 1) so the status filter has
    # something to drop, and so the booking CTE (status in (0,1)) is non-empty.
    n_booking = 30
    booking_orders = []
    booking_items = []
    booking_logs = []
    for _ in range(n_booking):
        pid = int(rng.choice(PRODUCT_IDS))
        pa_id, unit = attr_by_product[pid]
        pname = product_name_by_id[pid]
        wh = int(rng.choice(NORMAL_WAREHOUSES))
        created = datetime.combine(ASOF, datetime.min.time()) - timedelta(
            days=int(rng.integers(0, 10))
        )
        qty = int(rng.integers(2, 15))
        booking_orders.append(
            {
                "id": order_id,
                "created_at": created,
                "invoice": f"INV-{wh}-{order_id:06d}",
                "warehouse_id": wh,
                "superagent_id": 0,
                "status": int(rng.choice([0, 1])),
            }
        )
        booking_items.append(
            {
                "id": order_item_id,
                "order_id": order_id,
                "product_id": pid,
                "product_attribute_id": pa_id,
                "product_name": pname,
                "unit": unit,
                "quantity": qty,
                "deleted_at": pd.NaT,
                "deleted_by": pd.NA,
            }
        )
        booking_logs.append(
            {
                "order_id": order_id,
                "order_item_id": order_item_id,
                "quantity": -qty,
                "status": 1,
                "type": "order",
                "inventory_publish_id": publish_seq,
            }
        )
        order_id += 1
        order_item_id += 1
        publish_seq += 1

    orders = pd.concat([orders, pd.DataFrame(booking_orders)], ignore_index=True)
    order_items = pd.concat(
        [order_items, pd.DataFrame(booking_items)], ignore_index=True
    )
    order_logs = pd.concat([order_logs, pd.DataFrame(booking_logs)], ignore_index=True)

    return {
        "orders": orders,
        "order_items": order_items,
        "order_logs": order_logs,
    }


# --------------------------------------------------------------------------- #
# Inventory / stock position
# --------------------------------------------------------------------------- #


def build_inventory(
    rng: np.random.Generator,
    order_logs: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """Build inventories, inventory_published and margin_costs.

    ``inventory_published.id`` aligns with the ``inventory_publish_id`` values
    emitted in ``order_logs`` so the booking / margin joins resolve, and every
    inventory lot carries a ``margin_costs`` purchase price.
    """
    inventories_rows: list[dict] = []
    published_rows: list[dict] = []
    margin_rows: list[dict] = []

    inv_id = 1
    # One inventory lot + one published row per published id referenced in logs.
    publish_ids = sorted(order_logs["inventory_publish_id"].unique().tolist())

    # Build a deterministic grain pool to attach lots to (normal + RTP whs).
    grain_pool = [
        (wh, pid)
        for wh in (*NORMAL_WAREHOUSES, WH_RTP)
        for pid in (PRODUCT_IDS if wh in NORMAL_WAREHOUSES else RTP_PRODUCT_IDS)
    ]

    publish_types = ["regular", "flashsale", "reward", "rtp"]

    for pub_id in publish_ids:
        wh, pid = grain_pool[int(rng.integers(0, len(grain_pool)))]
        pa_id = pid  # 1:1 attribute mapping
        remaining = int(rng.integers(0, 200))
        created = datetime.combine(ASOF, datetime.min.time()) - timedelta(
            days=int(rng.integers(1, 120))
        )
        inventories_rows.append(
            {
                "id": inv_id,
                "product_id": pid,
                "product_attribute_id": pa_id,
                "warehouse_id": wh,
                "remaining_quantity": remaining,
                "created_at": created,
                "new_purchase_order_id": int(rng.integers(1, 120)),
                "production_order_id": 0,
                "purchase_order_id": int(rng.integers(1, 120)),
                "inventory_vendor_id": 0,  # own stock -> exercises cycle-time CTE
            }
        )
        published_rows.append(
            {
                "id": pub_id,
                "inventory_id": inv_id,
                "product_attribute_id": pa_id,
                "warehouse_id": wh,
                "remaining_quantity": int(rng.integers(0, 150)),
                "publish_type": publish_types[int(rng.integers(0, len(publish_types)))],
                "group_type": "grosir",
                "quantity": int(rng.integers(10, 250)),
                "created_at": created,
            }
        )
        # Round synthetic purchase price (inc PPN), obviously fake magnitudes.
        margin_rows.append(
            {
                "inventory_id": inv_id,
                "purchase_price_inc_ppn": float(int(rng.integers(5, 60)) * 1000),
            }
        )
        inv_id += 1

    # Extra unpublished inventory lots so stok_belum_rilis is non-empty per
    # grain in warehouses 1/2/3 (these have no published row).
    for wh in NORMAL_WAREHOUSES:
        for pid in PRODUCT_IDS:
            if rng.random() < 0.5:
                continue
            pa_id = pid
            remaining = int(rng.integers(0, 120))
            created = datetime.combine(ASOF, datetime.min.time()) - timedelta(
                days=int(rng.integers(1, 90))
            )
            inventories_rows.append(
                {
                    "id": inv_id,
                    "product_id": pid,
                    "product_attribute_id": pa_id,
                    "warehouse_id": wh,
                    "remaining_quantity": remaining,
                    "created_at": created,
                    "new_purchase_order_id": int(rng.integers(1, 120)),
                    "production_order_id": 0,
                    "purchase_order_id": int(rng.integers(1, 120)),
                    "inventory_vendor_id": 0,
                }
            )
            margin_rows.append(
                {
                    "inventory_id": inv_id,
                    "purchase_price_inc_ppn": float(int(rng.integers(5, 60)) * 1000),
                }
            )
            inv_id += 1

    return {
        "inventories": pd.DataFrame(inventories_rows),
        "inventory_published": pd.DataFrame(published_rows),
        "margin_costs": pd.DataFrame(margin_rows),
    }


# --------------------------------------------------------------------------- #
# Purchasing / receiving
# --------------------------------------------------------------------------- #


def build_purchasing(rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    """Build purchase_orders/_items/_status_logs and production_orders/_items."""
    po_rows: list[dict] = []
    poi_rows: list[dict] = []
    posl_rows: list[dict] = []

    poi_id = 1
    n_po = 120
    for po_id in range(1, n_po + 1):
        wh = int(rng.choice((*NORMAL_WAREHOUSES, WH_RTP)))
        # status spans the lead-time CTE filter (po_status in (1,2)) and the
        # incoming filter (status > 0 and < 2).
        status = int(rng.choice([1, 2, 3], p=[0.4, 0.4, 0.2]))
        # po_date within the last 6 months.
        po_date = ASOF - timedelta(days=int(rng.integers(0, 180)))
        created = datetime.combine(po_date, datetime.min.time())
        po_rows.append(
            {
                "id": po_id,
                "warehouse_id": wh,
                "status": status,
                "created_at": created,
                "po_code": f"PO-{po_id:05d}",
                "po_date": po_date,
                "supplier_id": int(rng.choice([1, 2, 3])),
                "company_type": rng.choice(["internal", "external"]).item(),
                "purchase_order_payment_id": int(rng.integers(1, 9999)),
            }
        )
        # status log: one row carrying current_status (max across logs).
        posl_rows.append(
            {"purchase_order_id": po_id, "current_status": status}
        )
        # 1-3 line items per PO.
        n_items = int(rng.integers(1, 4))
        chosen = rng.choice(PRODUCT_IDS, size=n_items, replace=False)
        for pid in chosen:
            pid = int(pid)
            qty = int(rng.integers(10, 300))
            # remaining_quantity drives stok_incoming; keep some > 0.
            remaining = int(rng.integers(0, qty + 1))
            poi_rows.append(
                {
                    "id": poi_id,
                    "purchase_order_id": po_id,
                    "product_id": pid,
                    "product_attribute_id": pid,
                    "quantity": qty,
                    "remaining_quantity": remaining,
                    "deleted_at": pd.NaT,
                    "deleted_by": pd.NA,
                }
            )
            poi_id += 1

    # Minimal production orders/items to satisfy the margin UNION ALL branch.
    pro_rows = []
    proi_rows = []
    proi_id = 1
    for pro_id in range(1, 11):
        pro_date = ASOF - timedelta(days=int(rng.integers(0, 180)))
        pro_rows.append(
            {
                "id": pro_id,
                "pro_code": f"PRO-{pro_id:04d}",
                "pro_date": pro_date,
                "supplier_id": int(rng.choice([1, 2, 3])),
            }
        )
        pid = int(rng.choice(PRODUCT_IDS))
        proi_rows.append(
            {
                "id": proi_id,
                "production_order_id": pro_id,
                "product_id": pid,
                "quantity": int(rng.integers(10, 100)),
            }
        )
        proi_id += 1

    return {
        "purchase_orders": pd.DataFrame(po_rows),
        "purchase_order_items": pd.DataFrame(poi_rows),
        "purchase_order_status_logs": pd.DataFrame(posl_rows),
        "production_orders": pd.DataFrame(pro_rows),
        "production_order_items": pd.DataFrame(proi_rows),
    }


# --------------------------------------------------------------------------- #
# Stock-request / pricing
# --------------------------------------------------------------------------- #


def build_stock_requests(rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    """Build stock_requests, product_stocks and product_selling_prices.

    Requests fall inside the last 7 days; ``product_selling_prices`` carries
    ``minimum_quantity = 1`` for the rows the request query keeps.
    """
    ps_rows: list[dict] = []
    psp_rows: list[dict] = []
    sr_rows: list[dict] = []

    ps_id = 1
    psp_id = 1
    # product_stocks: one row per (attribute, warehouse) for normal whs.
    for wh in NORMAL_WAREHOUSES:
        for pid in PRODUCT_IDS:
            pa_id = pid
            ps_rows.append(
                {"id": ps_id, "product_attribute_id": pa_id, "warehouse_id": wh}
            )
            # selling price (round synthetic), minimum_quantity = 1.
            psp_rows.append(
                {
                    "id": psp_id,
                    "product_stock_id": ps_id,
                    "selling_price": float(int(rng.integers(8, 80)) * 1000),
                    "minimum_quantity": 1,
                }
            )
            ps_id += 1
            psp_id += 1

    # ~60 stock requests within the last 7 days.
    n_req = 60
    sr_id = 1
    for _ in range(n_req):
        wh = int(rng.choice(NORMAL_WAREHOUSES))
        pid = int(rng.choice(PRODUCT_IDS))
        pa_id = pid
        created = datetime.combine(ASOF, datetime.min.time()) - timedelta(
            days=int(rng.integers(0, 7)), hours=int(rng.integers(0, 24))
        )
        sr_rows.append(
            {
                "id": sr_id,
                "product_id": pid,
                "customer_id": int(rng.integers(1000, 2000)),
                "product_attribute_id": pa_id,
                "warehouse_id": wh,
                "quantity": int(rng.integers(1, 50)),
                "created_at": created,
            }
        )
        sr_id += 1

    return {
        "stock_requests": pd.DataFrame(sr_rows),
        "product_stocks": pd.DataFrame(ps_rows),
        "product_selling_prices": pd.DataFrame(psp_rows),
    }


# --------------------------------------------------------------------------- #
# Lakehouse-view replacements (turnover + commercial sales fact)
# --------------------------------------------------------------------------- #


def build_turnover_history(rng: np.random.Generator) -> pd.DataFrame:
    """Daily turnover snapshots over the last ~40 days.

    Provides rows for ``period = asof - {1,8,15,22,31}`` (the L7/L14/L21/L30
    snapshot anchors) plus a continuous daily range so the incoming windows
    (`period >= asof - N and period < asof`) all populate and the recur ladder
    runs for warehouses 1/2/3.
    """
    rows: list[dict] = []
    # Cover every (product, warehouse) grain that has activity in normal + RTP.
    grains = [
        (pid, wh)
        for wh in (*NORMAL_WAREHOUSES, WH_RTP)
        for pid in (PRODUCT_IDS if wh in NORMAL_WAREHOUSES else RTP_PRODUCT_IDS)
    ]
    for pid, wh in grains:
        # A smooth-ish stock-value baseline per grain so TOR is finite & varied.
        base_value = float(int(rng.integers(100, 1000)) * 1000)
        for d in range(TURNOVER_HISTORY_DAYS + 1):
            period = ASOF - timedelta(days=d)
            # stock value drifts day to day.
            stock_value = base_value * (1.0 + 0.01 * float(rng.standard_normal()))
            # incoming components: mostly PO, occasional returns/transfers.
            sum_value_po = (
                float(int(rng.integers(0, 50)) * 1000) if rng.random() < 0.4 else 0.0
            )
            rows.append(
                {
                    "product_id": pid,
                    "warehouse_id": wh,
                    "period": period,
                    "stock_value": round(stock_value, 2),
                    "sum_value_po": round(sum_value_po, 2),
                    "sum_value_retur": 0.0,
                    "sum_value_retur_vendor": 0.0,
                    "sum_value_transfer": (
                        float(int(rng.integers(0, 10)) * 1000)
                        if rng.random() < 0.1
                        else 0.0
                    ),
                    "sum_value_po_vendor": 0.0,
                }
            )
    return pd.DataFrame(rows)


def build_sales_history(
    rng: np.random.Generator,
    aging_rows: list[dict],
) -> pd.DataFrame:
    """Commercial sell-out fact for the aging report's last-7-day join.

    Order dates fall inside the last 7 days; ``order_item_type`` is a mix of
    'regular' and 'reward' (reward is excluded by the aging query). The aging
    sell-out merge keys on ``(product_id, warehouse_name)`` (the original
    ``df_aging_stock.merge(df_revenue, on=['product_id','warehouse_name'])``),
    so we emit sell-out rows for a subset of the *actual* cohort grains in
    normal warehouses, guaranteeing a non-empty merge for kept rows. Each such
    grain also gets a 'reward' row that the aging query must exclude.
    """
    wh_name_by_id = {wid: name for wid, name, _ in WAREHOUSES}
    rows: list[dict] = []

    # Distinct (product_id, warehouse_name) grains in normal warehouses that the
    # cohort actually contains.
    cohort_grains = sorted(
        {
            (int(r["product_id"]), str(r["warehouse_name"]))
            for r in aging_rows
            if "Consignment" not in str(r["warehouse_name"])
        }
    )
    # Give sell-out to roughly every other grain (regular + a reward decoy).
    for i, (pid, wh_name) in enumerate(cohort_grains):
        if i % 2 != 0:
            continue
        rows.append(
            {
                "product_id": pid,
                "product_name": f"Product {pid}",
                "warehouse_name": wh_name,
                "order_date": ASOF - timedelta(days=int(rng.integers(0, 7))),
                "quantity": int(rng.integers(1, 30)),
                "gmv": float(int(rng.integers(50, 500)) * 1000),
                "order_item_type": "regular",
            }
        )
        # A reward row (must be excluded by the aging query) for the same grain.
        rows.append(
            {
                "product_id": pid,
                "product_name": f"Product {pid}",
                "warehouse_name": wh_name,
                "order_date": ASOF - timedelta(days=int(rng.integers(0, 7))),
                "quantity": int(rng.integers(1, 10)),
                "gmv": float(int(rng.integers(10, 100)) * 1000),
                "order_item_type": "reward",
            }
        )

    # A little extra general sell-out noise over the broader product set.
    for _ in range(40):
        pid = int(rng.choice(PRODUCT_IDS))
        wh = int(rng.choice(NORMAL_WAREHOUSES))
        rows.append(
            {
                "product_id": pid,
                "product_name": f"Product {pid}",
                "warehouse_name": wh_name_by_id[wh],
                "order_date": ASOF - timedelta(days=int(rng.integers(0, 7))),
                "quantity": int(rng.integers(1, 40)),
                "gmv": float(int(rng.integers(50, 800)) * 1000),
                "order_item_type": "regular",
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Committed CSV seeds
# --------------------------------------------------------------------------- #


def write_product_status_csv(rng: np.random.Generator, data_dir: Path) -> None:
    """Write data/product_status.csv (replaces the 'Product Status' sheet).

    ~20 rows covering a subset of grains in normal warehouses 1/2/3, with
    obviously-fake PIC tokens (Buyer-A / Buyer-B) and manual lead-time overrides.
    """
    rows: list[dict] = []
    # Cover a deterministic subset: first ~7 products in each normal warehouse.
    pids = PRODUCT_IDS[:7]
    priorities = ["High", "Medium", "Low", ""]
    for wh in NORMAL_WAREHOUSES:
        for i, pid in enumerate(pids):
            rows.append(
                {
                    "product_id": pid,
                    "product_attribute_id": pid,
                    "warehouse_id": wh,
                    "status": int(rng.choice([0, 1], p=[0.2, 0.8])),
                    "adj_lead_time": int(rng.integers(2, 8)),
                    "PIC": "Buyer-A" if (i + wh) % 2 == 0 else "Buyer-B",
                    "label_priority": priorities[i % len(priorities)],
                    "ragu_nonaktif": int(rng.choice([0, 1], p=[0.85, 0.15])),
                }
            )
    df = pd.DataFrame(
        rows,
        columns=[
            "product_id",
            "product_attribute_id",
            "warehouse_id",
            "status",
            "adj_lead_time",
            "PIC",
            "label_priority",
            "ragu_nonaktif",
        ],
    )
    df.to_csv(data_dir / "product_status.csv", index=False)


def build_aging_cohort_rows(rng: np.random.Generator) -> tuple[list[dict], list[int]]:
    """Build the aging cohort rows and the list of product ids they reference.

    18 explicit rows over the RTP cohort, spanning ``diff_days_inhouse`` values
    that cross both the Daily-Needs (>=15) and Lifestyle (>=31) thresholds. The
    set is constructed deterministically so the downstream aging stage has, for
    each category, a robust group of kept rows plus boundary/filter cases:

    * Daily-Needs keepers: Staples products at age >= 15, ``WL`` status, normal
      warehouse (several, so the per-(product,warehouse,category) aggregation
      sums non-trivially).
    * Lifestyle keepers: Apparel products at age >= 31, ``WL`` status, normal
      warehouse (several).
    * Boundary droppers: a Daily-Needs row at age 14 and a Lifestyle row at
      age 30 (just under their thresholds).
    * Filter droppers: a ``Consignment DC`` row (NOT LIKE filter) and a non-WL
      ``Reguler`` row (status_wl LIKE 'WL%' filter).

    Every ``product_id`` here also exists in ``product_rtp``. Daily-Needs maps to
    Staples products; Lifestyle maps to the Apparel products. See
    ``build_product_rtp``: index ``i % 3 == 2`` -> Lifestyle/Apparel.
    """
    # Resolve the category split exactly as build_product_rtp assigns it.
    staples_pids = [pid for i, pid in enumerate(RTP_PRODUCT_IDS) if i % 3 != 2]
    lifestyle_pids = [pid for i, pid in enumerate(RTP_PRODUCT_IDS) if i % 3 == 2]

    unit_by_pid = {
        pid: UNITS[(pid - PRODUCT_ID_START) % len(UNITS)] for pid in PRODUCT_IDS
    }

    # (product_id, warehouse_name, diff_days_inhouse, status_wl) — explicit so the
    # kept/dropped outcome of every row is obvious and stable.
    specs: list[tuple[int, str, int, str]] = []

    # --- Daily-Needs keepers (Staples, age >= 15, WL, normal warehouse) -------
    dn_keep_specs = [
        (staples_pids[0], "North DC", 15, "WL"),   # boundary keeper (==15)
        (staples_pids[0], "North DC", 22, "WL"),   # same grain, second lot
        (staples_pids[1], "South DC", 18, "WL-A"),
        (staples_pids[2], "Central DC", 35, "WL"),
        (staples_pids[3], "South DC", 45, "WL"),
        (staples_pids[4], "North DC", 60, "WL"),
        (staples_pids[5], "Central DC", 28, "WL"),
    ]
    specs.extend(dn_keep_specs)

    # --- Daily-Needs droppers -------------------------------------------------
    specs.append((staples_pids[6], "South DC", 14, "WL"))          # under threshold
    specs.append((staples_pids[7], "Consignment DC", 40, "WL"))    # excluded warehouse
    specs.append((staples_pids[1], "North DC", 33, "Reguler"))     # non-WL status

    # --- Lifestyle keepers (Apparel, age >= 31, WL, normal warehouse) ---------
    ls_keep_specs = [
        (lifestyle_pids[0], "Central DC", 31, "WL"),   # boundary keeper (==31)
        (lifestyle_pids[0], "Central DC", 45, "WL"),   # same grain, second lot
        (lifestyle_pids[1], "North DC", 33, "WL"),
        (lifestyle_pids[2], "South DC", 50, "WL-A"),
        (lifestyle_pids[3], "Central DC", 38, "WL"),
    ]
    specs.extend(ls_keep_specs)

    # --- Lifestyle droppers ---------------------------------------------------
    specs.append((lifestyle_pids[1], "South DC", 30, "WL"))        # under threshold
    specs.append((lifestyle_pids[2], "Consignment DC", 60, "WL"))  # excluded warehouse

    rows: list[dict] = []
    used_pids: list[int] = []
    for pid, wh_name, age, status_wl in specs:
        unit = unit_by_pid[pid]
        rows.append(
            {
                "product_id": pid,
                "product_unit": f"{_sku(pid)} ({unit})",
                "warehouse_name": wh_name,
                "diff_days_inhouse": int(age),
                "stok_gudang_tanpa_booking": float(int(rng.integers(10, 200))),
                "total_purchase_stok_tanpa_booking": float(
                    int(rng.integers(50, 900)) * 1000
                ),
                "status_wl": status_wl,
            }
        )
        used_pids.append(pid)
    return rows, sorted(set(used_pids))


def write_aging_cohort_csv(rows: list[dict], data_dir: Path) -> None:
    """Write data/aging_cohort.csv (replaces the external BI aging view)."""
    df = pd.DataFrame(
        rows,
        columns=[
            "product_id",
            "product_unit",
            "warehouse_name",
            "diff_days_inhouse",
            "stok_gudang_tanpa_booking",
            "total_purchase_stok_tanpa_booking",
            "status_wl",
        ],
    )
    df.to_csv(data_dir / "aging_cohort.csv", index=False)


# --------------------------------------------------------------------------- #
# DuckDB load
# --------------------------------------------------------------------------- #


def load_into_duckdb(db_path: Path, tables: dict[str, pd.DataFrame]) -> None:
    """Create (or replace) every table in the DuckDB database file.

    Uses ``CREATE OR REPLACE TABLE ... AS SELECT * FROM <registered frame>`` so
    the seeder is idempotent.
    """
    con = duckdb.connect(str(db_path))
    try:
        for name, frame in tables.items():
            con.register("_seed_frame", frame)
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _seed_frame")
            con.unregister("_seed_frame")
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def generate(db_path: str = "stocklens.duckdb", data_dir: str = "data") -> Path:
    """Generate the full synthetic database and committed CSV seeds.

    Returns the path to the written DuckDB file.
    """
    rng = np.random.default_rng(SEED)
    db = Path(db_path)
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)

    dims = build_dimensions(rng)
    product_rtp = build_product_rtp(rng)
    sales = build_orders(rng, dims["products"], dims["product_attributes"])
    inventory = build_inventory(rng, sales["order_logs"])
    purchasing = build_purchasing(rng)
    requests = build_stock_requests(rng)
    turnover_history = build_turnover_history(rng)

    # Aging cohort first (defines which grains need sell-out rows).
    aging_rows, _aging_pids = build_aging_cohort_rows(rng)
    sales_history = build_sales_history(rng, aging_rows)

    tables: dict[str, pd.DataFrame] = {
        **dims,
        "product_rtp": product_rtp,
        **sales,
        **inventory,
        **purchasing,
        **requests,
        "turnover_history": turnover_history,
        "sales_history": sales_history,
    }

    load_into_duckdb(db, tables)

    # Committed CSV seeds.
    write_product_status_csv(rng, data)
    write_aging_cohort_csv(aging_rows, data)

    return db


def _summary(db_path: Path) -> str:
    """Return a one-line row-count summary of the generated database."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        names = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
        counts = []
        for n in names:
            c = con.execute(f"SELECT count(*) FROM {n}").fetchone()[0]
            counts.append(f"{n}={c}")
        return ", ".join(counts)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the StockLens synthetic DuckDB.")
    parser.add_argument("--db", default="stocklens.duckdb", help="output DuckDB path")
    parser.add_argument("--data-dir", default="data", help="committed CSV output dir")
    args = parser.parse_args()

    db = generate(args.db, args.data_dir)
    print(f"Seeded {db} :: {_summary(db)}")


if __name__ == "__main__":
    main()
