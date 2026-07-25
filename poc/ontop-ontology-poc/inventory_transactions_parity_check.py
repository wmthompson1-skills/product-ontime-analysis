#!/usr/bin/env python3
"""
Inventory-transactions parity check: virtual SPARQL graph (Ontop) vs governed SQL.
================================================================================

The NINTH governed showcase. Proves that the inventory-transactions ledger
answered through the standards-based virtual knowledge graph (Ontop rewriting
SPARQL -> SQL over manufacturing.db) matches, to floating-point tolerance, the
numbers the governed SQLite grounding query produces over the SAME read-only
snapshot.

LIKE SHOWCASES 6 (customer-order demand), 7 (capacity planning), AND 8
(shop-floor routing), AND UNLIKE the on-time / OEE showcases, the
inventory-transactions layer has NO computation_template / concept — it was
delivered as SME-approved docs + a runnable SQLite grounding query only
(archived at hf-space-inventory-sqlgen/app_schema/ground_truth/sql_snippets/
_archived/manufacturing_inventorytransactions_20260704_000005.sql,
plus the Terminology Guide + Entry Index). So this check grounds the SPARQL
answers against that DIRECT governed SQL, run on the same snapshot. The SQL stays
the single source of truth; Ontop is only a publishing layer over it.

The signed movement effect is defined by the Terminology Guide / grounding SQL:
    effect on Quantity on Hand = +qty when type = 'I' (In),  -qty when type = 'O'.
Net movement is therefore SUM(In qty) - SUM(Out qty). Rather than express that
signed CASE inside a single SPARQL SUM (a shape Ontop can serialize as SQL SQLite
rejects), each side is a separate SCALAR SUM and the two are subtracted here in
Python — keeping every SPARQL query within the Ontop+SQLite shapes that serialize
cleanly (no GROUP BY / no OPTIONAL / no CASE inside SUM).

Two headline inventory questions, both restated from the grounding query's math:
  * NET total movement quantity across every transaction =
    SUM(In qty) - SUM(Out qty)   (the horizon-total change to Quantity on Hand).
  * NET movement for ONE specific part at ONE site = same signed sum for
    P-10011 at SITE-1 — the fully-reconciled case the grounding query walks
    (ledger net = on-hand = trace = 62 -> reconciled 'Y').

Read-only by design (same guarantees as parity_check.py): the live WAL-mode DB is
opened only to take a backup snapshot; Ontop and the governed SQL both read the
same snapshot, so the two engines provably see identical data; nothing ever writes
the live file and ArangoDB is never touched.

Not wired into scripts/post-merge.sh (it needs the Java + Ontop toolchain, like
parity_check.py). Run it directly:

    python3 poc/ontop-ontology-poc/inventory_transactions_parity_check.py

Exit code 0 on parity, 1 on mismatch or error.
"""
import os
import sqlite3
import subprocess
import sys

POC_DIR = os.path.dirname(os.path.abspath(__file__))
if POC_DIR not in sys.path:
    sys.path.insert(0, POC_DIR)

import parity_check as pc  # reuse snapshot / runtime-properties / CSV helpers + paths

ONTOP = pc.ONTOP
ONTOLOGY = os.path.join(POC_DIR, "ontology", "inventory_transactions.ttl")
MAPPING = os.path.join(POC_DIR, "mapping", "inventory_transactions.obda")
QUERIES = pc.QUERIES
RESULTS = pc.RESULTS
LIVE_DB = pc.LIVE_DB

QTY_TOLERANCE = 1e-6
# The part+site the grounding query reconciles fully (ledger 62 = on-hand 62 =
# trace 62 -> 'Y'). The inv_part_in_qty.rq / inv_part_out_qty.rq queries pin this
# same part IRI and site, so the SPARQL and SQL sides net the SAME transactions.
NET_PART = "P-10011"
NET_SITE = "SITE-1"


def run_sparql(props, query_file, out_csv):
    cmd = [
        ONTOP, "query",
        "-m", MAPPING,
        "-t", ONTOLOGY,
        "-p", props,
        "-q", query_file,
        "-o", out_csv,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=POC_DIR)
    if res.returncode != 0:
        sys.stderr.write(res.stdout + "\n" + res.stderr + "\n")
        raise SystemExit(f"ontop query failed for {os.path.basename(query_file)}")
    return out_csv


def sql_directional_qty(conn, direction):
    """Governed SQL: total quantity moved in one direction ('I' In / 'O' Out)."""
    return conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM inventory_transaction "
        "WHERE type = ?", (direction,)
    ).fetchone()[0]


def sql_part_directional_qty(conn, part_id, site_id, direction):
    """Governed SQL: quantity moved in one direction for one part at one site —
    the same signed inputs the grounding query nets for the reconciled case."""
    return conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM inventory_transaction "
        "WHERE part_id = ? AND site_id = ? AND type = ?",
        (part_id, site_id, direction)
    ).fetchone()[0]


