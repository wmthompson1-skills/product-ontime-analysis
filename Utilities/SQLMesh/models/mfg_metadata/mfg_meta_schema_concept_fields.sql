MODEL (
  name mfg_metadata.schema_concept_fields,
  kind SEED (
    path '$root/seeds/schema_concept_fields.csv'
  ),
  columns (
    id INTEGER,
    table_name TEXT,
    field_name TEXT,
    concept_id INTEGER,
    is_primary_meaning INTEGER,
    context_hint TEXT,
    component_index INTEGER,
    created_at TIMESTAMP,
    variable_name TEXT
  ),
  grain (id)
);
