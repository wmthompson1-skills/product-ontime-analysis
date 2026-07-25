MODEL (
  name mfg_data.gl_events,
  kind SEED (
    path '$root/seeds/gl_events.csv'
  ),
  columns (
    event_id INTEGER,
    job_id TEXT,
    event_type TEXT,
    amount DOUBLE,
    event_date TIMESTAMP,
    source_table TEXT,
    source_id TEXT
  ),
  grain (event_id)
);
