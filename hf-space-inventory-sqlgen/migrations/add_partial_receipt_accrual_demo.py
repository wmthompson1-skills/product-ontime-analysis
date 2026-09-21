"""Seed the Partial-Receipt Accrual Exposure populations (PO-line accrual).

The governed view ``payables_partialreceiptaccrual_20260708_000004`` (grounded
in the TWM accrual guides, docs/my-mrp-kb/07-three-way-match/) reports PO lines
in the partial-receipt accrual condition:

    receivedQty > 0 AND receivedQty < orderedQty AND invoicedQty < receivedQty

i.e. open Purchase Receipt Accrual (PRA) exposure on partially received lines.
After ``complete_three_way_match`` + ``add_uninvoiced_receipts_demo`` the twin
ledger has partial receipts, but every one of them is fully voucher-covered,
so the view is empty. This migration adds two engineered populations,
deterministically (no randomness, dates derived from existing data) and
idempotently (safe to re-run):

1. PARTIAL RECEIPT, NEVER INVOICED — PO-000009 gets ONE new engineered line
   (qty 20) received at 60% (12), with no payable at all. The full received
   quantity is open PRA.
2. PARTIAL RECEIPT, UNDER-INVOICED — PO-000010 gets ONE new engineered line
   (qty 20) received at 70% (14); its voucher (INV-PRA-01, Open / Pending)
   bills only half the received quantity (7), so voucher coverage < received.

Rules respected:
  - No new PO headers (demo-scale bands scale via PO LINES, never headers);
    engineered lines hang off existing non-cancelled material POs.
  - MRP-critical POs (PO-MRP-*, PO-CON-001) never touched; no
    purchase_order.status changes; no inventory-ledger writes (receipts here
    are AP evidence, not stock) — same scope rules as the sibling demos.
  - The engineered part on each line is chosen DETERMINISTICALLY from the
    supplier's own catalog: the alphabetically-first part this supplier
    already supplies on other po_lines that is not already on the target PO
    (grounded in the real supplier relationship, never invented).
  - purchase_order.total_cost is incremented by the new line_total so the
    header stays consistent with SUM(po_line.line_total).
  - Receipt dates are derived from MAX(receiving.receipt_date) with fixed
    per-population offsets; invoice dates follow receipt dates.
  - FAIL-CLOSED VERIFY at the end: each engineered population must appear in
    the governed view's result set with the exact quantity shape, else exit
    non-zero.
"""

import os
import sqlite3
import sys
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema",
                       "manufacturing.db")

ORDERED_QTY = 20.0
# (po_id, kind, received_qty)
POPULATIONS = [
    ("PO-000009", "uninvoiced", 12.0),       # 60% received, no voucher
    ("PO-000010", "under_invoiced", 14.0),   # 70% received, half-billed
]
UNDER_INVOICED_BILL_FRACTION = 0.5           # of the RECEIVED quantity
INVOICE_MARKER = "INV-PRA-01"
# Explicit idempotency marker stamped onto the engineered line's description
# (quantity-only detection is brittle under future seed drift).
DESCRIPTION_MARKER = " [PRA demo line]"
# receipt_date = base_date - offset_days (per PO, fixed & deterministic)
DATE_OFFSETS = {"PO-000009": 6, "PO-000010": 5}
PAYMENT_TERMS_DAYS = 30

VIEW_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app_schema", "ground_truth",
    "sql_snippets", "payables_partialreceiptaccrual_20260708_000004.sql")


def fmt(d):
    return d.isoformat()


def pick_supplier_part(cur, po_id, supplier_id):
    """Alphabetically-first part the supplier already supplies elsewhere that
    is not already on this PO — deterministic and grounded in the supplier's
    real catalog."""
    row = cur.execute(
        """SELECT pl.part_id
           FROM po_line pl
           JOIN purchase_order po ON po.po_id = pl.po_id
           WHERE po.supplier_id = ?
             AND pl.part_id NOT IN
                 (SELECT part_id FROM po_line WHERE po_id = ?)
           ORDER BY pl.part_id LIMIT 1""",
        (supplier_id, po_id)).fetchone()
    if row is None:
        raise SystemExit(
            f"FAIL: supplier {supplier_id} has no other-catalog part usable "
            f"for an engineered line on {po_id}")
    return row[0]


