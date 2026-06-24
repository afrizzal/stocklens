-- margin.sql — per-OUT-line margin facts (StockLens Track B, synthetic)
--
-- Ports consolidate_purchasing.py query_margin (orig In[92]). Schema prefixes
-- stripped; all tables are unqualified synthetic names. DuckDB dialect.
--
-- Joins orders -> order_items -> order_logs (OUT lines) -> inventory_published,
-- then a `pur` subquery that UNION ALLs the purchase-order lot cost and the
-- production-order lot cost (both carry purchase_price from margin_costs).
-- One row per sold (OUT) line; the module aggregates to gmv / total_margin / gm_rate.
--
-- Parameters (bound positionally as ? in order): :start, :end (date window).
--
-- Emits: order_id, warehouse_id, created_at, invoice, product_name, product_id,
--        unit, selling_price, quantity_out, purchase_price
SELECT
    o.id                       AS order_id,
    w.id                       AS warehouse_id,
    o.created_at               AS created_at,
    o.invoice                  AS invoice,
    oi.product_name            AS product_name,
    oi.product_id              AS product_id,
    oi.unit                    AS unit,
    psp.selling_price          AS selling_price,
    ol.quantity * -1           AS quantity_out,
    pur.purchase_price         AS purchase_price
FROM orders o
LEFT JOIN order_items oi
    ON o.id = oi.order_id
LEFT JOIN product_attributes pa
    ON oi.product_attribute_id = pa.id
LEFT JOIN order_logs ol
    ON o.id = ol.order_id
    AND oi.id = ol.order_item_id
LEFT JOIN inventory_published ip
    ON ip.id = ol.inventory_publish_id
LEFT JOIN warehouses w
    ON w.id = o.warehouse_id
LEFT JOIN product_stocks ps
    ON ps.product_attribute_id = oi.product_attribute_id
    AND ps.warehouse_id = o.warehouse_id
LEFT JOIN product_selling_prices psp
    ON psp.product_stock_id = ps.id
    AND psp.minimum_quantity = 1
LEFT JOIN (
    -- purchase-order lot cost
    SELECT
        i.new_purchase_order_id AS po_id,
        i.id                    AS inventory_id,
        i.product_id            AS product_id,
        mc.purchase_price_inc_ppn AS purchase_price
    FROM inventories i
    LEFT JOIN purchase_orders po
        ON i.new_purchase_order_id = po.id
    LEFT JOIN purchase_order_items poi
        ON po.id = poi.purchase_order_id
        AND i.product_id = poi.product_id
    LEFT JOIN suppliers s
        ON s.id = po.supplier_id
    LEFT JOIN margin_costs mc
        ON mc.inventory_id = i.id
    WHERE po.po_date > DATE '2022-01-01'
    UNION ALL
    -- production-order lot cost
    SELECT
        i.production_order_id   AS po_id,
        i.id                    AS inventory_id,
        i.product_id            AS product_id,
        mc.purchase_price_inc_ppn AS purchase_price
    FROM inventories i
    LEFT JOIN production_orders po
        ON i.production_order_id = po.id
    LEFT JOIN production_order_items poi
        ON po.id = poi.production_order_id
        AND i.product_id = poi.product_id
    LEFT JOIN suppliers s
        ON s.id = po.supplier_id
    LEFT JOIN margin_costs mc
        ON mc.inventory_id = i.id
    WHERE po.pro_date > DATE '2022-01-01'
) pur
    ON pur.inventory_id = ip.inventory_id
WHERE o.status > 0
    AND ol.type = 'order'
    AND ol.status > 0
    AND date(o.created_at) BETWEEN ? AND ?
