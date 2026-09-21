MODEL (
  name mfg_data.trace,
  kind SEED (
    path '$root/seeds/trace.csv'
  ),
  columns (
    trace_id INTEGER,
    part_id TEXT,
    lot_id TEXT,
    serial_id TEXT,
    in_qty DOUBLE,
    out_qty DOUBLE,
    production_date DATE,
    expiration_date DATE,
    site_id TEXT,
    created_at TIMESTAMP
  ),
  grain (trace_id)
);
