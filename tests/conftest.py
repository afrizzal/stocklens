"""Shared pytest fixtures for the StockLens (Track B) test-suite.

The tests assert the *worked examples* spelled out in ``docs/planning/BUILD-CONTRACT.md``
§6.4 and ``STOCKLENS_PLAN`` §1-2. Everything here is small, synthetic, and
deterministic — no network, no seeded ``stocklens.duckdb`` file, no live services.

Fixtures provided
-----------------
* :func:`rules` — the real :class:`stocklens.Rules` parsed from
  ``config/rules.toml`` (so the tests exercise the *locked* tunables, not copies).
* :func:`now` — a fixed reference date so age/window math is reproducible.
* :func:`mem_con` — a fresh in-memory DuckDB connection per test.
* :func:`turnover_db` — ``mem_con`` seeded with a tiny ``turnover_history`` that
  reproduces the TOR worked example (and an all-zero grain for the recur fallback).
* :func:`margin_db` — ``mem_con`` seeded with the minimal table set ``sql/margin.sql``
  reads, producing the ``gm_rate`` worked example (gmv 86,000 / cogs 59,000).
* :func:`aging_db` — ``mem_con`` seeded with ``product_rtp`` + ``sales_history`` for
  the aging-threshold / sell-out worked example.

Markers
-------
* ``unit``   — pure-function maths over DataFrames (no DuckDB).
* ``duckdb`` — exercises an in-memory DuckDB connection.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import duckdb
import pytest

from stocklens import Rules, load_rules

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from collections.abc import Iterator


# A fixed "today" so every age / rolling-window computation is reproducible.
FIXED_NOW = date(2026, 2, 1)


def pytest_configure(config: pytest.Config) -> None:
    """Register the suite's custom markers (keeps ``-W error`` / strict runs clean)."""
    config.addinivalue_line("markers", "unit: pure-function maths, no DuckDB")
    config.addinivalue_line("markers", "duckdb: exercises an in-memory DuckDB connection")


@pytest.fixture(scope="session")
def rules() -> Rules:
    """The locked tunables, parsed once from ``config/rules.toml``.

    Using the real loader (rather than a hand-built copy) guarantees the tests
    break loudly if a contract key is renamed or a default is changed.
    """
    return load_rules()


@pytest.fixture()
def now() -> date:
    """A fixed reference date for deterministic age / window math."""
    return FIXED_NOW


@pytest.fixture()
def mem_con() -> Iterator[duckdb.DuckDBPyConnection]:
    """A fresh in-memory DuckDB connection, closed at test teardown."""
    con = duckdb.connect()
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Turnover seed — drives load_turnover to the TOR worked example.
# ---------------------------------------------------------------------------
_TURNOVER_DDL = """
CREATE TABLE turnover_history (
    product_id BIGINT, warehouse_id BIGINT, period DATE, stock_value DOUBLE,
    sum_value_po DOUBLE, sum_value_retur DOUBLE, sum_value_retur_vendor DOUBLE,
    sum_value_transfer DOUBLE, sum_value_po_vendor DOUBLE
)
"""


def _insert_turnover(
    con: duckdb.DuckDBPyConnection,
    *,
    product_id: int,
    warehouse_id: int,
    period: date,
    stock_value: float,
    incoming: float = 0.0,
) -> None:
    """Insert one ``turnover_history`` row (incoming flows through ``sum_value_po``)."""
    con.execute(
        "INSERT INTO turnover_history VALUES (?,?,?,?,?,0,0,0,0)",
        [product_id, warehouse_id, period, stock_value, incoming],
    )


@pytest.fixture()
def turnover_db(
    mem_con: duckdb.DuckDBPyConnection,
    now: date,
) -> duckdb.DuckDBPyConnection:
    """Seed ``turnover_history`` for the TOR worked example.

    Grain 500/wh1 reproduces invStart=1,000,000 / incoming=200,000 / final=600,000
    on the L7D window → ``l7d_tor = 0.75``. The snapshot at ``asof-8`` is the L7D
    ``inv``; the snapshot at ``asof-1`` is ``final``; incoming sits inside the
    ``[asof-7, asof)`` rolling window. Grain 600/wh1 is all-zero so its
    ``recur_tor`` falls through the ladder to the configured fallback (14).
    """
    con = mem_con
    con.execute(_TURNOVER_DDL)

    # Grain 500/wh1: the 0.75 case.
    _insert_turnover(con, product_id=500, warehouse_id=1, period=now - timedelta(days=8),
                     stock_value=1_000_000.0)                          # l7d_inv (inv)
    _insert_turnover(con, product_id=500, warehouse_id=1, period=now - timedelta(days=1),
                     stock_value=600_000.0)                            # final_inv (final)
    _insert_turnover(con, product_id=500, warehouse_id=1, period=now - timedelta(days=3),
                     stock_value=0.0, incoming=200_000.0)              # incoming in L7D window

    # Grain 600/wh1: never-restocked → all windows zero → recur fallback.
    _insert_turnover(con, product_id=600, warehouse_id=1, period=now - timedelta(days=1),
                     stock_value=0.0)
    return con


