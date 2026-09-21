"""Wire "Three-Way Match Coverage" into the Query Palette (selector wiring).

The consolidated three-way-match coverage spine (governed snippet
``payables_threewaymatchcoverage_20260708_000005``) was mirrored into
``app_schema/queries/supplier_performance.sql`` as a palette query block, but
nothing registers it in ``schema_intent_queries``, so it is unreachable from
the Query Palette. This migration performs the same idempotent selector
wiring the Partial-Receipt Accrual entry used
(``add_partial_receipt_accrual_demo.py``):

  - "Three-Way Match Coverage" was inserted into supplier_performance.sql
    AFTER "Partial-Receipt Accrual Exposure" and BEFORE "Three-Way Match
    Exceptions", shifting file order: Coverage is the 8th "-- Query:" marker
    (0-based index 7) and Three-Way Match Exceptions moved to index 8.
  - schema_intent_queries has a unique index on (intent_id, query_file,
    query_index), so bump Three-Way Match Exceptions first, then insert
    Coverage at 7 under the same payables intent (18).
  - Idempotent: the guarded UPDATE matches nothing on re-run, and the
    INSERT OR IGNORE is a no-op once present.

FAIL-CLOSED VERIFY at the end:
  - the palette file's marker order must place Coverage at index 7 and
    Exceptions at index 8 (wiring and file must agree);
  - the mirrored palette SQL, NULL-bound, must execute and return the SAME
    row population as the governed snippet file (row-for-row), with all
    five match states present — else exit non-zero.

No data writes: this migration touches ONLY schema_intent_queries.
"""

import os
import re
import sqlite3
import sys

HF_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(HF_DIR, "app_schema", "manufacturing.db")
QUERIES_PATH = os.path.join(HF_DIR, "app_schema", "queries",
                            "supplier_performance.sql")
SNIPPET_PATH = os.path.join(
    HF_DIR, "app_schema", "ground_truth", "sql_snippets",
    "payables_threewaymatchcoverage_20260708_000005.sql")

INTENT_NAME = "supplier_payables_exposure"  # resolved by name — see add_supplier_payables_wiring.py
COVERAGE_NAME = "Three-Way Match Coverage"
EXCEPTIONS_NAME = "Three-Way Match Exceptions"
COVERAGE_INDEX = 7
EXCEPTIONS_INDEX = 8

MATCH_STATES = {"Not Received", "Received-Uninvoiced", "Partially Invoiced",
                "Matched", "Over-Invoiced"}

NULL_PARAMS = {"supplier_id": None, "start_date": None, "end_date": None}


def _query_marker_names(sql_text: str) -> list:
    return [line.replace("-- Query:", "").strip()
            for line in sql_text.splitlines()
            if line.startswith("-- Query:")]


def _extract_query(sql_text: str, name: str) -> str:
    """Extract a palette query body by its '-- Query:' marker (to the ';')."""
    marker = f"-- Query: {name}"
    start = sql_text.index(marker)
    body_start = sql_text.index("\n", start) + 1
    m = re.search(r"^(?!--)", sql_text[body_start:], re.M)
    body_start += m.start()
    end = sql_text.index(";", body_start)
    return sql_text[body_start:end + 1]


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"FAIL: database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    row = cur.execute(
        "SELECT intent_id FROM schema_intents WHERE intent_name = ?",
        (INTENT_NAME,),
    ).fetchone()
    if not row:
        raise SystemExit(
            f"FAIL: intent {INTENT_NAME!r} not found — run "
            "add_supplier_payables_wiring.py first")
    INTENT_ID = row[0]
    print(f"  resolved intent_id = {INTENT_ID}")
    print(f"add_twm_coverage_palette: wiring '{COVERAGE_NAME}' "
          f"into the Query Palette (intent {INTENT_ID})")

    # ── SELECTOR WIRING (same pattern as the PRA entry) ──────────────────
    cur.execute(
        """UPDATE schema_intent_queries SET query_index = ?
           WHERE intent_id = ? AND query_file = 'supplier_performance.sql'
             AND query_name = ? AND query_index = ?""",
        (EXCEPTIONS_INDEX, INTENT_ID, EXCEPTIONS_NAME, COVERAGE_INDEX))
    if cur.rowcount:
        print(f"  ~ {EXCEPTIONS_NAME}: query_index {COVERAGE_INDEX} -> "
              f"{EXCEPTIONS_INDEX} (file order shifted)")
    cur.execute(
        """INSERT OR IGNORE INTO schema_intent_queries
               (intent_id, query_category, query_file, query_index, query_name)
           VALUES (?, 'supplier_performance', 'supplier_performance.sql',
                   ?, ?)""",
        (INTENT_ID, COVERAGE_INDEX, COVERAGE_NAME))
    if cur.rowcount:
        print(f"  + intent_query '{COVERAGE_NAME}' wired to "
              f"supplier_payables_exposure (intent {INTENT_ID})")
    else:
        print(f"  = intent_query '{COVERAGE_NAME}' already wired, skipping")
    conn.commit()

    # ── FAIL-CLOSED VERIFY ────────────────────────────────────────────────
    # 1. File marker order and wiring must agree.
    with open(QUERIES_PATH, encoding="utf-8") as fh:
        palette_text = fh.read()
    names = _query_marker_names(palette_text)
    for name, idx in ((COVERAGE_NAME, COVERAGE_INDEX),
                      (EXCEPTIONS_NAME, EXCEPTIONS_INDEX)):
        if name not in names:
            raise SystemExit(
                f"FAIL: '-- Query: {name}' marker missing from "
                f"supplier_performance.sql")
        if names.index(name) != idx:
            raise SystemExit(
                f"FAIL: '{name}' is marker index {names.index(name)} in the "
                f"file but the wiring expects {idx} — remap the indexes")
        row = cur.execute(
            """SELECT query_index FROM schema_intent_queries
               WHERE intent_id = ? AND query_file = 'supplier_performance.sql'
                 AND query_name = ?""", (INTENT_ID, name)).fetchone()
        if row is None or row[0] != idx:
            raise SystemExit(
                f"FAIL: schema_intent_queries has '{name}' at "
                f"{row and row[0]}, expected {idx}")

    # 2. Mirrored palette SQL must return the SAME population as the
    #    governed snippet, NULL-bound, with all five match states.
    palette_sql = _extract_query(palette_text, COVERAGE_NAME)
    with open(SNIPPET_PATH, encoding="utf-8") as fh:
        snippet_sql = fh.read()
    palette_rows = cur.execute(palette_sql, NULL_PARAMS).fetchall()
    snippet_rows = cur.execute(snippet_sql, NULL_PARAMS).fetchall()
    if not palette_rows:
        raise SystemExit("FAIL: palette Coverage query returned 0 rows")
    if palette_rows != snippet_rows:
        raise SystemExit(
            f"FAIL: palette Coverage query drifted from the governed snippet "
            f"({len(palette_rows)} vs {len(snippet_rows)} rows / "
            f"row-set mismatch) — re-mirror the SQL")
    state_col = -1  # match_status is the last selected column
    states = {r[state_col] for r in palette_rows}
    if states != MATCH_STATES:
        raise SystemExit(
            f"FAIL: coverage population must span all five match states, "
            f"got {sorted(states)}")

    print(f"  VERIFY OK: '{COVERAGE_NAME}' at index {COVERAGE_INDEX}, "
          f"'{EXCEPTIONS_NAME}' at {EXCEPTIONS_INDEX}; palette SQL matches "
          f"the governed snippet ({len(palette_rows)} rows, all five "
          f"match states present)")
    conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
