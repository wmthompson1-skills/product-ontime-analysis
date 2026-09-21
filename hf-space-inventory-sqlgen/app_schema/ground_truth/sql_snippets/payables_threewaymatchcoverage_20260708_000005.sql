-- Three-Way Match Coverage (flat receipt/voucher row grain)
-- Consolidated three-way-match ground truth: ONE flat view spanning the full
-- coverage spectrum, at receipt-line / voucher-line pairing grain. Each of
-- the sibling exception views (Uninvoiced Receipts, Partial-Receipt Accrual
-- Exposure, Three-Way Match Exceptions) is a filter over this spine rather
-- than its own topology.
--
-- Design agreement (SME): flat joins only — no CTEs, no derived tables.
-- The initial po_line -> purchase_order join is INNER (a line without its
-- header is meaningless); every join after it is LEFT, so PO lines with
-- nothing received yet still appear ('Not Received' coverage state).
--
-- Lineage (synthetic twin, SQLite manufacturing.db — all base-table joins,
-- no subquery scopes, so the Join-relationships display shows only genuine
-- structural edges):
--   po_line -> purchase_order        (INNER: contractual baseline)
--   purchase_order -> suppliers      (LEFT: vendor enrichment)
--   po_line -> receiving_line        (LEFT: receipt leg via rl.po_line_id)
--   receiving_line -> receiving      (LEFT: receipt header/date)
--   receiving_line -> payable_line   (LEFT: voucher leg via the strict
--                                     payable_line.receipt_line_id linkage —
--                                     the voucher line that vouchers THIS
--                                     receipt line)
--   payable_line -> payables         (LEFT: voucher header/status/date)
--
-- Grain: one row per PO line x receipt line x voucher line. In the synthetic
-- twin the linkage is 1:1 (no PO line has multiple receipt lines, no receipt
-- line has multiple voucher lines), so row totals equal line totals; the
-- flat grain stays honest if that ever changes. Voucher lines with no
-- receipt_line_id linkage are TWM exceptions and remain the concern of the
-- Three-Way Match Exceptions view.
--
-- Field descriptions:
--   query_name         Report label for this coverage extract.
--   purc_order_id      Purchase order identifier (purchase_order.po_id).
--   po_line_id         Purchase order line identifier (po_line.line_id).
--   part_id            Part ordered on the line (po_line.part_id).
--   vendor_id          Supplier identifier (suppliers.supplier_id).
--   vendor_name        Supplier name (suppliers.supplier_name).
--   site_id            Purchasing site (purchase_order.site_id).
--   qty_ordered        Ordered quantity on the PO line (po_line.quantity).
--   po_exists          Contractual truth: 1 — the spine is PO-line driven,
--                      so the contractual leg is present on every row (kept
--                      explicit so the three truth flags read as a set; a
--                      FULL OUTER spine that surfaces Unexpected_Receipt /
--                      Invoice_No_PO states would make this conditional).
--   receipt_exists     Physical truth: 1 when a receiving_line covers the
--                      PO line, else 0.
--   voucher_exists     Financial truth: 1 when a payable_line vouchers the
--                      receipt line, else 0.
--   invoice_document_exists  Document truth: 1 when the voucher line rolls
--                      up to a payables header (the invoice document on
--                      file), else 0 — a voucher line whose header is
--                      missing is in-processing, not yet a document.
--   receipt_line_id    Receipt line key (receiving_line.receipt_line_id);
--                      NULL when nothing has been received.
--   qty_received       Row-level received quantity
--                      (receiving_line.quantity_received).
--   received_date      Dock date of the receipt (receiving.received_date).
--   invoice_number     Voucher number (payables.invoice_number); NULL when
--                      the receipt line is not yet vouchered.
--   invoice_date       Voucher date (payables.invoice_date).
--   voucher_status     Voucher header status (payables.status).
--   qty_invoiced       Row-level vouchered quantity (ABS(payable_line.qty));
--                      0 when unvouchered or the voucher is cancelled or
--                      falls after the :end_date netting snapshot.
--   match_status       Coverage state of the row:
--                        'Not Received'        no receipt line yet
--                        'Received-Uninvoiced' receipt with no live voucher
--                        'Partially Invoiced'  live voucher < received qty
--                        'Matched'             live voucher = received qty
--                        'Over-Invoiced'       live voucher > received qty
--
-- Parameters (every guard is "(:param IS NULL OR <predicate>)", so binding
-- all to NULL reproduces the full population; receipt-date guards pass rows
-- with no receipt so 'Not Received' lines are never dropped by a horizon):
--   :supplier_id Restrict to a single vendor (suppliers.supplier_id).
--   :start_date  Horizon lower bound on receiving.received_date.
--   :end_date    Dual role: (1) horizon upper bound on received_date;
--                (2) netting snapshot on payables.invoice_date — vouchers
--                after :end_date count as no coverage.
-- Cancelled purchase orders are excluded — a cancelled PO is not a live
-- contractual baseline.
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
          OR NOT (:end_date IS NULL OR pay.invoice_date <= :end_date)
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
ORDER BY s.supplier_id, po.po_id, pl.line_id, rl.receipt_line_id ASC
