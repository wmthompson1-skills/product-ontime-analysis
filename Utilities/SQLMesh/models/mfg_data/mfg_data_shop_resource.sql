MODEL (
  name mfg_data.shop_resource,
  kind SEED (
    path '$root/seeds/shop_resource.csv'
  ),
  columns (
    resource_id TEXT,
    description TEXT,
    resource_type TEXT,
    run_cost_per_hr DOUBLE,
    bur_per_hr_run DOUBLE,
    active INTEGER,
    created_at TIMESTAMP
  ),
  grain (resource_id)
);
