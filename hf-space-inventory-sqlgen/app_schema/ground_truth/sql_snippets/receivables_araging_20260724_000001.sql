-- AR Aging Report — Open + Disputed invoices only
-- Ages each open invoice by days past due_date relative to a data-derived
-- as-of date (MAX(payment_date) from Paid invoices — the most recent
-- collection event in the system).
--
-- After collect_june2026_ar runs, all pre-July-2026 Open/Disputed invoices are
-- Paid and this query returns zero rows for that cohort, confirming the
-- cash-to-cash cycle closed correctly.
--
-- Aging buckets (days past due_date as of as_of_date):
--   Current    : due_date >= as_of_date (not yet overdue)
--   1-30 days  : 1–30 days past due
--   31-60 days : 31–60 days past due
--   >60 days   : more than 60 days past due
--
-- Optional parameter:
--   :start_date  — restrict to invoices with invoice_date >= start_date
WITH as_of AS (
    SELECT MAX(payment_date) AS as_of_date
    FROM receivable
    WHERE status = 'Paid'
)
SELECT
    r.invoice_number,
    r.customer_name,
    r.order_id,
    r.status,
    r.invoice_date,
    r.due_date,
    r.amount_dollars,
    CAST(julianday(a.as_of_date) - julianday(r.due_date) AS INTEGER) AS days_past_due,
    CASE
        WHEN julianday(r.due_date) >= julianday(a.as_of_date)          THEN 'Current'
        WHEN julianday(a.as_of_date) - julianday(r.due_date) <= 30     THEN '1-30 days'
        WHEN julianday(a.as_of_date) - julianday(r.due_date) <= 60     THEN '31-60 days'
        ELSE '>60 days'
    END                                                                 AS aging_bucket,
    a.as_of_date
FROM receivable r
CROSS JOIN as_of a
WHERE r.status IN ('Open', 'Disputed')
  AND (:start_date IS NULL OR r.invoice_date >= :start_date)
ORDER BY days_past_due DESC, r.amount_dollars DESC;
