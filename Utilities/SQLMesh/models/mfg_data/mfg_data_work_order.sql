MODEL (
  name mfg_data.work_order,
  kind SEED (
    path '$root/seeds/work_order.csv'
  ),
  columns (
    wo_id TEXT,
    workorder_type TEXT,
    part_id TEXT,
    part_description TEXT,
    quantity DOUBLE,
    status TEXT,
    open_date DATE,
    close_date DATE,
    required_date DATE,
    routing_template TEXT,
    act_lab_cost DOUBLE,
    act_bur_cost DOUBLE,
    act_ser_cost DOUBLE,
    act_mat_cost DOUBLE,
    desired_rls_date TIMESTAMP,
    sched_start_date TIMESTAMP,
    sched_finish_date TIMESTAMP,
    outside_service INTEGER,
    service_date DATE,
    vendor_id TEXT,
    site_id TEXT,
    created_at TIMESTAMP,
    demand_order_line_id INTEGER
  ),
  grain (wo_id)
);
