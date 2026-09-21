SELECT
    s.supplier_id,
    s.supplier_name,
    COUNT(p.invoice_id)                                         AS open_invoices,
    ROUND(SUM(p.amount_dollars), 2)                             AS total_due,
    ROUND(SUM(CASE WHEN p.status = 'Disputed'
                   THEN p.amount_dollars ELSE 0 END), 2)        AS disputed_amount,
    ROUND(SUM(CASE WHEN p.due_date <
                        (SELECT MAX(invoice_date) FROM payables)
                   THEN p.amount_dollars ELSE 0 END), 2)        AS overdue_amount,
    MIN(p.due_date)                                             AS earliest_due,
    MAX(p.due_date)                                             AS latest_due
FROM suppliers s
JOIN payables p
    ON p.supplier_id = s.supplier_id
WHERE p.status IN ('Open', 'Disputed')
  AND (:supplier_id IS NULL OR s.supplier_id = :supplier_id)
GROUP BY s.supplier_id, s.supplier_name
ORDER BY total_due DESC;