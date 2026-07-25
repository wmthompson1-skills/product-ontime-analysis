MODEL (
  name mfg_data.inventory_transaction,
  kind SEED (
    path '$root/seeds/inventory_transaction.csv'
  ),
  columns (
    transaction_id INTEGER,
    class TEXT,
    type TEXT,
    part_id TEXT,
    wo_id TEXT,
    po_id TEXT,
    site_id TEXT,
    quantity DOUBLE,
    trans_date DATE,
    created_at TIMESTAMP
  ),
  grain (transaction_id)
);
