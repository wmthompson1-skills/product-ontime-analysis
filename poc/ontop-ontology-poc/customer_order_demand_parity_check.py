#!/usr/bin/env python3
"""
Customer Order demand parity check: virtual SPARQL graph (Ontop) vs governed SQL.
================================================================================

The SIXTH governed showcase. Proves that the customer-order DEMAND layer answered
through the standards-based virtual knowledge graph (Ontop rewriting SPARQL -> SQL
over manufacturing.db) matches, to floating-point tolerance, the numbers the
governed SQLite grounding query produces over the SAME read-only snapshot.

WHAT GROUNDS PARITY HERE IS DIFFERENT from the on-time (parity_check.py) and OEE
(oee_parity_check.py) showcases. Those compare SPARQL to SolderEngine, because
each metric has a semantic-layer computation_template. The customer-order demand
layer has NO computation_template / concept — it was delivered as SME-approved
docs + a runnable SQLite grounding query only
(archived at hf-space-inventory-sqlgen/app_schema/ground_truth/sql_snippets/_archived/
manufacturing_customerorderdemand_20260704_000002.sql). So this check grounds the
SPARQL answers against that DIRECT governed SQL, run on the same snapshot. The
SQL stays the single source of truth; Ontop is only a publishing layer over it.

Three parity numbers, all restated from the grounding query:
  * open demand VALUE = SUM(order_qty * unit_price) over lines whose order is Open
  * open demand QTY   = SUM(order_qty)             over lines whose order is Open
  * ATP for the tightest-ATP part (P-10026) = part.on_hand_qty - SUM(open order_qty)

All three SPARQL queries are scalar (no GROUP BY / no OPTIONAL), so they stay
within the Ontop+SQLite shapes that serialize cleanly.

Read-only by design (same guarantees as parity_check.py): the live WAL-mode DB is
opened only to take a backup snapshot; Ontop and the governed SQL both read the
same snapshot, so the two engines provably see identical data; nothing ever writes
the live file and ArangoDB is never touched.

Not wired into scripts/post-merge.sh (it needs the Java + Ontop toolchain, like
parity_check.py). Run it directly:

    python3 poc/ontop-ontology-poc/customer_order_demand_parity_check.py

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
ONTOLOGY = os.path.join(POC_DIR, "ontology", "customer_order_demand.ttl")
MAPPING = os.path.join(POC_DIR, "mapping", "customer_order_demand.obda")
QUERIES = pc.QUERIES
RESULTS = pc.RESULTS
LIVE_DB = pc.LIVE_DB

VALUE_TOLERANCE = 1e-6
QTY_TOLERANCE = 1e-9
# The tightest-ATP part in the demand doc's ATP table. The part_on_hand.rq /
# part_open_qty.rq queries pin this same IRI, so the SPARQL and SQL sides compare
# the ATP of the SAME part (parity holds for it whether or not it stays tightest).
ATP_PART = "P-10026"


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


def sql_open_value(conn):
    """Governed SQL: total open demand value (Query 1 of the grounding doc,
    extended line value, summed over the open-demand set)."""
    return conn.execute(
        "SELECT SUM(col.order_qty * col.unit_price) "
        "FROM customer_order_line col "
        "JOIN customer_order co ON col.order_id = co.order_id "
        "WHERE co.status = 'Open'"
    ).fetchone()[0]


def sql_open_qty(conn):
    """Governed SQL: total open demand quantity over the open-demand set."""
    return conn.execute(
        "SELECT SUM(col.order_qty) "
        "FROM customer_order_line col "
        "JOIN customer_order co ON col.order_id = co.order_id "
        "WHERE co.status = 'Open'"
    ).fetchone()[0]


def sql_part_atp(conn, part_id):
    """Governed SQL: the ATP derivation from Query 2 of the grounding doc for one
    part — ATP = on_hand_qty - SUM(open order_qty). Returns (on_hand, open_qty,
    atp)."""
    on_hand = conn.execute(
        "SELECT on_hand_qty FROM part WHERE part_id = ?", (part_id,)
    ).fetchone()[0]
    open_qty = conn.execute(
        "SELECT COALESCE(SUM(col.order_qty), 0) "
        "FROM customer_order_line col "
        "JOIN customer_order co ON col.order_id = co.order_id "
        "WHERE co.status = 'Open' AND col.part_id = ?", (part_id,)
    ).fetchone()[0]
    return on_hand, open_qty, on_hand - open_qty


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
    qty_csv = run_sparql(props, os.path.join(QUERIES, "open_demand_qty.rq"),
                         os.path.join(RESULTS, "open_demand_qty.csv"))
    value_csv = run_sparql(props, os.path.join(QUERIES, "open_demand_value.rq"),
                           os.path.join(RESULTS, "open_demand_value.csv"))
    on_hand_csv = run_sparql(props, os.path.join(QUERIES, "part_on_hand.rq"),
                             os.path.join(RESULTS, "part_on_hand.csv"))
    part_qty_csv = run_sparql(props, os.path.join(QUERIES, "part_open_qty.rq"),
                              os.path.join(RESULTS, "part_open_qty.csv"))

    sparql_open_qty = pc.read_first_column(qty_csv)
    sparql_open_value = pc.read_first_column(value_csv)
    sparql_on_hand = pc.read_first_column(on_hand_csv)
    sparql_part_open_qty = pc.read_first_column(part_qty_csv)
    sparql_atp = sparql_on_hand - sparql_part_open_qty

    print("Running the governed SQL aggregates on the same snapshot...")
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        sql_value = sql_open_value(conn)
        sql_qty = sql_open_qty(conn)
        sql_on_hand, sql_part_open_qty, sql_atp = sql_part_atp(conn, ATP_PART)
    finally:
        conn.close()

    print()
    print("=" * 68)
    print("  CUSTOMER ORDER DEMAND \u2014 PARITY CHECK (SPARQL vs governed SQL)")
    print("=" * 68)
    print(f"  Open demand value  (SQL / SPARQL): {sql_value!r} / {sparql_open_value!r}")
    print(f"  Open demand qty    (SQL / SPARQL): {sql_qty!r} / {sparql_open_qty!r}")
    print(f"  {ATP_PART} ATP        (SQL / SPARQL): {sql_atp!r} / {sparql_atp!r}")
    print("-" * 68)
    print(f"    {ATP_PART}: on_hand {sql_on_hand!r} - open demand {sql_part_open_qty!r} "
          f"= ATP {sql_atp!r}")
    print("=" * 68)

    value_ok = abs(sparql_open_value - sql_value) <= VALUE_TOLERANCE
    qty_ok = abs(sparql_open_qty - sql_qty) <= QTY_TOLERANCE
    atp_ok = abs(sparql_atp - sql_atp) <= QTY_TOLERANCE

    ok = value_ok and qty_ok and atp_ok
    if ok:
        print("  RESULT: PARITY CONFIRMED \u2014 the virtual SPARQL graph and the")
        print("          governed SQL grounding query return the same demand numbers.")
    else:
        print("  RESULT: MISMATCH")
        if not value_ok:
            print(f"    open value differs by {abs(sparql_open_value - sql_value)}")
        if not qty_ok:
            print(f"    open qty differs by {abs(sparql_open_qty - sql_qty)}")
        if not atp_ok:
            print(f"    {ATP_PART} ATP differs by {abs(sparql_atp - sql_atp)}")
    print("=" * 68)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
