MODEL (
  name mfg_metadata.schema_intent_concepts,
  kind SEED (
    path '$root/seeds/schema_intent_concepts.csv'
  ),
  columns (
    id INTEGER,
    intent_id INTEGER,
    concept_id INTEGER,
    intent_factor_weight DOUBLE,
    explanation TEXT,
    created_at TIMESTAMP
  ),
  grain (id)
);
