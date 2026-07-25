MODEL (
  name mfg_data.customer_order_line,
  kind SEED (
    path '$root/seeds/customer_order_line.csv'
  ),
  columns (
    order_line_id INTEGER,
    order_id TEXT,
    line_no INTEGER,
    part_id TEXT,
    site_id TEXT,
    order_qty DOUBLE,
    unit_price DOUBLE,
    need_by_date DATE,
    desired_release_date DATE,
    created_at TIMESTAMP,
    shipped_qty DOUBLE,
    shipped_date DATE
  ),
  grain (order_line_id)
);
