-- Ground Truth SQL: Supplier / Payables Queries
-- Perspective: Payables · Quality
-- Category: supplier_performance
-- Source: Manufacturing SQL Semantic Layer (Replit public repo)
--
-- NOTE ON FRAMING: Supplier queries ARE payables queries.
-- The Finance/AP stakeholder drives most of these intents:
--   "Which suppliers owe us credits?" = supplier_cost_penalties (Finance)
--   "Which suppliers are at risk?"    = supplier_scorecard (Quality → also Finance)
--   "What is our AP exposure?"        = supplier_payables_exposure (Finance)
--
-- When the dispatcher hears "payables", "AP", "vendor invoice", or "owe",
-- it should route to this category before considering quality-only intents.
--
-- Temporal binding key: :start_date  (ISO date, e.g. '2025-07-01')
-- Supplier binding key: :supplier_id (integer, optional)

-- ============================================================
-- Query 1 — supplier_scorecard
-- Intent:   Which suppliers are meeting delivery and quality thresholds?
-- Typical:  "Supplier scorecard for the last 30 days"
--           "Which vendors are underperforming on quality?"
-- Params:   :start_date (delivery_date lower bound)
-- Perspectives: Quality (primary) · Finance (vendor payment-term decisions)
-- ============================================================
-- Query: Supplier Scorecard
-- Description: Which suppliers are meeting delivery and quality thresholds? Delivery counts, on-time %, quality score, and late-rate penalty signal per supplier.
SELECT
    s.supplier_id,
    s.supplier_name,
    s.performance_rating,
    s.certification_level,
    COUNT(d.delivery_id)                                        AS total_deliveries,
    ROUND(AVG(d.ontime_rate) * 100, 1)                          AS avg_ontime_pct,
    ROUND(AVG(d.quality_score) * 100, 1)                        AS avg_quality_score,
    COUNT(CASE WHEN d.ontime_rate >= 0.95 THEN 1 END)           AS on_time_deliveries,
    COUNT(CASE WHEN d.ontime_rate < 0.80  THEN 1 END)           AS late_deliveries,
    -- AP signal: late delivery rate drives penalty credit calculation
    ROUND(
        CAST(COUNT(CASE WHEN d.ontime_rate < 0.80 THEN 1 END) AS REAL)
        / NULLIF(COUNT(d.delivery_id), 0) * 100, 1
    )                                                           AS late_rate_pct
FROM suppliers s
LEFT JOIN daily_deliveries d
    ON s.supplier_id = d.supplier_id
   AND d.delivery_date >= :start_date
GROUP BY s.supplier_id, s.supplier_name, s.performance_rating, s.certification_level
ORDER BY avg_ontime_pct ASC, avg_quality_score ASC;

-- ============================================================
-- Query 2 — supplier_cost_penalties
-- Intent:   What penalty credits are owed for late deliveries?
-- Typical:  "What penalties are owed for late deliveries since July 1st?"
--           "Calculate vendor penalty exposure from July"
-- Params:   :start_date
-- Perspective: Payables — this is the primary payables query
-- ============================================================
-- Query: Supplier Cost Penalties
-- Description: What penalty credits are owed for late deliveries? Estimated penalty credit per supplier (2% of delivery value per late event).
SELECT
    s.supplier_id,
    s.supplier_name,
    s.certification_level,
    COUNT(d.delivery_id)                                        AS total_deliveries,
    COUNT(CASE WHEN d.ontime_rate < 0.80 THEN 1 END)           AS late_deliveries,
    -- Penalty model: 2% of delivery value per late event (illustrative rate)
    ROUND(
        COUNT(CASE WHEN d.ontime_rate < 0.80 THEN 1 END)
        * d.actual_quantity * 0.02, 2
    )                                                           AS estimated_penalty_credit,
    ROUND(AVG(d.quality_score), 3)                              AS avg_quality,
    MIN(d.delivery_date)                                        AS earliest_delivery,
    MAX(d.delivery_date)                                        AS latest_delivery
FROM suppliers s
JOIN daily_deliveries d
    ON s.supplier_id = d.supplier_id
WHERE d.delivery_date >= :start_date
GROUP BY s.supplier_id, s.supplier_name, s.certification_level, d.actual_quantity
HAVING COUNT(CASE WHEN d.ontime_rate < 0.80 THEN 1 END) > 0
ORDER BY estimated_penalty_credit DESC;

