MODEL (
  name mfg_data.receivable_line,
  kind SEED (
    path '$root/seeds/receivable_line.csv'
  ),
  columns (
    receivable_line_id INTEGER,
    invoice_id INTEGER,
    line_no INTEGER,
    order_id TEXT,
    part_id TEXT,
    qty DOUBLE,
    amount DOUBLE,
    order_line_id INTEGER,
    shipment_line_id INTEGER,
    created_at TIMESTAMP
  ),
  grain (receivable_line_id)
);
