-- stocks.sql — per-grain multi-source stock position (StockLens, DuckDB)
--
-- Sanitized clean-room port of consolidate_purchasing.py In[39] (`query_stocks`).
-- All schema prefixes stripped; tables are unqualified synthetic names (see BUILD-CONTRACT §1).
-- Grain key throughout: (warehouse_id, product_id, product_attribute_id).
--
-- Parameters (passed positionally by stock_position.load_stocks):
--   $premium_tag_id  -> the synthetic "Premium" product_tag id (config classification.premium_tag_id)
--   $po_lookback     -> incoming/cycle-time PO window in months (config windows.po_lookback_months)
--   $special_like    -> warehouse-name LIKE token for the "Exclusivity" divider rule (e.g. '%RTP DC%')
--
-- Assembles five stock buckets (belum_rilis / rilis_* / booking / incoming), lead time and
-- cycle time, plus the `divider` segmentation CASE, over the products x warehouses universe.

with _warehouses as (
    select id as warehouse_id, name as warehouse_name
    from warehouses
),
_products as (
    select distinct
        p.id as product_id,
        pa.id as product_attribute_id,
        pa.position,
        p.sku,
        p.name as product_name,
        pa.unit,
        p.status as product_status,
        pa.status as product_attribute_status,
        p.category_id,
        c.name as category_name,
        p.brand_id,
        b.name as brand_name
    from products p
    left join product_attributes pa on p.id = pa.product_id
    left join categories c on c.id = p.category_id
    left join brands b on b.id = p.brand_id
),
_wp as (
    select
        w.warehouse_id,
        w.warehouse_name,
        p.product_id,
        p.product_attribute_id,
        p.sku,
        p.product_name,
        p.unit,
        p.position,
        p.product_status,
        p.product_attribute_status,
        p.category_id,
        p.category_name,
        p.brand_id,
        p.brand_name
    from _warehouses w
    cross join _products p
),
_stok_belum_rilis as (
    select
        i.warehouse_id,
        i.product_id,
        i.product_attribute_id,
        sum(i.remaining_quantity) as stok_belum_rilis,
        sum(i.remaining_quantity * mc.purchase_price_inc_ppn) as total_purchase_stok_belum_rilis
    from inventories i
    left join margin_costs mc on mc.inventory_id = i.id
    group by 1, 2, 3
),
_stok_rilis_regular as (
    select
        ip.warehouse_id,
        i.product_id,
        ip.product_attribute_id,
        sum(ip.remaining_quantity) as stok_rilis_regular,
        sum(ip.remaining_quantity * mc.purchase_price_inc_ppn) as total_purchase_stok_rilis_regular
    from inventory_published ip
    left join inventories i
        on ip.inventory_id = i.id
        and ip.product_attribute_id = i.product_attribute_id
    left join margin_costs mc on mc.inventory_id = i.id
    where ip.publish_type = 'regular'
        and ip.group_type = 'grosir'
        and ip.remaining_quantity >= 0
    group by 1, 2, 3
),
_stok_rilis_fs as (
    select
        ip.warehouse_id,
        i.product_id,
        ip.product_attribute_id,
        sum(ip.remaining_quantity) as stok_rilis_flashsale,
        sum(ip.remaining_quantity * mc.purchase_price_inc_ppn) as total_purchase_stok_rilis_flashsale
    from inventory_published ip
    left join inventories i
        on ip.inventory_id = i.id
        and ip.product_attribute_id = i.product_attribute_id
    left join margin_costs mc on mc.inventory_id = i.id
    where ip.publish_type = 'flashsale'
        and ip.group_type = 'grosir'
        and ip.remaining_quantity >= 0
    group by 1, 2, 3
),
_stok_rilis_reward as (
    select
        ip.warehouse_id,
        i.product_id,
        ip.product_attribute_id,
        sum(ip.remaining_quantity) as stok_rilis_reward,
        sum(ip.remaining_quantity * mc.purchase_price_inc_ppn) as total_purchase_stok_rilis_reward
    from inventory_published ip
    left join inventories i
        on ip.inventory_id = i.id
        and ip.product_attribute_id = i.product_attribute_id
    left join margin_costs mc on mc.inventory_id = i.id
    where ip.publish_type = 'reward'
        and ip.group_type = 'grosir'
        and ip.remaining_quantity >= 0
    group by 1, 2, 3
),
_stok_rilis_rtp as (
    select
        ip.warehouse_id,
        i.product_id,
        ip.product_attribute_id,
        sum(ip.remaining_quantity) as stok_rilis_rtp,
        sum(ip.remaining_quantity * mc.purchase_price_inc_ppn) as total_purchase_stok_rilis_rtp
    from inventory_published ip
    left join inventories i
        on ip.inventory_id = i.id
        and ip.product_attribute_id = i.product_attribute_id
    left join margin_costs mc on mc.inventory_id = i.id
    where ip.publish_type = 'rtp'
        and ip.group_type = 'grosir'
        and ip.remaining_quantity >= 0
    group by 1, 2, 3
),
_stok_booking as (
    select
        o.warehouse_id,
        oi.product_id,
        oi.product_attribute_id,
        sum(ol.quantity) * -1 as stok_booking,
        sum(ol.quantity * mc.purchase_price_inc_ppn) * -1 as total_purchase_stok_booking
    from order_items oi
    left join orders o on oi.order_id = o.id
    left join order_logs ol
        on o.id = ol.order_id
        and oi.id = ol.order_item_id
    left join inventory_published ip on ip.id = ol.inventory_publish_id
    left join inventories i
        on ip.inventory_id = i.id
        and ip.product_attribute_id = i.product_attribute_id
    left join margin_costs mc on mc.inventory_id = i.id
    where o.status in (0, 1)
        and oi.deleted_by is null
        and ol.status = 1
        and ol.quantity < 0
        and ol.type = 'order'
    group by 1, 2, 3
),
_incoming_goods as (
    select
        po.warehouse_id,
        poi.product_attribute_id,
        poi.product_id,
        sum(poi.remaining_quantity) as stok_incoming
    from purchase_orders po
    left join purchase_order_items poi on po.id = poi.purchase_order_id
    where po.status > 0 and po.status < 2
        and poi.deleted_by is null
        and poi.quantity > 0
        and date(po.created_at) >= date_add(current_date, to_months(-$po_lookback))
    group by 1, 2, 3
    having sum(poi.remaining_quantity) > 0
),
_cycle_time as (
    select
        ct.product_id,
        ct.product_attribute_id,
        ct.warehouse_id,
        avg(date_diff('day', ct.received_date, ct.last_order)) as cycle_time
    from (
        select
            i.product_id,
            i.product_attribute_id,
            i.warehouse_id,
            i.new_purchase_order_id,
            min(i.created_at) as received_date,
            max(o.created_at) as last_order
        from inventories i
        left join inventory_published ip on i.id = ip.inventory_id
        left join order_logs ol on ip.id = ol.inventory_publish_id
        left join order_items oi on oi.id = ol.order_item_id
        left join orders o on o.id = ol.order_id
        where date(i.created_at) >= date_add(current_date, to_months(-$po_lookback))
            and oi.deleted_by is null
            and i.inventory_vendor_id = 0
            and ol.status = 1
            and ol.quantity < 0
            and ol.type = 'order'
        group by 1, 2, 3, 4
    ) ct
    group by 1, 2, 3
),
_lead_time as (
    select
        w.id as warehouse_id,
        poi.product_id,
        ceil(avg(
            case
                when date_diff('day', date(po.created_at), date(iv.created_at)) <= 0 then 1.00
                else cast(date_diff('day', date(po.created_at), date(iv.created_at)) as double)
            end
        )) as avg_lead_time,
        ceil(max(
            case
                when date_diff('day', date(po.created_at), date(iv.created_at)) <= 0 then 1.00
                else cast(date_diff('day', date(po.created_at), date(iv.created_at)) as double)
            end
        )) as max_lead_time
    from purchase_orders po
    join purchase_order_items poi on po.id = poi.purchase_order_id
    left join warehouses w on w.id = po.warehouse_id
    left join (
        select posl.purchase_order_id, max(posl.current_status) as po_status
        from purchase_order_status_logs posl
        group by 1
    ) pos on pos.purchase_order_id = po.id
    left join inventories iv
        on iv.purchase_order_id = poi.purchase_order_id
        and iv.product_id = poi.product_id
        and iv.product_attribute_id = poi.product_attribute_id
    where poi.deleted_by is null
        and pos.po_status in (1, 2)
        and poi.deleted_at is null
        and poi.quantity > 0
        and po.purchase_order_payment_id is not null
    group by 1, 2
),
sg as (
    select p.id as product_id, ptr.product_tag_id
    from products p
    left join product_tag_relations ptr on ptr.product_id = p.id
    where ptr.product_tag_id = $premium_tag_id
)
select
    wp.warehouse_id,
    wp.warehouse_name,
    wp.product_id,
    wp.product_attribute_id,
    wp.sku,
    wp.product_name,
    wp.category_id,
    wp.category_name,
    wp.brand_id,
    wp.brand_name,
    wp.unit,
    wp.position,
    wp.product_status,
    wp.product_attribute_status,
    case
        when pr.rtp_sub_category notnull then 'Private Label'
        when sg.product_tag_id = $premium_tag_id then 'Premium'
        when wp.warehouse_name like $special_like then 'Exclusivity'
        else 'General Product'
    end as divider,
    sberg.stok_belum_rilis,
    srir.stok_rilis_regular,
    sbor.stok_booking,
    srif.stok_rilis_flashsale,
    srirew.stok_rilis_reward,
    srirtp.stok_rilis_rtp,
    ig.stok_incoming,
    sberg.total_purchase_stok_belum_rilis,
    srir.total_purchase_stok_rilis_regular,
    sbor.total_purchase_stok_booking,
    srif.total_purchase_stok_rilis_flashsale,
    srirew.total_purchase_stok_rilis_reward,
    srirtp.total_purchase_stok_rilis_rtp,
    lt.avg_lead_time,
    lt.max_lead_time,
    ct.cycle_time
