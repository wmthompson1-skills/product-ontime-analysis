"""Gate test — Cash Receipts governed view + NLQ routing for AR payments.

Locks the invariants of:
  - receivables_cashreceipts_20260724_000002.sql (approved cash receipts snippet)
  - migrations/add_cash_receipts_palette_wiring.py (ar_cash_receipts intent,
    Receivables palette row at query_index 4, concept/perspective links)
  - production_dispatcher.py MOCK_ROUTES ordering (cash-receipt keywords must
    win over the generic "invoice" keyword)

Checks:
  1. The snippet SQL file is present and parseable via SQLGlot.
  2. The manifest entry is APPROVED with a v2 fingerprint covering
     receivable + receivable_payment and the receivable_payment->receivable join.
  3. The SolderEngine serves the snippet without error (fail-closed path OK).
  4. The snippet executes against the DB; totals reconcile: for every fully
     paid invoice the final remaining_balance is ~0.
  5. The ar_cash_receipts intent exists with the correct primary_binding_key,
     Receivables perspective link, ARCashReceiptAmount concept link, and the
     palette row is present under category 'receivables'.
  6. The mock dispatcher routes AR-payment questions to ar_cash_receipts and
     serves the governed SQL (never OUT_OF_SCOPE, never AR aging).

Run gate-style:
    cd hf-space-inventory-sqlgen
    python tests/test_cash_receipts_routing.py
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
BINDING_KEY = "receivables_cashreceipts_20260724_000002"
SNIPPET_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app_schema", "ground_truth",
    "sql_snippets", f"{BINDING_KEY}.sql",
)
INTENT_NAME = "ar_cash_receipts"

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
    sql_text = ""
    if os.path.isfile(SNIPPET_PATH):
        with open(SNIPPET_PATH) as fh:
            sql_text = fh.read()
        try:
            import sqlglot
            parsed = sqlglot.parse(sql_text, dialect="sqlite")
            check("snippet parses without error", bool(parsed))
        except Exception as exc:
            check("snippet parses without error", False, str(exc))

    # ── 2. Manifest entry APPROVED with v2 fingerprint over both tables ────
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
                "fingerprint base_tables == [receivable, receivable_payment]",
                sorted(fp.get("base_tables", [])) == ["receivable", "receivable_payment"],
                str(fp.get("base_tables")),
            )
            check(
                "fingerprint extractor is v2 (join-aware)",
                fp.get("extractor", "").endswith("-v2"),
                fp.get("extractor", ""),
            )
            check(
                "fingerprint captures the payment->invoice join edge",
                len(fp.get("join_edges", [])) >= 1
                and not fp.get("unresolved_joins"),
                f"join_edges={fp.get('join_edges')} unresolved={fp.get('unresolved_joins')}",
            )

    # ── 3. SolderEngine serves the snippet ─────────────────────────────────
    try:
        from solder_engine import SolderEngine

        engine = SolderEngine(db_path=DB_PATH, manifest_path=MANIFEST_PATH)
        result = engine.resolve_by_binding_key(BINDING_KEY)
        ok = "sql" in result and "error" not in result
        check(
            "SolderEngine serves snippet without error",
            ok,
            str(result.get("fail_condition", result.get("error", result.get("message", "")))),
        )
    except Exception as exc:
        check("SolderEngine serves snippet without error", False, str(exc))

    # ── 4. Snippet executes; paid invoices reconcile to zero balance ───────
    if not os.path.isfile(DB_PATH):
        skip("snippet executes and reconciles", "DB not found")
    else:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        has_payments = cur.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='receivable_payment'"
        ).fetchone()[0]
        if not has_payments:
            skip("snippet executes and reconciles",
                 "receivable_payment missing — run collect_june2026_ar.py first")
        elif not sql_text:
            skip("snippet executes and reconciles", "snippet SQL unavailable")
        else:
            try:
                runnable = (sql_text
                            .replace(":start_date", "NULL")
                            .replace(":end_date", "NULL")
                            .replace(":invoice_number", "NULL"))
                rows = cur.execute(runnable).fetchall()
                check("snippet returns payment rows", len(rows) > 0, "0 rows")
                # Column order: invoice_number, customer_name, order_id,
                # invoice_date, invoice_amount, installment_no, payment_date,
                # payment_amount, cumulative_paid, remaining_balance
                # Reconcile: for invoices whose payments sum to the invoice
                # amount in the raw tables, the max-installment row must show
                # remaining_balance ~ 0.
                paid_invoices = {
                    r[0] for r in cur.execute(
                        """
                        SELECT r.invoice_number
                        FROM receivable r
                        JOIN receivable_payment p ON p.invoice_id = r.invoice_id
                        GROUP BY r.invoice_id
                        HAVING ABS(SUM(p.amount) - r.amount_dollars) < 0.01
                        """
                    ).fetchall()
                }
                final_balance = {}
                for row in rows:
                    inv, inst, bal = row[0], row[5], row[9]
                    if inv not in final_balance or inst > final_balance[inv][0]:
                        final_balance[inv] = (inst, bal)
                bad = [
                    (inv, bal) for inv, (_, bal) in final_balance.items()
                    if inv in paid_invoices and abs(bal) >= 0.01
                ]
                check(
                    "fully paid invoices reconcile to zero remaining balance",
                    not bad,
                    str(bad),
                )
            except Exception as exc:
                check("snippet returns payment rows", False, str(exc))
        conn.close()

    # ── 5. Intent + palette + semantic wiring ───────────────────────────────
    if not os.path.isfile(DB_PATH):
        skip("intent wiring present", "DB not found")
    else:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        row = cur.execute(
            "SELECT intent_id, primary_binding_key FROM schema_intents "
            "WHERE intent_name = ?",
            (INTENT_NAME,),
        ).fetchone()
        check("ar_cash_receipts intent exists", row is not None)
        if row:
            rid, pbk = row
            check(
                "intent primary_binding_key points at cash receipts snippet",
                pbk == BINDING_KEY, str(pbk),
            )
            pal = cur.execute(
                "SELECT query_name, query_category FROM schema_intent_queries "
                "WHERE intent_id = ? AND query_file = ?",
                (rid, BINDING_KEY),
            ).fetchone()
            check("palette row present under intent", pal is not None)
            if pal:
                check("palette category is receivables",
                      pal[1] == "receivables", str(pal[1]))
            persp = cur.execute(
                "SELECT 1 FROM schema_intent_perspectives ip "
                "JOIN schema_perspectives p ON p.perspective_id = ip.perspective_id "
                "WHERE ip.intent_id = ? AND p.perspective_name = 'Receivables'",
                (rid,),
            ).fetchone()
            check("Receivables perspective link present", persp is not None)
            conc = cur.execute(
                "SELECT 1 FROM schema_intent_concepts ic "
                "JOIN schema_concepts c ON c.concept_id = ic.concept_id "
                "WHERE ic.intent_id = ? AND c.concept_name = 'ARCashReceiptAmount'",
                (rid,),
            ).fetchone()
            check("ARCashReceiptAmount concept link present", conc is not None)
        conn.close()

    # ── 6. Dispatcher routes AR-payment questions to governed SQL ──────────
    try:
        from production_dispatcher import ProductionDispatcher

        d = ProductionDispatcher(db_path=DB_PATH, use_live_api=False)
        questions = [
            "what payments came in this month?",
            "show me cash receipts against invoice AR-123",
            "which installments were collected in July?",
            "list customer payments applied to invoices",
        ]
        for q in questions:
            r = d.dispatch(q, force_mock=True)
            check(
                f"routes to ar_cash_receipts: {q!r}",
                r.intent == INTENT_NAME
                and r.binding_key == BINDING_KEY
                and not r.out_of_scope
                and bool(r.assembled_sql)
                and "receivable_payment" in r.assembled_sql,
                f"intent={r.intent} binding_key={r.binding_key} "
                f"out_of_scope={r.out_of_scope}",
            )
        # AR aging must still route to its own snippet (no regression).
        r = d.dispatch("ar aging report", force_mock=True)
        check(
            "AR aging still routes to order_revenue_recognition",
            r.intent == "order_revenue_recognition"
            and r.binding_key == "receivables_araging_20260724_000001",
            f"intent={r.intent} binding_key={r.binding_key}",
        )
    except Exception as exc:
        check("dispatcher routing checks", False, str(exc))

    # ── Summary ─────────────────────────────────────────────────────────────
    print()
    if SKIPPED:
        print(f"SKIPPED: {len(SKIPPED)} check(s) (prerequisites not met): {SKIPPED}")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("ALL CHECKS PASSED" + (f" ({len(SKIPPED)} skipped)" if SKIPPED else ""))


if __name__ == "__main__":
    main()