# ---------------------------------------------------------------------------
# Margin seed — minimal table set sql/margin.sql reads, gm_rate worked example.
# ---------------------------------------------------------------------------
_MARGIN_DDL = (
    "CREATE TABLE orders(id BIGINT, created_at TIMESTAMP, invoice VARCHAR, "
    "warehouse_id BIGINT, superagent_id BIGINT, status INTEGER)",
    "CREATE TABLE order_items(id BIGINT, order_id BIGINT, product_id BIGINT, "
    "product_attribute_id BIGINT, product_name VARCHAR, unit VARCHAR, quantity INTEGER, "
    "deleted_at TIMESTAMP, deleted_by BIGINT)",
    "CREATE TABLE order_logs(order_id BIGINT, order_item_id BIGINT, quantity INTEGER, "
    "status INTEGER, type VARCHAR, inventory_publish_id BIGINT)",
    "CREATE TABLE inventory_published(id BIGINT, inventory_id BIGINT, "
    "product_attribute_id BIGINT, warehouse_id BIGINT, remaining_quantity INTEGER, "
    "publish_type VARCHAR, group_type VARCHAR, quantity INTEGER, created_at TIMESTAMP)",
    "CREATE TABLE inventories(id BIGINT, product_id BIGINT, product_attribute_id BIGINT, "
    "warehouse_id BIGINT, remaining_quantity INTEGER, created_at TIMESTAMP, "
    "new_purchase_order_id BIGINT, production_order_id BIGINT, purchase_order_id BIGINT, "
    "inventory_vendor_id BIGINT)",
    "CREATE TABLE purchase_orders(id BIGINT, warehouse_id BIGINT, status INTEGER, "
    "created_at TIMESTAMP, po_code VARCHAR, po_date DATE, supplier_id BIGINT, "
    "company_type VARCHAR, purchase_order_payment_id BIGINT)",
    "CREATE TABLE purchase_order_items(id BIGINT, purchase_order_id BIGINT, "
    "product_id BIGINT, product_attribute_id BIGINT, quantity INTEGER, "
    "remaining_quantity INTEGER, deleted_at TIMESTAMP, deleted_by BIGINT)",
    "CREATE TABLE suppliers(id BIGINT, name VARCHAR)",
    "CREATE TABLE margin_costs(inventory_id BIGINT, purchase_price_inc_ppn DOUBLE)",
    "CREATE TABLE production_orders(id BIGINT, pro_code VARCHAR, pro_date DATE, "
    "supplier_id BIGINT)",
    "CREATE TABLE production_order_items(id BIGINT, production_order_id BIGINT, "
    "product_id BIGINT, quantity INTEGER)",
    "CREATE TABLE product_attributes(id BIGINT, product_id BIGINT, unit VARCHAR, "
    "position INTEGER, status INTEGER)",
    "CREATE TABLE product_stocks(id BIGINT, product_attribute_id BIGINT, "
    "warehouse_id BIGINT)",
    "CREATE TABLE product_selling_prices(id BIGINT, product_stock_id BIGINT, "
    "selling_price DOUBLE, minimum_quantity INTEGER)",
    "CREATE TABLE warehouses(id BIGINT, name VARCHAR, type VARCHAR)",
)


