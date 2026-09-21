MODEL (
  name mfg_data.inv_trans_dist,
  kind SEED (
    path '$root/seeds/inv_trans_dist.csv'
  ),
  columns (
    dist_id INTEGER,
    in_trans_id INTEGER,
    out_trans_id INTEGER,
    dist_qty DOUBLE,
    created_at TIMESTAMP
  ),
  grain (dist_id)
);
