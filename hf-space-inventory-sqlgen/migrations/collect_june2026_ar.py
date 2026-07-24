"""Collect June 2026 AR — three weekly installments (July 7, July 14, July 21).

Business story: all five open June 2026 accounts-receivable invoices are
collected across three equal weekly installments in July 2026. This completes
the cash-to-cash cycle for the June close and makes the AR aging story
credible through July 2026.

Five invoices open as of 2026-06-30 (total $60,823.54):
  AR-CO-00007  $1,051.98   Open
  AR-CO-00004  $4,629.32   Open
  AR-CO-00006  $403.84     Open
  AR-CO-00013  $816.05     Open
  AR-CO-00009  $53,922.35  Disputed (Gulfstream Aerospace)

What this migration does (deterministic, idempotent, fail-closed):

1. DDL — creates receivable_payment with a UNIQUE(invoice_id, installment_no)
   constraint (idempotency key). Also registered in schema_nodes +
   schema_edges (edges 44-45).

2. INSTALLMENTS — for each of the 5 open June invoices, splits the amount
   into three installments (integer-cent thirds; final tranche carries the
   cent-exact remainder). Payment dates: 2026-07-07, 2026-07-14, 2026-07-21.
   Inserts via INSERT OR IGNORE (safe to re-run).

3. GL POSTING — calls post_cash_receipt for each installment row, keyed by
   (source_table='receivable_payment', source_id=payment_id). Idempotent via
   the (source_table, source_id, event_type) key in gl_events.

4. STATUS UPDATE — marks all collected invoices status='Paid',
   payment_date='2026-07-21', guarded by: all 3 installments present AND
   SUM(installments) == amount_dollars ± 0.01. Only transitions Open/Disputed
   -> Paid; already-Paid invoices are skipped.

5. FAIL-CLOSED VERIFY — checks (all N-invoice-agnostic — no hardcoded count):
   - receivable_payment row count == N invoices × 3 installments
   - SUM(installments) == amount_dollars per invoice (±$0.01)
   - all collected invoices are now Paid with payment_date='2026-07-21'
   - gl_events gained exactly N × 3 CASH_RECEIPT rows
   - all CASH_RECEIPT amounts are positive

Out of scope (deliberate):
  - New AR invoices for the CO-MRP-002 July 23 shipment (July AR, not June AR).
  - GL control accounts / cash balance accounts (none by design).
  - Revenue recognition entries (shipment already relieves FG cost).
  - Changes to MRP or demand logic.

Run once (safe to re-run):
    cd hf-space-inventory-sqlgen
    python migrations/collect_june2026_ar.py
"""

import os
import sqlite3
import sys

HF_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(HF_DIR, "app_schema", "manufacturing.db")

sys.path.insert(0, HF_DIR)

PAYMENT_DATES = ["2026-07-07", "2026-07-14", "2026-07-21"]
FINAL_PAYMENT_DATE = "2026-07-21"
JUNE_CUTOFF = "2026-07-01"

DDL_PAYMENT = """
CREATE TABLE IF NOT EXISTS receivable_payment (
    payment_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      INTEGER NOT NULL,
    installment_no  INTEGER NOT NULL CHECK(installment_no IN (1,2,3)),
    payment_date    DATE NOT NULL,
    amount          REAL NOT NULL CHECK(amount > 0),
    source_event_id INTEGER,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (invoice_id)      REFERENCES receivable(invoice_id),
    FOREIGN KEY (source_event_id) REFERENCES gl_events(event_id),
    UNIQUE (invoice_id, installment_no)
);
"""

PAYMENT_TABLE_REG = (
    "receivable_payment",
    "AR installment payment records — one row per installment per invoice, "
    "linked to the cash-receipt GL event",
)

PAYMENT_EDGES = [
    (44, "receivable_payment", "receivable", "FOREIGN_KEY", "invoice_id",
     "AR payment installment collected against a customer invoice"),
    (45, "receivable_payment", "gl_events", "FOREIGN_KEY", "source_event_id",
     "AR payment installment linked to its cash-receipt GL event"),
]


def _fail(msg):
    raise SystemExit(f"[collect_june2026_ar] FAIL-CLOSED: {msg}")


