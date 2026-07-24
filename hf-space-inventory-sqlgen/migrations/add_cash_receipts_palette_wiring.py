"""Wire the Cash Receipts snippet into the semantic layer and Query Palette.

The receivables_cashreceipts_20260724_000002 snippet (cash receipts by
invoice — receivable_payment installments joined to their invoice header)
is approved in reviewer_manifest.json.  This migration gives natural-language
questions about AR payments a real route:

  1. Adds intent ar_cash_receipts (category receivables) with
     primary_binding_key pointing at the approved snippet, so the NLQ
     dispatcher can serve governed SQL for "what payments came in this
     month?" / "show cash receipts against invoice AR-123".
  2. Links the intent to the Receivables perspective.
  3. Links the intent to the ARCashReceiptAmount concept so the
     Table → Column → Concept → Intent → Query selector chain reaches it
     from the receivable_payment columns.
  4. Adds the Query Palette row (category receivables, query_index 4 —
     receivables.sql occupies 0-2 and the AR aging snippet occupies 3).

ID-INDEPENDENT: intent_id is resolved by intent_name at runtime — never
hardcode autoincrement IDs (they differ between fresh bootstraps and the
live DB).

Run once (safe to re-run):
    cd hf-space-inventory-sqlgen
    python migrations/add_cash_receipts_palette_wiring.py
"""

import os
import sqlite3

DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app_schema", "manufacturing.db"
)

BINDING_KEY = "receivables_cashreceipts_20260724_000002"
INTENT_NAME = "ar_cash_receipts"
PERSPECTIVE_NAME = "Receivables"
CONCEPT_NAME = "ARCashReceiptAmount"

NEW_INTENT = {
    "intent_name": INTENT_NAME,
    "intent_category": "receivables",
    "description": (
        "Cash receipts applied against AR invoices — every "
        "receivable_payment installment joined to its invoice header, with "
        "running cumulative-paid and remaining-balance per invoice"
    ),
    "typical_question": (
        "What payments came in this month? "
        "Show me cash receipts against invoice AR-123."
    ),
    "primary_binding_key": BINDING_KEY,
}

PERSPECTIVE_EXPLANATION = (
    "ar_cash_receipts within Receivables perspective — "
    "the collections read of the invoice book"
)

CONCEPT_EXPLANATION = (
    "Cash receipts are read from receivable_payment.amount; "
    "ARCashReceiptAmount is the concept that anchors the chain"
)

PALETTE_ENTRY = {
    "query_category": "receivables",
    "query_file": BINDING_KEY,
    "query_index": 4,
    "query_name": "Cash Receipts by Invoice",
}


def _fail(msg: str) -> None:
    raise SystemExit(f"[add_cash_receipts_palette_wiring] FAIL-CLOSED: {msg}")


