MODEL (
  name mfg_data.EMPLOYEE,
  kind SEED (
    path '$root/seeds/EMPLOYEE.csv'
  ),
  columns (
    employee_id TEXT,
    employee_name TEXT,
    job_title TEXT,
    department TEXT,
    hourly_rate DOUBLE,
    buyer_code TEXT,
    home_resource_id TEXT,
    hire_date DATE,
    active INTEGER,
    created_at TIMESTAMP
  ),
  grain (employee_id)
);
