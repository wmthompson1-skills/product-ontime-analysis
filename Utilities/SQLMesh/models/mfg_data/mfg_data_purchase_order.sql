MODEL (
  name mfg_data.purchase_order,
  kind SEED (
    path '$root/seeds/purchase_order.csv'
  ),
  columns (
    po_id TEXT,
    supplier_id TEXT,
    po_type TEXT,
    po_date DATE,
    required_date DATE,
    status TEXT,
    total_cost DOUBLE,
    wo_id TEXT,
    service_id TEXT,
    buyer_id TEXT,
    site_id TEXT,
    created_at TIMESTAMP
  ),
  grain (po_id)
);