def _split_thirds(amount_dollars):
    """Split amount into 3 installments (integer-cent arithmetic).

    Returns [inst1, inst2, inst3] where inst1 == inst2 and inst3 carries
    the cent-exact remainder so the three sum to amount_dollars exactly.
    """
    cents = round(amount_dollars * 100)
    base = cents // 3
    remainder = cents - 2 * base
    return [base / 100, base / 100, remainder / 100]


def run():
    from gl_posting import post_cash_receipt

    print(f"DB path: {os.path.abspath(DB_PATH)}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=10000")
    cur = conn.cursor()

    # Phase 1 — DDL
    print("\nPhase 1 — create receivable_payment table ...")
    cur.executescript(DDL_PAYMENT)

    cur.execute(
        "INSERT OR IGNORE INTO schema_nodes (table_name, table_type, description) "
        "VALUES (?, 'Table', ?)",
        PAYMENT_TABLE_REG,
    )
    for edge_id, ft, tt, rel, col, alias in PAYMENT_EDGES:
        cur.execute(
            "INSERT OR IGNORE INTO schema_edges "
            "(edge_id, from_table, to_table, relationship_type, join_column, "
            "weight, natural_language_alias) VALUES (?,?,?,?,?,1,?)",
            (edge_id, ft, tt, rel, col, alias),
        )

    # Phase 2 — identify the 5 open June 2026 invoices
    print("\nPhase 2 — identify open June 2026 invoices ...")
    invoices = cur.execute(
        """
        SELECT invoice_id, invoice_number, amount_dollars, status
        FROM receivable
        WHERE status IN ('Open','Disputed')
          AND invoice_date < ?
        ORDER BY invoice_id
        """,
        (JUNE_CUTOFF,),
    ).fetchall()

    if not invoices:
        print("  No open June invoices found — nothing to collect.")
        conn.close()
        return

    print(f"  Found {len(invoices)} open June invoice(s):")
    for inv_id, inv_no, amt, status in invoices:
        print(f"    {inv_no}  {status}  ${amt:,.2f}")

    # Phase 3 — insert installments and post GL events
    print("\nPhase 3 — insert installments and post CASH_RECEIPT events ...")
    total_inserted = 0
    total_posted = 0

    for invoice_id, invoice_number, amount_dollars, _status in invoices:
        thirds = _split_thirds(amount_dollars)
        for inst_no, (inst_amount, pay_date) in enumerate(
            zip(thirds, PAYMENT_DATES), start=1
        ):
            cur.execute(
                """
                INSERT OR IGNORE INTO receivable_payment
                    (invoice_id, installment_no, payment_date, amount)
                VALUES (?, ?, ?, ?)
                """,
                (invoice_id, inst_no, pay_date, inst_amount),
            )
            if cur.rowcount > 0:
                total_inserted += 1
            payment_id = cur.execute(
                "SELECT payment_id FROM receivable_payment "
                "WHERE invoice_id = ? AND installment_no = ?",
                (invoice_id, inst_no),
            ).fetchone()[0]

            ev = post_cash_receipt(
                cur, invoice_id, inst_amount, pay_date,
                "receivable_payment", payment_id,
            )
            if ev is not None:
                total_posted += 1

    print(
        f"  installment rows inserted this run: {total_inserted} "
        f"(already-present rows skipped via INSERT OR IGNORE)"
    )
    print(f"  CASH_RECEIPT events posted this run: {total_posted}")

    # Phase 4 — mark all collected invoices Paid (guarded)
    print("\nPhase 4 — mark invoices Paid (guarded transition) ...")
    marked_paid = 0
    for invoice_id, invoice_number, amount_dollars, _status in invoices:
        inst_rows = cur.execute(
            "SELECT COUNT(*), ROUND(SUM(amount), 2) "
            "FROM receivable_payment WHERE invoice_id = ?",
            (invoice_id,),
        ).fetchone()
        n_inst, inst_sum = inst_rows
        if n_inst != 3:
            _fail(
                f"{invoice_number}: expected 3 installments, found {n_inst}"
            )
        if abs(inst_sum - round(amount_dollars, 2)) > 0.01:
            _fail(
                f"{invoice_number}: installment sum {inst_sum} != "
                f"invoice amount {amount_dollars} (tolerance 0.01)"
            )
        cur.execute(
            """
            UPDATE receivable
            SET status = 'Paid', payment_date = ?
            WHERE invoice_id = ? AND status IN ('Open','Disputed')
            """,
            (FINAL_PAYMENT_DATE, invoice_id),
        )
        if cur.rowcount > 0:
            marked_paid += 1
    print(f"  invoices marked Paid this run: {marked_paid}")

    # Phase 5 — fail-closed verify (reads uncommitted writes through same conn)
    print("\nPhase 5 — fail-closed verify ...")
    errors = []

    expected_payments = len(invoices) * 3
    n_payments = cur.execute(
        "SELECT COUNT(*) FROM receivable_payment "
        "WHERE invoice_id IN (SELECT invoice_id FROM receivable "
        "WHERE invoice_date < ? )",
        (JUNE_CUTOFF,),
    ).fetchone()[0]
    if n_payments != expected_payments:
        errors.append(
            f"receivable_payment row count = {n_payments}, "
            f"expected {expected_payments} ({len(invoices)} invoice(s) × 3)"
        )

    for invoice_id, invoice_number, amount_dollars, _ in invoices:
        row = cur.execute(
            "SELECT COUNT(*), ROUND(SUM(amount), 2) FROM receivable_payment "
            "WHERE invoice_id = ?",
            (invoice_id,),
        ).fetchone()
        n, s = row
        if n != 3:
            errors.append(f"{invoice_number}: {n} installments (expected 3)")
        if abs(s - round(amount_dollars, 2)) > 0.01:
            errors.append(
                f"{invoice_number}: SUM(installments)={s} != amount={amount_dollars}"
            )

    not_paid = cur.execute(
        """
        SELECT COUNT(*) FROM receivable
        WHERE status IN ('Open','Disputed') AND invoice_date < ?
        """,
        (JUNE_CUTOFF,),
    ).fetchone()[0]
    if not_paid:
        errors.append(f"{not_paid} June invoices still not Paid")

    invoice_ids_sql = ",".join(str(inv[0]) for inv in invoices)
    wrong_pay_date = cur.execute(
        f"""
        SELECT COUNT(*) FROM receivable
        WHERE invoice_id IN ({invoice_ids_sql}) AND status = 'Paid'
          AND (payment_date IS NULL OR payment_date != ?)
        """,
        (FINAL_PAYMENT_DATE,),
    ).fetchone()[0]
    if wrong_pay_date:
        errors.append(
            f"{wrong_pay_date} of the {len(invoices)} collected invoices "
            f"have wrong payment_date"
        )

    # Count only the CASH_RECEIPT events linked to this migration's payments
    # (scoped by payment_id so future migrations can add other CASH_RECEIPT rows
    # without breaking the gate).
    cr_count = cur.execute(
        f"SELECT COUNT(*) FROM gl_events WHERE event_type = 'CASH_RECEIPT' "
        f"AND source_table = 'receivable_payment' "
        f"AND source_id IN ("
        f"  SELECT payment_id FROM receivable_payment "
        f"  WHERE invoice_id IN ({invoice_ids_sql})"
        f")"
    ).fetchone()[0]
    if cr_count != expected_payments:
        errors.append(
            f"CASH_RECEIPT event count = {cr_count}, "
            f"expected {expected_payments} ({len(invoices)} invoice(s) × 3)"
        )

    neg_cr = cur.execute(
        "SELECT COUNT(*) FROM gl_events "
        "WHERE event_type = 'CASH_RECEIPT' AND amount <= 0"
    ).fetchone()[0]
    if neg_cr:
        errors.append(f"{neg_cr} CASH_RECEIPT event(s) with non-positive amount")

    if errors:
        for e in errors:
            print(f"  VERIFY FAIL: {e}")
        conn.close()
        _fail("verification failed — see VERIFY FAIL lines above")

    conn.commit()
    print(
        f"  VERIFY OK — {expected_payments} installments, "
        f"all {len(invoices)} June invoices Paid, "
        f"{expected_payments} CASH_RECEIPT events, all amounts positive"
    )
    conn.close()
    print("\n[collect_june2026_ar] committed — June 2026 AR fully collected.")


if __name__ == "__main__":
    run()
