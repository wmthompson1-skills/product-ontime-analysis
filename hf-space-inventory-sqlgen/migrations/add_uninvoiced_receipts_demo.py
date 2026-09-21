"""Seed the Uninvoiced Receipts exception populations (three-way match).

The governed view ``payables_uninvoicedreceipts_20260706_000003`` (refactored
from the private-repo ground truth "203 3WM Uninvoiced Receipts") reports
receipt lines not fully matched to a payable. After ``complete_three_way_match``
the twin ledger is *too* clean: only one receipt line qualifies. This migration
adds three engineered exception populations, deterministically (no randomness,
dates derived from existing data) and idempotently (safe to re-run):

1. RECEIVED, NEVER INVOICED — PO-000002 and PO-000006 (previously receipt-less
   and invoice-less) get a full receipt of their FIRST line, no payable at all.
   These join PO-000009 in the "goods in, no invoice on file" population.
2. UNDER-INVOICED — PO-000007's first line is received in full but its new
   payable (INV-UNINV-01, Open / Pending) bills only 60% of the received
   quantity, so payable coverage < received quantity.
3. CANCELLED VOUCHER — PO-000013's first line is received in full and fully
   billed, but the voucher (INV-UNINV-02) is status 'Cancelled'; the exception
   logic excludes cancelled vouchers from coverage (the twin analog of the
   real query's PAY_STATUS <> 'L' / 'X'), so the line stays uninvoiced.

Rules respected:
  - No new PO headers (demo-scale bands untouched); receipts hang off existing
    open POs that carry no receipts and no invoices today.
  - MRP-critical POs (PO-MRP-*, PO-CON-001) never touched; no
    purchase_order.status changes; no inventory-ledger writes (receipts here
    are AP evidence, not stock) — same scope rules as complete_three_way_match.
  - Receipt dates are derived from MAX(receiving.receipt_date) with fixed
    per-population offsets; invoice dates follow receipt dates.
  - FAIL-CLOSED VERIFY at the end: each engineered population must appear in
    the governed view's result set, else exit non-zero.
"""

import os
import sqlite3
import sys
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema",
                       "manufacturing.db")

# (po_id, kind) — kind drives the payable treatment.
POPULATIONS = [
    ("PO-000002", "uninvoiced"),
    ("PO-000006", "uninvoiced"),
    ("PO-000007", "under_invoiced"),   # INV-UNINV-01, bills 60%
    ("PO-000013", "cancelled_voucher"),  # INV-UNINV-02, Cancelled
]

UNDER_INVOICED_FRACTION = 0.6
INVOICE_MARKERS = {"under_invoiced": "INV-UNINV-01",
                   "cancelled_voucher": "INV-UNINV-02"}
# receipt_date = base_date - offset_days (per PO, fixed & deterministic)
DATE_OFFSETS = {"PO-000002": 4, "PO-000006": 3,
                "PO-000007": 2, "PO-000013": 1}
PAYMENT_TERMS_DAYS = 30


def fmt(d):
    return d.isoformat()


def first_line(conn, po_id):
    row = conn.execute(
        """SELECT line_id, part_id, quantity, unit_cost
           FROM po_line WHERE po_id = ? ORDER BY line_id LIMIT 1""",
        (po_id,)).fetchone()
    if row is None:
        raise SystemExit(f"FAIL: {po_id} has no po_line rows")
    return row


