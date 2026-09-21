MODEL (
  name mfg_data.receivable_payment,
  kind SEED (
    path '$root/seeds/receivable_payment.csv'
  ),
  columns (
    payment_id INTEGER,
    invoice_id INTEGER,
    installment_no INTEGER,
    payment_date DATE,
    amount DOUBLE,
    source_event_id INTEGER,
    created_at TIMESTAMP
  ),
  grain (payment_id)
);
