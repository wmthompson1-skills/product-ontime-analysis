#!/usr/bin/env python3
"""
Three-way-match parity check: virtual SPARQL graph (Ontop) vs governed SQL.
================================================================================

The TENTH governed showcase. Proves that the procure-to-pay three-way match
(PO line <-> receipt line <-> invoice line) answered through the standards-based
virtual knowledge graph (Ontop rewriting SPARQL -> SQL over manufacturing.db)
matches, to floating-point tolerance, the numbers direct governed SQL produces
over the SAME read-only snapshot.

Grounding. The match is governed by THREE SME-approved views in the Ground
Truth SQL layer (perspective Payables, category delivery_performance):

  * payables_ordersreceived_20260706_000001      — POs fully received (legs 1-2)
  * payables_ordersunreceived_20260706_000002    — POs unreceived / short
  * payables_uninvoicedreceipts_20260706_000003  — receipt lines not fully
    covered by non-cancelled payable lines (the receipt <-> invoice leg 3,
    refactored from the private-repo "203 3WM Uninvoiced Receipts")

plus the SME doc set in docs/my-mrp-kb/07-three-way-match/ ("Uninvoiced
Receivers Report - Detailed"). The scalar per-leg aggregates checked here are
restated directly from those governed surfaces' base tables (po_line /
receiving_line / payable_line), and the governed views themselves are executed
on the same snapshot as cross-checks: the received/unreceived PO sets must
stay disjoint (the invariant the ground-truth gate
tests/test_procurement_views.py locks in), and the uninvoiced-receipts view
must report exactly the receipt set an independent grouped-LEFT-JOIN
restatement finds.

Invoiced-link semantics (aligned end-to-end, SPARQL <-> SQL):

  * The :invoicesReceipt LINK is defined by the receipt pointer ALONE — a
    payable line with receipt_line_id populated invoices that receipt, whether
    or not its qty column is populated. The SQL side states the same thing
    (COUNT ... WHERE receipt_line_id IS NOT NULL / LEFT JOIN ... IS NULL).
  * :invoicedQty is published only where BOTH receipt_line_id AND qty are
    non-null — a (possibly) smaller population, checked separately by row
    count AND sum against the identically-filtered SQL.

The headline "Uninvoiced Receivers" number (receipt lines nobody has billed
yet) is NOT expressed as SPARQL negation — FILTER NOT EXISTS is outside the
Ontop+SQLite shapes this POC keeps to (no GROUP BY / no OPTIONAL / no CASE
inside SUM). Instead, two scalar queries are subtracted here in Python:

    uninvoiced receivers = COUNT(all receipt lines)
                         - COUNT(payable lines linked to a receipt line)

exactly like the inventory showcase nets In/Out movement in Python. A built-in
SEMANTICS REGRESSION (in-memory fixture DB, runs before anything else) locks in
the qty-null edge case: a payable line with receipt_line_id NOT NULL and qty
NULL still counts as an invoiced link (so its receipt is NOT an uninvoiced
receiver) while contributing nothing to the invoiced-qty population.

Read-only by design (same guarantees as parity_check.py): the live WAL-mode DB
is opened only to take a backup snapshot; Ontop and the governed SQL both read
the same snapshot, so the two engines provably see identical data; nothing ever
writes the live file and ArangoDB is never touched.

Not wired into scripts/post-merge.sh (it needs the Java + Ontop toolchain, like
parity_check.py). Run it directly:

    python3 poc/ontop-ontology-poc/three_way_match_parity_check.py

Exit code 0 on parity, 1 on mismatch or error.
"""
import json
import os
import sqlite3
import subprocess
import sys

POC_DIR = os.path.dirname(os.path.abspath(__file__))
if POC_DIR not in sys.path:
    sys.path.insert(0, POC_DIR)

import parity_check as pc  # reuse snapshot / runtime-properties / CSV helpers + paths

ONTOP = pc.ONTOP
ONTOLOGY = os.path.join(POC_DIR, "ontology", "three_way_match.ttl")
MAPPING = os.path.join(POC_DIR, "mapping", "three_way_match.obda")
QUERIES = pc.QUERIES
RESULTS = pc.RESULTS
LIVE_DB = pc.LIVE_DB

QTY_TOLERANCE = 1e-6

GROUND_TRUTH_DIR = os.path.join(
    pc.REPO_ROOT, "hf-space-inventory-sqlgen", "app_schema", "ground_truth")
