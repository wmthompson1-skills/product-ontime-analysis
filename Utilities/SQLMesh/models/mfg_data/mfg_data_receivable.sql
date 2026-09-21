MODEL (
  name mfg_data.receivable,
  kind SEED (
    path '$root/seeds/receivable.csv'
  ),
  columns (
    invoice_id INTEGER,
    order_id TEXT,
    customer_name TEXT,
    invoice_number TEXT,
    invoice_date DATE,
    due_date DATE,
    amount_dollars DOUBLE,
    status TEXT,
    payment_date DATE,
    created_at TIMESTAMP
  ),
  grain (invoice_id)
);
