"""
Migration: Wire the Receivables side of the semantic layer.

The OrderAccountingState concept (customer_order.status, Receivables
perspective) had ZERO intent links, so the selector chain dead-ended at
"No analytical intent elevates this concept". This migration:

  1. Adds intent order_revenue_recognition — revenue recognition roll-up
     of the customer order book.
  2. Links it to the Receivables perspective.
  3. Wires it to the three ground-truth queries in receivables.sql.
  4. Links it to the OrderAccountingState concept so the
     Table → Column → Concept → Intent → Query chain is complete.

ID-INDEPENDENT (2026-07-24 fix): on a fresh bootstrap the schema seed
assigns intent IDs in insertion order, so the ledger intents occupy IDs
18-19 and the historical hardcoded ``intent_id = 19`` insert silently
no-opped (INSERT OR IGNORE conflicting on the primary key) —
order_revenue_recognition never existed on fresh clones, and its palette
rows attached to whatever intent held ID 19 (ledger_fg_production).
The intent is now inserted BY NAME (intent_name is UNIQUE), the real
intent_id is resolved at runtime, and rows misattached by the old bug are
repaired idempotently (no-ops on healthy databases).

Run once (safe to re-run):
    cd hf-space-inventory-sqlgen
    python migrations/add_receivables_wiring.py
"""

import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema", "manufacturing.db")

INTENT_NAME = "order_revenue_recognition"
PERSPECTIVE_NAME = "Receivables"

NEW_INTENT = {
    "intent_name": INTENT_NAME,
    "intent_category": "receivables",
    "description": "Revenue recognition roll-up of the customer order book — recognized (Closed), billable (Shipped), and backlog (Open) value, per state and per customer",
    "typical_question": "How much order revenue is recognized vs billable vs backlog? Which customers carry the most unbilled AR?",
    "primary_binding_key": "start_date",
}

PERSPECTIVE_EXPLANATION = (
    "order_revenue_recognition within Receivables perspective — "
    "the accounting read of the order book"
)

NEW_INTENT_QUERIES = [
    {"query_category": "receivables",
     "query_file": "receivables.sql", "query_index": 0,
     "query_name": "Order Revenue Recognition Status"},
    {"query_category": "receivables",
     "query_file": "receivables.sql", "query_index": 1,
     "query_name": "Customer AR Exposure"},
    {"query_category": "receivables",
     "query_file": "receivables.sql", "query_index": 2,
     "query_name": "Open Order Backlog Aging"},
]

# Concept link: order_revenue_recognition ← OrderAccountingState
# (resolves to customer_order.status) so the intent — and its queries —
# are reachable through the Table → Column → Concept chain.
CONCEPT_NAME = "OrderAccountingState"
CONCEPT_EXPLANATION = (
    "Revenue recognition is read from customer_order.status; "
    "OrderAccountingState is the concept that anchors the chain"
)


def _fail(msg: str) -> None:
    raise SystemExit(f"[add_receivables_wiring] FAIL-CLOSED: {msg}")


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=10000")
    cur = conn.cursor()

    # 1. New intent — inserted BY NAME (intent_name is UNIQUE); never pin
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

    # 1b. Repair rows misattached by the old hardcoded-ID bug (fresh
    #     bootstraps where ID 19 belonged to a ledger intent). UPDATE OR
    #     REPLACE moves the row to the correct intent, replacing any
    #     duplicate that already sits there. No-ops on healthy databases.
    cur.execute(
        "UPDATE OR REPLACE schema_intent_queries SET intent_id = ? "
        "WHERE query_category = 'receivables' AND intent_id <> ?",
        (rid, rid),
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

    # 2. Perspective link
    cur.execute("""
        INSERT OR IGNORE INTO schema_intent_perspectives
            (intent_id, perspective_id, intent_factor_weight, explanation)
        VALUES (?, ?, 1, ?)
    """, (rid, pid, PERSPECTIVE_EXPLANATION))
    print(f"  intent_perspective {rid}↔{pid}: {cur.rowcount} inserted")

    # 3. Query wiring
    for q in NEW_INTENT_QUERIES:
        cur.execute("""
            INSERT OR IGNORE INTO schema_intent_queries
                (intent_id, query_category, query_file, query_index, query_name)
            VALUES (?, ?, ?, ?, ?)
        """, (rid, q["query_category"], q["query_file"], q["query_index"], q["query_name"]))
        print(f"  intent_query '{q['query_name']}': {cur.rowcount} inserted")

    # 4. Concept link so the intent is reachable via Table→Column→Concept chain
    cur.execute("""
        INSERT OR IGNORE INTO schema_intent_concepts
            (intent_id, concept_id, intent_factor_weight, explanation)
        SELECT ?, c.concept_id, 1, ?
        FROM schema_concepts c
        WHERE c.concept_name = ?
    """, (rid, CONCEPT_EXPLANATION, CONCEPT_NAME))
    print(f"  intent_concept {rid}↔{CONCEPT_NAME}: {cur.rowcount} inserted")

    conn.commit()

    # 5. Fail-closed verify — the chain must be complete under the real id.
    n_q = cur.execute(
        "SELECT COUNT(*) FROM schema_intent_queries "
        "WHERE intent_id = ? AND query_file = 'receivables.sql'",
        (rid,),
    ).fetchone()[0]
    if n_q < len(NEW_INTENT_QUERIES):
        conn.close()
        _fail(f"expected >= {len(NEW_INTENT_QUERIES)} receivables.sql palette rows under intent {rid}, found {n_q}")
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

    conn.close()
    print("\nDone. Receivables wiring complete.")


if __name__ == "__main__":
    print(f"DB: {os.path.abspath(DB_PATH)}")
    run()