-- ============================================================
-- Query 3 — supplier_payables_exposure
-- Intent:   What is our total AP exposure by supplier for a period?
-- Typical:  "Show me payables exposure by vendor from July 1st"
--           "What do we owe suppliers this quarter?"
-- Params:   :start_date, :supplier_id (optional)
-- Perspective: Payables — pure payables roll-up
-- Note:     unit_cost is sourced from product_lines.unit_price as a proxy
--           until a purchase_orders table is added to this schema.
-- ============================================================
-- Query: Supplier Payables Exposure
-- Description: What is our total AP exposure by supplier for a period? Invoice counts, units received, estimated AP exposure, and payment recommendation.
SELECT
    s.supplier_id,
    s.supplier_name,
    s.certification_level,
    s.performance_rating,
    COUNT(d.delivery_id)                                        AS invoice_count,
    SUM(d.actual_quantity)                                      AS total_units_received,
    -- Cost proxy: actual_quantity × median unit_price from product_lines
    ROUND(
        SUM(d.actual_quantity) * (
            SELECT AVG(pl.unit_price) FROM product_lines pl
        ), 2
    )                                                           AS estimated_ap_exposure,
    -- Discount signal: high quality + on-time → eligible for early-pay discount
    CASE
        WHEN AVG(d.quality_score) >= 0.95
         AND AVG(d.ontime_rate)   >= 0.95 THEN 'Early-Pay Eligible'
        WHEN AVG(d.quality_score) >= 0.80 THEN 'Standard Terms'
        ELSE 'Hold — Review Required'
    END                                                         AS payment_recommendation,
    ROUND(AVG(d.ontime_rate) * 100, 1)                          AS avg_ontime_pct,
    ROUND(AVG(d.quality_score) * 100, 1)                        AS avg_quality_score
FROM suppliers s
JOIN daily_deliveries d
    ON s.supplier_id = d.supplier_id
WHERE d.delivery_date >= :start_date
  AND (:supplier_id IS NULL OR s.supplier_id = :supplier_id)
GROUP BY s.supplier_id, s.supplier_name, s.certification_level, s.performance_rating
ORDER BY estimated_ap_exposure DESC;

-- ============================================================
-- Query 4 — supplier_payables_exposure (total due)
-- Intent:   What is our AP exposure by supplier? (exposure = total due)
-- Typical:  "What do we owe each supplier right now?"
--           "Total AP due by vendor"
-- Params:   :supplier_id (optional)
-- Perspective: Payables — exposure defined as TOTAL DUE: the sum of all
--           unpaid (Open or Disputed) invoice amounts from the payables
--           ledger. No estimates or cost proxies.
-- ============================================================
-- Query: Supplier AP Total Due
-- Binding: gt_supplier_ap_total_due_20260920_181003
-- Description: What is our AP exposure by supplier, defined as total due — the sum of all unpaid (Open or Disputed) invoice amounts, with overdue split.
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

-- ============================================================
-- Query 5 — supplier_payables_exposure (aging)
-- Intent:   How old is our unpaid AP, bucketed by days past due?
-- Typical:  "AP aging report by supplier"
--           "Which invoices are 90+ days past due?"
-- Params:   :supplier_id (optional)
-- Perspective: Payables — aging is computed against a data-derived
--           as-of date (MAX invoice_date in the ledger), so the report
--           is deterministic and never depends on the wall clock.
-- ============================================================
-- Query: AP Aging by Supplier
-- Description: How old is our unpaid AP? Unpaid (Open or Disputed) amounts bucketed by days past due — current, 1-30, 31-60, 61-90, and 90+ — per supplier.
WITH as_of AS (
    SELECT MAX(invoice_date) AS as_of_date FROM payables
)
SELECT
    s.supplier_id,
    s.supplier_name,
    ROUND(SUM(CASE WHEN p.due_date >= a.as_of_date
                   THEN p.amount_dollars ELSE 0 END), 2)        AS current_not_due,
    ROUND(SUM(CASE WHEN julianday(a.as_of_date) - julianday(p.due_date)
                        BETWEEN 1 AND 30
                   THEN p.amount_dollars ELSE 0 END), 2)        AS past_due_1_30,
    ROUND(SUM(CASE WHEN julianday(a.as_of_date) - julianday(p.due_date)
                        BETWEEN 31 AND 60
                   THEN p.amount_dollars ELSE 0 END), 2)        AS past_due_31_60,
    ROUND(SUM(CASE WHEN julianday(a.as_of_date) - julianday(p.due_date)
                        BETWEEN 61 AND 90
                   THEN p.amount_dollars ELSE 0 END), 2)        AS past_due_61_90,
    ROUND(SUM(CASE WHEN julianday(a.as_of_date) - julianday(p.due_date) > 90
                   THEN p.amount_dollars ELSE 0 END), 2)        AS past_due_over_90,
    ROUND(SUM(p.amount_dollars), 2)                             AS total_due
