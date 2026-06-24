-- orders.sql — sales-order pull for demand classification (StockLens Track B, synthetic)
--
-- Ported from the production `query_sql_orders` (consolidate_purchasing.py In[9]).
-- Sanitized: schema prefixes stripped, proprietary brand/tag tokens replaced with
-- synthetic equivalents. Runs against the unqualified DuckDB tables seeded by
-- seed/generate.py.
--
-- DuckDB dialect: date(...), current_date, LIKE, named ($name) bind parameters so the
-- calling module passes the date window in (no live "today" function in the SQL).
--
-- Parameters (DuckDB named placeholders, supplied by demand_classify.load_orders):
--   $premium_tag_id  -> classification.premium_tag_id (synthetic premium-tag id)
--   $start           -> now - windows.sales_lookback_days (a DATE)
--   $end             -> now (a DATE)
--
-- product_tag CASE (synthetic): premium tag -> 'Premium'; RTP white-list flag
--   (status_wl LIKE '%WL%') -> 'RTP'; else 'Reguler'.
-- filter_mandiri CASE: Premium / RTP / superagent_id = 0 (own/mandiri) -> 'Include',
--   else 'Exclude' (the module keeps only 'Include').
--
-- One row per order-item line in window. Downstream the module applies the
-- status > 1, exclude-warehouse and Include filters, then the velocity/IQR math.

SELECT
    date(o.created_at)                          AS order_date,
    o.id                                        AS order_id,
    o.invoice                                   AS invoice,
    oi.id                                       AS order_item_id,
    oi.product_name                             AS product_name,
    CASE
        WHEN pp.product_tag_id = $premium_tag_id THEN 'Premium'
        WHEN pr.status_wl LIKE '%WL%'            THEN 'RTP'
        ELSE 'Reguler'
    END                                         AS product_tag,
    CASE
        WHEN pp.product_tag_id = $premium_tag_id THEN 'Include'
        WHEN pr.status_wl LIKE '%WL%'            THEN 'Include'
        WHEN o.superagent_id = 0                 THEN 'Include'
        ELSE 'Exclude'
    END                                         AS filter_mandiri,
    o.superagent_id                             AS superagent_id,
    oi.product_id                               AS product_id,
    oi.product_attribute_id                     AS product_attribute_id,
    oi.unit                                     AS unit,
    oi.quantity                                 AS qty_sales,
    o.status                                    AS status,
    o.warehouse_id                              AS warehouse_id,
    w.name                                      AS warehouse_name
FROM order_items oi
LEFT JOIN orders o
    ON o.id = oi.order_id
LEFT JOIN warehouses w
    ON w.id = o.warehouse_id
LEFT JOIN products p
    ON p.id = oi.product_id
-- Premium-tag flag: keep only the synthetic premium tag relation.
LEFT JOIN (
    SELECT ptr.product_id, ptr.product_tag_id
    FROM product_tag_relations ptr
    WHERE ptr.product_tag_id = $premium_tag_id
) pp
    ON pp.product_id = oi.product_id
-- RTP white-list window: the tag applies only while the order date sits inside
-- the product's [start_date, end_date] (open-ended end_date -> today).
LEFT JOIN product_rtp pr
    ON pr.product_id = oi.product_id
    AND date(o.created_at) >= pr.start_date
    AND CASE
            WHEN pr.end_date IS NULL THEN date(o.created_at) <= current_date
            ELSE date(o.created_at) <= pr.end_date
        END
-- Signed OUT log line confirms the item actually shipped (qty < 0, status = 1).
LEFT JOIN order_logs ol
    ON ol.order_id = oi.order_id
    AND ol.order_item_id = oi.id
    AND ol.quantity < 0
    AND ol.status = 1
WHERE o.status > -1
    AND oi.deleted_by IS NULL
    AND oi.deleted_at IS NULL
    AND oi.quantity > 0
    AND date(o.created_at) BETWEEN $start AND $end
