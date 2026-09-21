"""
Migration: Add Customer_Order intents and ground truth query wiring.

The Customer_Order perspective is already seeded by the base schema
(schema_sqlite.sql) — this migration used to also try to INSERT it at a
hardcoded perspective_id (6), which is wrong (id 6 is genuinely CRM) and
unnecessary (the perspective already exists). It now only resolves it by
name; the INSERT is dropped entirely.

ID-INDEPENDENT (2026-09-21 fix): on a fresh bootstrap the schema seed assigns
intent IDs in insertion order, so IDs 12-14 are already taken by inventory
intents (inventory_minimum_stock, inventory_maximum_stock, inventory_eoq) by
the time this migration runs. The historical hardcoded intent_id inserts
silently no-opped (INSERT OR IGNORE conflicting on the primary key): none of
the three Customer_Order intents ever existed under their own names, and
their perspective/query wiring attached to whatever inventory intent held
that ID instead. Same bug class already fixed for order_revenue_recognition
in add_receivables_wiring.py — this applies the same fix: each intent is
inserted BY NAME (intent_name is UNIQUE), the real intent_id is resolved at
runtime, and rows misattached by the old bug are repaired idempotently
(no-ops on healthy databases).

PERSPECTIVE NAME FIX (2026-09-21): the historical version also hardcoded a
secondary "Customer" perspective cross-link (perspective_id 5) for
customer_order_delivery/quality_exposure — but there is no "Customer"
perspective in this schema (id 5 is really Receivables). Per user decision,
that stray "Customer" cross-link is mapped to CRM (the closest real
perspective to a generic customer-facing lens); the Quality cross-link on
customer_order_quality_exposure was already correct (id 1 is genuinely
Quality) and is now resolved by name for robustness.

Run once:
    cd hf-space-inventory-sqlgen
    python migrations/add_customer_order_perspective.py

Safe to re-run — uses INSERT OR IGNORE on all rows.
"""

import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema", "manufacturing.db")

INTENTS = [
    {
        "intent_name": "customer_order_lifecycle",
        "intent_category": "customer_order",
        "description": "Order fulfillment status — completion rate vs plan by product line",
        "typical_question": "Show me order completion for July. Which lines are behind schedule?",
        "primary_binding_key": "start_date",
    },
    {
        "intent_name": "customer_order_delivery",
        "intent_category": "customer_order",
        "description": "Delivery performance to customer — on-time rate, fill rate, quality score",
        "typical_question": "What is our on-time delivery rate from July 1st?",
        "primary_binding_key": "start_date",
    },
    {
        "intent_name": "customer_order_quality_exposure",
        "intent_category": "customer_order",
        "description": "Defect exposure on customer-facing orders — severity breakdown and estimated rework cost",
        "typical_question": "Show quality exposure on Aerospace orders since July 1st.",
        "primary_binding_key": "start_date",
    },
]

# intent_name -> [(perspective_name, explanation), ...]
# lifecycle → Customer_Order only
# delivery → Customer_Order + CRM (stray "Customer" cross-link, mapped to CRM)
# quality_exposure → Customer_Order + Quality
INTENT_PERSPECTIVES = {
    "customer_order_lifecycle": [
        ("Customer_Order", "customer_order_lifecycle within Customer_Order perspective"),
    ],
    "customer_order_delivery": [
        ("Customer_Order", "customer_order_delivery within Customer_Order perspective"),
        ("CRM", "customer_order_delivery also within CRM perspective — customer-facing delivery signal"),
    ],
    "customer_order_quality_exposure": [
        ("Customer_Order", "customer_order_quality_exposure within Customer_Order perspective"),
        ("Quality", "customer_order_quality_exposure also within Quality perspective"),
    ],
}

# intent_name -> (query_index, query_name)
INTENT_QUERIES = {
    "customer_order_lifecycle": (1, "Order lifecycle fulfillment status"),
    "customer_order_delivery": (2, "Customer delivery performance"),
    "customer_order_quality_exposure": (3, "Customer order quality exposure"),
}

QUERY_FILE = "customer_order.sql"


def _fail(msg: str) -> None:
    raise SystemExit(f"[add_customer_order_perspective] FAIL-CLOSED: {msg}")


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
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    cur = conn.cursor()

    # Customer_Order is already seeded by schema_sqlite.sql — just confirm it exists.
    _resolve_perspective(cur, "Customer_Order")
    print("  perspective Customer_Order: already present (base schema)")

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
            "WHERE query_category = 'customer_order' AND query_name = ? AND intent_id <> ?",
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
            VALUES (?, 'customer_order', ?, ?, ?)
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
    print("\nDone. Customer_Order perspective wired successfully.")


if __name__ == "__main__":
    print(f"DB: {os.path.abspath(DB_PATH)}")
    run()
