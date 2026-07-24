"""Wire the AR Aging snippet into the Query Palette under the Receivables intent.

The receivables_araging_20260724_000001 snippet (AR aging bucketed by days
past due_date) is now approved in reviewer_manifest.json.  This migration
adds it as query_index=3 under the order_revenue_recognition intent
(Receivables perspective) so it appears in the Query Palette when analysts
drill into the Receivables → OrderAccountingState chain.

ID-INDEPENDENT: the intent_id is resolved by intent_name at runtime — on a
fresh bootstrap the autoincrement IDs differ from the live DB (ID 19 there
belongs to a ledger intent), so hardcoding the ID silently wires the palette
entry to the wrong intent.

What this migration does (deterministic, idempotent, fail-closed):

  1. Resolves order_revenue_recognition by name; exits with a clear error
     if add_receivables_wiring.py has not been run first.
  2. Repairs any palette row misattached by the old hardcoded-ID bug, then
     inserts the palette entry via INSERT OR IGNORE (safe to re-run).
  3. Points the intent's primary_binding_key at the approved AR aging
     snippet (add_receivables_wiring.py seeded the placeholder 'start_date',
     which resolves to nothing and would make the NLQ dispatcher fail closed).
  4. Verifies the row and binding key are present after the updates.

Run once (safe to re-run):
    cd hf-space-inventory-sqlgen
    python migrations/add_ar_aging_palette_wiring.py
"""

import os
import sqlite3
import sys

DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app_schema", "manufacturing.db"
)

BINDING_KEY = "receivables_araging_20260724_000001"
INTENT_NAME = "order_revenue_recognition"

PALETTE_ENTRY = {
    "query_category": "receivables",
    "query_file": BINDING_KEY,
    "query_index": 3,
    "query_name": "AR Aging — Open & Disputed Invoices",
}


def _fail(msg: str) -> None:
    raise SystemExit(f"[add_ar_aging_palette_wiring] FAIL-CLOSED: {msg}")


def run() -> None:
    print(f"DB: {os.path.abspath(DB_PATH)}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=10000")
    cur = conn.cursor()

    # 1 — resolve the intent BY NAME (add_receivables_wiring.py must have
    # run). Never hardcode the autoincrement intent_id: it differs between
    # fresh bootstraps and the live DB.
    row = cur.execute(
        "SELECT intent_id FROM schema_intents WHERE intent_name = ?",
        (INTENT_NAME,),
    ).fetchone()
    if not row:
        conn.close()
        _fail(
            f"intent {INTENT_NAME!r} not found. "
            "Run migrations/add_receivables_wiring.py first."
        )
    rid = row[0]
    print(f"  intent {INTENT_NAME} found: intent_id = {rid}")

    # 1b — repair a palette row misattached by the old hardcoded-ID bug
    # (fresh bootstraps where ID 19 belonged to a ledger intent). UPDATE OR
    # REPLACE moves it to the correct intent, replacing any duplicate that
    # already sits there. No-op on healthy databases.
    cur.execute(
        "UPDATE OR REPLACE schema_intent_queries SET intent_id = ? "
        "WHERE query_file = ? AND intent_id <> ?",
        (rid, BINDING_KEY, rid),
    )
    if cur.rowcount:
        print(f"  repaired {cur.rowcount} misattached palette row(s) -> intent {rid}")

    # 2 — insert palette entry (idempotent)
    cur.execute(
        """
        INSERT OR IGNORE INTO schema_intent_queries
            (intent_id, query_category, query_file, query_index, query_name)
        VALUES (?, ?, ?, ?, ?)
        """,
        (rid, PALETTE_ENTRY["query_category"], PALETTE_ENTRY["query_file"],
         PALETTE_ENTRY["query_index"], PALETTE_ENTRY["query_name"]),
    )
    inserted = cur.rowcount
    print(
        f"  palette entry '{PALETTE_ENTRY['query_name']}': "
        f"{'inserted' if inserted else 'already present (skipped)'}"
    )

    # 3 — point the intent's primary_binding_key at the approved snippet.
    # add_receivables_wiring.py seeded the placeholder 'start_date' (no such
    # binding key exists), which makes the dispatcher's binding path fail
    # closed for every question routed to this intent. Idempotent: only
    # rewrites the placeholder or a NULL, never a hand-set key.
    cur.execute(
        """
        UPDATE schema_intents
        SET primary_binding_key = ?
        WHERE intent_id = ?
          AND (primary_binding_key IS NULL
               OR primary_binding_key = 'start_date'
               OR primary_binding_key = ?)
        """,
        (BINDING_KEY, rid, BINDING_KEY),
    )
    print(
        f"  primary_binding_key {'set to ' + BINDING_KEY if cur.rowcount else 'left untouched (hand-set value present)'}"
    )

    conn.commit()

    # 4 — verify
    exists = cur.execute(
        "SELECT 1 FROM schema_intent_queries "
        "WHERE intent_id = ? AND query_file = ?",
        (rid, BINDING_KEY),
    ).fetchone()
    if not exists:
        conn.close()
        _fail("palette entry not found after insert — something went wrong")

    # A hand-set (non-placeholder, non-NULL) key is deliberately left
    # untouched by step 3, so accept it here instead of failing the chain.
    bk = cur.execute(
        "SELECT primary_binding_key FROM schema_intents WHERE intent_id = ?",
        (rid,),
    ).fetchone()
    if not bk or bk[0] in (None, "", "start_date"):
        conn.close()
        _fail(
            f"intent {rid} ({INTENT_NAME}) primary_binding_key is "
            f"{bk[0] if bk else None!r} — still unset/placeholder after update, "
            f"expected {BINDING_KEY!r}"
        )
    if bk[0] != BINDING_KEY:
        print(
            f"  NOTE: primary_binding_key is hand-set to {bk[0]!r} "
            f"(not {BINDING_KEY!r}); leaving as-is."
        )

    conn.close()
    print(
        "\n[add_ar_aging_palette_wiring] done — "
        f"AR Aging query wired to Receivables intent (intent {rid}, query_index 3, "
        f"primary_binding_key={BINDING_KEY})."
    )


if __name__ == "__main__":
    run()
