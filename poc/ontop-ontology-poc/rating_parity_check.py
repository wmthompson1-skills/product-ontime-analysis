#!/usr/bin/env python3
"""
Full supplier-rating parity proof + SQLGlot lift of the SQLite OPTIONAL/GROUP BY limit.
=======================================================================================

Proves two things at once, read-only, against a throwaway snapshot of the live
manufacturing database.

  1. THE FULL "MY MRP" SUPPLIER RATING, RECOMPUTED THROUGH THE VIRTUAL GRAPH.
     The deterministic supplier performance rating
         rating = clamp(5 * (0.55*OTD + 0.45*quality), 1, 5), rounded to 2dp
     (quality = (Passed+Waived)/(Passed+Waived+Failed), neutral 0.75 when there
     are no graded receipts; a supplier with no receipts at all = neutral 3.0) is
     reassembled ENTIRELY from triples Ontop publishes over the SQL layer:
         AVG(:opsOnTimeScore) per supplier  -> OTD
         AVG(:qualityScore)   per supplier  -> quality
         COUNT(:hasDelivery)  per supplier  -> receipt count
     and the result is compared, per supplier, to the migration's stored
     suppliers.performance_rating. Exit non-zero on any mismatch.

  2. THE SQLite OPTIONAL + GROUP BY LIMIT IS LIFTED.
     The OTD and quality queries are multi-triple OPTIONALs combined with
     GROUP BY + AVG. Ontop serializes those as a nested LEFT JOIN whose SQL uses
     stacked ON clauses (A LEFT JOIN B JOIN C ON inner ON outer) that the SQLite
     parser rejects ("near ON"). This check captures Ontop's generated SQL from
     its DEBUG log, proves SQLite rejects it raw, then re-transpiles it with
     SQLGlot (sql_lift.lift_join_groups parenthesizes the nested join group) and
     runs the lifted SQL successfully on the same snapshot.

Read-only by design (same guarantees as parity_check.py): the live WAL-mode DB is
opened only to take a backup snapshot; Ontop and this checker both read the
snapshot; nothing ever writes the live file and ArangoDB is never touched.

Not wired into scripts/post-merge.sh (it needs the Java + Ontop toolchain, like
parity_check.py). Run it directly:

    python3 poc/ontop-ontology-poc/rating_parity_check.py

Exit code 0 on full parity + a demonstrated lift, 1 on mismatch / lift failure / error.
"""
import os
import sqlite3
import subprocess
import sys

POC_DIR = os.path.dirname(os.path.abspath(__file__))
if POC_DIR not in sys.path:
    sys.path.insert(0, POC_DIR)

import parity_check as pc  # reuse snapshot / runtime-properties helpers + paths
import sql_lift

ONTOP = pc.ONTOP
ONTOLOGY = pc.ONTOLOGY
MAPPING = pc.MAPPING
QUERIES = pc.QUERIES
RESULTS = pc.RESULTS
LIVE_DB = pc.LIVE_DB

# My MRP rating constants — these MIRROR
# hf-space-inventory-sqlgen/migrations/backfill_supplier_rating_and_wo_actuals.py
W_OTD = 0.55
W_QUALITY = 0.45
NEUTRAL_QUALITY = 0.75   # supplier with no graded (Passed/Failed/Waived) receipts
NO_HISTORY_RATING = 3.0  # supplier with no receipts at all -> neutral, not penalized
# Both sides round to 2 decimals, so they should agree to well within this.
RATING_TOLERANCE = 1e-6

OTD_QUERY = "supplier_otd_avg.rq"
QUALITY_QUERY = "supplier_quality_avg.rq"
RECS_QUERY = "supplier_delivery_count.rq"


def clamp(value, low, high):
    return max(low, min(high, value))


