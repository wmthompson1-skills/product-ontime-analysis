MODEL (
  name mfg_data.payables,
  kind SEED (
    path '$root/seeds/payables.csv'
  ),
  columns (
    invoice_id INTEGER,
    po_id TEXT,
    supplier_id TEXT,
    invoice_number TEXT,
    invoice_date DATE,
    due_date DATE,
    amount_dollars DOUBLE,
    status TEXT,
    payment_date DATE,
    three_way_match_status TEXT,
    created_at TIMESTAMP
  ),
  grain (invoice_id)
);
