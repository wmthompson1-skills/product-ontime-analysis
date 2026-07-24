"""One-command database bootstrap for a fresh clone.

manufacturing.db is intentionally gitignored, so a fresh clone (or a fresh
Replit / Hugging Face copy) has no database. This script rebuilds the whole
thing deterministically:

    cd hf-space-inventory-sqlgen && python scripts/bootstrap_db.py

WARNING — second run on an existing DB is NOT safe:
    Running this script a second time on a fully-bootstrapped DB can silently
    corrupt PO references.  Here is why:

      1. seed_erp_synthetic.py uses INSERT OR IGNORE, so rows that were
         pruned in the first run are re-inserted, inflating the PO count back
         to the seeded total (~202 POs).
      2. prune_erp_to_demo_scale.py sees the inflated count, runs the trim
         again, and may select a DIFFERENT set of 15 survivors — POs that
         complete_three_way_match.py needs (e.g. PO-000003) can disappear.
      3. complete_three_way_match.py then fails (or warns/skips) because its
         hard-coded FULL_RECEIPT_POS / PARTIAL_RECEIPTS lists reference POs
         that no longer exist.

    If you need to recover from a partial failure:
        rm hf-space-inventory-sqlgen/app_schema/manufacturing.db
        cd hf-space-inventory-sqlgen && python scripts/bootstrap_db.py

    Each individual migration IS idempotent and safe to re-run in isolation
    once the DB is at the correct chain state for that step.

Partial bootstrap: pass --stop-after <migration_name> (path as listed in
STEPS or just the basename) to stop the chain right after that step
completes. Useful for exercising deliberate mid-chain states, e.g. the
pre-collection AR aging population (stop after ship_august_first_bucket.py,
i.e. just before collect_june2026_ar.py). The final MRP readiness check is
skipped; unknown names fail closed before any step runs.

Steps (all idempotent — safe to re-run on an existing DB):
  1. Apply app_schema/schema_sqlite.sql if the DB is missing (core ERP +
     semantic-layer tables and reference seeds).
  2. Run the structural migrations that own tables/columns NOT in
     schema_sqlite.sql (wave-4 traceability spine, employees/buyers, ...).
  3. Seed synthetic ERP data (scripts/seed_erp_synthetic.py).
  4. Run the documented high-fidelity backfill chain in its required order
     (see the seed_erp_synthetic.py docstring).
  5. Build the MRP demand/supply foundation.
  6. Prune to demo scale (15 CO / 15 WO / 15-20 PO band).
  7. Split receiving into header + line and broaden the commodity mix.
  8. Link work orders to their customer-order demand lines (>= 50%), set
     safety stock (= 1), and seed the forecast demand source.
  9. Verify the MRP tab has planning parts and can compute a grid —
     fail closed if not.

Everything is plain SQLite — no ArangoDB, no API keys, no network needed.
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import time

HF_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(HF_DIR, "app_schema", "manufacturing.db")
SCHEMA_SQL = os.path.join(HF_DIR, "app_schema", "schema_sqlite.sql")

# (relative script path, args) — order matters.
STEPS = [
    # rename first: existing DBs still carrying invoice_header get the
    # payables name before anything downstream reads or writes it
    # (no-op on a fresh DB built from the updated schema_sqlite.sql)
    ("migrations/rename_invoice_header_to_payables.py", []),
    # structural migrations first: tables the seeder writes into
    ("migrations/add_wave4_traceability_tables.py", []),
    # synthetic ERP data
    ("scripts/seed_erp_synthetic.py", []),
    # structural migrations that ground themselves in seeded data
    ("migrations/add_employees_and_buyers.py", []),
    ("migrations/add_warehouse_part_location.py", []),
    ("migrations/add_supplier_payables_wiring.py", []),
    ("migrations/add_receivables_wiring.py", []),
    ("migrations/add_customer_order_completed_date.py", []),
    # documented high-fidelity chain (order is mandatory)
    ("migrations/add_operation_type.py", []),
    ("migrations/regap_and_seed_requirements.py", []),
    ("migrations/relabel_work_order_status.py", []),
    ("migrations/backfill_operation_progress.py", []),
    ("migrations/backfill_operation_schedule.py", []),
    ("migrations/backfill_supplier_rating_and_wo_actuals.py", []),
    ("migrations/backfill_operation_actuals.py", []),
    ("migrations/backfill_labor_chain.py", []),
    # planner master data + MRP foundation
    ("migrations/backfill_pn_parts_master_and_planner.py", []),
    ("migrations/backfill_mrp_demand_supply.py", []),
    # demo scale + receiving line split / commodity mix
    ("migrations/prune_erp_to_demo_scale.py", []),
    ("migrations/add_receiving_line_and_commodities.py", []),
    # 100+ plannable parts for the MRP dropdown (runs LAST: after the prune so
    # the demo-scale trim never sees — or removes — the expansion rows)
    ("migrations/expand_mrp_part_universe.py", []),
    # demand linkage (WO -> customer_order_line, >=50%), safety stock, forecast
    # demand source (runs after the expansion so its WOs/CO lines participate)
    ("migrations/add_demand_linkage_and_forecast.py", []),
    # complete the PO <-> receiver <-> payable three-way match chain (runs
    # after the prune + receiving-line split so it sees the final PO set;
    # never touches MRP-critical POs)
    ("migrations/complete_three_way_match.py", []),
    # demand-side expansion: close three shop orders (finished goods to
    # stock), repair outside-service op keys, re-run the cost cascade, and
    # add three Open customer orders whose lines make every stocked MAKE
    # part a planning part (runs after the three-way match completion so
    # received service POs accrue onto their operations)
    ("migrations/expand_demand_and_completions.py", []),
    # last-week completions: close three more in-progress shop orders inside the
    # 7-day window ending on AS_OF (2026-01-21), receipt their finished goods to
    # stock as synthetic supply, and re-run the cost/labor cascade. Headers stay
    # at 20; AS_OF never moves. Runs after expand_demand_and_completions so it
    # sees the same closed baseline.
    ("migrations/complete_last_week_work_orders.py", []),
    # synthetic demand so planned orders visibly net against on-hand: adds open
    # customer-order demand lines (scale via LINES, headers stay at 20; AS_OF
    # never moves) for the parts whose on-hand the completions boosted, sized
    # above on-hand so the grid draws PAB down to safety stock and then plans
    # the shortfall. Runs after the completions so it sees the boosted on-hand.
    ("migrations/add_synthetic_demand_for_netting.py", []),
    # add the physical receiving.received_date column (dock-arrival date, the
    # noun the Temporal Parameter Contract's Horizon Filter binds to) and
    # backfill it = receipt_date. Runs BEFORE the uninvoiced-receipts demo so
    # its governed-view verify (which now filters on received_date) resolves the
    # column, and AFTER every earlier receiving insert so the backfill is total.
    ("migrations/add_received_date.py", []),
    # uninvoiced-receipts exception populations for the governed 3WM view
    # payables_uninvoicedreceipts (runs after the match completion so the
    # engineered exceptions are never "repaired" back into a clean ledger)
    ("migrations/add_uninvoiced_receipts_demo.py", []),
    # partial-receipt accrual exposure populations for the governed PRA view
    # payables_partialreceiptaccrual (adds engineered PO LINES — never headers —
    # partially received and under/never vouchered; runs after the uninvoiced
    # demo so both exception families coexist without repair)
    ("migrations/add_partial_receipt_accrual_demo.py", []),
    # wire the consolidated Three-Way Match Coverage spine into the Query
    # Palette (selector wiring only — no data writes; runs after the PRA demo
    # so its fail-closed verify sees all five match states in the population)
    ("migrations/add_twm_coverage_palette.py", []),
    # synthetic planned orders: 40 unreleased work orders (WO-PLN-*) on
    # non-pinned MAKE parts, most due >30 days after AS_OF; the firm-order
    # band [10,20] excludes them (planned orders scale separately). Runs
    # after the netting demo so its pinned-grid verifies never see them.
    ("migrations/add_planned_work_orders.py", []),
    # physical AR ledger: receivable / receivable_line tables + deterministic
    # invoice backfill from the final order book (runs after every migration
    # that adds or prunes customer orders so the 1:1 invoicing sees the
    # finished demand side; Open orders stay uninvoiced by design)
    ("migrations/add_receivable_tables.py", []),
    # daily synthetic July 2026 throughput: one closed WO per July day plus a
    # matching open customer order with daily demand lines (CO-JUL-01 /
    # WO-JUL-01..31). Shifts the data-derived AS_OF anchor to 2026-07-31, so
    # the two MRP re-anchor runs below recompute horizon dates against the new
    # anchor (both are idempotent recomputes; CO-JUL demand dates are exempt
    # from the demand backfill and keep their daily cadence).
    ("migrations/seed_july_throughput.py", []),
    ("migrations/backfill_mrp_demand_supply.py", []),
    ("migrations/expand_mrp_part_universe.py", []),
    ("migrations/add_demand_linkage_and_forecast.py", []),
    # fabricated angle-bracket detail part + design-level BOM usage on several
    # parents (incl. MCH-51006, scheduled Fri 2026-07-31). Design-level rows
    # only; no work-order/GL impact.
    ("migrations/seed_angle_bracket_design.py", []),
    # minimal synthetic GL ledger tables (gl_events + RM/WIP/FG inventory +
    # job cost detail) — DDL only, idempotent; posting/population is a later
    # task. job_id links structurally to work_order.wo_id.
    ("migrations/add_gl_ledger_tables.py", []),
    # deterministic GL ledger backfill: replay material_issue / labor_ticket /
    # closed work_order rows through the simple posting functions
    # (gl_posting.py) with idempotency keys; fails closed unless per-job
    # ledger detail ties to work_order actuals to the cent
    ("migrations/backfill_gl_ledger.py", []),
    # register the GL ledger tables in schema_nodes / schema_edges and run the
    # fail-closed ledger-to-source reconciliation (cent-exact; runs after the
    # GL backfill so it sees the posted ledger)
    ("migrations/add_gl_schema_registry.py", []),
    # weekly August 2026 demand (Aug 7/14/21) for the three highest
    # on-hand-value parts (P-10037 / P-10014 / P-10024) — lines on the Open
    # CO-MRP-002 header, sized to exceed on-hand so planned orders net
    # visibly; fail-closed verify keeps AS_OF and the CO band intact
    ("migrations/seed_august_weekly_demand.py", []),
    # ship the first weekly August bucket early (2026-07-23) — physical
    # stock move + CUSTOMER_SHIPMENT ledger events relieving P-10024's FG;
    # fail-closed verify keeps AS_OF, the June close, and the CO band intact
    ("migrations/ship_august_first_bucket.py", []),
    # collect June 2026 AR across three weekly installments (July 7/14/21):
    # creates receivable_payment table, 15 installment rows (5 invoices × 3),
    # 15 CASH_RECEIPT gl_events, and marks all 5 June invoices Paid.
    ("migrations/collect_june2026_ar.py", []),
    # wire the AR aging governed view (receivables_araging_20260724_000001)
    # to Receivables intent 19 (order_revenue_recognition) at query_index 3.
    ("migrations/add_ar_aging_palette_wiring.py", []),
    # wire the cash receipts governed view
    # (receivables_cashreceipts_20260724_000002) to the new ar_cash_receipts
    # intent (Receivables) at query_index 4, so NLQ questions about AR
    # payments route to governed SQL over receivable_payment
    ("migrations/add_cash_receipts_palette_wiring.py", []),
    # index every APPROVED reviewer-manifest snippet (incl. the governed
    # ledger queries) into ground_truth_table_usage so a fresh DB's Ground
    # Truth mosaic sees their table usage without waiting for an app boot
    ("migrations/add_snippet_table_usage.py", []),
    # re-declare structural FKs the frozen graph records but fresh DDL lacks
    # (declared-FK-only consumers like metric assembly fail closed without
    # them; intentionally LAST so every table in the graph already exists,
    # including receivable_payment created by collect_june2026_ar.py above)
    ("migrations/declare_structural_fks.py", []),
]

# The MRP Schedule dropdown (open in-horizon demand parts) must list at least
# this many parts after a full bootstrap (expand_mrp_part_universe.py).
MIN_MRP_PLANNING_PARTS = 100


def init_schema_if_missing():
    if os.path.exists(DB_PATH):
        print(f"[bootstrap] DB exists at {DB_PATH} — skipping schema init")
        return
    print(f"[bootstrap] creating fresh DB from schema_sqlite.sql")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        with open(SCHEMA_SQL) as f:
            conn.executescript(f.read())
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        print(f"[bootstrap] schema applied — {n} tables")
    finally:
        conn.close()


def _resolve_stop_index(stop_after):
    """Map --stop-after to the index of the LAST matching STEPS entry.

    Accepts either the relative path exactly as listed in STEPS
    ("migrations/collect_june2026_ar.py") or just the basename
    ("collect_june2026_ar.py"). Some migrations appear more than once in the
    chain (re-anchor re-runs); stopping after the last occurrence means "the
    chain up through this migration is fully complete". Fails closed on an
    unknown name.
    """
    matches = [
        i for i, (rel, _args) in enumerate(STEPS)
        if rel == stop_after or os.path.basename(rel) == stop_after
    ]
    if not matches:
        names = "\n  ".join(rel for rel, _ in STEPS)
        raise SystemExit(
            f"[bootstrap] FAIL-CLOSED: --stop-after '{stop_after}' does not "
            f"match any step. Valid steps (in chain order):\n  {names}"
        )
    return matches[-1]


def run_steps(stop_after=None):
    stop_index = None if stop_after is None else _resolve_stop_index(stop_after)
    for i, (rel, args) in enumerate(STEPS):
        path = os.path.join(HF_DIR, rel)
        if not os.path.exists(path):
            raise SystemExit(f"[bootstrap] FAIL-CLOSED: missing script {rel}")
        t0 = time.time()
        print(f"[bootstrap] → {rel}")
        r = subprocess.run([sys.executable, path, *args], cwd=HF_DIR)
        if r.returncode != 0:
            raise SystemExit(
                f"[bootstrap] FAIL-CLOSED: {rel} exited {r.returncode} — "
                "fix the error above and re-run (all steps are idempotent)."
            )
        print(f"[bootstrap]   done in {time.time() - t0:.1f}s")
        if stop_index is not None and i == stop_index:
            remaining = len(STEPS) - i - 1
            print(
                f"[bootstrap] PARTIAL — stopped after {rel} as requested "
                f"(--stop-after); {remaining} step(s) NOT run. This DB is in "
                "a deliberate mid-chain state. To complete the chain, either "
                "run the remaining migrations individually in STEPS order, "
                "or wipe the DB and re-run the full bootstrap (a full re-run "
                "on an existing DB restarts from step 1)."
            )
            return True
    return False


def verify_mrp():
    print("[bootstrap] verifying MRP readiness…")
    sys.path.insert(0, HF_DIR)
    import mrp_engine as mrp

    conn = sqlite3.connect(DB_PATH)
    try:
        mrp.validate_planning_inputs(conn)
        parts = mrp.list_planning_parts(conn)
        if not parts:
            raise SystemExit(
                "[bootstrap] FAIL-CLOSED: MRP has no planning parts "
                "(no open demand in the horizon)."
            )
        if len(parts) < MIN_MRP_PLANNING_PARTS:
            raise SystemExit(
                f"[bootstrap] FAIL-CLOSED: MRP dropdown must list at least "
                f"{MIN_MRP_PLANNING_PARTS} planning parts, got {len(parts)} "
                "(expand_mrp_part_universe.py did not take effect)."
            )
        p0 = parts[0]
        if isinstance(p0, dict):
            first = p0["part_id"]
        elif isinstance(p0, (tuple, list)):
            first = p0[0]
        else:
            first = p0
        grid = mrp.compute_mrp_grid(conn, first)
        if not grid:
            raise SystemExit(
                f"[bootstrap] FAIL-CLOSED: empty MRP grid for {first}"
            )
        print(f"[bootstrap] MRP OK — {len(parts)} planning part(s); "
              f"grid computed for {first}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="One-command database bootstrap for a fresh clone."
    )
    parser.add_argument(
        "--stop-after",
        metavar="MIGRATION_NAME",
        default=None,
        help=(
            "Stop the chain right after this step completes (path as listed "
            "in STEPS, or just the basename, e.g. ship_august_first_bucket.py). "
            "If the step appears more than once, stops after its LAST "
            "occurrence. Skips the final MRP readiness check — the DB is "
            "left in a deliberate mid-chain state. Fails closed on an "
            "unknown name."
        ),
    )
    opts = parser.parse_args()
    if opts.stop_after is not None:
        _resolve_stop_index(opts.stop_after)  # fail closed before any work

    t0 = time.time()
    init_schema_if_missing()
    stopped_early = run_steps(stop_after=opts.stop_after)
    if stopped_early:
        print(f"[bootstrap] partial bootstrap done in {time.time() - t0:.1f}s "
              "— MRP readiness check SKIPPED (mid-chain state).")
        return
    verify_mrp()
    print(f"[bootstrap] complete in {time.time() - t0:.1f}s — "
          "start the app with: PORT=5000 python app.py")


if __name__ == "__main__":
    main()
