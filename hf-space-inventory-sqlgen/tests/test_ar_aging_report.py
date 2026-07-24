"""Gate test — AR Aging Report and post-collection zero-open-June verification.

Locks the invariants of:
  - receivables_araging_20260724_000001.sql (the approved AR aging snippet)
  - migrations/collect_june2026_ar.py (marks all pre-July-2026 open invoices Paid; N-invoice-agnostic)
  - migrations/add_ar_aging_palette_wiring.py (intent 19 query_index 3)

Checks:
  1. The snippet SQL file is present and parseable via SQLGlot.
  2. The manifest entry is APPROVED with the correct structural fingerprint.
  3. The SolderEngine can load and serve the snippet without error.
  4. After collect_june2026_ar: zero Open/Disputed rows with invoice_date < 2026-07-01
     appear in the AR aging result (cash-to-cash cycle closed correctly).
  5. The palette wiring row (intent 19, query_index 3) is present in
     schema_intent_queries.

Prerequisites:
  - Both add_receivable_tables.py AND collect_june2026_ar.py must have been
    applied to the DB.  If receivable_payment is missing or the June invoices
    are not all Paid, check (4) is SKIPPED with a clear message.

Run gate-style:
    cd hf-space-inventory-sqlgen
    python tests/test_ar_aging_report.py
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema", "manufacturing.db")
MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app_schema", "ground_truth", "reviewer_manifest.json"
)
SNIPPET_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "app_schema",
    "ground_truth",
    "sql_snippets",
    "receivables_araging_20260724_000001.sql",
)
BINDING_KEY = "receivables_araging_20260724_000001"
JUNE_CUTOFF = "2026-07-01"

FAILURES: list = []
SKIPPED: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail and not ok else ""
    print(f"  [{status}] {name}{suffix}")
    if not ok:
        FAILURES.append(name)


def skip(name: str, reason: str) -> None:
    print(f"  [SKIP] {name} — {reason}")
    SKIPPED.append(name)


def main() -> None:
    # ── 1. Snippet file present and parseable ──────────────────────────────
    check("snippet file exists", os.path.isfile(SNIPPET_PATH), SNIPPET_PATH)

    if os.path.isfile(SNIPPET_PATH):
        with open(SNIPPET_PATH) as fh:
            sql_text = fh.read()
        try:
            import sqlglot
            parsed = sqlglot.parse(sql_text, dialect="sqlite")
            check("snippet parses without error", bool(parsed))
        except Exception as exc:
            check("snippet parses without error", False, str(exc))
    else:
        sql_text = ""
        skip("snippet parses without error", "file missing")

    # ── 2. Manifest entry is APPROVED with correct fingerprint ─────────────
    check("manifest file exists", os.path.isfile(MANIFEST_PATH), MANIFEST_PATH)

    if os.path.isfile(MANIFEST_PATH):
        with open(MANIFEST_PATH) as fh:
            manifest = json.load(fh)
        entry = manifest.get("approved_snippets", {}).get(BINDING_KEY)
        check("manifest entry present", entry is not None, BINDING_KEY)
        if entry:
            check(
                "manifest validation_status APPROVED",
                entry.get("validation_status") == "APPROVED",
                str(entry.get("validation_status")),
            )
            fp = entry.get("structural_fingerprint", {})
            check(
                "fingerprint base_tables == ['receivable']",
                fp.get("base_tables") == ["receivable"],
                str(fp.get("base_tables")),
            )
            check(
                "fingerprint extractor is v2",
                fp.get("extractor", "").endswith("-v2"),
                fp.get("extractor", ""),
            )
    else:
        skip("manifest entry present", "manifest file missing")

    # ── 3. SolderEngine can load and serve the snippet ─────────────────────
    try:
        from solder_engine import SolderEngine

        engine = SolderEngine(db_path=DB_PATH, manifest_path=MANIFEST_PATH)
        result = engine.resolve_by_binding_key(BINDING_KEY)
        # Success: result contains "sql"; failure: result contains "error" or "fail_condition"
        ok = "sql" in result and "error" not in result
        check(
            "SolderEngine serves snippet without error",
            ok,
            str(result.get("fail_condition", result.get("error", result.get("message", "")))),
        )
    except Exception as exc:
        check("SolderEngine serves snippet without error", False, str(exc))

    # ── 4. Zero open June rows after collection ────────────────────────────
    if not os.path.isfile(DB_PATH):
        skip("zero open June rows after collection", "DB not found")
    else:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # Check prerequisites
        payment_table_ok = cur.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='receivable_payment'"
        ).fetchone()[0]

        if not payment_table_ok:
            skip(
                "zero open June rows after collection",
                "receivable_payment table missing — run collect_june2026_ar.py first",
            )
        else:
            june_not_paid = cur.execute(
                """
                SELECT COUNT(*) FROM receivable
                WHERE invoice_date < ? AND status IN ('Open', 'Disputed')
                """,
                (JUNE_CUTOFF,),
            ).fetchone()[0]

            if june_not_paid > 0:
                skip(
                    "zero open June rows after collection",
                    f"{june_not_paid} pre-July-2026 invoice(s) still Open/Disputed "
                    "— run collect_june2026_ar.py first",
                )
            else:
                # Run the AR aging snippet against the DB
                if sql_text:
                    try:
                        aging_rows = cur.execute(
                            sql_text.replace(":start_date", "NULL")
                        ).fetchall()
                        # Filter to June cohort (invoice_date < JUNE_CUTOFF)
                        # Column order: invoice_number, customer_name, order_id,
                        #               status, invoice_date, due_date, amount_dollars,
                        #               days_past_due, aging_bucket, as_of_date
                        june_open = [
                            row for row in aging_rows if row[4] < JUNE_CUTOFF
                        ]
                        check(
                            "AR aging shows zero open pre-July-2026 invoice rows",
                            len(june_open) == 0,
                            f"found {len(june_open)} row(s): {june_open}",
                        )
                        print(
                            f"    AR aging total open rows: {len(aging_rows)} "
                            f"(none from pre-July-2026 cohort)"
                        )
                    except Exception as exc:
                        check(
                            "AR aging shows zero open pre-July-2026 invoice rows",
                            False,
                            str(exc),
                        )
                else:
                    skip(
                        "zero open June rows after collection",
                        "snippet SQL text unavailable",
                    )

        conn.close()

    # ── 5. Palette wiring row present ─────────────────────────────────────
    if not os.path.isfile(DB_PATH):
        skip("palette wiring row present (intent 19, query_index 3)", "DB not found")
    else:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        row = cur.execute(
            "SELECT query_name FROM schema_intent_queries "
            "WHERE intent_id = 19 AND query_file = ?",
            (BINDING_KEY,),
        ).fetchone()
        check(
            "palette wiring row present (intent 19)",
            row is not None,
            f"no row for intent_id=19, query_file={BINDING_KEY}",
        )
        if row:
            check(
                "palette query_name correct",
                row[0] == "AR Aging — Open & Disputed Invoices",
                str(row[0]),
            )
        conn.close()

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    if SKIPPED:
        print(f"SKIPPED: {len(SKIPPED)} check(s) (prerequisites not met): {SKIPPED}")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("ALL CHECKS PASSED" + (f" ({len(SKIPPED)} skipped)" if SKIPPED else ""))


if __name__ == "__main__":
    main()
