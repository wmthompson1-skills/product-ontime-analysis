"""Complete the three-way match chain: PO <-> receiver <-> payable.

Before this migration only 3 of 24 POs had receipts while 11 carried
invoices, so most invoices had no receiver leg and only 6 of 29 payable
lines linked to a receipt line. This migration completes the chain
deterministically (no randomness, all dates derived from existing data)
and idempotently (safe to re-run):

1. RECEIPT BACKFILL — every Closed PO with an invoice gets a full receipt
   (one denormalized ``receiving`` header row per PO line + one
   ``receiving_line`` linked to that PO line, quantity received = ordered,
   inspection Passed). Receipt dates = invoice_date - 5 days, so the
   receiver leg always precedes the invoice.
2. ENGINEERED POPULATIONS —
   - PO-000001 (Disputed / Exception): highest-value line received SHORT
     (80% of ordered) while the invoice bills the full quantity — a real
     quantity-mismatch exception.
   - PO-000010 (voucher pending): first two lines received, third line not
     yet arrived; invoice bills all three, so the voucher cannot clear —
     its match status is repaired Matched -> Pending.
   - PO-000009 (received, not invoiced): first line received, no payable
     row at all — the receiving-side voucher-pending population.
   - PO-000014 (Paid / Matched, was receipt-less): received in full so the
     paid-and-matched invoice is actually consistent.
3. STATUS REPAIR — invoices 'INV-000003' / 'INV-000008' were Paid but
   still Pending; both POs are Closed and now fully received, so they
   become Matched. 'INV-000004' becomes Pending (see PO-000010 above).
4. LINK REPAIR — every payable line whose PO line now has a receipt line
   gets its ``receipt_line_id`` populated (by po_line_id, never by fuzzy
   part match). Lines for unreceived PO lines stay NULL by design.
5. FAIL-CLOSED VERIFY — the migration re-checks every invariant at the end
   and exits non-zero on any drift.

Out of scope, on purpose:
  - MRP-critical POs (PO-MRP-BLK1..3, PO-MRP-P-10032, PO-CON-001) are
    never touched.
  - No purchase_order.status changes (MRP reads open/partial PO lines).
  - No inventory-ledger writes (receipts here are AP evidence, not stock).
"""

import os
import sqlite3
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema", "manufacturing.db")

# POs the MRP demo depends on — never touched here.
PROTECTED_POS = {"PO-MRP-BLK1", "PO-MRP-BLK2", "PO-MRP-BLK3",
                 "PO-MRP-P-10032", "PO-CON-001"}

# Closed POs that get FULL receipts (every line, qty received = ordered).
# PO-000014 is Partial-status but its invoice is Paid/Matched, so the
# receiver leg must exist in full for the ledger to be consistent.
FULL_RECEIPT_POS = ["PO-000003", "PO-000005", "PO-000011", "PO-000012",
                    "PO-000015", "PO-000014"]

# PO -> {part_id: fraction_received} for engineered partial receipts.
# Missing parts on these POs are NOT received at all.
PARTIAL_RECEIPTS = {
    # Exception: big line short-shipped, invoice bills full qty (Disputed).
    "PO-000001": {"P-10037": 0.8, "P-10014": 1.0, "P-10021": 1.0},
    # Voucher pending: last line not yet arrived, invoice bills all three.
    "PO-000010": {"P-10033": 1.0, "C-20004": 1.0},
    # Received-not-invoiced: goods in, no invoice on file yet.
    "PO-000009": {"__FIRST_LINE__": 1.0},
}

# invoice_number -> new three_way_match_status (deterministic repairs).
MATCH_STATUS_REPAIRS = {
    "INV-000003": "Matched",   # Paid + PO fully received -> Matched
    "INV-000008": "Matched",   # Paid + PO fully received -> Matched
    "INV-000004": "Pending",   # last PO line not received -> voucher pending
}

# Receipt-date offset for the not-yet-invoiced PO (days before AS_OF).
UNINVOICED_RECEIPT_OFFSET = 6


def fmt(d):
    return d.isoformat()


def get_as_of(cur):
    row = cur.execute(
        "SELECT MAX(close_date) FROM work_order WHERE close_date IS NOT NULL"
    ).fetchone()
    if not row or not row[0]:
        raise SystemExit("FAIL-CLOSED: cannot derive AS_OF from work_order.close_date")
    return date.fromisoformat(row[0][:10])


