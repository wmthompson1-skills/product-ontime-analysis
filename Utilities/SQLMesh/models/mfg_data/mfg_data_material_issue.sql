MODEL (
  name mfg_data.material_issue,
  kind SEED (
    path '$root/seeds/material_issue.csv'
  ),
  columns (
    issue_id INTEGER,
    wo_id TEXT,
    part_id TEXT,
    part_description TEXT,
    quantity DOUBLE,
    unit_cost DOUBLE,
    total_cost DOUBLE,
    issue_date DATE,
    issued_by TEXT,
    created_at TIMESTAMP
  ),
  grain (issue_id)
);
