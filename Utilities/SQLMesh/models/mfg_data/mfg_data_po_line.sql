MODEL (
  name mfg_data.po_line,
  kind SEED (
    path '$root/seeds/po_line.csv'
  ),
  columns (
    line_id INTEGER,
    po_id TEXT,
    part_id TEXT,
    part_description TEXT,
    quantity DOUBLE,
    unit_cost DOUBLE,
    line_total DOUBLE,
    created_at TIMESTAMP
  ),
  grain (line_id)
);
