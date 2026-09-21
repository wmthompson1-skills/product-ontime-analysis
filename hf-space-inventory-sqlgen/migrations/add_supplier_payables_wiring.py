"""
Migration: Re-wire supplier intents to reflect the payables framing.

Changes:
  1. Add Payables (2) cross-link to supplier_scorecard (intent 4) — was Quality-only,
     but late-rate_pct is an AP signal that drives payment-term decisions.
  2. Add new intent supplier_payables_exposure — pure AP roll-up,
     Payables perspective only, maps to queries 3-5 in supplier_performance.sql.

ID-INDEPENDENT (2026-09-21 fix): on a fresh bootstrap the schema seed assigns
intent IDs in insertion order, so the ledger intents occupy IDs up through 19
(ledger_material_issued lands on 18) — the historical hardcoded
``intent_id = 18`` insert silently no-opped (INSERT OR IGNORE conflicting on
the primary key): supplier_payables_exposure never existed as its own intent,
and its palette/perspective/concept rows attached to whatever intent held ID
18 (ledger_material_issued) instead. Same bug class already fixed once for
order_revenue_recognition in add_receivables_wiring.py — this applies the
same fix: the intent is inserted BY NAME (intent_name is UNIQUE), the real
intent_id is resolved at runtime, and rows misattached by the old bug are
repaired idempotently (no-ops on healthy databases).

Run once:
    cd hf-space-inventory-sqlgen
    python migrations/add_supplier_payables_wiring.py

Safe to re-run — uses INSERT OR IGNORE on all rows.
"""

import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema", "manufacturing.db")

INTENT_NAME = "supplier_payables_exposure"

NEW_INTENT = {
    "intent_name": INTENT_NAME,
    "intent_category": "supplier_performance",
    "description": "AP exposure roll-up by supplier — total units received, estimated payables, and early-pay eligibility flag",
    "typical_question": "Show me payables exposure by vendor from July 1st. What do we owe suppliers this quarter?",
    "primary_binding_key": "start_date",
}

# supplier_scorecard (4) already has Quality (1) — add Payables (2). This one
# is NOT part of the ID-collision bug (intent 4 is genuinely supplier_scorecard,
# and perspective_id 2 is genuinely Payables).
SCORECARD_FINANCE_LINK = {
    "intent_id": 4, "perspective_id": 2,
    "explanation": "supplier_scorecard within Payables perspective — late-rate drives AP penalty and payment-term decisions",
}

PERSPECTIVE_NAME = "Payables"
PERSPECTIVE_EXPLANATION = "supplier_payables_exposure within Payables perspective — pure AP roll-up"

NEW_INTENT_QUERIES = [
    # NOTE: index 2 ("Supplier Payables Exposure", cost-proxy estimate) is
    # deliberately NOT wired — it reads daily_deliveries / product_lines,
    # which are removed from the schema, and its estimate framing conflicts
    # with the SME definition: AP exposure = TOTAL DUE from the payables ledger.
    # index 3 — exposure = TOTAL DUE (sum of unpaid invoice amounts)
    {"query_category": "supplier_performance",
     "query_file": "supplier_performance.sql", "query_index": 3,
     "query_name": "Supplier AP Total Due"},
    # index 4 — AP aging buckets
    {"query_category": "supplier_performance",
     "query_file": "supplier_performance.sql", "query_index": 4,
     "query_name": "AP Aging by Supplier"},
    # index 5 — three-way match exception holds
    {"query_category": "supplier_performance",
     "query_file": "supplier_performance.sql", "query_index": 5,
     "query_name": "Three-Way Match Exceptions"},
]

# Concept link: supplier_payables_exposure ← ThreeWayMatchState
# (resolves to payables.three_way_match_status) so the intent — and its
# queries — are reachable through the Table → Column → Concept chain.
CONCEPT_NAME = "ThreeWayMatchState"
CONCEPT_EXPLANATION = "AP exposure is read from the payables ledger; ThreeWayMatchState is the payables-table concept that anchors the chain"