def run_ontop_capture(props, query_file):
    """Run an Ontop SPARQL query with DEBUG logging on and return the full log
    (stdout + stderr). Ontop logs the native SQL it will execute at DEBUG even
    when it then fails to run it (the multi-triple OPTIONAL + GROUP BY case on
    SQLite), so one invocation both *captures* the SQL and *demonstrates* the raw
    failure."""
    os.makedirs(RESULTS, exist_ok=True)
    out_csv = os.path.join(RESULTS, "._rating_capture.csv")
    env = dict(os.environ, ONTOP_LOG_LEVEL="DEBUG")
    cmd = [
        ONTOP, "query",
        "-m", MAPPING,
        "-t", ONTOLOGY,
        "-p", props,
        "-q", os.path.join(QUERIES, query_file),
        "-o", out_csv,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=POC_DIR, env=env)
    return res.stdout + "\n" + res.stderr


def _by_supplier(rows, supplier_ids):
    """Reduce a 2-column aggregate result to {supplier_id: value}. The supplier
    column is identified as the one whose values are ALL known supplier ids
    (robust to Ontop's internal column aliasing); the other column is the value."""
    if not rows:
        return {}
    ncols = len(rows[0])
    sid_col = None
    for c in range(ncols):
        if all(r[c] is not None and str(r[c]) in supplier_ids for r in rows):
            sid_col = c
            break
    if sid_col is None:
        raise SystemExit(
            "rating_parity_check: could not identify the supplier-id column in "
            "the lifted aggregate result"
        )
    val_col = next(c for c in range(ncols) if c != sid_col)
    return {str(r[sid_col]): r[val_col] for r in rows}


def supplier_aggregate(props, snap, query_file, supplier_ids):
    """Capture Ontop's native SQL for an aggregate query, record whether SQLite
    rejects it raw, lift it with SQLGlot, run the lifted SQL on the read-only
    snapshot, and return ({supplier_id: value}, report)."""
    log = run_ontop_capture(props, query_file)
    try:
        raw_sql = sql_lift.extract_native_sql(log)
    except ValueError as exc:
        raise SystemExit(
            f"rating_parity_check: could not capture Ontop's native SQL for "
            f"{query_file} ({exc}). Is the toolchain present and DEBUG logging on?"
        )
    lifted_sql, wrapped = sql_lift.lift_join_groups(raw_sql)

    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        raw_error = None  # None means SQLite accepted the raw SQL as-is
        try:
            conn.execute(raw_sql).fetchall()
        except sqlite3.Error as exc:
            raw_error = str(exc)
        rows = conn.execute(lifted_sql).fetchall()
    finally:
        conn.close()

    values = _by_supplier(rows, supplier_ids)
    report = {
        "query": query_file,
        "raw_sql": raw_sql,
        "lifted_sql": lifted_sql,
        "wrapped": wrapped,
        "raw_error": raw_error,
        "log_flagged": sql_lift.raw_sql_rejected_by_sqlite(log),
        "rows": len(rows),
    }
    return values, report


def stored_ratings(snap):
    """Ground truth: the migration's stored rating per supplier, plus the set of
    supplier ids (TEXT, to line up with the SPARQL/SQL keys)."""
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT CAST(supplier_id AS TEXT), performance_rating FROM suppliers"
        ).fetchall()
    finally:
        conn.close()
    stored = {str(sid): rating for sid, rating in rows}
    return stored, set(stored.keys())


def compute_rating(otd, quality, recs):
    """The exact My MRP formula from the backfill migration."""
    if not recs:  # no receipts at all -> neutral, no track record to score
        return NO_HISTORY_RATING
    otd = otd if otd is not None else 0.0
    quality = quality if quality is not None else NEUTRAL_QUALITY
    return round(clamp(5.0 * (W_OTD * otd + W_QUALITY * quality), 1.0, 5.0), 2)


