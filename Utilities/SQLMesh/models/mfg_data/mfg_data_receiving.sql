MODEL (
  name mfg_data.receiving,
  kind SEED (
    path '$root/seeds/receiving.csv'
  ),
  columns (
    receipt_id INTEGER,
    po_id TEXT,
    supplier_id TEXT,
    part_id TEXT,
    quantity_ordered DOUBLE,
    quantity_received DOUBLE,
    receipt_date DATE,
    inspection_status TEXT,
    cert_required INTEGER,
    created_at TIMESTAMP,
    received_date DATE
  ),
  grain (receipt_id)
);
