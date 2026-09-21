"""
Migration: Add CRM customer intents and ground truth query wiring.

ID-INDEPENDENT (2026-09-21 fix): on a fresh bootstrap the schema seed assigns
intent IDs in insertion order, so IDs 15-17 are already taken by the ledger
intents (ledger_inventory_balance, ledger_job_cost_summary,
ledger_event_trace) by the time this migration runs. The historical
hardcoded intent_id inserts silently no-opped (INSERT OR IGNORE conflicting
on the primary key): none of the three CRM intents ever existed under their
own names, and their perspective/query wiring attached to whatever ledger
intent held that ID instead. Same bug class already fixed for
order_revenue_recognition in add_receivables_wiring.py — this applies the
same fix: each intent is inserted BY NAME (intent_name is UNIQUE), the real
intent_id is resolved at runtime, and rows misattached by the old bug are
repaired idempotently (no-ops on healthy databases).

PERSPECTIVE NAME FIX (2026-09-21): the historical version also hardcoded
perspective_id 5/6 as "Customer"/"Customer_Order" — but there is no
"Customer" perspective in this schema (only CRM and Customer_Order exist;
id 5 is really Receivables, id 6 is really CRM). Per user decision, the
stray "Customer" perspective is mapped to CRM (its natural home — these are
crm_customer_* intents), and the genuine Customer_Order cross-link is now
resolved by name instead of the stale literal id 6.

Run once:
    cd hf-space-inventory-sqlgen
    python migrations/add_crm_customer_intents.py

Safe to re-run — uses INSERT OR IGNORE on all rows.

Category:    crm
Perspectives: CRM, Customer_Order
SQL file:    app_schema/queries/crm_customer.sql
"""

import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema", "manufacturing.db")

INTENTS = [
    {
        "intent_name": "crm_customer_profile",
        "intent_category": "crm",
        "description": "Customer master + primary shipping address — the CRM_Join structural edge resolved for display",
        "typical_question": "Show me the customer profile and shipping address for account 42.",
        "primary_binding_key": "customer_id",
    },
    {
        "intent_name": "crm_customer_revenue",
        "intent_category": "crm",
        "description": "Revenue per customer for a date range — joins customer, customer_address, and sales",
        "typical_question": "Show me revenue by customer from July 1st. Which customers drove the most sales?",
        "primary_binding_key": "start_date",
    },
    {
        "intent_name": "crm_customer_address_lookup",
        "intent_category": "crm",
        "description": "Address-first territory lookup — find customers by city or state",
        "typical_question": "Which customers are in California? Show me all ship-to addresses in the Southwest.",
        "primary_binding_key": "state",
    },
]

# intent_name -> [(perspective_name, explanation), ...]
# crm_customer_profile → CRM only
# crm_customer_revenue → CRM + Customer_Order — revenue bridges both
# crm_customer_address_lookup → CRM only
INTENT_PERSPECTIVES = {
    "crm_customer_profile": [
        ("CRM", "crm_customer_profile within CRM perspective"),
    ],
    "crm_customer_revenue": [
        ("CRM", "crm_customer_revenue within CRM perspective"),
        ("Customer_Order", "crm_customer_revenue also within Customer_Order perspective — revenue bridges CRM and order fulfillment"),
    ],
    "crm_customer_address_lookup": [
        ("CRM", "crm_customer_address_lookup within CRM perspective"),
    ],
}

# intent_name -> (query_index, query_name)
INTENT_QUERIES = {
    "crm_customer_profile": (1, "Customer profile with shipping address"),
    "crm_customer_revenue": (2, "Revenue by customer with address enrichment"),
    "crm_customer_address_lookup": (3, "Customer address territory lookup"),
}

QUERY_FILE = "crm_customer.sql"


def _fail(msg: str) -> None:
    raise SystemExit(f"[add_crm_customer_intents] FAIL-CLOSED: {msg}")


