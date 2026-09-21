MODEL (
  name mfg_data.requirement,
  kind SEED (
    path '$root/seeds/requirement.csv'
  ),
  columns (
    requirement_id TEXT,
    component_type TEXT,
    component_id TEXT,
    requirement_level TEXT,
    requirement_type TEXT,
    operation_seq INTEGER,
    operation_rowid INTEGER,
    material_part_id TEXT,
    std_qty DOUBLE,
    actual_qty DOUBLE,
    unit_cost DOUBLE,
    extended_cost DOUBLE,
    created_at TIMESTAMP
  ),
  grain (requirement_id)
);
