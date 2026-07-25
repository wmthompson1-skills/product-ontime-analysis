#!/usr/bin/env python3
"""
Capacity-planning parity check: virtual SPARQL graph (Ontop) vs governed SQL.
================================================================================

The SEVENTH governed showcase. Proves that the capacity-planning LOAD layer
answered through the standards-based virtual knowledge graph (Ontop rewriting
SPARQL -> SQL over manufacturing.db) matches, to floating-point tolerance, the
numbers the governed SQLite grounding query produces over the SAME read-only
snapshot.

LIKE SHOWCASE 6 (customer-order demand) AND UNLIKE the on-time / OEE showcases,
the capacity layer has NO computation_template / concept — it was delivered as
SME-approved docs + a runnable SQLite grounding query only
(archived at hf-space-inventory-sqlgen/app_schema/ground_truth/sql_snippets/_archived/
manufacturing_capacityplanning_20260704_000001.sql). So this check grounds the SPARQL
answers against that DIRECT governed SQL, run on the same snapshot. The SQL stays
the single source of truth; Ontop is only a publishing layer over it.

Two headline capacity questions, both restated from the grounding query:
  * total in-house standard load = SUM(setup_hrs + run_hrs) over every in-house,
    scheduled routing operation (the horizon-total load).
  * busiest work center's total load = the same SUM filtered to LB-003 (the
    Level-II Assembly Technician — the most-loaded work center in the doc's
    bottleneck ranking).

Both SPARQL queries are scalar (no GROUP BY / no OPTIONAL), so they stay within
the Ontop+SQLite shapes that serialize cleanly. The in-house-only and
scheduled-only governance is baked into the .obda source SQL, so the SPARQL SUM
over all published operations already equals the governed in-house load.

Read-only by design (same guarantees as parity_check.py): the live WAL-mode DB is
opened only to take a backup snapshot; Ontop and the governed SQL both read the
same snapshot, so the two engines provably see identical data; nothing ever writes
the live file and ArangoDB is never touched.

Not wired into scripts/post-merge.sh (it needs the Java + Ontop toolchain, like
parity_check.py). Run it directly:

    python3 poc/ontop-ontology-poc/capacity_planning_parity_check.py

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
ONTOLOGY = os.path.join(POC_DIR, "ontology", "capacity_planning.ttl")
MAPPING = os.path.join(POC_DIR, "mapping", "capacity_planning.obda")
QUERIES = pc.QUERIES
RESULTS = pc.RESULTS
LIVE_DB = pc.LIVE_DB

LOAD_TOLERANCE = 1e-6
# The busiest work center in the capacity doc's bottleneck ranking. The
# work_center_load.rq query pins this same resource, so the SPARQL and SQL sides
# compare the load of the SAME work center (parity holds for it whether or not it
# stays the busiest).
BUSIEST_WC = "LB-003"


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


# The governed grounding query's load basis: standard hours = setup + run,
# COALESCEd so a missing component never voids an operation's load. The published
# set is in-house only (service_id IS NULL + a machine/labor work center) and
# scheduled only (sched_start_date IS NOT NULL) — exactly what the .obda mapping
# encodes, so the SPARQL totals line up with these SQL totals.
_GOVERNED_FROM = (
    "FROM operation o "
    "JOIN shop_resource sr ON sr.resource_id = o.resource_id "
    "WHERE o.service_id IS NULL "
    "AND sr.resource_type IN ('M', 'L') "
    "AND o.sched_start_date IS NOT NULL"
)


def sql_total_load(conn):
    """Governed SQL: total in-house standard load over the whole horizon."""
    return conn.execute(
        "SELECT SUM(COALESCE(o.setup_hrs, 0) + COALESCE(o.run_hrs, 0)) "
        + _GOVERNED_FROM
    ).fetchone()[0]


def sql_work_center_load(conn, resource_id):
    """Governed SQL: total in-house standard load on one work center."""
    return conn.execute(
        "SELECT SUM(COALESCE(o.setup_hrs, 0) + COALESCE(o.run_hrs, 0)) "
        + _GOVERNED_FROM
        + " AND o.resource_id = ?",
        (resource_id,),
    ).fetchone()[0]


def sql_count(conn, resource_id=None):
    """Governed SQL: how many in-house operations are loaded (optionally on one
    work center) — the supporting count beside each load sum."""
    sql = "SELECT COUNT(*) " + _GOVERNED_FROM
    args = ()
    if resource_id is not None:
        sql += " AND o.resource_id = ?"
        args = (resource_id,)
    return conn.execute(sql, args).fetchone()[0]


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
    total_csv = run_sparql(props, os.path.join(QUERIES, "capacity_total_load.rq"),
                           os.path.join(RESULTS, "capacity_total_load.csv"))
    wc_csv = run_sparql(props, os.path.join(QUERIES, "work_center_load.rq"),
                        os.path.join(RESULTS, "work_center_load.csv"))

    sparql_total = pc.read_first_column(total_csv)
    sparql_wc = pc.read_first_column(wc_csv)
    # Supporting counts (second column) for context, not asserted.
    _, total_rows = pc.read_result_rows(total_csv)
    _, wc_rows = pc.read_result_rows(wc_csv)
    sparql_total_ops = int(float(total_rows[0][1])) if total_rows and len(total_rows[0]) > 1 else None
    sparql_wc_ops = int(float(wc_rows[0][1])) if wc_rows and len(wc_rows[0]) > 1 else None

    print("Running the governed SQL aggregates on the same snapshot...")
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        sql_total = sql_total_load(conn)
        sql_wc = sql_work_center_load(conn, BUSIEST_WC)
        sql_total_ops = sql_count(conn)
        sql_wc_ops = sql_count(conn, BUSIEST_WC)
    finally:
        conn.close()

    print()
    print("=" * 68)
    print("  CAPACITY PLANNING \u2014 PARITY CHECK (SPARQL vs governed SQL)")
    print("=" * 68)
    print(f"  Total in-house load   (SQL / SPARQL): {sql_total!r} / {sparql_total!r}")
    print(f"    operations loaded   (SQL / SPARQL): {sql_total_ops!r} / {sparql_total_ops!r}")
    print(f"  {BUSIEST_WC} total load   (SQL / SPARQL): {sql_wc!r} / {sparql_wc!r}")
    print(f"    operations on {BUSIEST_WC} (SQL / SPARQL): {sql_wc_ops!r} / {sparql_wc_ops!r}")
    print("-" * 68)
    print(f"    load basis = SUM(setup_hrs + run_hrs), in-house + scheduled only;")
    print(f"    {BUSIEST_WC} is the busiest work center in the capacity bottleneck ranking.")
    print("=" * 68)

    total_ok = abs(sparql_total - sql_total) <= LOAD_TOLERANCE
    wc_ok = abs(sparql_wc - sql_wc) <= LOAD_TOLERANCE
    # The operation COUNTs are asserted too, not just printed. COALESCE means every
    # published in-house operation carries both hours, so the SPARQL inner join
    # publishes exactly the governed SQL population. Asserting the counts guards
    # against a future change that drops rows (population drift) while the load sum
    # coincidentally still matches. A count that could not be parsed (None) fails
    # loudly here rather than being silently skipped.
    total_ops_ok = sparql_total_ops == sql_total_ops
    wc_ops_ok = sparql_wc_ops == sql_wc_ops

    ok = total_ok and wc_ok and total_ops_ok and wc_ops_ok
    if ok:
        print("  RESULT: PARITY CONFIRMED \u2014 the virtual SPARQL graph and the")
        print("          governed SQL grounding query return the same load numbers")
        print("          and the same operation counts.")
    else:
        print("  RESULT: MISMATCH")
        if not total_ok:
            print(f"    total load differs by {abs(sparql_total - sql_total)}")
        if not wc_ok:
            print(f"    {BUSIEST_WC} load differs by {abs(sparql_wc - sql_wc)}")
        if not total_ops_ok:
            print(f"    total ops differ: SQL {sql_total_ops!r} vs SPARQL {sparql_total_ops!r}")
        if not wc_ops_ok:
            print(f"    {BUSIEST_WC} ops differ: SQL {sql_wc_ops!r} vs SPARQL {sparql_wc_ops!r}")
    print("=" * 68)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