FROM suppliers s
JOIN payables p
    ON p.supplier_id = s.supplier_id
CROSS JOIN as_of a
WHERE p.status IN ('Open', 'Disputed')
  AND (:supplier_id IS NULL OR s.supplier_id = :supplier_id)
GROUP BY s.supplier_id, s.supplier_name
ORDER BY past_due_over_90 DESC, total_due DESC;

-- ============================================================
-- Query 6 — supplier_payables_exposure (match exceptions)
-- Intent:   Which unpaid invoices failed three-way match?
-- Typical:  "Show invoices on match exception hold"
--           "What AP is blocked by three-way match failures?"
-- Params:   :supplier_id (optional)
-- Perspective: Payables — unpaid invoices whose three_way_match_status
--           is Exception (PO / receipt / invoice disagree). These should
--           be held from payment until resolved.
-- ============================================================
-- Query: Uninvoiced Receipts
-- Description: Which receipts have goods in the door but no matching supplier invoice? Receipt lines whose non-cancelled payable coverage is short or missing entirely (three-way match leg 2 vs leg 3).
-- Binding: payables_uninvoicedreceipts_20260706_000003
-- Temporal Parameter Contract: named, NULL-guarded placeholders baked in by the
-- SME (SolderEngine serves verbatim). :start_date/:end_date = Horizon Filter on
-- receiving.received_date; :end_date also = Netting Snapshot on
-- payables.invoice_date inside both coverage subqueries; :supplier_id restricts
-- to one vendor. Bind all three NULL for the full unfiltered population.
-- Grain: one row per receipt HEADER with >=1 unmatched line. EXISTS semi-join
-- (not header->line JOIN + DISTINCT) keeps the receipt grain without a fan-out.
SELECT
    'Uninvoiced Receipts' AS query_name,
    r.receipt_id          AS receiver_id,
    r.received_date       AS received_date,
    r.po_id               AS purc_order_id,
    s.supplier_id         AS vendor_id,
    s.supplier_name       AS vendor_name,
    po.site_id            AS site_id
FROM receiving r
JOIN purchase_order po ON po.po_id = r.po_id
JOIN suppliers s       ON s.supplier_id = po.supplier_id
WHERE po.site_id = 'SITE-1'
  AND (:supplier_id IS NULL OR s.supplier_id = :supplier_id)
  AND (:start_date IS NULL OR r.received_date >= :start_date)
  AND (:end_date IS NULL OR r.received_date <= :end_date)
  AND EXISTS (
        SELECT 1
        FROM receiving_line rl
        WHERE rl.receipt_id = r.receipt_id
          AND (
                rl.quantity_received > (
                    SELECT COALESCE(SUM(ABS(pl.qty)), 0)
                    FROM payable_line pl
                    JOIN payables pay ON pay.invoice_id = pl.invoice_id
                    WHERE pl.receipt_line_id = rl.receipt_line_id
                      AND pay.status <> 'Cancelled'
                      AND (:end_date IS NULL OR pay.invoice_date <= :end_date)
                )
                OR rl.receipt_line_id NOT IN (
                    SELECT pl.receipt_line_id
                    FROM payable_line pl
                    JOIN payables pay ON pay.invoice_id = pl.invoice_id
                    WHERE pl.receipt_line_id IS NOT NULL
                      AND pay.status <> 'Cancelled'
                      AND (:end_date IS NULL OR pay.invoice_date <= :end_date)
                )
          )
      )
ORDER BY s.supplier_id, po.site_id, r.received_date ASC;

