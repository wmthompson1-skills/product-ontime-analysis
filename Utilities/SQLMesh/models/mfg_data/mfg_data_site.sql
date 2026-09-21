MODEL (
  name mfg_data.site,
  kind SEED (
    path '$root/seeds/site.csv'
  ),
  columns (
    site_id TEXT,
    site_name TEXT,
    region TEXT,
    created_at TIMESTAMP
  ),
  grain (site_id)
);