MANIFEST = os.path.join(GROUND_TRUTH_DIR, "reviewer_manifest.json")
RECEIVED_KEY = "payables_ordersreceived_20260706_000001"
UNRECEIVED_KEY = "payables_ordersunreceived_20260706_000002"
UNINVOICED_KEY = "payables_uninvoicedreceipts_20260706_000003"

# The governed SQL definitions — module-level constants so the semantics
# regression below exercises the EXACT SQL the parity comparison runs.
SQL_RECEIPT_LINES = (
    "SELECT COUNT(*), COALESCE(SUM(quantity_received), 0) FROM receiving_line")
# The LINK: receipt pointer alone, independent of qty (mirrors the
# map-twm-invoice-receipt-link mapping / twm_invoiced_receipt_lines.rq).
SQL_INVOICED_LINKS = (
    "SELECT COUNT(*) FROM payable_line WHERE receipt_line_id IS NOT NULL")
# The QTY population: pointer AND qty non-null (mirrors map-twm-invoiced-qty /
# twm_invoiced_qty.rq).
SQL_INVOICED_QTY = (
    "SELECT COUNT(*), COALESCE(SUM(qty), 0) FROM payable_line "
    "WHERE receipt_line_id IS NOT NULL AND qty IS NOT NULL")
SQL_PO_LINES = "SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM po_line"
# Uninvoiced Receivers stated directly (LEFT-JOIN negation) — must equal the
# COUNT(receipt lines) - COUNT(invoiced links) subtraction the SPARQL side does.
SQL_UNINVOICED = (
    "SELECT COUNT(*) FROM receiving_line rl "
    "LEFT JOIN payable_line pll ON pll.receipt_line_id = rl.receipt_line_id "
    "WHERE pll.payable_line_id IS NULL")
# Independent restatement of the governed Uninvoiced Receipts view
# (payables_uninvoicedreceipts_20260706_000003): receipt headers (SITE-1
# scope) with at least one line whose NON-cancelled payable coverage is
# absent or below the received quantity. Stated with a grouped LEFT JOIN
# instead of the view's correlated subqueries, so agreement between the two
# is a real cross-check rather than a restated tautology. A receipt-pointing
# payable line whose qty is NULL yields coverage 0 here (SUM over NULLs ->
# NULL -> COALESCE 0) and trips the view's quantity branch the same way.
SQL_UNINVOICED_RECEIPT_IDS = (
    "SELECT DISTINCT r.receipt_id FROM receiving r "
    "JOIN receiving_line rl ON rl.receipt_id = r.receipt_id "
    "JOIN purchase_order po ON po.po_id = r.po_id "
    "LEFT JOIN (SELECT pl.receipt_line_id AS rlid, SUM(ABS(pl.qty)) AS covered "
    "           FROM payable_line pl "
    "           JOIN payables pay ON pay.invoice_id = pl.invoice_id "
    "           WHERE pl.receipt_line_id IS NOT NULL "
    "             AND pay.status <> 'Cancelled' "
    "           GROUP BY pl.receipt_line_id) cov "
    "  ON cov.rlid = rl.receipt_line_id "
    "WHERE po.site_id = 'SITE-1' "
    "  AND (cov.rlid IS NULL OR rl.quantity_received > COALESCE(cov.covered, 0))")