def _fail(msg: str) -> None:
    raise SystemExit(f"[add_supplier_payables_wiring] FAIL-CLOSED: {msg}")


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=10000")
    cur = conn.cursor()

    # 1. supplier_scorecard Payables cross-link — unaffected by the ID bug.
    cur.execute("""
        INSERT OR IGNORE INTO schema_intent_perspectives
            (intent_id, perspective_id, intent_factor_weight, explanation)
        VALUES (:intent_id, :perspective_id, 1, :explanation)
    """, SCORECARD_FINANCE_LINK)
    print(f"  intent_perspective {SCORECARD_FINANCE_LINK['intent_id']}↔{SCORECARD_FINANCE_LINK['perspective_id']}: {cur.rowcount} inserted")

    # 2. New intent — inserted BY NAME (intent_name is UNIQUE); never pin
    #    the autoincrement intent_id, it differs between fresh and live DBs.
    cur.execute("""
        INSERT OR IGNORE INTO schema_intents
            (intent_name, intent_category, description, typical_question, primary_binding_key)
        VALUES (:intent_name, :intent_category, :description, :typical_question, :primary_binding_key)
    """, NEW_INTENT)
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

    prow = cur.execute(
        "SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?",
        (PERSPECTIVE_NAME,),
    ).fetchone()
    if not prow:
        conn.close()
        _fail(f"perspective {PERSPECTIVE_NAME!r} not found — schema seed missing?")
    pid = prow[0]

    # 2b. Repair rows misattached by the old hardcoded-ID bug (fresh
    #     bootstraps where ID 18 belonged to ledger_material_issued). UPDATE
    #     OR REPLACE moves the row to the correct intent, replacing any
    #     duplicate that already sits there. No-ops on healthy databases.
    cur.execute(
        "UPDATE OR REPLACE schema_intent_queries SET intent_id = ? "
        "WHERE query_category = 'supplier_performance' AND intent_id <> ? "
        "AND query_name IN (?, ?, ?)",
        (rid, rid, *[q["query_name"] for q in NEW_INTENT_QUERIES]),
    )
    if cur.rowcount:
        print(f"  repaired {cur.rowcount} misattached palette row(s) -> intent {rid}")
    cur.execute(
        "UPDATE OR REPLACE schema_intent_perspectives SET intent_id = ? "
        "WHERE explanation = ? AND intent_id <> ?",
        (rid, PERSPECTIVE_EXPLANATION, rid),
    )
    if cur.rowcount:
        print(f"  repaired {cur.rowcount} misattached perspective link(s) -> intent {rid}")
    cur.execute(
        "UPDATE OR REPLACE schema_intent_concepts SET intent_id = ? "
        "WHERE explanation = ? AND intent_id <> ?",
        (rid, CONCEPT_EXPLANATION, rid),
    )
    if cur.rowcount:
        print(f"  repaired {cur.rowcount} misattached concept link(s) -> intent {rid}")

    # 3. Perspective link
    cur.execute("""
        INSERT OR IGNORE INTO schema_intent_perspectives
            (intent_id, perspective_id, intent_factor_weight, explanation)
        VALUES (?, ?, 1, ?)
    """, (rid, pid, PERSPECTIVE_EXPLANATION))
    print(f"  intent_perspective {rid}↔{pid}: {cur.rowcount} inserted")

    # 4. Query wiring for new intent.
    # Self-heal: an earlier version of this migration inserted index 3 with a
    # name that matches no "-- Query:" marker in the file, so it never resolved.
    cur.execute("""
        DELETE FROM schema_intent_queries
        WHERE intent_id = ? AND query_file = 'supplier_performance.sql'
          AND query_name IN (
              'Supplier AP exposure and payment recommendation',
              'Supplier Payables Exposure'
          )
    """, (rid,))
    if cur.rowcount:
        print(f"  removed {cur.rowcount} stale intent_query row(s)")
    for q in NEW_INTENT_QUERIES:
        cur.execute("""
            INSERT OR IGNORE INTO schema_intent_queries
                (intent_id, query_category, query_file, query_index, query_name)
            VALUES (?, ?, ?, ?, ?)
        """, (rid, q["query_category"], q["query_file"], q["query_index"], q["query_name"]))
        print(f"  intent_query '{q['query_name']}': {cur.rowcount} inserted")

    # 5. Concept link so the intent is reachable via Table→Column→Concept chain
    cur.execute("""
        INSERT OR IGNORE INTO schema_intent_concepts
            (intent_id, concept_id, intent_factor_weight, explanation)
        SELECT ?, c.concept_id, 1, ?
        FROM schema_concepts c
        WHERE c.concept_name = ?
    """, (rid, CONCEPT_EXPLANATION, CONCEPT_NAME))
    print(f"  intent_concept {rid}↔{CONCEPT_NAME}: {cur.rowcount} inserted")

    # 5b. Perspective ↔ concept link — separate from intent_concept above.
    # The Selector's Payables-perspective query filter walks
    # perspective -> schema_perspective_concepts -> concept -> intent_concepts
    # -> intent -> query, NOT intent_concepts alone. Without this row,
    # ThreeWayMatchState (and its queries) is unreachable by perspective
    # filter even though the intent_concept link above is correct.
    cur.execute("""
        INSERT OR IGNORE INTO schema_perspective_concepts
            (perspective_id, concept_id, relationship_type, priority_weight)
        SELECT ?, c.concept_id, 'USES_DEFINITION', 2
        FROM schema_concepts c
        WHERE c.concept_name = ?
    """, (pid, CONCEPT_NAME))
    print(f"  perspective_concept {pid}↔{CONCEPT_NAME}: {cur.rowcount} inserted")

    conn.commit()

    # 6. Fail-closed verify — the chain must be complete under the real id.
    n_q = cur.execute(
        "SELECT COUNT(*) FROM schema_intent_queries "
        "WHERE intent_id = ? AND query_file = 'supplier_performance.sql'",
        (rid,),
    ).fetchone()[0]
    if n_q < len(NEW_INTENT_QUERIES):
        conn.close()
        _fail(f"expected >= {len(NEW_INTENT_QUERIES)} supplier_performance.sql palette rows under intent {rid}, found {n_q}")
    if not cur.execute(
        "SELECT 1 FROM schema_intent_perspectives WHERE intent_id = ? AND perspective_id = ?",
        (rid, pid),
    ).fetchone():
        conn.close()
        _fail(f"perspective link {rid}↔{pid} missing after insert")
    if not cur.execute(
        "SELECT 1 FROM schema_intent_concepts ic JOIN schema_concepts c "
        "ON c.concept_id = ic.concept_id "
        "WHERE ic.intent_id = ? AND c.concept_name = ?",
        (rid, CONCEPT_NAME),
    ).fetchone():
        conn.close()
        _fail(f"concept link {rid}↔{CONCEPT_NAME} missing after insert")
    if not cur.execute(
        "SELECT 1 FROM schema_perspective_concepts pc JOIN schema_concepts c "
        "ON c.concept_id = pc.concept_id "
        "WHERE pc.perspective_id = ? AND c.concept_name = ?",
        (pid, CONCEPT_NAME),
    ).fetchone():
        conn.close()
        _fail(f"perspective_concept link {pid}↔{CONCEPT_NAME} missing after insert")
    # Disentanglement check: ledger_material_issued must keep its own row.
    ledger_row = cur.execute(
        "SELECT 1 FROM schema_intent_queries WHERE query_file = 'job_costing_ledger.sql' "
        "AND query_name = 'Material Issued over a Period' AND intent_id <> ?",
        (rid,),
    ).fetchone()
    if not ledger_row:
        conn.close()
        _fail("ledger_material_issued's own query row is missing or was "
              f"wrongly swept onto intent {rid} — disentanglement failed")

    conn.close()
    print("\nDone. Supplier → payables wiring complete.")


if __name__ == "__main__":
    print(f"DB: {os.path.abspath(DB_PATH)}")
    run()
