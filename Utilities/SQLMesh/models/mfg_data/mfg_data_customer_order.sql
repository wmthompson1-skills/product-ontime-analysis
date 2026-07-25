MODEL (
  name mfg_data.customer_order,
  kind SEED (
    path '$root/seeds/customer_order.csv'
  ),
  columns (
    order_id TEXT,
    customer_name TEXT,
    order_date DATE,
    site_id TEXT,
    status TEXT,
    created_at TIMESTAMP,
    completed_date DATE
  ),
  grain (order_id)
);