from _wp wp
left join product_rtp pr on pr.product_id = wp.product_id
left join _stok_belum_rilis sberg
    on wp.warehouse_id = sberg.warehouse_id
    and wp.product_id = sberg.product_id
    and wp.product_attribute_id = sberg.product_attribute_id
left join _stok_rilis_regular srir
    on wp.warehouse_id = srir.warehouse_id
    and wp.product_id = srir.product_id
    and wp.product_attribute_id = srir.product_attribute_id
left join _stok_rilis_fs srif
    on wp.warehouse_id = srif.warehouse_id
    and wp.product_id = srif.product_id
    and wp.product_attribute_id = srif.product_attribute_id
left join _stok_rilis_reward srirew
    on wp.warehouse_id = srirew.warehouse_id
    and wp.product_id = srirew.product_id
    and wp.product_attribute_id = srirew.product_attribute_id
left join _stok_rilis_rtp srirtp
    on wp.warehouse_id = srirtp.warehouse_id
    and wp.product_id = srirtp.product_id
    and wp.product_attribute_id = srirtp.product_attribute_id
left join _stok_booking sbor
    on wp.warehouse_id = sbor.warehouse_id
    and wp.product_id = sbor.product_id
    and wp.product_attribute_id = sbor.product_attribute_id
left join _incoming_goods ig
    on ig.product_id = wp.product_id
    and ig.product_attribute_id = wp.product_attribute_id
    and ig.warehouse_id = wp.warehouse_id
left join _cycle_time ct
    on ct.product_id = wp.product_id
    and ct.product_attribute_id = wp.product_attribute_id
    and ct.warehouse_id = wp.warehouse_id
left join _lead_time lt
    on lt.product_id = wp.product_id
    and lt.warehouse_id = wp.warehouse_id
left join sg on sg.product_id = wp.product_id
where (
    sberg.stok_belum_rilis is not null
    or srir.stok_rilis_regular is not null
    or srif.stok_rilis_flashsale is not null
    or srirew.stok_rilis_reward is not null
    or srirtp.stok_rilis_rtp is not null
    or sbor.stok_booking is not null
)