-- Query: Partial-Receipt Accrual Exposure
-- Description: Which PO lines are partially received with the received portion not yet fully invoiced? Received > 0 but < ordered, voucher coverage short — the open Purchase Receipt Accrual (PRA) exposure at PO-price valuation.
-- Binding: payables_partialreceiptaccrual_20260708_000004
-- Temporal Parameter Contract: named, NULL-guarded placeholders baked in by the
-- SME (SolderEngine serves verbatim). :start_date/:end_date = Horizon Filter on
-- receiving.received_date; :end_date also = Netting Snapshot on
-- payables.invoice_date inside the voucher-coverage aggregate; :supplier_id
-- restricts to one vendor. Bind all three NULL for the full population.
-- Grain: one row per PO LINE in the accrual condition. Receipt and voucher
-- coverage are pre-aggregated in derived tables so the PO-line grain never
-- fans out. Cancelled POs and cancelled vouchers excluded.
SELECT
    'Partial-Receipt Accrual Exposure'   AS query_name,
    po.po_id                             AS purc_order_id,
    pl.line_id                           AS po_line_id,
    pl.part_id                           AS part_id,
    s.supplier_id                        AS vendor_id,
    s.supplier_name                      AS vendor_name,
    po.site_id                           AS site_id,
    ROUND(pl.quantity, 1)                AS qty_ordered,
    ROUND(rcv.qty_received, 1)           AS qty_received,
    ROUND(COALESCE(inv.qty_invoiced, 0), 1) AS qty_invoiced,
    ROUND(rcv.qty_received - COALESCE(inv.qty_invoiced, 0), 1) AS qty_uninvoiced,
    ROUND((rcv.qty_received - COALESCE(inv.qty_invoiced, 0)) * pl.unit_cost, 2)
                                         AS accrued_value,
    rcv.last_receipt_date                AS last_receipt_date
FROM po_line pl
JOIN purchase_order po ON po.po_id = pl.po_id
JOIN suppliers s       ON s.supplier_id = po.supplier_id
JOIN (
    SELECT
        rl.po_line_id,
        SUM(rl.quantity_received) AS qty_received,
        MAX(r.received_date)      AS last_receipt_date
    FROM receiving_line rl
    JOIN receiving r ON r.receipt_id = rl.receipt_id
    WHERE (:start_date IS NULL OR r.received_date >= :start_date)
      AND (:end_date IS NULL OR r.received_date <= :end_date)
    GROUP BY rl.po_line_id
) rcv ON rcv.po_line_id = pl.line_id
LEFT JOIN (
    SELECT
        pl2.po_line_id,
        SUM(ABS(pl2.qty)) AS qty_invoiced
    FROM payable_line pl2
    JOIN payables pay ON pay.invoice_id = pl2.invoice_id
    WHERE pl2.po_line_id IS NOT NULL
      AND pay.status <> 'Cancelled'
      AND (:end_date IS NULL OR pay.invoice_date <= :end_date)
    GROUP BY pl2.po_line_id
) inv ON inv.po_line_id = pl.line_id
WHERE po.status <> 'Cancelled'
  AND (:supplier_id IS NULL OR s.supplier_id = :supplier_id)
  AND rcv.qty_received > 0
  AND rcv.qty_received < pl.quantity
  AND COALESCE(inv.qty_invoiced, 0) < rcv.qty_received
ORDER BY s.supplier_id, po.po_id, pl.line_id ASC;

