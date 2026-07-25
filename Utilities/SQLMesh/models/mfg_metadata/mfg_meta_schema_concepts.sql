MODEL (
  name mfg_metadata.schema_concepts,
  kind SEED (
    path '$root/seeds/schema_concepts.csv'
  ),
  columns (
    concept_id INTEGER,
    concept_name TEXT,
    description TEXT,
    domain TEXT,
    synonyms TEXT,
    tags TEXT,
    parent_concept_id INTEGER,
    created_at TIMESTAMP,
    computation_template TEXT
  ),
  grain (concept_id)
);
