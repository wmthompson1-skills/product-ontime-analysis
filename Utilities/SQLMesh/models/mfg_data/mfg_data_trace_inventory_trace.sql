MODEL (
  name mfg_data.trace_inventory_trace,
  kind SEED (
    path '$root/seeds/trace_inventory_trace.csv'
  ),
  columns (
    trace_inv_id INTEGER,
    part_id TEXT,
    trace_id INTEGER,
    transaction_id INTEGER,
    qty DOUBLE,
    created_at TIMESTAMP
  ),
  grain (trace_inv_id)
);
