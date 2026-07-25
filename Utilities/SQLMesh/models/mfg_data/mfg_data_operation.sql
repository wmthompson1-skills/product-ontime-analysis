MODEL (
  name mfg_data.operation,
  kind SEED (
    path '$root/seeds/operation.csv'
  ),
  columns (
    rowid_pk INTEGER,
    wo_id TEXT,
    workorder_type TEXT,
    sequence_no INTEGER,
    resource_id TEXT,
    service_id TEXT,
    vendor_id TEXT,
    run_type TEXT,
    setup_hrs DOUBLE,
    run_hrs DOUBLE,
    act_setup_hrs DOUBLE,
    act_run_hrs DOUBLE,
    est_atl_lab_cost DOUBLE,
    est_atl_bur_cost DOUBLE,
    est_atl_ser_cost DOUBLE,
    act_atl_lab_cost DOUBLE,
    act_atl_bur_cost DOUBLE,
    act_atl_ser_cost DOUBLE,
    status TEXT,
    sched_start_date TIMESTAMP,
    sched_finish_date TIMESTAMP,
    service_begin_date TIMESTAMP,
    close_date TIMESTAMP,
    last_disp_date TIMESTAMP,
    last_recv_date TIMESTAMP,
    operation_type_id TEXT
  ),
  grain (rowid_pk)
);