def run() -> None:
    print(f"DB: {os.path.abspath(DB_PATH)}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=10000")
    cur = conn.cursor()

    # 1 — intent, inserted BY NAME (intent_name is UNIQUE)
    cur.execute(
        """
        INSERT OR IGNORE INTO schema_intents
            (intent_name, intent_category, description, typical_question,
             primary_binding_key)
        VALUES (:intent_name, :intent_category, :description,
                :typical_question, :primary_binding_key)
        """,
        NEW_INTENT,
    )
    print(f"  intent {INTENT_NAME}: {cur.rowcount} inserted")

    row = cur.execute(
        "SELECT intent_id FROM schema_intents WHERE intent_name = ?",
        (INTENT_NAME,),
    ).fetchone()
    if not row:
        conn.close()
        _fail(f"intent {INTENT_NAME!r} missing after insert")
    rid = row[0]
    print(f"  resolved intent_id = {rid}")

    # 1b — ensure the binding key is set even if the intent pre-existed with
    # a NULL/placeholder key (idempotent; never overwrites a hand-set key).
    cur.execute(
        """
        UPDATE schema_intents
        SET primary_binding_key = ?
        WHERE intent_id = ?
          AND (primary_binding_key IS NULL
               OR primary_binding_key = ''
               OR primary_binding_key = ?)
        """,
        (BINDING_KEY, rid, BINDING_KEY),
    )

    # 2 — perspective link
    prow = cur.execute(
        "SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?",
        (PERSPECTIVE_NAME,),
    ).fetchone()
    if not prow:
        conn.close()
        _fail(f"perspective {PERSPECTIVE_NAME!r} not found — schema seed missing?")
    pid = prow[0]
    cur.execute(
        """
        INSERT OR IGNORE INTO schema_intent_perspectives
            (intent_id, perspective_id, intent_factor_weight, explanation)
        VALUES (?, ?, 1, ?)
        """,
        (rid, pid, PERSPECTIVE_EXPLANATION),
    )
    print(f"  intent_perspective {rid}<->{pid}: {cur.rowcount} inserted")

    # 3 — concept link (ARCashReceiptAmount resolves to receivable_payment.amount)
    cur.execute(
        """
        INSERT OR IGNORE INTO schema_intent_concepts
            (intent_id, concept_id, intent_factor_weight, explanation)
        SELECT ?, c.concept_id, 1, ?
        FROM schema_concepts c
        WHERE c.concept_name = ?
        """,
        (rid, CONCEPT_EXPLANATION, CONCEPT_NAME),
    )
    print(f"  intent_concept {rid}<->{CONCEPT_NAME}: {cur.rowcount} inserted")

    # 4 — palette row
    cur.execute(
        """
        INSERT OR IGNORE INTO schema_intent_queries
            (intent_id, query_category, query_file, query_index, query_name)
        VALUES (?, ?, ?, ?, ?)
        """,
        (rid, PALETTE_ENTRY["query_category"], PALETTE_ENTRY["query_file"],
         PALETTE_ENTRY["query_index"], PALETTE_ENTRY["query_name"]),
    )
    print(
        f"  palette entry '{PALETTE_ENTRY['query_name']}': "
        f"{'inserted' if cur.rowcount else 'already present (skipped)'}"
    )

    conn.commit()

    # 5 — fail-closed verify
    bk = cur.execute(
        "SELECT primary_binding_key FROM schema_intents WHERE intent_id = ?",
        (rid,),
    ).fetchone()
    if not bk or not bk[0]:
        conn.close()
        _fail(f"intent {rid} primary_binding_key still unset")
    if bk[0] != BINDING_KEY:
        print(
            f"  NOTE: primary_binding_key is hand-set to {bk[0]!r} "
            f"(not {BINDING_KEY!r}); leaving as-is."
        )
    if not cur.execute(
        "SELECT 1 FROM schema_intent_queries WHERE intent_id = ? AND query_file = ?",
        (rid, BINDING_KEY),
    ).fetchone():
        conn.close()
        _fail("palette entry not found after insert")
    if not cur.execute(
        "SELECT 1 FROM schema_intent_perspectives WHERE intent_id = ? AND perspective_id = ?",
        (rid, pid),
    ).fetchone():
        conn.close()
        _fail(f"perspective link {rid}<->{pid} missing after insert")
    if not cur.execute(
        "SELECT 1 FROM schema_intent_concepts ic JOIN schema_concepts c "
        "ON c.concept_id = ic.concept_id "
        "WHERE ic.intent_id = ? AND c.concept_name = ?",
        (rid, CONCEPT_NAME),
    ).fetchone():
        conn.close()
        _fail(f"concept link {rid}<->{CONCEPT_NAME} missing after insert")

    conn.close()
    print(
        "\n[add_cash_receipts_palette_wiring] done — "
        f"Cash Receipts wired (intent {rid}, query_index 4, "
        f"primary_binding_key={BINDING_KEY})."
    )


if __name__ == "__main__":
    run()