def main():
    # NOTE: FK enforcement stays OFF (project convention: declared FKs are
    # structural metadata only).
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    base = cur.execute("SELECT MAX(receipt_date) FROM receiving").fetchone()[0]
    if not base:
        raise SystemExit("FAIL: receiving is empty — run the 3WM chain first")
    base_date = date.fromisoformat(base)

    engineered = {}  # po_id -> (line_id, part_id, unit_cost, received_qty)

    for po_id, kind, received_qty in POPULATIONS:
        row = cur.execute(
            """SELECT status, supplier_id, po_type FROM purchase_order
               WHERE po_id = ?""", (po_id,)).fetchone()
        if row is None:
            raise SystemExit(f"FAIL: {po_id} not found")
        status, supplier_id, po_type = row
        if status == "Cancelled":
            raise SystemExit(f"FAIL: {po_id} is Cancelled — bad fixture pick")
        if po_type != "material":
            raise SystemExit(f"FAIL: {po_id} is not a material PO")

        # ── Engineered PO line ──
        # Idempotency guard: the engineered line is stamped with an explicit
        # DESCRIPTION_MARKER suffix, so a prior run is detected by marker —
        # NOT by re-running the part pick, whose result shifts once the
        # engineered part is on the PO, and NOT by quantity alone, which is
        # brittle if the seed later gains another ORDERED_QTY line.
        existing_line = cur.execute(
            """SELECT line_id, part_id, unit_cost FROM po_line
               WHERE po_id = ? AND part_description LIKE ?
               ORDER BY line_id LIMIT 1""",
            (po_id, "%" + DESCRIPTION_MARKER)).fetchone()
        if existing_line is None:
            # Legacy self-heal: earlier revisions detected by quantity only
            # and wrote no marker. Stamp such a line once so future runs
            # match on the marker.
            legacy = cur.execute(
                """SELECT line_id, part_id, unit_cost FROM po_line
                   WHERE po_id = ? AND quantity = ?
                   ORDER BY line_id LIMIT 1""",
                (po_id, ORDERED_QTY)).fetchone()
            if legacy:
                cur.execute(
                    """UPDATE po_line
                       SET part_description = part_description || ?
                       WHERE line_id = ?""",
                    (DESCRIPTION_MARKER, legacy[0]))
                existing_line = legacy
                print(f"  ~ {po_id}: legacy engineered line {legacy[0]} "
                      f"stamped with marker (self-heal)")
        if existing_line:
            line_id, part_id, unit_cost = existing_line
            print(f"  = {po_id}: engineered line already present "
                  f"(line {line_id}, {part_id}), reusing (idempotent)")
        else:
            part_id = pick_supplier_part(cur, po_id, supplier_id)
            prow = cur.execute(
                """SELECT part_description, unit_cost FROM part
                   WHERE part_id = ?""", (part_id,)).fetchone()
            if prow is None:
                raise SystemExit(f"FAIL: part {part_id} missing from part master")
            part_description, unit_cost = prow
            part_description = part_description + DESCRIPTION_MARKER
            line_total = round(ORDERED_QTY * unit_cost, 2)
            cur.execute(
                """INSERT INTO po_line (po_id, part_id, part_description,
                         quantity, unit_cost, line_total)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (po_id, part_id, part_description, ORDERED_QTY, unit_cost,
                 line_total))
            line_id = cur.lastrowid
            cur.execute(
                """UPDATE purchase_order
                   SET total_cost = ROUND(total_cost + ?, 2)
                   WHERE po_id = ?""", (line_total, po_id))
            print(f"  + {po_id}: engineered line {line_id} "
                  f"({part_id} x {ORDERED_QTY} @ {unit_cost})")

        engineered[po_id] = (line_id, part_id, unit_cost, received_qty)

        # ── Partial receipt (guarded on its own; self-heals partial runs) ──
        rcpt_date = base_date - timedelta(days=DATE_OFFSETS[po_id])
        existing_rl = cur.execute(
            """SELECT receipt_line_id FROM receiving_line
               WHERE po_line_id = ? ORDER BY receipt_line_id LIMIT 1""",
            (line_id,)).fetchone()
        if existing_rl:
            receipt_line_id = existing_rl[0]
            print(f"  = {po_id}: partial receipt already present "
                  f"(line {receipt_line_id}), reusing (idempotent)")
        else:
            cur.execute(
                """INSERT INTO receiving (po_id, supplier_id, part_id,
                         quantity_ordered, quantity_received, receipt_date,
                         received_date, inspection_status, cert_required)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Passed', 0)""",
                (po_id, supplier_id, part_id, ORDERED_QTY, received_qty,
                 fmt(rcpt_date), fmt(rcpt_date)))
            receipt_id = cur.lastrowid
            cur.execute(
                """INSERT INTO receiving_line (receipt_id, line_no, po_line_id,
                         part_id, quantity_ordered, quantity_received,
                         inspection_status, cert_required)
                   VALUES (?, 1, ?, ?, ?, ?, 'Passed', 0)""",
                (receipt_id, line_id, part_id, ORDERED_QTY, received_qty))
            receipt_line_id = cur.lastrowid
            print(f"  + {po_id}: partial receipt {receipt_id} line "
                  f"{receipt_line_id} ({part_id} {received_qty} of "
                  f"{ORDERED_QTY}, {fmt(rcpt_date)})")

        if kind == "uninvoiced":
            continue  # no payable at all — that IS the population

        # ── Under-billing voucher (marker-guarded, idempotent) ──
        if cur.execute("SELECT COUNT(*) FROM payables WHERE invoice_number = ?",
                       (INVOICE_MARKER,)).fetchone()[0]:
            print(f"  = {INVOICE_MARKER} already present, skipping (idempotent)")
            continue
        billed_qty = round(received_qty * UNDER_INVOICED_BILL_FRACTION, 1)
        amount = round(billed_qty * unit_cost, 2)
        inv_date = rcpt_date + timedelta(days=2)
        cur.execute(
            """INSERT INTO payables (po_id, supplier_id, invoice_number,
                     invoice_date, due_date, amount_dollars, status,
                     payment_date, three_way_match_status)
               VALUES (?, ?, ?, ?, ?, ?, 'Open', NULL, 'Pending')""",
            (po_id, supplier_id, INVOICE_MARKER, fmt(inv_date),
             fmt(inv_date + timedelta(days=PAYMENT_TERMS_DAYS)), amount))
        invoice_id = cur.lastrowid
        cur.execute(
            """INSERT INTO payable_line (invoice_id, line_no, po_id, part_id,
                     qty, amount, po_line_id, receipt_line_id)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?)""",
            (invoice_id, po_id, part_id, billed_qty, amount, line_id,
             receipt_line_id))
        print(f"  + {INVOICE_MARKER}: Open voucher bills {billed_qty} of "
              f"{received_qty} received against line {line_id}")

    conn.commit()

    # ── SELECTOR WIRING ──────────────────────────────────────────────────
    # "Partial-Receipt Accrual Exposure" was inserted into
    # supplier_performance.sql AFTER "Uninvoiced Receipts" and BEFORE
    # "Three-Way Match Exceptions", shifting file order: PRA is the 7th
    # "-- Query:" marker (0-based index 6) and Three-Way moved to index 7.
    # schema_intent_queries has a unique index on (intent_id, query_file,
    # query_index), so bump Three-Way first, then insert PRA at 6 under the
    # same payables intent. Intent ID is resolved BY NAME (never hardcode
    # autoincrement IDs — see add_supplier_payables_wiring.py's 2026-09-21
    # fix). Idempotent: the guarded UPDATE matches nothing on re-run, and
    # the INSERT OR IGNORE is a no-op once present.
    payables_intent = cur.execute(
        "SELECT intent_id FROM schema_intents WHERE intent_name = 'supplier_payables_exposure'"
    ).fetchone()
    if not payables_intent:
        raise SystemExit(
            "FAIL: intent 'supplier_payables_exposure' not found — run "
            "add_supplier_payables_wiring.py first")
    payables_intent_id = payables_intent[0]

    cur.execute(
        """UPDATE schema_intent_queries SET query_index = 7
           WHERE intent_id = ? AND query_file = 'supplier_performance.sql'
             AND query_name = 'Three-Way Match Exceptions'
             AND query_index = 6""", (payables_intent_id,))
    if cur.rowcount:
        print("  ~ Three-Way Match Exceptions: query_index 6 -> 7 "
              "(file order shifted)")
    cur.execute(
        """INSERT OR IGNORE INTO schema_intent_queries
               (intent_id, query_category, query_file, query_index, query_name)
           VALUES (?, 'supplier_performance', 'supplier_performance.sql',
                   6, 'Partial-Receipt Accrual Exposure')""", (payables_intent_id,))
    if cur.rowcount:
        print(f"  + intent_query 'Partial-Receipt Accrual Exposure' wired "
              f"to supplier_payables_exposure (intent {payables_intent_id})")
    else:
        print("  = intent_query 'Partial-Receipt Accrual Exposure' already "
              "wired, skipping")
    conn.commit()

    # ── FAIL-CLOSED VERIFY ────────────────────────────────────────────────
    with open(VIEW_PATH, encoding="utf-8") as fh:
        view_sql = fh.read()
    view_params = {"start_date": None, "end_date": None, "supplier_id": None}
    rows = cur.execute(view_sql, view_params).fetchall()
    # view columns: query_name, purc_order_id, po_line_id, part_id, vendor_id,
    # vendor_name, site_id, qty_ordered, qty_received, qty_invoiced,
    # qty_uninvoiced, accrued_value, last_receipt_date
    by_line = {r[2]: r for r in rows}
    for po_id, kind, received_qty in POPULATIONS:
        line_id, part_id, unit_cost, _ = engineered[po_id]
        row = by_line.get(line_id)
        if row is None:
            raise SystemExit(
                f"FAIL: engineered line {line_id} ({po_id}) missing from the "
                f"governed view (reported lines: {sorted(by_line)})")
        qty_ordered, qty_received, qty_invoiced, qty_uninvoiced = row[7:11]
        if qty_ordered != ORDERED_QTY or qty_received != received_qty:
            raise SystemExit(
                f"FAIL: {po_id} line {line_id} expected ordered "
                f"{ORDERED_QTY} / received {received_qty}, got "
                f"{qty_ordered} / {qty_received}")
        if not (0 < qty_received < qty_ordered):
            raise SystemExit(
                f"FAIL: {po_id} line {line_id} is not a partial receipt")
        if kind == "uninvoiced":
            if qty_invoiced != 0:
                raise SystemExit(
                    f"FAIL: {po_id} line {line_id} is 'never invoiced' but "
                    f"shows voucher coverage {qty_invoiced}")
        else:
            expected_billed = round(received_qty *
                                    UNDER_INVOICED_BILL_FRACTION, 1)
            if qty_invoiced != expected_billed:
                raise SystemExit(
                    f"FAIL: {po_id} line {line_id} expected voucher coverage "
                    f"{expected_billed}, got {qty_invoiced}")
        if qty_uninvoiced != round(qty_received - qty_invoiced, 1):
            raise SystemExit(
                f"FAIL: {po_id} line {line_id} qty_uninvoiced arithmetic "
                f"drifted: {qty_uninvoiced}")

    # Header consistency: total_cost must equal SUM(line_total) for touched POs.
    for po_id in engineered:
        drift = cur.execute(
            """SELECT ROUND(po.total_cost, 2) - ROUND(SUM(pl.line_total), 2)
               FROM purchase_order po JOIN po_line pl ON pl.po_id = po.po_id
               WHERE po.po_id = ? GROUP BY po.po_id""", (po_id,)).fetchone()[0]
        if abs(drift) > 0.01:
            raise SystemExit(
                f"FAIL: {po_id} header total_cost drifted from line sum "
                f"by {drift}")

    print(f"  VERIFY OK: governed view reports {len(rows)} partial-receipt "
          f"accrual exposure line(s); both fixture shapes confirmed")
    conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