def _resolve_perspective(cur, name: str) -> int:
    row = cur.execute(
        "SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?",
        (name,),
    ).fetchone()
    if not row:
        _fail(f"perspective {name!r} not found — schema seed missing?")
    return row[0]


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=10000")
    cur = conn.cursor()

    for intent in INTENTS:
        name = intent["intent_name"]

        # 1. Insert BY NAME — never pin the autoincrement intent_id.
        cur.execute("""
            INSERT OR IGNORE INTO schema_intents
                (intent_name, intent_category, description, typical_question, primary_binding_key)
            VALUES (:intent_name, :intent_category, :description, :typical_question, :primary_binding_key)
        """, intent)
        print(f"  intent {name}: {cur.rowcount} inserted")

        row = cur.execute(
            "SELECT intent_id FROM schema_intents WHERE intent_name = ?", (name,)
        ).fetchone()
        if not row:
            conn.close()
            _fail(f"intent {name!r} missing after insert")
        rid = row[0]
        print(f"  resolved intent_id({name}) = {rid}")

        # 2. Repair rows misattached by the old hardcoded-ID bug.
        _, query_name = INTENT_QUERIES[name]
        cur.execute(
            "UPDATE OR REPLACE schema_intent_queries SET intent_id = ? "
            "WHERE query_category = 'crm' AND query_name = ? AND intent_id <> ?",
            (rid, query_name, rid),
        )
        if cur.rowcount:
            print(f"  repaired {cur.rowcount} misattached palette row(s) -> intent {rid}")
        for _pname, explanation in INTENT_PERSPECTIVES[name]:
            cur.execute(
                "UPDATE OR REPLACE schema_intent_perspectives SET intent_id = ? "
                "WHERE explanation = ? AND intent_id <> ?",
                (rid, explanation, rid),
            )
            if cur.rowcount:
                print(f"  repaired {cur.rowcount} misattached perspective link(s) -> intent {rid}")

        # 3. Perspective links — resolved by name.
        for pname, explanation in INTENT_PERSPECTIVES[name]:
            pid = _resolve_perspective(cur, pname)
            cur.execute("""
                INSERT OR IGNORE INTO schema_intent_perspectives
                    (intent_id, perspective_id, intent_factor_weight, explanation)
                VALUES (?, ?, 1, ?)
            """, (rid, pid, explanation))
            print(f"  intent_perspective {rid}↔{pid} ({pname}): {cur.rowcount} inserted")

        # 4. Query wiring
        query_index, query_name = INTENT_QUERIES[name]
        cur.execute("""
            INSERT OR IGNORE INTO schema_intent_queries
                (intent_id, query_category, query_file, query_index, query_name)
            VALUES (?, 'crm', ?, ?, ?)
        """, (rid, QUERY_FILE, query_index, query_name))
        print(f"  intent_query '{query_name}': {cur.rowcount} inserted")

    conn.commit()

    # 5. Fail-closed verify — every intent's chain must be complete under its real id.
    for name in INTENT_QUERIES:
        rid = cur.execute(
            "SELECT intent_id FROM schema_intents WHERE intent_name = ?", (name,)
        ).fetchone()[0]
        query_index, query_name = INTENT_QUERIES[name]
        if not cur.execute(
            "SELECT 1 FROM schema_intent_queries WHERE intent_id = ? AND query_name = ?",
            (rid, query_name),
        ).fetchone():
            conn.close()
            _fail(f"query {query_name!r} missing under intent {name!r} ({rid})")
        for pname, _explanation in INTENT_PERSPECTIVES[name]:
            pid = _resolve_perspective(cur, pname)
            if not cur.execute(
                "SELECT 1 FROM schema_intent_perspectives WHERE intent_id = ? AND perspective_id = ?",
                (rid, pid),
            ).fetchone():
                conn.close()
                _fail(f"perspective link {rid}↔{pid} ({pname}) missing for {name!r}")

    conn.close()
    print("\nDone. CRM customer intents wired successfully.")


if __name__ == "__main__":
    print(f"DB: {os.path.abspath(DB_PATH)}")
    run()
