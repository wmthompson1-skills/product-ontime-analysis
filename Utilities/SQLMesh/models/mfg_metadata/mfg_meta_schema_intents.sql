MODEL (
  name mfg_metadata.schema_intents,
  kind SEED (
    path '$root/seeds/schema_intents.csv'
  ),
  columns (
    intent_id INTEGER,
    intent_name TEXT,
    intent_category TEXT,
    description TEXT,
    typical_question TEXT,
    primary_binding_key TEXT,
    created_at TIMESTAMP
  ),
  grain (intent_id)
);