def print_lift_demonstration(report):
    print()
    print("=" * 68)
    print("  SQLGlot LIFT \u2014 Ontop OPTIONAL + GROUP BY on SQLite")
    print("=" * 68)
    print(f"  Query: {report['query']}")
    if report["raw_error"]:
        print(f"  Raw Ontop SQL rejected by SQLite : YES ({report['raw_error']})")
    else:
        print("  Raw Ontop SQL rejected by SQLite : NO (SQLite accepted it as-is)")
    print(f"  Nested join group(s) parenthesized by SQLGlot : {report['wrapped']}")
    print(f"  Lifted SQL ran on the snapshot   : YES ({report['rows']} rows)")
    print("=" * 68)


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
    stored, supplier_ids = stored_ratings(snap)

    print("Capturing + lifting Ontop SQL for the per-supplier aggregates...")
    otd_vals, otd_rep = supplier_aggregate(props, snap, OTD_QUERY, supplier_ids)
    qual_vals, qual_rep = supplier_aggregate(props, snap, QUALITY_QUERY, supplier_ids)
    recs_vals, recs_rep = supplier_aggregate(props, snap, RECS_QUERY, supplier_ids)

    # ── Proof 2: the lift ──────────────────────────────────────────────────────
    # The lift is genuinely needed only when a multi-triple OPTIONAL spans MORE
    # THAN ONE physical table: OTD draws :hasDelivery from `receiving` and
    # :opsOnTimeScore from `receiving JOIN purchase_order`, so Ontop emits a
    # nested LEFT JOIN with stacked ON clauses that SQLite rejects. The quality
    # OPTIONAL is also multi-triple, but BOTH inputs resolve to `receiving`, so
    # Ontop emits SQLite-compatible SQL and the lift is a safe no-op. recs is a
    # single-triple OPTIONAL. Running all three through the same pipeline shows
    # the lift is needed exactly when it should be and is harmless otherwise.
    for rep in (otd_rep, qual_rep, recs_rep):
        print_lift_demonstration(rep)
    needed_lift = [r["query"] for r in (otd_rep, qual_rep, recs_rep) if r["wrapped"] >= 1]
    # Proof requires the lift to be ACTUALLY exercised: the OTD query must fail
    # raw on SQLite and run only after SQLGlot parenthesizes its nested join.
    lift_ok = (
        otd_rep["raw_error"] is not None
        and otd_rep["wrapped"] >= 1
        and otd_rep["rows"] > 0
    )
    print()
    print(f"  Queries that required the SQLGlot lift: "
          f"{', '.join(needed_lift) if needed_lift else 'none'}")

    # ── Proof 1: full rating parity, per supplier ──────────────────────────────
    print()
    print("=" * 68)
    print("  FULL SUPPLIER RATING \u2014 GRAPH-RECOMPUTED vs STORED (My MRP)")
    print("=" * 68)
    print(f"  {'supplier':<11}{'OTD':>7}{'quality':>9}{'recs':>6}"
          f"{'graph':>8}{'stored':>8}  ok")
    print("  " + "-" * 64)
    mismatches = []
    for sid in sorted(supplier_ids):
        recs = recs_vals.get(sid) or 0
        otd = otd_vals.get(sid)
        quality = qual_vals.get(sid)
        graph_rating = compute_rating(otd, quality, recs)
        want = stored.get(sid)
        ok = want is not None and abs(graph_rating - want) <= RATING_TOLERANCE
        if not ok:
            mismatches.append((sid, graph_rating, want))
        otd_s = f"{otd:.3f}" if otd is not None else "-"
        q_s = f"{quality:.3f}" if quality is not None else "-"
        want_s = f"{want:.2f}" if want is not None else "-"
        print(f"  {sid:<11}{otd_s:>7}{q_s:>9}{int(recs):>6}"
              f"{graph_rating:>8.2f}{want_s:>8}  {'ok' if ok else 'XX'}")
    print("  " + "-" * 64)
    print(f"  suppliers checked: {len(supplier_ids)}   mismatches: {len(mismatches)}")
    print("=" * 68)

    rating_ok = not mismatches
    if rating_ok and lift_ok:
        print("  RESULT: RATING PARITY CONFIRMED + SQLite OPTIONAL/GROUP BY LIFTED \u2014")
        print("          every supplier's My MRP rating recomputed through the virtual")
        print("          graph equals the migration's stored performance_rating, and")
        print("          Ontop's SQLite-incompatible OTD aggregate SQL ran only after")
        print("          SQLGlot re-transpiled its nested join group.")
    else:
        print("  RESULT: FAILED")
        for sid, got, want in mismatches:
            print(f"    {sid}: graph {got} != stored {want}")
        if not lift_ok:
            print("    the SQLGlot lift was not exercised: the OTD query's raw SQL did "
                  "not fail on SQLite, or no nested join group was parenthesized")
    print("=" * 68)

    return 0 if (rating_ok and lift_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