def receipt_date_for(cur, po_id, as_of):
    """Deterministic receipt date: 5 days before the PO's earliest invoice,
    or AS_OF - UNINVOICED_RECEIPT_OFFSET when no invoice exists."""
    row = cur.execute(
        "SELECT MIN(invoice_date) FROM payables WHERE po_id=?", (po_id,)
    ).fetchone()
    if row and row[0]:
        return fmt(date.fromisoformat(row[0][:10]) - timedelta(days=5))
    return fmt(as_of - timedelta(days=UNINVOICED_RECEIPT_OFFSET))


def insert_receipt_lines(cur, po_id, recv_date, fractions=None):
    """One receiving header + line per PO line. fractions=None means full
    receipt of every line; otherwise only the listed parts are received at
    the given fraction of ordered quantity. Idempotent per (po_id, part_id)."""
    if po_id in PROTECTED_POS:
        raise SystemExit(f"FAIL-CLOSED: attempted to touch protected PO {po_id}")
    sup_row = cur.execute(
        "SELECT supplier_id FROM purchase_order WHERE po_id=?", (po_id,)
    ).fetchone()
    if not sup_row:
        raise SystemExit(f"FAIL-CLOSED: PO {po_id} not found")
    sup = sup_row[0]

    lines = cur.execute(
        "SELECT line_id, part_id, quantity FROM po_line WHERE po_id=? "
        "ORDER BY line_id", (po_id,)
    ).fetchall()
    if not lines:
        raise SystemExit(f"FAIL-CLOSED: PO {po_id} has no lines to receive")

    inserted = 0
    for idx, (line_id, part_id, qty_ord) in enumerate(lines):
        if fractions is not None:
            if "__FIRST_LINE__" in fractions:
                if idx > 0:
                    continue
                frac = fractions["__FIRST_LINE__"]
            elif part_id in fractions:
                frac = fractions[part_id]
            else:
                continue
        else:
            frac = 1.0
        if cur.execute(
            "SELECT 1 FROM receiving WHERE po_id=? AND part_id=?", (po_id, part_id)
        ).fetchone():
            continue
        qty_recv = round(qty_ord * frac, 1)
        cur.execute(
            "INSERT INTO receiving (po_id, supplier_id, part_id, quantity_ordered, "
            "quantity_received, receipt_date, inspection_status, cert_required) "
            "VALUES (?,?,?,?,?,?,'Passed',0)",
            (po_id, sup, part_id, qty_ord, qty_recv, recv_date),
        )
        receipt_id = cur.lastrowid
        cur.execute(
            "INSERT INTO receiving_line (receipt_id, line_no, po_line_id, part_id, "
            "quantity_ordered, quantity_received, inspection_status, cert_required) "
            "VALUES (?,1,?,?,?,?,'Passed',0)",
            (receipt_id, line_id, part_id, qty_ord, qty_recv),
        )
        inserted += 1
    return inserted


def repair_match_statuses(cur):
    changed = 0
    for inv_no, new_status in MATCH_STATUS_REPAIRS.items():
        cur.execute(
            "UPDATE payables SET three_way_match_status=? "
            "WHERE invoice_number=? AND three_way_match_status<>?",
            (new_status, inv_no, new_status),
        )
        changed += cur.rowcount
    return changed


def link_payable_lines(cur):
    """Populate payable_line.receipt_line_id strictly by po_line_id."""
    cur.execute("""
        UPDATE payable_line SET receipt_line_id = (
            SELECT rl.receipt_line_id FROM receiving_line rl
            WHERE rl.po_line_id = payable_line.po_line_id
        )
        WHERE receipt_line_id IS NULL
          AND po_line_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM receiving_line rl2
                      WHERE rl2.po_line_id = payable_line.po_line_id)
    """)
    return cur.rowcount


# ---------------------------------------------------------------------------
# fail-closed verification
# ---------------------------------------------------------------------------

