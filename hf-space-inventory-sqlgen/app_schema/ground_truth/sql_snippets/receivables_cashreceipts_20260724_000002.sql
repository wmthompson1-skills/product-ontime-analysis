-- Cash Receipts by Invoice — payments applied against AR invoices
-- Every installment collected against a receivable, joined back to the
-- invoice header, with a running cumulative-paid and remaining-balance
-- per invoice so partial collections are visible line by line.
--
-- Answers questions like:
--   "What payments came in this month?"
--   "Show me cash receipts against invoice AR-123"
--
-- Optional parameters (each independently NULL-able):
--   :start_date     — restrict to payments with payment_date >= start_date
--   :end_date       — restrict to payments with payment_date <= end_date
--   :invoice_number — restrict to a single invoice (exact match)
SELECT
    r.invoice_number,
    r.customer_name,
    r.order_id,
    r.invoice_date,
    r.amount_dollars                                   AS invoice_amount,
    p.installment_no,
    p.payment_date,
    p.amount                                           AS payment_amount,
    SUM(p.amount) OVER (
        PARTITION BY p.invoice_id
        ORDER BY p.installment_no
    )                                                  AS cumulative_paid,
    r.amount_dollars - SUM(p.amount) OVER (
        PARTITION BY p.invoice_id
        ORDER BY p.installment_no
    )                                                  AS remaining_balance
FROM receivable_payment p
JOIN receivable r
  ON r.invoice_id = p.invoice_id
WHERE (:start_date IS NULL OR p.payment_date >= :start_date)
  AND (:end_date IS NULL OR p.payment_date <= :end_date)
  AND (:invoice_number IS NULL OR r.invoice_number = :invoice_number)
ORDER BY p.payment_date, r.invoice_number, p.installment_no;
