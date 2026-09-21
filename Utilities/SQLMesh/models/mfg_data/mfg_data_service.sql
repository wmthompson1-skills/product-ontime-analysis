MODEL (
  name mfg_data.service,
  kind SEED (
    path '$root/seeds/service.csv'
  ),
  columns (
    service_id TEXT,
    description TEXT,
    default_vendor TEXT,
    base_charge DOUBLE,
    created_at TIMESTAMP
  ),
  grain (service_id)
);
