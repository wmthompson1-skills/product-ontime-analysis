MODEL (
  name mfg_data.payable_line,
  kind SEED (
    path '$root/seeds/payable_line.csv'
  ),
  columns (
    payable_line_id INTEGER,
    invoice_id INTEGER,
    line_no INTEGER,
    po_id TEXT,
    part_id TEXT,
    qty DOUBLE,
    amount DOUBLE,
    created_at TIMESTAMP,
    po_line_id INTEGER,
    receipt_line_id INTEGER
  ),
  grain (payable_line_id)
);
