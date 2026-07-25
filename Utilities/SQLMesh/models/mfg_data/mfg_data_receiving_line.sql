MODEL (
  name mfg_data.receiving_line,
  kind SEED (
    path '$root/seeds/receiving_line.csv'
  ),
  columns (
    receipt_line_id INTEGER,
    receipt_id INTEGER,
    line_no INTEGER,
    po_line_id INTEGER,
    part_id TEXT,
    quantity_ordered DOUBLE,
    quantity_received DOUBLE,
    inspection_status TEXT,
    cert_required INTEGER,
    created_at TIMESTAMP
  ),
  grain (receipt_line_id)
);
