MODEL (
  name mfg_data.suppliers,
  kind SEED (
    path '$root/seeds/suppliers.csv'
  ),
  columns (
    supplier_id TEXT,
    supplier_name TEXT,
    contact_email TEXT,
    phone TEXT,
    address TEXT,
    performance_rating DOUBLE,
    certification_level TEXT,
    category TEXT,
    payment_terms TEXT,
    lead_time_days INTEGER,
    outside_service INTEGER,
    active INTEGER,
    created_date TIMESTAMP
  ),
  grain (supplier_id)
);