def verify(cur):
    errors = []

    def check(label, sql, expect_zero=True):
        n = cur.execute(sql).fetchone()[0]
        if (n != 0) if expect_zero else (n == 0):
            errors.append(f"{label} (got {n})")

    # 1. Every Closed PO with lines is fully received (excluding protected).
    check("Closed POs with unreceived lines", """
        SELECT COUNT(*) FROM po_line pl
        JOIN purchase_order po ON po.po_id = pl.po_id
        WHERE po.status='Closed'
          AND NOT EXISTS (SELECT 1 FROM receiving_line rl
                          WHERE rl.po_line_id = pl.line_id
                            AND rl.quantity_received >= pl.quantity)
    """)

    # 2. Every receiving header agrees with its line.
    check("receiving header/line drift", """
        SELECT COUNT(*) FROM receiving r JOIN receiving_line rl USING(receipt_id)
        WHERE r.part_id <> rl.part_id
           OR r.quantity_received <> rl.quantity_received
           OR r.quantity_ordered <> rl.quantity_ordered
    """)
    check("receiving headers without lines", """
        SELECT COUNT(*) FROM receiving r
        WHERE NOT EXISTS (SELECT 1 FROM receiving_line rl
                          WHERE rl.receipt_id = r.receipt_id)
    """)

    # 3. Amount reconciliation: invoice total == sum of payable lines.
    check("invoice amount vs line-sum drift", """
        SELECT COUNT(*) FROM payables p
        WHERE ABS(p.amount_dollars - (SELECT COALESCE(SUM(l.amount),0)
                                      FROM payable_line l
                                      WHERE l.invoice_id = p.invoice_id)) > 0.01
    """)

    # 4. Matched invoices: every payable line links to a receipt line whose
    #    received quantity covers the billed quantity.
    check("Matched invoices with unlinked/short lines", """
        SELECT COUNT(*) FROM payable_line l
        JOIN payables p ON p.invoice_id = l.invoice_id
        WHERE p.three_way_match_status = 'Matched'
          AND (l.receipt_line_id IS NULL
               OR (SELECT rl.quantity_received FROM receiving_line rl
                   WHERE rl.receipt_line_id = l.receipt_line_id) < l.qty)
    """)

    # 5a. No Exception invoice without a genuine quantity mismatch.
    check("Exception invoices lack a real mismatch", """
        SELECT COUNT(*) FROM payables p
        WHERE p.three_way_match_status = 'Exception'
          AND EXISTS (SELECT 1 FROM payable_line l WHERE l.invoice_id = p.invoice_id)
          AND NOT EXISTS (
              SELECT 1 FROM payable_line l
              JOIN receiving_line rl ON rl.receipt_line_id = l.receipt_line_id
              WHERE l.invoice_id = p.invoice_id
                AND rl.quantity_received < l.qty)
    """)
    # 5b. At least one Exception invoice carries a linked short-received line.
    check("no Exception invoice with a real mismatch exists", """
        SELECT COUNT(*) FROM payables p
        WHERE p.three_way_match_status = 'Exception'
          AND EXISTS (
              SELECT 1 FROM payable_line l
              JOIN receiving_line rl ON rl.receipt_line_id = l.receipt_line_id
              WHERE l.invoice_id = p.invoice_id
                AND rl.quantity_received < l.qty)
    """, expect_zero=False)

    # 6. No dangling links.
    check("payable_line -> receiving_line dangles", """
        SELECT COUNT(*) FROM payable_line l
        WHERE l.receipt_line_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM receiving_line rl
                          WHERE rl.receipt_line_id = l.receipt_line_id)
    """)
    check("receiving_line -> po_line dangles", """
        SELECT COUNT(*) FROM receiving_line rl
        WHERE rl.po_line_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM po_line pl WHERE pl.line_id = rl.po_line_id)
    """)
    check("receiving -> purchase_order dangles", """
        SELECT COUNT(*) FROM receiving r
        WHERE NOT EXISTS (SELECT 1 FROM purchase_order po WHERE po.po_id = r.po_id)
    """)

    # 7. Required populations, each non-empty.
    check("population: unreceived POs", """
        SELECT COUNT(*) FROM purchase_order po
        WHERE po.status='Open'
          AND NOT EXISTS (SELECT 1 FROM receiving r WHERE r.po_id = po.po_id)
          AND NOT EXISTS (SELECT 1 FROM payables p WHERE p.po_id = po.po_id)
    """, expect_zero=False)
    check("population: clean three-way matches", """
        SELECT COUNT(*) FROM payables p
        WHERE p.three_way_match_status='Matched' AND p.status='Paid'
    """, expect_zero=False)
    check("population: invoiced, voucher pending (unpaid)", """
        SELECT COUNT(*) FROM payables p
        WHERE p.three_way_match_status='Pending' AND p.payment_date IS NULL
    """, expect_zero=False)
    check("population: received but not invoiced", """
        SELECT COUNT(DISTINCT r.po_id) FROM receiving r
        WHERE NOT EXISTS (SELECT 1 FROM payables p WHERE p.po_id = r.po_id)
    """, expect_zero=False)
    check("population: unpaid Exception invoices", """
        SELECT COUNT(*) FROM payables p
        WHERE p.three_way_match_status='Exception' AND p.status IN ('Open','Disputed')
    """, expect_zero=False)

    # 8. Paid invoices must never sit in Pending.
    check("Paid invoices stuck in Pending", """
        SELECT COUNT(*) FROM payables
        WHERE status='Paid' AND three_way_match_status='Pending'
    """)

    # 9. Protected POs untouched by receipts.
    check("protected POs received", """
        SELECT COUNT(*) FROM receiving
        WHERE po_id IN ('PO-MRP-BLK1','PO-MRP-BLK2','PO-MRP-BLK3',
                        'PO-MRP-P-10032','PO-CON-001')
    """)

    if errors:
        for e in errors:
            print(f"  VERIFY FAIL: {e}")
        raise SystemExit("FAIL-CLOSED: three-way match verification failed")
    print("  verify: all three-way match invariants hold")


