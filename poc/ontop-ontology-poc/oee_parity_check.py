#!/usr/bin/env python3
"""
OEE parity check: virtual SPARQL graph (Ontop) vs the governed SQL semantic layer.
=================================================================================

The SECOND governed showcase. Proves that the operational OEE (run-hours
efficiency) answered through the standards-based virtual knowledge graph (Ontop
rewriting SPARQL -> SQL over manufacturing.db) matches, to floating-point
tolerance, the number produced by SolderEngine's assembled SQL for the
`OEEOperational` metric — over a DIFFERENT table (operation) and a DIFFERENT
computation shape (a ratio of two SUMs) than the on-time-delivery showcase.

The metric is the semantic layer's dialect-agnostic computation template:
    SUM({act_run_hrs}) / NULLIF(SUM({run_hrs}), 0)
with act_run_hrs -> operation.act_run_hrs and run_hrs -> operation.run_hrs. The
mapping exposes the per-operation hours; the SPARQL query sums and divides them.

Read-only by design (same guarantees as parity_check.py): the live WAL-mode DB is
opened only to take a backup snapshot; Ontop and SolderEngine both read the same
snapshot, so the two engines provably see identical data; nothing ever writes the
live file and ArangoDB is never touched.

Not wired into scripts/post-merge.sh (it needs the Java + Ontop toolchain, like
parity_check.py). Run it directly:

    python3 poc/ontop-ontology-poc/oee_parity_check.py

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
ONTOLOGY = os.path.join(POC_DIR, "ontology", "operational_efficiency.ttl")
MAPPING = os.path.join(POC_DIR, "mapping", "operational_efficiency.obda")
QUERIES = pc.QUERIES
RESULTS = pc.RESULTS
LIVE_DB = pc.LIVE_DB
HF_DIR = pc.HF_DIR

METRIC = "OEEOperational"
TOLERANCE = 1e-9


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


def solder_value(snap):
    sys.path.insert(0, HF_DIR)
    from solder_engine import SolderEngine

    engine = SolderEngine(db_path=snap)
    result = engine.assemble_metric_sql(METRIC, "sqlite")
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        value = conn.execute(result.sql).fetchone()[0]
    finally:
        conn.close()
    return value, result.sql


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
    oee_csv = run_sparql(props, os.path.join(QUERIES, "oee_operational.rq"),
                         os.path.join(RESULTS, "oee_operational.csv"))
    sparql_oee = pc.read_first_column(oee_csv)

    print("Running SolderEngine assembled SQL...")
    solder_oee, solder_sql = solder_value(snap)

    print()
    print("=" * 68)
    print("  OPERATIONAL OEE (RUN-HOURS EFFICIENCY) \u2014 PARITY CHECK")
    print("=" * 68)
    print(f"  SolderEngine assembled SQL : {solder_oee!r}")
    print(f"  SPARQL  (virtual graph)    : {sparql_oee!r}")
    print("-" * 68)
    print("  SolderEngine SQL:")
    for line in solder_sql.splitlines():
        print("    " + line)
    print("=" * 68)

    ok = abs(sparql_oee - solder_oee) <= TOLERANCE
    if ok:
        print("  RESULT: PARITY CONFIRMED \u2014 the virtual SPARQL graph and the")
        print("          governed SQL semantic layer return the same OEE.")
    else:
        print("  RESULT: MISMATCH")
        print(f"    OEE differs by {abs(sparql_oee - solder_oee)}")
    print("=" * 68)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
