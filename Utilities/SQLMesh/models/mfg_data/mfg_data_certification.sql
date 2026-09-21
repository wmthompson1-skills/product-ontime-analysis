MODEL (
  name mfg_data.certification,
  kind SEED (
    path '$root/seeds/certification.csv'
  ),
  columns (
    cert_id INTEGER,
    receipt_id INTEGER,
    part_id TEXT,
    supplier_id TEXT,
    cert_type TEXT,
    issued_date DATE,
    expiry_date DATE,
    status TEXT,
    created_at TIMESTAMP
  ),
  grain (cert_id)
);