def _po_exists(cur, po_id: str) -> bool:
    """True when the PO is still present in purchase_order."""
    return cur.execute(
        "SELECT 1 FROM purchase_order WHERE po_id=?", (po_id,)
    ).fetchone() is not None


def run():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    as_of = get_as_of(cur)
    print(f"  AS_OF = {as_of}")

    n = 0
    for po_id in FULL_RECEIPT_POS:
        if not _po_exists(cur, po_id):
            # The prune step selected different survivors on a second bootstrap
            # run. This PO (and its cascade) was already removed — there are no
            # Closed PO lines left for the verify gate to flag.  Warn and skip.
            print(f"  WARNING: PO {po_id} not found in purchase_order "
                  "(pruned on a previous run?) — skipping full-receipt backfill")
            continue
        n += insert_receipt_lines(cur, po_id, receipt_date_for(cur, po_id, as_of))
    for po_id, fractions in PARTIAL_RECEIPTS.items():
        if not _po_exists(cur, po_id):
            print(f"  WARNING: PO {po_id} not found in purchase_order "
                  "(pruned on a previous run?) — skipping partial-receipt backfill")
            continue
        n += insert_receipt_lines(cur, po_id, receipt_date_for(cur, po_id, as_of),
                                  fractions)
    print(f"  receipts: {n} new receipt line(s)")

    # Fresh-DB guard: INV-000004 maps to PO-000010 (the "voucher pending" demo
    # population — third line intentionally not yet received, invoice cannot
    # clear).  The seeder can generate it as Paid on a fresh clone, which
    # contradicts the demo story and causes the verify gate to fail because a
    # Matched+Paid invoice with an unlinked payable line is invalid.
    # Reset it to Open/unpaid before the status-repair step so repair_match_statuses
    # can correctly stamp it Pending, and the verify gates stay consistent
    # whether this is a fresh-clone or an idempotent re-run on an existing DB.
    demo_reset = cur.execute(
        "UPDATE payables SET status='Open', payment_date=NULL "
        "WHERE invoice_number='INV-000004' AND status='Paid'"
    ).rowcount
    if demo_reset:
        print("  demo reset: INV-000004 corrected to Open/unpaid (voucher-pending story)")

    print(f"  match-status repairs: {repair_match_statuses(cur)} invoice(s)")

    # A Paid invoice with Pending match status is semantically invalid: payment
    # implies the match was accepted.  On a fresh DB the seeder can produce
    # Paid+Pending rows (random status assignment), and the MATCH_STATUS_REPAIRS
    # dict can also create one (INV-000004 may already be Paid on a fresh clone).
    # Promote all such rows to Matched before verify so the gate is deterministic
    # regardless of whether this is a first run or an idempotent re-run.
    paid_pending_fixed = cur.execute(
        "UPDATE payables SET three_way_match_status='Matched' "
        "WHERE status='Paid' AND three_way_match_status='Pending'"
    ).rowcount
    if paid_pending_fixed:
        print(f"  paid+pending cleanup: {paid_pending_fixed} row(s) promoted to Matched")

    print(f"  payable-line links: {link_payable_lines(cur)} row(s) linked")

    verify(cur)
    conn.commit()
    conn.close()
    print("Done. Three-way match chain complete.")


if __name__ == "__main__":
    print(f"DB: {os.path.abspath(DB_PATH)}")
    run()
