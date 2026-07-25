MODEL (
  name mfg_metadata.ground_truth_table_usage,
  kind SEED (
    path '$root/seeds/ground_truth_table_usage.csv'
  ),
  columns (
    id INTEGER,
    query_file TEXT,
    category_id TEXT,
    query_name TEXT,
    table_name TEXT,
    reference_count INTEGER,
    select_count INTEGER,
    created_at TEXT
  ),
  grain (id)
);
