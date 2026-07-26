MODEL (
  name mfg_metadata.schema_perspectives,
  kind SEED (
    path '$root/seeds/schema_perspectives.csv'
  ),
  columns (
    perspective_id INTEGER,
    perspective_name TEXT,
    description TEXT,
    stakeholder_role TEXT,
    priority_focus TEXT,
    created_at TIMESTAMP
  ),
  grain (perspective_id)
);