@pytest.fixture()
def margin_db(
    mem_con: duckdb.DuckDBPyConnection,
) -> duckdb.DuckDBPyConnection:
    """Seed the minimal tables ``sql/margin.sql`` joins for the gm_rate example.

    Two OUT lines for product 500 in North DC:

    * 5 units @ selling 10,000 / purchase 7,000
    * 3 units @ selling 12,000 / purchase 8,000

    → gmv = 86,000; total_margin = 27,000; gm_rate ≈ 0.3140. The two distinct
    selling prices come from two ``product_attribute`` → ``product_stock`` →
    ``product_selling_prices`` chains (one price per stock row).
    """
    con = mem_con
    for ddl in _MARGIN_DDL:
        con.execute(ddl)

    con.execute("INSERT INTO warehouses VALUES (1,'North DC','normal')")
    con.execute(
        "INSERT INTO orders VALUES (1, TIMESTAMP '2026-01-15 10:00', 'INV1', 1, 0, 2)"
    )
    con.execute(
        "INSERT INTO order_items VALUES "
        "(10,1,500,5000,'SKU-0500','pcs',5,NULL,NULL),"
        "(11,1,500,5001,'SKU-0500','pcs',3,NULL,NULL)"
    )
    # order_logs OUT rows are negative; quantity_out = ol.quantity * -1.
    con.execute(
        "INSERT INTO order_logs VALUES (1,10,-5,1,'order',900),(1,11,-3,1,'order',901)"
    )
    con.execute(
        "INSERT INTO inventory_published VALUES "
        "(900,800,5000,1,0,'regular','grosir',0,TIMESTAMP '2026-01-15 10:00'),"
        "(901,801,5001,1,0,'regular','grosir',0,TIMESTAMP '2026-01-15 10:00')"
    )
    con.execute(
        "INSERT INTO inventories VALUES "
        "(800,500,5000,1,0,TIMESTAMP '2026-01-01',700,NULL,NULL,0),"
        "(801,500,5001,1,0,TIMESTAMP '2026-01-01',700,NULL,NULL,0)"
    )
    con.execute(
        "INSERT INTO purchase_orders VALUES "
        "(700,1,1,TIMESTAMP '2026-01-01','PO1',DATE '2026-01-01',1,'x',NULL)"
    )
    con.execute("INSERT INTO purchase_order_items VALUES (1,700,500,5000,8,8,NULL,NULL)")
    con.execute("INSERT INTO suppliers VALUES (1,'Supplier Alpha')")
    # Lot costs: inventory 800 → 7,000 ; inventory 801 → 8,000.
    con.execute("INSERT INTO margin_costs VALUES (800,7000.0),(801,8000.0)")
    con.execute(
        "INSERT INTO product_attributes VALUES (5000,500,'pcs',1,1),(5001,500,'pcs',2,1)"
    )
    con.execute("INSERT INTO product_stocks VALUES (60,5000,1),(61,5001,1)")
    con.execute(
        "INSERT INTO product_selling_prices VALUES (1,60,10000.0,1),(2,61,12000.0,1)"
    )
    return con


# ---------------------------------------------------------------------------
# Aging seed — product_rtp + sales_history for the threshold / sell-out example.
# ---------------------------------------------------------------------------
_AGING_DDL = (
    "CREATE TABLE product_rtp(product_id BIGINT, rtp_category VARCHAR, "
    "rtp_sub_category VARCHAR, status_wl VARCHAR, start_date DATE, end_date DATE)",
    "CREATE TABLE sales_history(product_id BIGINT, product_name VARCHAR, "
    "warehouse_name VARCHAR, order_date DATE, quantity INTEGER, gmv DOUBLE, "
    "order_item_type VARCHAR)",
)


@pytest.fixture()
def aging_db(
    mem_con: duckdb.DuckDBPyConnection,
    now: date,
) -> duckdb.DuckDBPyConnection:
    """Seed ``product_rtp`` + ``sales_history`` for the aging worked example.

    * 201 — ``Staples`` / ``Flour`` → **Daily Needs** (threshold 15d).
    * 202 — ``Lifestyle`` / ``Apparel`` → **Lifestyle** (threshold 31d).
    * 203 — ``Lifestyle`` / ``Flour`` → **Daily Needs** via the sub-category LIKE.

    ``sales_history`` carries one ``regular`` and one ``reward`` sell-out row for 203
    inside the 7-day window so the test proves the ``reward`` rows are excluded.
    """
    con = mem_con
    for ddl in _AGING_DDL:
        con.execute(ddl)

    con.execute(
        "INSERT INTO product_rtp VALUES "
        "(201,'Staples','Flour','WL',DATE '2025-01-01',NULL),"
        "(202,'Lifestyle','Apparel','WL',DATE '2025-01-01',NULL),"
        "(203,'Lifestyle','Flour','WL',DATE '2025-01-01',NULL)"
    )
    sale_day = (now - timedelta(days=2)).isoformat()
    con.execute(
        "INSERT INTO sales_history VALUES "
        f"(203,'SKU-0203','North DC',DATE '{sale_day}',7,70000.0,'regular'),"
        f"(203,'SKU-0203','North DC',DATE '{sale_day}',100,999999.0,'reward')"
    )
    return con
