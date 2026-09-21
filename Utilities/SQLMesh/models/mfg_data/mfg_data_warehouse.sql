MODEL (
  name mfg_data.warehouse,
  kind SEED (
    path '$root/seeds/warehouse.csv'
  ),
  columns (
    warehouse_id TEXT,
    description TEXT,
    name TEXT,
    site_id TEXT,
    region_id TEXT,
    independent TEXT,
    mrp_exempt TEXT,
    created_at TIMESTAMP
  ),
  grain (warehouse_id)
);