def semantics_regression():
    """Lock in the qty-null edge case on an in-memory fixture DB, using the
    same SQL constants the live comparison runs.

    Fixture: 3 receipt lines; RL-1 invoiced with qty, RL-2 invoiced with qty
    NULL (pointer only), RL-3 not invoiced at all. Expected: 3 receipt lines,
    2 invoiced links, 1 uninvoiced receiver (RL-3 only — the qty-null link
    still invoices RL-2), and an invoiced-qty population of exactly 1 row / 5.0.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript("""
            CREATE TABLE receiving_line (
                receipt_line_id TEXT PRIMARY KEY, quantity_received REAL);
            CREATE TABLE payable_line (
                payable_line_id TEXT PRIMARY KEY, receipt_line_id TEXT, qty REAL);
            INSERT INTO receiving_line VALUES ('RL-1', 10), ('RL-2', 7), ('RL-3', 4);
            INSERT INTO payable_line VALUES
                ('PL-1', 'RL-1', 5.0),
                ('PL-2', 'RL-2', NULL),
                ('PL-3', NULL,   9.0);
        """)
        recv_lines, _ = conn.execute(SQL_RECEIPT_LINES).fetchone()
        links = conn.execute(SQL_INVOICED_LINKS).fetchone()[0]
        qty_rows, qty_sum = conn.execute(SQL_INVOICED_QTY).fetchone()
        uninvoiced = conn.execute(SQL_UNINVOICED).fetchone()[0]
    finally:
        conn.close()

    expected = (3, 2, 1, 5.0, 1)
    actual = (recv_lines, links, qty_rows, qty_sum, uninvoiced)
    if actual != expected:
        raise SystemExit(
            "SEMANTICS REGRESSION FAILED — the SQL definitions no longer honor "
            "the qty-null link semantics (a receipt-pointing payable line with "
            "NULL qty must still count as an invoiced link, and must not enter "
            f"the invoiced-qty population). expected {expected}, got {actual}")
    if recv_lines - links != uninvoiced:
        raise SystemExit(
            "SEMANTICS REGRESSION FAILED — the LEFT-JOIN negation and the "
            "COUNT(receipts) - COUNT(links) subtraction disagree on the fixture")
    print("Semantics regression (qty-null invoiced link fixture): OK")


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


def read_row(csv_path, n_cols):
    """First result row of a scalar query -> tuple of floats (len n_cols)."""
    _, rows = pc.read_result_rows(csv_path)
    if not rows or len(rows[0]) < n_cols:
        raise SystemExit(f"expected one row with {n_cols} columns in {csv_path}")
    return tuple(float(v) for v in rows[0][:n_cols])


def load_governed_view_sql(binding_key):
    with open(MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)
    entry = manifest["approved_snippets"][binding_key]
    if entry["validation_status"] != "APPROVED":
        raise SystemExit(f"{binding_key} is not APPROVED in the reviewer manifest")
    path = os.path.join(pc.REPO_ROOT, "hf-space-inventory-sqlgen", entry["file_path"])
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main():
    if not os.path.exists(ONTOP):
        raise SystemExit(
            "Ontop CLI not found. Run: python3 replit_integrations/ontop_poc_setup.py "
            "first to download the toolchain."
        )
    if not os.path.exists(LIVE_DB):
        raise SystemExit(f"DuckDB snapshot not found at {LIVE_DB}. Run: cd Utilities/SQLMesh && sqlmesh run")

    semantics_regression()

    print("Building read-only snapshot of the live database...")
    snap = pc.make_snapshot()
    props = pc.write_runtime_properties(snap)

    print("Running SPARQL through the Ontop virtual graph...")
    recv_csv = run_sparql(props, os.path.join(QUERIES, "twm_receipt_lines.rq"),
                          os.path.join(RESULTS, "twm_receipt_lines.csv"))
    links_csv = run_sparql(props, os.path.join(QUERIES, "twm_invoiced_receipt_lines.rq"),
                           os.path.join(RESULTS, "twm_invoiced_receipt_lines.csv"))
    qty_csv = run_sparql(props, os.path.join(QUERIES, "twm_invoiced_qty.rq"),
                         os.path.join(RESULTS, "twm_invoiced_qty.csv"))
    ord_csv = run_sparql(props, os.path.join(QUERIES, "twm_ordered_qty.rq"),
                         os.path.join(RESULTS, "twm_ordered_qty.csv"))

    recv_cnt_f, recv_qty = read_row(recv_csv, 2)
    sparql_recv_lines = int(recv_cnt_f)
    sparql_inv_links = int(read_row(links_csv, 1)[0])
    sparql_inv_qty, qty_rows_f = read_row(qty_csv, 2)
    sparql_qty_rows = int(qty_rows_f)
    po_cnt_f, sparql_ord_qty = read_row(ord_csv, 2)
    sparql_po_lines = int(po_cnt_f)
    sparql_uninvoiced = sparql_recv_lines - sparql_inv_links

    print("Running the governed SQL aggregates on the same snapshot...")
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        sql_recv_lines, sql_recv_qty = conn.execute(SQL_RECEIPT_LINES).fetchone()
        sql_inv_links = conn.execute(SQL_INVOICED_LINKS).fetchone()[0]
        sql_qty_rows, sql_inv_qty = conn.execute(SQL_INVOICED_QTY).fetchone()
        sql_po_lines, sql_ord_qty = conn.execute(SQL_PO_LINES).fetchone()
        sql_uninvoiced = conn.execute(SQL_UNINVOICED).fetchone()[0]

        # Cross-check: the two governed views on the same snapshot stay disjoint.
        received_pos = {r[0] for r in conn.execute(load_governed_view_sql(RECEIVED_KEY))}
        unreceived_pos = {r[0] for r in conn.execute(load_governed_view_sql(UNRECEIVED_KEY))}
        view_overlap = received_pos & unreceived_pos

        # Cross-check: the governed Uninvoiced Receipts view must report
        # exactly the receipts the independent grouped-LEFT-JOIN restatement
        # finds on the same snapshot (receipt_id is column 2 of the view).
        uninvoiced_view_receipts = {
            r[1] for r in conn.execute(load_governed_view_sql(UNINVOICED_KEY))}
        uninvoiced_restated_receipts = {
            r[0] for r in conn.execute(SQL_UNINVOICED_RECEIPT_IDS)}
    finally:
        conn.close()

    print()
    print("=" * 68)
    print("  THREE-WAY MATCH \u2014 PARITY CHECK (SPARQL vs governed SQL)")
    print("=" * 68)
    print(f"  Uninvoiced receivers    (SQL / SPARQL): {sql_uninvoiced!r} / {sparql_uninvoiced!r}")
    print(f"    receipt lines  (SQL / SPARQL): {sql_recv_lines!r} / {sparql_recv_lines!r}  "
          f"[qty {sql_recv_qty!r} / {recv_qty!r}]")
    print(f"    invoiced links (SQL / SPARQL): {sql_inv_links!r} / {sparql_inv_links!r}")
    print(f"    invoiced qty   (SQL / SPARQL): {sql_inv_qty!r} / {sparql_inv_qty!r}  "
          f"[{sql_qty_rows} / {sparql_qty_rows} qty rows]")
    print(f"  PO lines / ordered qty  (SQL / SPARQL): {sql_po_lines!r} / {sparql_po_lines!r}  "
          f"[qty {sql_ord_qty!r} / {sparql_ord_qty!r}]")
    print("-" * 68)
    print(f"    governed views on the same snapshot: {len(received_pos)} POs fully "
          f"received, {len(unreceived_pos)} unreceived/short, overlap {len(view_overlap)}")
    print(f"    uninvoiced receipts (governed view / restated SQL): "
          f"{len(uninvoiced_view_receipts)} / {len(uninvoiced_restated_receipts)}")
    print("=" * 68)

    recv_ok = (sparql_recv_lines == sql_recv_lines
               and abs(recv_qty - sql_recv_qty) <= QTY_TOLERANCE)
    links_ok = (sparql_inv_links == sql_inv_links)
    qty_ok = (sparql_qty_rows == sql_qty_rows
              and abs(sparql_inv_qty - sql_inv_qty) <= QTY_TOLERANCE)
    ord_ok = (sparql_po_lines == sql_po_lines
              and abs(sparql_ord_qty - sql_ord_qty) <= QTY_TOLERANCE)
    uninvoiced_ok = (sparql_uninvoiced == sql_uninvoiced)
    views_ok = (not view_overlap and received_pos and unreceived_pos)
    uninvoiced_view_ok = (
        uninvoiced_view_receipts == uninvoiced_restated_receipts
        and bool(uninvoiced_view_receipts))

    ok = (recv_ok and links_ok and qty_ok and ord_ok and uninvoiced_ok
          and views_ok and uninvoiced_view_ok)
    if ok:
        print("  RESULT: PARITY CONFIRMED \u2014 the virtual SPARQL graph and the")
        print("          governed SQL return the same per-leg populations and")
        print("          quantities, the same Uninvoiced Receivers count, and the")
        print("          governed received/unreceived views stay disjoint.")
    else:
        print("  RESULT: MISMATCH")
        if not recv_ok:
            print("    receipt-line population/qty differs")
        if not links_ok:
            print(f"    invoiced-link count differs: SQL {sql_inv_links!r} "
                  f"vs SPARQL {sparql_inv_links!r}")
        if not qty_ok:
            print("    invoiced-qty population/sum differs")
        if not ord_ok:
            print("    PO-line population/qty differs")
        if not uninvoiced_ok:
            print(f"    uninvoiced receivers differ: SQL {sql_uninvoiced!r} "
                  f"vs SPARQL {sparql_uninvoiced!r}")
        if not views_ok:
            print(f"    governed view cross-check failed (overlap {sorted(view_overlap)!r}, "
                  f"received {len(received_pos)}, unreceived {len(unreceived_pos)})")
        if not uninvoiced_view_ok:
            print("    uninvoiced-receipts view cross-check failed: view "
                  f"{sorted(uninvoiced_view_receipts)!r} vs restated "
                  f"{sorted(uninvoiced_restated_receipts)!r}")
    print("=" * 68)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