def main():
    if not os.path.exists(ONTOP):
        raise SystemExit(
            "Ontop CLI not found. Run: python3 replit_integrations/ontop_poc_setup.py "
            "first to download the toolchain."
        )
    if not os.path.exists(LIVE_DB):
        raise SystemExit(f"DuckDB snapshot not found at {LIVE_DB}. Run: cd Utilities/SQLMesh && sqlmesh run")

    print("Building read-only snapshot of the live database...")
    snap = pc.make_snapshot()
    props = pc.write_runtime_properties(snap)

    print("Running SPARQL through the Ontop virtual graph...")
    in_csv = run_sparql(props, os.path.join(QUERIES, "inv_total_in_qty.rq"),
                        os.path.join(RESULTS, "inv_total_in_qty.csv"))
    out_csv = run_sparql(props, os.path.join(QUERIES, "inv_total_out_qty.rq"),
                         os.path.join(RESULTS, "inv_total_out_qty.csv"))
    part_in_csv = run_sparql(props, os.path.join(QUERIES, "inv_part_in_qty.rq"),
                             os.path.join(RESULTS, "inv_part_in_qty.csv"))
    part_out_csv = run_sparql(props, os.path.join(QUERIES, "inv_part_out_qty.rq"),
                              os.path.join(RESULTS, "inv_part_out_qty.csv"))

    sparql_in = pc.read_first_column(in_csv)
    sparql_out = pc.read_first_column(out_csv)
    sparql_net = sparql_in - sparql_out
    sparql_part_in = pc.read_first_column(part_in_csv)
    sparql_part_out = pc.read_first_column(part_out_csv)
    sparql_part_net = sparql_part_in - sparql_part_out
    # Supporting transaction counts (second column of the total queries).
    _, in_rows = pc.read_result_rows(in_csv)
    _, out_rows = pc.read_result_rows(out_csv)
    sparql_in_txns = int(float(in_rows[0][1])) if in_rows and len(in_rows[0]) > 1 else None
    sparql_out_txns = int(float(out_rows[0][1])) if out_rows and len(out_rows[0]) > 1 else None

    print("Running the governed SQL aggregates on the same snapshot...")
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        sql_in = sql_directional_qty(conn, "I")
        sql_out = sql_directional_qty(conn, "O")
        sql_net = sql_in - sql_out
        sql_part_in = sql_part_directional_qty(conn, NET_PART, NET_SITE, "I")
        sql_part_out = sql_part_directional_qty(conn, NET_PART, NET_SITE, "O")
        sql_part_net = sql_part_in - sql_part_out
        sql_in_txns = conn.execute(
            "SELECT COUNT(*) FROM inventory_transaction WHERE type = 'I'").fetchone()[0]
        sql_out_txns = conn.execute(
            "SELECT COUNT(*) FROM inventory_transaction WHERE type = 'O'").fetchone()[0]
    finally:
        conn.close()

    print()
    print("=" * 68)
    print("  INVENTORY TRANSACTIONS \u2014 PARITY CHECK (SPARQL vs governed SQL)")
    print("=" * 68)
    print(f"  Net movement, all txns  (SQL / SPARQL): {sql_net!r} / {sparql_net!r}")
    print(f"    In qty  (SQL / SPARQL): {sql_in!r} / {sparql_in!r}  "
          f"[{sql_in_txns} / {sparql_in_txns} txns]")
    print(f"    Out qty (SQL / SPARQL): {sql_out!r} / {sparql_out!r}  "
          f"[{sql_out_txns} / {sparql_out_txns} txns]")
    print(f"  {NET_PART} @ {NET_SITE} net    (SQL / SPARQL): {sql_part_net!r} / {sparql_part_net!r}")
    print("-" * 68)
    print(f"    signed effect = +qty for type 'I' (In), -qty for type 'O' (Out);")
    print(f"    {NET_PART} @ {NET_SITE} is the fully-reconciled case the grounding query")
    print(f"    walks (ledger net = on-hand = trace = {sql_part_net!r} -> 'Y').")
    print("=" * 68)

    net_ok = abs(sparql_net - sql_net) <= QTY_TOLERANCE
    in_ok = abs(sparql_in - sql_in) <= QTY_TOLERANCE
    out_ok = abs(sparql_out - sql_out) <= QTY_TOLERANCE
    part_net_ok = abs(sparql_part_net - sql_part_net) <= QTY_TOLERANCE
    # Assert the directional transaction counts too, not just print them. The
    # SPARQL type filter publishes exactly the governed directional population, so
    # a future change that drops rows (population drift) is caught even if a sum
    # coincidentally still matches. A count that could not be parsed (None) fails
    # loudly here rather than being silently skipped.
    txns_ok = (sparql_in_txns == sql_in_txns and sparql_out_txns == sql_out_txns)

    ok = net_ok and in_ok and out_ok and part_net_ok and txns_ok
    if ok:
        print("  RESULT: PARITY CONFIRMED \u2014 the virtual SPARQL graph and the")
        print("          governed SQL grounding query return the same net movement,")
        print("          directional quantities, transaction counts, and per-part net.")
    else:
        print("  RESULT: MISMATCH")
        if not net_ok:
            print(f"    net movement differs by {abs(sparql_net - sql_net)}")
        if not in_ok:
            print(f"    In qty differs by {abs(sparql_in - sql_in)}")
        if not out_ok:
            print(f"    Out qty differs by {abs(sparql_out - sql_out)}")
        if not part_net_ok:
            print(f"    {NET_PART} @ {NET_SITE} net differs by {abs(sparql_part_net - sql_part_net)}")
        if not txns_ok:
            print(f"    txn counts differ: In SQL {sql_in_txns!r} vs SPARQL {sparql_in_txns!r}, "
                  f"Out SQL {sql_out_txns!r} vs SPARQL {sparql_out_txns!r}")
    print("=" * 68)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
