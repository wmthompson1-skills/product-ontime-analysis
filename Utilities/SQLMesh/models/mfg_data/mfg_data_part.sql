MODEL (
  name mfg_data.part,
  kind SEED (
    path '$root/seeds/part.csv'
  ),
  columns (
    part_id TEXT,
    part_description TEXT,
    part_class TEXT,
    unit_of_measure TEXT,
    unit_cost DOUBLE,
    lead_time_days INTEGER,
    reorder_point DOUBLE,
    on_hand_qty DOUBLE,
    revision TEXT,
    cage_code TEXT,
    drawing_number TEXT,
    material_spec TEXT,
    planner_code TEXT,
    buyer_code TEXT,
    active INTEGER,
    created_at TIMESTAMP,
    commodity_code TEXT,
    safety_stock DOUBLE
  ),
  grain (part_id)
);
