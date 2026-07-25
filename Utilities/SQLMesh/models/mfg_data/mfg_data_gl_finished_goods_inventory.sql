MODEL (
  name mfg_data.gl_finished_goods_inventory,
  kind SEED (
    path '$root/seeds/gl_finished_goods_inventory.csv'
  ),
  columns (
    line_id INTEGER,
    event_id INTEGER,
    job_id TEXT,
    part_id TEXT,
    amount DOUBLE,
    event_type TEXT,
    event_date TIMESTAMP
  ),
  grain (line_id)
);