-- Query: Three-Way Match Coverage
-- Description: The full three-way-match coverage spectrum in one flat spine — every non-cancelled PO line at receipt-line/voucher-line grain with contractual, physical, financial, and document truth flags and a five-state match_status (Not Received, Received-Uninvoiced, Partially Invoiced, Matched, Over-Invoiced). The sibling exception views are filters over this spine.
-- Binding: payables_threewaymatchcoverage_20260708_000005
-- Temporal Parameter Contract: named, NULL-guarded placeholders baked in by the
-- SME (SolderEngine serves verbatim). :start_date/:end_date = Horizon Filter on
-- receiving.received_date (receipt-date guards pass rows with no receipt so
-- 'Not Received' lines are never dropped by a horizon); :end_date also =
-- Netting Snapshot on payables.invoice_date — vouchers after :end_date count
-- as no coverage; :supplier_id restricts to one vendor. Bind all three NULL
-- for the full unfiltered coverage population.
-- Grain: one row per PO line x receipt line x voucher line (flat joins only —
-- no CTEs, no derived tables). po_line -> purchase_order is INNER; every join
-- after it is LEFT so never-received PO lines still appear. Voucher lines with
-- no receipt_line_id linkage are excluded by design — they remain the concern
-- of the Three-Way Match Exceptions view.
SELECT
    'Three-Way Match Coverage'           AS query_name,
    po.po_id                             AS purc_order_id,
    pl.line_id                           AS po_line_id,
    pl.part_id                           AS part_id,
    s.supplier_id                        AS vendor_id,
    s.supplier_name                      AS vendor_name,
    po.site_id                           AS site_id,
    ROUND(pl.quantity, 1)                AS qty_ordered,
    1                                    AS po_exists,
    CASE WHEN rl.receipt_line_id IS NOT NULL
         THEN 1 ELSE 0 END               AS receipt_exists,
    CASE WHEN pyl.payable_line_id IS NOT NULL
         THEN 1 ELSE 0 END               AS voucher_exists,
    CASE WHEN pay.invoice_id IS NOT NULL
         THEN 1 ELSE 0 END               AS invoice_document_exists,
    rl.receipt_line_id                   AS receipt_line_id,
    ROUND(COALESCE(rl.quantity_received, 0), 1) AS qty_received,
    r.received_date                      AS received_date,
    pay.invoice_number                   AS invoice_number,
    pay.invoice_date                     AS invoice_date,
    pay.status                           AS voucher_status,
    ROUND(CASE
        WHEN pyl.payable_line_id IS NOT NULL
         AND pay.status <> 'Cancelled'
         AND (:end_date IS NULL OR pay.invoice_date <= :end_date)
        THEN ABS(pyl.qty) ELSE 0 END, 1) AS qty_invoiced,
    CASE
        WHEN rl.receipt_line_id IS NULL THEN 'Not Received'
        WHEN pyl.payable_line_id IS NULL
          OR pay.status = 'Cancelled'
          OR (:end_date IS NOT NULL AND pay.invoice_date > :end_date)
            THEN 'Received-Uninvoiced'
        WHEN ABS(pyl.qty) < rl.quantity_received THEN 'Partially Invoiced'
        WHEN ABS(pyl.qty) = rl.quantity_received THEN 'Matched'
        ELSE 'Over-Invoiced'
    END                                  AS match_status
FROM po_line pl
JOIN purchase_order po      ON po.po_id = pl.po_id
LEFT JOIN suppliers s       ON s.supplier_id = po.supplier_id
LEFT JOIN receiving_line rl ON rl.po_line_id = pl.line_id
LEFT JOIN receiving r       ON r.receipt_id = rl.receipt_id
LEFT JOIN payable_line pyl  ON pyl.receipt_line_id = rl.receipt_line_id
LEFT JOIN payables pay      ON pay.invoice_id = pyl.invoice_id
WHERE po.status <> 'Cancelled'
  AND (:supplier_id IS NULL OR s.supplier_id = :supplier_id)
  AND (:start_date IS NULL OR rl.receipt_line_id IS NULL
       OR r.received_date >= :start_date)
  AND (:end_date IS NULL OR rl.receipt_line_id IS NULL
       OR r.received_date <= :end_date)
ORDER BY s.supplier_id, po.po_id, pl.line_id, rl.receipt_line_id ASC;

-- Query: Three-Way Match Exceptions
-- Description: Which unpaid invoices failed three-way match? Exception-status invoices per supplier with amounts at risk and the linked PO.
SELECT
    s.supplier_id,
    s.supplier_name,
    p.invoice_number,
    p.po_id,
    p.invoice_date,
    p.due_date,
    ROUND(p.amount_dollars, 2)                                  AS amount_on_hold,
    p.status                                                    AS invoice_status
FROM payables p
JOIN suppliers s
    ON s.supplier_id = p.supplier_id
WHERE p.three_way_match_status = 'Exception'
  AND p.status IN ('Open', 'Disputed')
  AND (:supplier_id IS NULL OR s.supplier_id = :supplier_id)
ORDER BY p.amount_dollars DESC;
