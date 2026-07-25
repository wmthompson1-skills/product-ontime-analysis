#!/usr/bin/env python3
"""
Shop-floor routing parity check: virtual SPARQL graph (Ontop) vs governed SQL.
================================================================================

The EIGHTH governed showcase. Proves that the shop-floor work & routing layer
answered through the standards-based virtual knowledge graph (Ontop rewriting
SPARQL -> SQL over manufacturing.db) matches, to floating-point tolerance, the
numbers the governed SQLite grounding query produces over the SAME read-only
snapshot.

LIKE SHOWCASES 6 (customer-order demand) AND 7 (capacity planning), AND UNLIKE
the on-time / OEE showcases, the routing layer has NO computation_template /
concept — it was delivered as SME-approved docs + a runnable SQLite grounding
query only (archived at hf-space-inventory-sqlgen/app_schema/ground_truth/sql_snippets/
_archived/manufacturing_shopfloorrouting_20260704_000003.sql). So this check grounds
the SPARQL answers against that DIRECT governed SQL, run on the same snapshot.
The SQL stays the single source of truth; Ontop is only a publishing layer over
it.

The grounding is a STRICT TWO-TABLE join of work_order and operation on wo_id; no
third table is added (the work-station name lives in prose only, matching the
grounding SQL).

Two headline routing questions, both restated from the grounding query:
  * total routing-step run hours across work orders = SUM(run_hrs) over every
    routing operation (the horizon-total run hours).
  * routing-step count for ONE specific work order = COUNT(operations) for
    WO-240003, the closed AIRFRAME traveler the grounding doc walks step by step.

Both SPARQL queries are scalar (no GROUP BY / no OPTIONAL), so they stay within
the Ontop+SQLite shapes that serialize cleanly.

Read-only by design (same guarantees as parity_check.py): the live WAL-mode DB is
opened only to take a backup snapshot; Ontop and the governed SQL both read the
same snapshot, so the two engines provably see identical data; nothing ever writes
the live file and ArangoDB is never touched.

Not wired into scripts/post-merge.sh (it needs the Java + Ontop toolchain, like
parity_check.py). Run it directly:

    python3 poc/ontop-ontology-poc/shop_floor_routing_parity_check.py

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
ONTOLOGY = os.path.join(POC_DIR, "ontology", "shop_floor_routing.ttl")
MAPPING = os.path.join(POC_DIR, "mapping", "shop_floor_routing.obda")
QUERIES = pc.QUERIES
RESULTS = pc.RESULTS
LIVE_DB = pc.LIVE_DB

HOURS_TOLERANCE = 1e-6
# The work order the grounding doc walks step by step (the routing "traveler").
# The work_order_step_count.rq query pins this same wo_id, so the SPARQL and SQL
# sides compare the step count of the SAME work order.
TRAVELER_WO = "WO-240003"


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


# The governed grounding query is a strict two-table join of work_order and
# operation on wo_id (archived at hf-space-inventory-sqlgen/app_schema/ground_truth/
# sql_snippets/_archived/manufacturing_shopfloorrouting_20260704_000003.sql). The .obda
# mapping restates exactly that join in its source SQL, so the published
# operation set is exactly this joined set and the SPARQL totals line up.
_GOVERNED_FROM = "FROM work_order wo JOIN operation op ON op.wo_id = wo.wo_id"


def sql_total_run_hours(conn):
    """Governed SQL: total routing-step run hours across every work order."""
    return conn.execute(
        "SELECT SUM(op.run_hrs) " + _GOVERNED_FROM
    ).fetchone()[0]


def sql_step_count(conn, wo_id):
    """Governed SQL: how many routing steps one work order has."""
    return conn.execute(
        "SELECT COUNT(*) " + _GOVERNED_FROM + " WHERE wo.wo_id = ?", (wo_id,)
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
    hours_csv = run_sparql(props, os.path.join(QUERIES, "routing_total_run_hours.rq"),
                           os.path.join(RESULTS, "routing_total_run_hours.csv"))
    steps_csv = run_sparql(props, os.path.join(QUERIES, "work_order_step_count.rq"),
                           os.path.join(RESULTS, "work_order_step_count.csv"))

    sparql_total_hours = pc.read_first_column(hours_csv)
    sparql_steps = pc.read_first_column(steps_csv)
    # Supporting count (second column of the run-hours query) for context.
    _, hours_rows = pc.read_result_rows(hours_csv)
    sparql_total_ops = int(float(hours_rows[0][1])) if hours_rows and len(hours_rows[0]) > 1 else None

    print("Running the governed SQL aggregates on the same snapshot...")
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        sql_total_hours = sql_total_run_hours(conn)
        sql_steps = sql_step_count(conn, TRAVELER_WO)
        sql_total_ops = conn.execute(
            "SELECT COUNT(*) " + _GOVERNED_FROM).fetchone()[0]
    finally:
        conn.close()

    print()
    print("=" * 68)
    print("  SHOP FLOOR ROUTING \u2014 PARITY CHECK (SPARQL vs governed SQL)")
    print("=" * 68)
    print(f"  Total routing run hours (SQL / SPARQL): {sql_total_hours!r} / {sparql_total_hours!r}")
    print(f"    operations routed     (SQL / SPARQL): {sql_total_ops!r} / {sparql_total_ops!r}")
    print(f"  {TRAVELER_WO} step count   (SQL / SPARQL): {sql_steps!r} / {sparql_steps!r}")
    print("-" * 68)
    print(f"    strict two-table join (work_order + operation on wo_id);")
    print(f"    {TRAVELER_WO} is the closed AIRFRAME traveler the grounding doc walks.")
    print("=" * 68)

    hours_ok = abs(sparql_total_hours - sql_total_hours) <= HOURS_TOLERANCE
    steps_ok = int(round(sparql_steps)) == int(sql_steps)
    # Assert the routed-operation count too, not just print it. The SPARQL inner
    # join publishes exactly the governed two-table-join population, so a future
    # change that drops rows (population drift) is caught even if the run-hours sum
    # coincidentally still matches. A count that could not be parsed (None) fails
    # loudly here rather than being silently skipped.
    ops_ok = sparql_total_ops == sql_total_ops

    ok = hours_ok and steps_ok and ops_ok
    if ok:
        print("  RESULT: PARITY CONFIRMED \u2014 the virtual SPARQL graph and the")
        print("          governed SQL grounding query return the same routing")
        print("          run hours, step count, and routed-operation count.")
    else:
        print("  RESULT: MISMATCH")
        if not hours_ok:
            print(f"    total run hours differ by {abs(sparql_total_hours - sql_total_hours)}")
        if not steps_ok:
            print(f"    {TRAVELER_WO} step count differs: SQL {sql_steps!r} vs SPARQL {sparql_steps!r}")
        if not ops_ok:
            print(f"    routed ops differ: SQL {sql_total_ops!r} vs SPARQL {sparql_total_ops!r}")
    print("=" * 68)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
