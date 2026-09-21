MODEL (
  name mfg_data.part_location,
  kind SEED (
    path '$root/seeds/part_location.csv'
  ),
  columns (
    part_location_id INTEGER,
    part_id TEXT,
    warehouse_id TEXT,
    location_id TEXT,
    description TEXT,
    qty DOUBLE,
    committed_qty DOUBLE,
    status TEXT,
    last_count_date DATE,
    created_at TIMESTAMP
  ),
  grain (part_location_id)
);
