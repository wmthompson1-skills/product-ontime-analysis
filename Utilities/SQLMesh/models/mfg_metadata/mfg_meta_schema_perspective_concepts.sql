MODEL (
  name mfg_metadata.schema_perspective_concepts,
  kind SEED (
    path '$root/seeds/schema_perspective_concepts.csv'
  ),
  columns (
    id INTEGER,
    perspective_id INTEGER,
    concept_id INTEGER,
    relationship_type TEXT,
    priority_weight DOUBLE,
    created_at TIMESTAMP
  ),
  grain (id)
);
