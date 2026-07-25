MODEL (
  name mfg_data.gl_job_cost_detail,
  kind SEED (
    path '$root/seeds/gl_job_cost_detail.csv'
  ),
  columns (
    line_id INTEGER,
    event_id INTEGER,
    job_id TEXT,
    amount DOUBLE,
    event_type TEXT,
    event_date TIMESTAMP
  ),
  grain (line_id)
);