def main():
    # NOTE: FK enforcement stays OFF (project convention: declared FKs are
    # structural metadata only; e.g. receiving -> work_order is not enforceable).
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    base = cur.execute("SELECT MAX(receipt_date) FROM receiving").fetchone()[0]
    if not base:
        raise SystemExit("FAIL: receiving is empty — run the 3WM chain first")
    base_date = date.fromisoformat(base)

    for po_id, kind in POPULATIONS:
        status, supplier_id = cur.execute(
            "SELECT status, supplier_id FROM purchase_order WHERE po_id = ?",
            (po_id,)).fetchone() or (None, None)
        if status is None:
            raise SystemExit(f"FAIL: {po_id} not found")
        if status == "Cancelled":
            raise SystemExit(f"FAIL: {po_id} is Cancelled — bad fixture pick")

        line_id, part_id, qty, unit_cost = first_line(cur, po_id)
        rcpt_date = base_date - timedelta(days=DATE_OFFSETS[po_id])

        # Receipt step — guarded on its own so a partially-applied run
        # (receipt in, voucher missing) self-heals: the payable step below
        # re-checks its OWN marker instead of piggybacking on this guard.
        existing = cur.execute(
            """SELECT rl.receipt_line_id FROM receiving r
               JOIN receiving_line rl ON rl.receipt_id = r.receipt_id
               WHERE r.po_id = ? ORDER BY rl.receipt_line_id LIMIT 1""",
            (po_id,)).fetchone()
        if existing:
            receipt_line_id = existing[0]
            print(f"  = {po_id}: receipt already present "
                  f"(line {receipt_line_id}), reusing (idempotent)")
        else:
            cur.execute(
                """INSERT INTO receiving (po_id, supplier_id, part_id,
                         quantity_ordered, quantity_received, receipt_date,
                         received_date, inspection_status, cert_required)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Passed', 0)""",
                (po_id, supplier_id, part_id, qty, qty,
                 fmt(rcpt_date), fmt(rcpt_date)))
            receipt_id = cur.lastrowid
            cur.execute(
                """INSERT INTO receiving_line (receipt_id, line_no, po_line_id,
                         part_id, quantity_ordered, quantity_received,
                         inspection_status, cert_required)
                   VALUES (?, 1, ?, ?, ?, ?, 'Passed', 0)""",
                (receipt_id, line_id, part_id, qty, qty))
            receipt_line_id = cur.lastrowid
            print(f"  + {po_id}: receipt {receipt_id} line {receipt_line_id} "
                  f"({part_id} x {qty}, {fmt(rcpt_date)})")

        if kind == "uninvoiced":
            continue  # no payable at all — that IS the population

        marker = INVOICE_MARKERS[kind]
        if cur.execute("SELECT COUNT(*) FROM payables WHERE invoice_number = ?",
                       (marker,)).fetchone()[0]:
            print(f"  = {marker} already present, skipping (idempotent)")
            continue

        if kind == "under_invoiced":
            billed_qty = round(qty * UNDER_INVOICED_FRACTION, 1)
            pay_status, match_status = "Open", "Pending"
        else:  # cancelled_voucher — bills in full but the voucher is void
            billed_qty = qty
            pay_status, match_status = "Cancelled", "Pending"
        amount = round(billed_qty * unit_cost, 2)
        inv_date = rcpt_date + timedelta(days=2)

        cur.execute(
            """INSERT INTO payables (po_id, supplier_id, invoice_number,
                     invoice_date, due_date, amount_dollars, status,
                     payment_date, three_way_match_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (po_id, supplier_id, marker, fmt(inv_date),
             fmt(inv_date + timedelta(days=PAYMENT_TERMS_DAYS)),
             amount, pay_status, match_status))
        invoice_id = cur.lastrowid
        cur.execute(
            """INSERT INTO payable_line (invoice_id, line_no, po_id, part_id,
                     qty, amount, po_line_id, receipt_line_id)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?)""",
            (invoice_id, po_id, part_id, billed_qty, amount, line_id,
             receipt_line_id))
        print(f"  + {marker}: {pay_status} voucher bills {billed_qty} of {qty} "
              f"against receipt line {receipt_line_id}")

    conn.commit()

    # ── SELECTOR WIRING ──────────────────────────────────────────────────
    # "Uninvoiced Receipts" was inserted into supplier_performance.sql
    # BEFORE "Three-Way Match Exceptions", shifting file order: Uninvoiced
    # is now the 6th "-- Query:" marker (0-based index 5) and Three-Way
    # moved to index 6. schema_intent_queries has a unique index on
    # (intent_id, query_file, query_index), so bump Three-Way first, then
    # insert Uninvoiced at 5 under the same payables intent. Intent ID is
    # resolved BY NAME (never hardcode autoincrement IDs — see
    # add_supplier_payables_wiring.py's 2026-09-21 fix). Idempotent: the
    # guarded UPDATE matches nothing on re-run, and the INSERT OR IGNORE is
    # a no-op once the row exists.
    payables_intent = cur.execute(
        "SELECT intent_id FROM schema_intents WHERE intent_name = 'supplier_payables_exposure'"
    ).fetchone()
    if not payables_intent:
        raise SystemExit(
            "FAIL: intent 'supplier_payables_exposure' not found — run "
            "add_supplier_payables_wiring.py first")
    payables_intent_id = payables_intent[0]

    cur.execute(
        """UPDATE schema_intent_queries SET query_index = 6
           WHERE intent_id = ? AND query_file = 'supplier_performance.sql'
             AND query_name = 'Three-Way Match Exceptions'
             AND query_index = 5""", (payables_intent_id,))
    if cur.rowcount:
        print("  ~ Three-Way Match Exceptions: query_index 5 -> 6 "
              "(file order shifted)")
    cur.execute(
        """INSERT OR IGNORE INTO schema_intent_queries
               (intent_id, query_category, query_file, query_index, query_name)
           VALUES (?, 'supplier_performance', 'supplier_performance.sql',
                   5, 'Uninvoiced Receipts')""", (payables_intent_id,))
    if cur.rowcount:
        print(f"  + intent_query 'Uninvoiced Receipts' wired to "
              f"supplier_payables_exposure (intent {payables_intent_id})")
    else:
        print("  = intent_query 'Uninvoiced Receipts' already wired, skipping")
    conn.commit()

    # ── FAIL-CLOSED VERIFY ────────────────────────────────────────────────
    view_path = os.path.join(
        os.path.dirname(__file__), "..", "app_schema", "ground_truth",
        "sql_snippets", "payables_uninvoicedreceipts_20260706_000003.sql")
    with open(view_path, encoding="utf-8") as fh:
        view_sql = fh.read()
    # The governed view now carries the Temporal Parameter Contract's named
    # placeholders (:start_date / :end_date / :supplier_id). They are all
    # NULL-guarded, so binding every one to NULL reproduces the unfiltered
    # exception population this verify asserts on.
    view_params = {"start_date": None, "end_date": None, "supplier_id": None}
    rows = cur.execute(view_sql, view_params).fetchall()
    reported_pos = {r[3] for r in rows}
    missing = [po for po, _ in POPULATIONS if po not in reported_pos]
    if missing:
        raise SystemExit(
            f"FAIL: engineered populations missing from the governed view: "
            f"{missing} (reported: {sorted(reported_pos)})")

    # Per-population fixture-shape assertions — presence in the view is not
    # enough; each engineered PO must carry the exact payable shape its
    # population promises (else a partial run could pass on presence alone).
    for po_id, kind in POPULATIONS:
        vouchers = cur.execute(
            """SELECT p.invoice_number, p.status, pl.qty
               FROM payables p JOIN payable_line pl
                 ON pl.invoice_id = p.invoice_id
               WHERE p.po_id = ? AND p.invoice_number LIKE 'INV-UNINV-%'""",
            (po_id,)).fetchall()
        if kind == "uninvoiced":
            if vouchers:
                raise SystemExit(
                    f"FAIL: {po_id} is 'never invoiced' but carries "
                    f"engineered voucher(s): {vouchers}")
            continue
        marker = INVOICE_MARKERS[kind]
        match = [v for v in vouchers if v[0] == marker]
        if len(match) != 1:
            raise SystemExit(
                f"FAIL: {po_id} expected exactly one voucher {marker}, "
                f"found: {vouchers}")
        inv_no, pay_status, billed_qty = match[0]
        _, _, qty, _ = first_line(cur, po_id)
        if kind == "under_invoiced":
            expected_qty = round(qty * UNDER_INVOICED_FRACTION, 1)
            if pay_status != "Open" or billed_qty != expected_qty:
                raise SystemExit(
                    f"FAIL: {po_id}/{marker} expected Open voucher billing "
                    f"{expected_qty}, got status={pay_status} qty={billed_qty}")
        else:  # cancelled_voucher
            if pay_status != "Cancelled" or billed_qty != qty:
                raise SystemExit(
                    f"FAIL: {po_id}/{marker} expected Cancelled voucher "
                    f"billing {qty} in full, got status={pay_status} "
                    f"qty={billed_qty}")

    print(f"  VERIFY OK: governed view reports {len(rows)} uninvoiced "
          f"receipt(s) across POs {sorted(reported_pos)}; all fixture "
          f"shapes confirmed")
    conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
