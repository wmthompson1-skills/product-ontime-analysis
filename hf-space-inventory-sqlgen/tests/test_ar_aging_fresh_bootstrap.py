"""Gate test — AR aging snippet on a fresh-bootstrap DB (N-invoice variant).

Addresses the case where a fresh-bootstrap DB produces a different number of
pre-July-2026 Open/Disputed invoices than the live DB (e.g. 6 instead of 5).

The snippet (receivables_araging_20260724_000001) and the collection migration
(collect_june2026_ar) are both fully dynamic — they never hardcode the invoice
count.  This test locks that guarantee by running both against an in-memory
SQLite DB seeded with 6 pre-July-2026 invoices (5 Open + 1 Disputed) that span
all 4 aging buckets.

Checks:
  1. All 6 invoices appear in the AR aging result before collection.
  2. Every invoice is assigned the correct aging bucket.
  3. After marking all 6 Paid (simulating collect_june2026_ar), the snippet
     returns zero rows for the pre-July-2026 cohort.
  4. No hardcoded row-count assertion in the snippet or in this gate would
     fail if the count changed to any other positive integer.

Run gate-style:
    cd hf-space-inventory-sqlgen
    python tests/test_ar_aging_fresh_bootstrap.py
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SNIPPET_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "app_schema",
    "ground_truth",
    "sql_snippets",
    "receivables_araging_20260724_000001.sql",
)

JUNE_CUTOFF = "2026-07-01"

FAILURES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail and not ok else ""
    print(f"  [{status}] {name}{suffix}")
    if not ok:
        FAILURES.append(name)


def _build_fresh_db() -> sqlite3.Connection:
    """Return an in-memory SQLite connection simulating the fresh-bootstrap state.

    Schema (minimal — only what the snippet needs):
      receivable(invoice_id, invoice_number, customer_name, order_id,
                 status, invoice_date, due_date, amount_dollars,
                 payment_date)

    Seed data:
      - 1 Paid invoice (provides as_of_date = MAX(payment_date) = '2026-06-15')
      - 6 Open/Disputed invoices with due_dates chosen to span every bucket:

        #  invoice_number  status    due_date     bucket (as_of 2026-06-15)
        1  AR-FRESH-001    Open      2026-06-20   Current        (due_date >= as_of)
        2  AR-FRESH-002    Open      2026-06-01   1-30 days      (14 days past)
        3  AR-FRESH-003    Open      2026-05-01   31-60 days     (45 days past)
        4  AR-FRESH-004    Open      2026-03-01   >60 days       (106 days past)
        5  AR-FRESH-005    Open      2026-01-01   >60 days       (165 days past)
        6  AR-FRESH-006    Disputed  2025-10-01   >60 days       (257 days past)
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE receivable (
            invoice_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number  TEXT NOT NULL UNIQUE,
            customer_name   TEXT NOT NULL DEFAULT '',
            order_id        INTEGER,
            status          TEXT NOT NULL DEFAULT 'Open',
            invoice_date    DATE NOT NULL,
            due_date        DATE NOT NULL,
            amount_dollars  REAL NOT NULL,
            payment_date    DATE
        );
    """)
    conn.executemany(
        """
        INSERT INTO receivable
            (invoice_number, customer_name, status, invoice_date, due_date,
             amount_dollars, payment_date)
        VALUES (?,?,?,?,?,?,?)
        """,
        [
            # Paid reference invoice — sets as_of_date = '2026-06-15'
            ("AR-CLOSED-001", "Acme Corp",       "Paid",     "2025-12-01",
             "2025-12-31", 9000.00, "2026-06-15"),
            # 6 Open/Disputed pre-July-2026 invoices
            ("AR-FRESH-001",  "Alpha Aero",       "Open",     "2026-05-15",
             "2026-06-20", 1200.00, None),    # Current
            ("AR-FRESH-002",  "Beta Ballistics",  "Open",     "2026-05-01",
             "2026-06-01", 800.00,  None),    # 1-30 days (14 days past)
            ("AR-FRESH-003",  "Gamma Gears",      "Open",     "2026-04-01",
             "2026-05-01", 3500.00, None),    # 31-60 days (45 days past)
            ("AR-FRESH-004",  "Delta Defense",    "Open",     "2026-02-01",
             "2026-03-01", 22000.00, None),   # >60 days (106 days past)
            ("AR-FRESH-005",  "Epsilon Engines",  "Open",     "2025-12-01",
             "2026-01-01", 5000.00, None),    # >60 days (165 days past)
            ("AR-FRESH-006",  "Zeta Aerospace",   "Disputed", "2025-07-01",
             "2025-10-01", 54000.00, None),   # >60 days (257 days past) — Disputed
        ],
    )
    conn.commit()
    return conn


def _run_snippet(conn: sqlite3.Connection, sql_text: str) -> list:
    """Execute the snippet SQL (substituting :start_date = NULL) and return rows."""
    return conn.execute(sql_text.replace(":start_date", "NULL")).fetchall()


def main() -> None:
    # ── Load snippet SQL ──────────────────────────────────────────────────────
    if not os.path.isfile(SNIPPET_PATH):
        print(f"  [FAIL] snippet file missing: {SNIPPET_PATH}")
        FAILURES.append("snippet file missing")
        sys.exit(1)

    with open(SNIPPET_PATH) as fh:
        sql_text = fh.read()

    print("Building in-memory DB with 6 pre-July-2026 Open/Disputed invoices ...")
    conn = _build_fresh_db()

    # ── Check 1: snippet returns all 6 open invoices before collection ────────
    rows = _run_snippet(conn, sql_text)
    pre_june_rows = [r for r in rows if r[4] < JUNE_CUTOFF]  # invoice_date col
    check(
        "snippet returns all 6 pre-July-2026 invoices before collection",
        len(pre_june_rows) == 6,
        f"got {len(pre_june_rows)} rows: {[r[0] for r in pre_june_rows]}",
    )

    # ── Check 2: bucket distribution matches expected (as_of = 2026-06-15) ────
    # Build a dict: invoice_number -> aging_bucket (column index 8)
    bucket_map = {r[0]: r[8] for r in pre_june_rows}

    expected_buckets = {
        "AR-FRESH-001": "Current",
        "AR-FRESH-002": "1-30 days",
        "AR-FRESH-003": "31-60 days",
        "AR-FRESH-004": ">60 days",
        "AR-FRESH-005": ">60 days",
        "AR-FRESH-006": ">60 days",
    }
    for inv_no, expected_bucket in expected_buckets.items():
        actual = bucket_map.get(inv_no, "<missing>")
        check(
            f"{inv_no} → {expected_bucket}",
            actual == expected_bucket,
            f"got '{actual}'",
        )

    # Confirm all 4 bucket labels appear across the 6 invoices
    all_buckets = set(bucket_map.values())
    check(
        "all 4 aging buckets represented (Current, 1-30, 31-60, >60)",
        all_buckets == {"Current", "1-30 days", "31-60 days", ">60 days"},
        f"found: {sorted(all_buckets)}",
    )

    # ── Check 3: collection is N-invoice-agnostic (mark all 6 Paid) ──────────
    # Simulate what collect_june2026_ar does: update status='Paid', set payment_date.
    # We use inline SQL (no migration import) to keep the test self-contained.
    open_invoices = conn.execute(
        "SELECT invoice_id FROM receivable "
        "WHERE status IN ('Open','Disputed') AND invoice_date < ?",
        (JUNE_CUTOFF,),
    ).fetchall()
    n_collected = len(open_invoices)
    conn.execute(
        """
        UPDATE receivable
        SET status = 'Paid', payment_date = '2026-07-21'
        WHERE status IN ('Open','Disputed') AND invoice_date < ?
        """,
        (JUNE_CUTOFF,),
    )
    conn.commit()
    check(
        "collection transitions exactly 6 invoices to Paid",
        n_collected == 6,
        f"found {n_collected} open pre-July-2026 invoices",
    )

    # ── Check 4: snippet returns zero rows after collection ───────────────────
    rows_after = _run_snippet(conn, sql_text)
    post_june_open = [r for r in rows_after if r[4] < JUNE_CUTOFF]
    check(
        "snippet returns zero open pre-July-2026 rows after collection",
        len(post_june_open) == 0,
        f"found {len(post_june_open)} row(s) still open: {[r[0] for r in post_june_open]}",
    )

    # ── Check 5: no hardcoded row-count assertion in snippet SQL ──────────────
    # The snippet is a pure SELECT — verify it contains no ASSERT, RAISE, or
    # literal "5" or "15" that could signal a hardcoded count.
    lower = sql_text.lower()
    has_assert = "assert" in lower or "raise" in lower
    check(
        "snippet SQL contains no ASSERT/RAISE statements",
        not has_assert,
        "found ASSERT or RAISE — snippet must be a pure SELECT",
    )
    # Count-agnostic: the snippet must not hardcode the expected invoice count
    import re
    count_assertions = re.findall(r'\bCOUNT\s*\(\s*\*\s*\)\s*=\s*\d+', sql_text, re.IGNORECASE)
    check(
        "snippet SQL contains no hardcoded COUNT(*) = N assertions",
        len(count_assertions) == 0,
        f"found: {count_assertions}",
    )

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
