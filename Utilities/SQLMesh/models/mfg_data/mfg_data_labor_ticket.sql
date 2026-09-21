MODEL (
  name mfg_data.labor_ticket,
  kind SEED (
    path '$root/seeds/labor_ticket.csv'
  ),
  columns (
    ticket_id INTEGER,
    wo_id TEXT,
    sequence_no INTEGER,
    employee_id TEXT,
    resource_id TEXT,
    clock_in TIMESTAMP,
    clock_out TIMESTAMP,
    total_hours DOUBLE,
    labor_cost DOUBLE,
    burden_cost DOUBLE,
    created_at TIMESTAMP
  ),
  grain (ticket_id)
);
