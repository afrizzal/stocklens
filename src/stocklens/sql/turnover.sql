-- turnover.sql — inventory / incoming snapshots for the TOR ladder (StockLens Track B)
--
-- Ports consolidate_purchasing.py query_tor (orig In[103]). Schema prefixes stripped;
-- reads the unqualified synthetic `turnover_history`. DuckDB dialect.
--
-- `fins`/`s7`/`s14`/`s21`/`s30` snapshot stock_value at period = asof - {1,8,15,22,31}.
-- `in7`/`in14`/`in21`/`in30` sum incoming over the rolling window [asof-N, asof).
-- The module computes the per-window tors, caps, and recur_tor ladder from these.
--
-- Parameter (bound once, repeated): :asof (a DATE). Passed positionally; this query
-- has 13 positional placeholders (5 snapshot CTEs + 4 incoming CTEs x 2 bounds),
-- each bound to the same asof anchor in left-to-right order. The module counts the
-- placeholders at runtime so the binding can never drift from this file.
--
-- Emits one row per (product_id, warehouse_id):
--   product_id, warehouse_id, final_inv, l7d_inv, l14d_inv, l21d_inv, l30d_inv,
--   l7d_inc, l14d_inc, l21d_inc, l30d_inc
WITH fins AS (
    SELECT product_id, warehouse_id, sum(stock_value) AS stock_value
    FROM turnover_history
    WHERE period = (CAST(? AS DATE) - INTERVAL 1 DAY)
    GROUP BY 1, 2
),
s7 AS (
    SELECT product_id, warehouse_id, sum(stock_value) AS stock_value
    FROM turnover_history
    WHERE period = (CAST(? AS DATE) - INTERVAL 8 DAY)
    GROUP BY 1, 2
),
s14 AS (
    SELECT product_id, warehouse_id, sum(stock_value) AS stock_value
    FROM turnover_history
    WHERE period = (CAST(? AS DATE) - INTERVAL 15 DAY)
    GROUP BY 1, 2
),
s21 AS (
    SELECT product_id, warehouse_id, sum(stock_value) AS stock_value
    FROM turnover_history
    WHERE period = (CAST(? AS DATE) - INTERVAL 22 DAY)
    GROUP BY 1, 2
),
s30 AS (
    SELECT product_id, warehouse_id, sum(stock_value) AS stock_value
    FROM turnover_history
    WHERE period = (CAST(? AS DATE) - INTERVAL 31 DAY)
    GROUP BY 1, 2
),
in7 AS (
    SELECT product_id, warehouse_id,
        sum(sum_value_po + sum_value_retur + sum_value_retur_vendor
            + sum_value_transfer + sum_value_po_vendor) AS incoming
    FROM turnover_history
    WHERE period >= (CAST(? AS DATE) - INTERVAL 7 DAY)
        AND period < CAST(? AS DATE)
    GROUP BY 1, 2
),
in14 AS (
    SELECT product_id, warehouse_id,
        sum(sum_value_po + sum_value_retur + sum_value_retur_vendor
            + sum_value_transfer + sum_value_po_vendor) AS incoming
    FROM turnover_history
    WHERE period >= (CAST(? AS DATE) - INTERVAL 14 DAY)
        AND period < CAST(? AS DATE)
    GROUP BY 1, 2
),
in21 AS (
    SELECT product_id, warehouse_id,
        sum(sum_value_po + sum_value_retur + sum_value_retur_vendor
            + sum_value_transfer + sum_value_po_vendor) AS incoming
    FROM turnover_history
    WHERE period >= (CAST(? AS DATE) - INTERVAL 21 DAY)
        AND period < CAST(? AS DATE)
    GROUP BY 1, 2
),
in30 AS (
    SELECT product_id, warehouse_id,
        sum(sum_value_po + sum_value_retur + sum_value_retur_vendor
            + sum_value_transfer + sum_value_po_vendor) AS incoming
    FROM turnover_history
    WHERE period >= (CAST(? AS DATE) - INTERVAL 30 DAY)
        AND period < CAST(? AS DATE)
    GROUP BY 1, 2
),
grains AS (
    SELECT DISTINCT product_id, warehouse_id
    FROM turnover_history
)
SELECT
    g.product_id,
    g.warehouse_id,
    round(fins.stock_value) AS final_inv,
    round(s7.stock_value)   AS l7d_inv,
    round(s14.stock_value)  AS l14d_inv,
    round(s21.stock_value)  AS l21d_inv,
    round(s30.stock_value)  AS l30d_inv,
    round(in7.incoming)     AS l7d_inc,
    round(in14.incoming)    AS l14d_inc,
    round(in21.incoming)    AS l21d_inc,
    round(in30.incoming)    AS l30d_inc
FROM grains g
LEFT JOIN fins ON fins.product_id = g.product_id AND fins.warehouse_id = g.warehouse_id
LEFT JOIN s7   ON s7.product_id = g.product_id   AND s7.warehouse_id = g.warehouse_id
LEFT JOIN s14  ON s14.product_id = g.product_id  AND s14.warehouse_id = g.warehouse_id
LEFT JOIN s21  ON s21.product_id = g.product_id  AND s21.warehouse_id = g.warehouse_id
LEFT JOIN s30  ON s30.product_id = g.product_id  AND s30.warehouse_id = g.warehouse_id
LEFT JOIN in7  ON in7.product_id = g.product_id  AND in7.warehouse_id = g.warehouse_id
LEFT JOIN in14 ON in14.product_id = g.product_id AND in14.warehouse_id = g.warehouse_id
LEFT JOIN in21 ON in21.product_id = g.product_id AND in21.warehouse_id = g.warehouse_id
LEFT JOIN in30 ON in30.product_id = g.product_id AND in30.warehouse_id = g.warehouse_id
