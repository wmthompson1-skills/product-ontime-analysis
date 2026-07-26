"""Prune the synthetic ERP to demo scale — deterministic, fail-closed.

Targets (user-requested):
    - customer_order   : 10-20 rows (from 60)
    - work_order       : 10-20 rows (from 120)
    - purchase_order   : 10-25 rows (from 202; band top is 25 because
                         add_receiving_line_and_commodities adds 5 POs on top
                         of the seeded 15, plus a possible MRP top-up PO)
    - CNC machines     : 4-6 (consolidate the legacy routing cells into the
                         canonical MC-xxx machine list -> exactly 5 CNC machines)

Selection rules (all deterministic, no randomness):
    Customer orders (15): every Open order (MRP demand must survive), then the
        most recent Shipped (2), Closed (2) and Cancelled (1) orders by
        (order_date DESC, order_id DESC) for status variety.
    Work orders (15): open work orders (unreleased/firmed/released) whose part
        is on a kept Open customer-order line (supply basis for MRP), ordered
        by (required_date, wo_id), capped at 9; padded to 15 with the most
        recently closed work orders (close_date DESC, wo_id DESC) so the
        data-derived AS_OF anchor (MAX(close_date)) is always retained.
    Purchase orders (15): only POs whose wo_id is NULL or points at a kept
        work order; open/partial POs supplying a kept demand part first
        (po_date DESC, po_id DESC), then padded by recency.

CNC consolidation (legacy cell -> canonical machine):
    CNC-MILL-1 -> MC-001   (both are the Haas VF-4)
    CNC-MILL-2 -> MC-002   (both are the Mazak Integrex)
    LATHE-1    -> MC-005   (large CNC lathe -> Haas SL-40)
    DRILL-PRESS and INSPECT-CMM stay: they are manual workstations, not CNC.
    Rewired in: operation, labor_ticket, operation_type, EMPLOYEE.home_resource_id.

Cascades removed with their parents:
    CO: customer_order_line
    WO: operation, labor_ticket, material_issue, requirement (WORK_ORDER),
        trace (lot LOT-<wo_id>), inventory_transaction (wo_id)
    PO: po_line, receiving, payables, payable_line,
        inventory_transaction (po_id)
    plus trace_inventory_trace / inv_trans_dist rows referencing deleted
    trace or inventory_transaction rows.

Fail-closed postconditions: CO/WO counts in [10, 20], PO count in [10, 25];
zero orphans in every child
table; AS_OF still derivable; zero references to retired resource ids;
exactly 5 CNC machines; MRP planning inputs still validate.

Idempotent: re-running keeps the same survivors and deletes nothing further.
Run scripts/../migrations/add_employees_and_buyers.py afterwards to re-ground
buyer ownership on the surviving PO history.
"""
from __future__ import annotations

import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_HERE)
DB_PATH = os.path.join(_APP_DIR, "app_schema", "manufacturing.db")

sys.path.insert(0, _APP_DIR)

CO_TARGET = 15
WO_TARGET = 15
PO_TARGET = 15
SUPPLY_WO_CAP = 9

RESOURCE_REMAP = {
    "CNC-MILL-1": "MC-001",
    "CNC-MILL-2": "MC-002",
    "LATHE-1": "MC-005",
}
CNC_MACHINES = ["MC-001", "MC-002", "MC-003", "MC-004", "MC-005"]


def _ids(cur, sql, params=()):
    return [r[0] for r in cur.execute(sql, params).fetchall()]


def pick_customer_orders(cur) -> list[str]:
    # CO-JUL-% is a protected synthetic series (seed_july_throughput.py):
    # excluded from the demo-scale keep-set selection so its presence never
    # reshuffles which legacy orders survive; preserved separately in prune().
    keep = _ids(cur, "SELECT order_id FROM customer_order WHERE status = 'Open' "
                     "AND order_id NOT LIKE 'CO-JUL-%' "
                     "ORDER BY order_date DESC, order_id DESC")
    for status, n in (("Shipped", 2), ("Closed", 2), ("Cancelled", 1)):
        keep += _ids(
            cur,
            "SELECT order_id FROM customer_order WHERE status = ? "
            "ORDER BY order_date DESC, order_id DESC LIMIT ?",
            (status, n),
        )
    return keep[:CO_TARGET] if len(keep) > CO_TARGET else keep


def pick_work_orders(cur, kept_cos: list[str]) -> list[str]:
    ph = ",".join("?" * len(kept_cos))
    demand_parts = set(_ids(
        cur,
        f"SELECT DISTINCT l.part_id FROM customer_order_line l "
        f"JOIN customer_order o ON o.order_id = l.order_id "
        f"WHERE l.order_id IN ({ph}) AND o.status = 'Open'",
        kept_cos,
    ))
    supply = []
    if demand_parts:
        dph = ",".join("?" * len(demand_parts))
        supply = _ids(
            cur,
            f"SELECT wo_id FROM work_order "
            f"WHERE status IN ('unreleased','firmed','released') "
            f"AND part_id IN ({dph}) "
            f"ORDER BY required_date, wo_id LIMIT ?",
            list(demand_parts) + [SUPPLY_WO_CAP],
        )
    keep = list(supply)
    pad = WO_TARGET - len(keep)
    if pad > 0:
        kph = ",".join("?" * len(keep)) if keep else "''"
        keep += _ids(
            cur,
            f"SELECT wo_id FROM work_order "
            f"WHERE wo_id NOT IN ({kph}) AND status = 'closed' "
            f"AND wo_id NOT LIKE 'WO-JUL-%' "
            f"ORDER BY close_date DESC, wo_id DESC LIMIT ?",
            keep + [pad],
        )
    return keep


def pick_purchase_orders(cur, kept_wos: list[str], demand_parts: set[str]) -> list[str]:
    wph = ",".join("?" * len(kept_wos))
    candidates = f"(wo_id IS NULL OR wo_id IN ({wph}))"
    keep: list[str] = []
    if demand_parts:
        dph = ",".join("?" * len(demand_parts))
        keep = _ids(
            cur,
            f"SELECT DISTINCT po.po_id FROM purchase_order po "
            f"JOIN po_line l ON l.po_id = po.po_id "
            f"WHERE {candidates} AND po.status IN ('Open','Partial') "
            f"AND l.part_id IN ({dph}) "
            f"ORDER BY po.po_id DESC LIMIT ?",
            kept_wos + list(demand_parts) + [PO_TARGET],
        )
    pad = PO_TARGET - len(keep)
    if pad > 0:
        kph = ",".join("?" * len(keep)) if keep else "''"
        keep += _ids(
            cur,
            f"SELECT po_id FROM purchase_order "
            f"WHERE {candidates} AND po_id NOT IN ({kph}) "
            f"ORDER BY po_date DESC, po_id DESC LIMIT ?",
            kept_wos + keep + [pad],
        )
    return keep


def consolidate_cnc(cur) -> None:
    for old, new in RESOURCE_REMAP.items():
        for table, col in (
            ("operation", "resource_id"),
            ("labor_ticket", "resource_id"),
            ("operation_type", "resource_id"),
            ("EMPLOYEE", "home_resource_id"),
        ):
            cur.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (new, old))
        cur.execute("DELETE FROM shop_resource WHERE resource_id = ?", (old,))
    print(f"Consolidated legacy CNC cells into {sorted(set(RESOURCE_REMAP.values()))}")


def prune(cur) -> None:
    kept_cos = pick_customer_orders(cur)
    # Preserve the protected July daily-throughput series (may be absent when
    # the prune runs before seed_july_throughput.py in a fresh chain).
    kept_cos += _ids(cur, "SELECT order_id FROM customer_order "
                          "WHERE order_id LIKE 'CO-JUL-%'")
    ph_co = ",".join("?" * len(kept_cos))
    demand_parts = set(_ids(
        cur,
        f"SELECT DISTINCT l.part_id FROM customer_order_line l "
        f"JOIN customer_order o ON o.order_id = l.order_id "
        f"WHERE l.order_id IN ({ph_co}) AND o.status = 'Open'",
        kept_cos,
    ))
    kept_wos = pick_work_orders(cur, kept_cos)
    kept_wos += _ids(cur, "SELECT wo_id FROM work_order "
                          "WHERE wo_id LIKE 'WO-JUL-%'")
    # Pin the three shop orders that expand_demand_and_completions.py closes.
    # They must survive the prune or the downstream migration fails closed.
    for _pin in ("WO-00007", "WO-00015", "WO-00009"):
        if _pin not in kept_wos:
            kept_wos += _ids(cur, "SELECT wo_id FROM work_order WHERE wo_id = ?", (_pin,))
    ph_wo = ",".join("?" * len(kept_wos))
    kept_pos = pick_purchase_orders(cur, kept_wos, demand_parts)
    ph_po = ",".join("?" * len(kept_pos))

    if not (kept_cos and kept_wos and kept_pos):
        raise RuntimeError("Keep-set selection produced an empty set — aborting")

    # --- customer order cascade -------------------------------------------
    cur.execute(f"DELETE FROM customer_order_line WHERE order_id NOT IN ({ph_co})", kept_cos)
    cur.execute(f"DELETE FROM customer_order WHERE order_id NOT IN ({ph_co})", kept_cos)

    # --- work order cascade ------------------------------------------------
    for table in ("operation", "labor_ticket", "material_issue"):
        cur.execute(f"DELETE FROM {table} WHERE wo_id NOT IN ({ph_wo})", kept_wos)
    cur.execute(
        f"DELETE FROM requirement WHERE component_type = 'WORK_ORDER' "
        f"AND component_id NOT IN ({ph_wo})",
        kept_wos,
    )
    cur.execute(
        f"DELETE FROM trace WHERE REPLACE(lot_id, 'LOT-', '') IN "
        f"(SELECT wo_id FROM work_order WHERE wo_id NOT IN ({ph_wo}))",
        kept_wos,
    )
    cur.execute(
        f"DELETE FROM inventory_transaction WHERE wo_id IS NOT NULL "
        f"AND wo_id NOT IN ({ph_wo})",
        kept_wos,
    )
    cur.execute(f"DELETE FROM work_order WHERE wo_id NOT IN ({ph_wo})", kept_wos)

    # --- purchase order cascade ---------------------------------------------
    for table in ("po_line", "receiving", "payables", "payable_line"):
        cur.execute(f"DELETE FROM {table} WHERE po_id NOT IN ({ph_po})", kept_pos)
    # receiving_line exists only after add_receiving_line_and_commodities ran;
    # cascade it when present so its rows never outlive their receiving header.
    if cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receiving_line'"
    ).fetchone():
        cur.execute(
            "DELETE FROM receiving_line "
            "WHERE receipt_id NOT IN (SELECT receipt_id FROM receiving)"
        )
    cur.execute(
        "DELETE FROM certification WHERE receipt_id IS NOT NULL "
        "AND receipt_id NOT IN (SELECT receipt_id FROM receiving)"
    )
    cur.execute(
        f"DELETE FROM inventory_transaction WHERE po_id IS NOT NULL "
        f"AND po_id NOT IN ({ph_po})",
        kept_pos,
    )
    cur.execute(f"DELETE FROM purchase_order WHERE po_id NOT IN ({ph_po})", kept_pos)

    # --- linker cleanup -------------------------------------------------------
    cur.execute(
        "DELETE FROM trace_inventory_trace WHERE trace_id NOT IN (SELECT trace_id FROM trace) "
        "OR transaction_id NOT IN (SELECT transaction_id FROM inventory_transaction)"
    )
    cur.execute(
        "DELETE FROM inv_trans_dist WHERE "
        "in_trans_id NOT IN (SELECT transaction_id FROM inventory_transaction) "
        "OR out_trans_id NOT IN (SELECT transaction_id FROM inventory_transaction)"
    )
    print(f"Kept: {len(kept_cos)} customer orders, {len(kept_wos)} work orders, "
          f"{len(kept_pos)} purchase orders")


def validate(cur) -> None:
    failures = []

    # PO band top is 25: add_receiving_line_and_commodities adds 5 POs on top
    # of the seeded 15 (+ a possible deterministic MRP top-up PO). The
    # protected July series (CO-JUL-% / WO-JUL-%) is outside the bands by
    # design and excluded from the count.
    for table, pk, excl, lo, hi in (
        ("customer_order", "order_id", "CO-JUL-%", 10, 20),
        ("work_order", "wo_id", "WO-JUL-%", 10, 20),
        ("purchase_order", "po_id", None, 10, 25),
    ):
        where = f" WHERE {pk} NOT LIKE '{excl}'" if excl else ""
        n = cur.execute(f"SELECT COUNT(*) FROM {table}{where}").fetchone()[0]
        if not (lo <= n <= hi):
            failures.append(f"{table} count {n} outside [{lo}, {hi}]")

    orphan_checks = [
        ("customer_order_line", "order_id NOT IN (SELECT order_id FROM customer_order)"),
        ("operation", "wo_id NOT IN (SELECT wo_id FROM work_order)"),
        ("labor_ticket", "wo_id NOT IN (SELECT wo_id FROM work_order)"),
        ("material_issue", "wo_id NOT IN (SELECT wo_id FROM work_order)"),
        ("requirement", "component_type = 'WORK_ORDER' AND component_id NOT IN "
                        "(SELECT wo_id FROM work_order)"),
        ("po_line", "po_id NOT IN (SELECT po_id FROM purchase_order)"),
        ("receiving", "po_id NOT IN (SELECT po_id FROM purchase_order)"),
        ("payables", "po_id NOT IN (SELECT po_id FROM purchase_order)"),
        ("payable_line", "po_id NOT IN (SELECT po_id FROM purchase_order)"),
        ("inventory_transaction", "wo_id IS NOT NULL AND wo_id NOT IN "
                                  "(SELECT wo_id FROM work_order)"),
        ("inventory_transaction", "po_id IS NOT NULL AND po_id NOT IN "
                                  "(SELECT po_id FROM purchase_order)"),
        ("purchase_order", "wo_id IS NOT NULL AND wo_id NOT IN "
                           "(SELECT wo_id FROM work_order)"),
        ("trace_inventory_trace", "trace_id NOT IN (SELECT trace_id FROM trace)"),
        ("certification", "receipt_id IS NOT NULL AND receipt_id NOT IN "
                          "(SELECT receipt_id FROM receiving)"),
    ]
    for table, cond in orphan_checks:
        n = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {cond}").fetchone()[0]
        if n:
            failures.append(f"{n} orphan rows in {table} ({cond[:50]}...)")

    as_of = cur.execute("SELECT MAX(close_date) FROM work_order").fetchone()[0]
    if not as_of:
        failures.append("AS_OF anchor lost — no closed work order retained")

    retired = list(RESOURCE_REMAP.keys())
    rph = ",".join("?" * len(retired))
    for table, col in (("operation", "resource_id"), ("labor_ticket", "resource_id"),
                       ("operation_type", "resource_id"), ("EMPLOYEE", "home_resource_id"),
                       ("shop_resource", "resource_id")):
        n = cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({rph})", retired
        ).fetchone()[0]
        if n:
            failures.append(f"{n} rows in {table} still reference retired resources")

    cph = ",".join("?" * len(CNC_MACHINES))
    n = cur.execute(
        f"SELECT COUNT(*) FROM shop_resource WHERE resource_id IN ({cph})", CNC_MACHINES
    ).fetchone()[0]
    if n != len(CNC_MACHINES):
        failures.append(f"expected {len(CNC_MACHINES)} CNC machines, found {n}")

    if failures:
        raise RuntimeError("Prune validation FAILED:\n  " + "\n  ".join(failures))
    print(f"All fail-closed checks passed (AS_OF = {as_of})")


def validate_mrp(con) -> None:
    from mrp_engine import validate_planning_inputs
    summary = validate_planning_inputs(con)
    print(f"MRP planning inputs valid: {summary['demand_parts']} demand parts in horizon")


def already_at_demo_scale(cur) -> bool:
    """True when CO/WO/PO counts are already inside the demo band — a fresh
    bootstrap seeds directly at demo scale, so the trim would over-prune.

    The July daily-throughput series (CO-JUL-% / WO-JUL-%, added by
    seed_july_throughput.py AFTER this prune in the chain) is a protected
    synthetic series: excluded from the band count and never trimmed."""
    for table, pk, excl, hi in (
        ("customer_order", "order_id", "CO-JUL-%", 20),
        ("work_order", "wo_id", "WO-JUL-%", 20),
        ("purchase_order", "po_id", None, 25),
    ):
        where = f" WHERE {pk} NOT LIKE '{excl}'" if excl else ""
        n = cur.execute(f"SELECT COUNT(*) FROM {table}{where}").fetchone()[0]
        if not (10 <= n <= hi):
            return False
    return True


def main() -> int:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        consolidate_cnc(cur)
        if already_at_demo_scale(cur):
            print("Counts already within demo band (CO/WO [10, 20], PO [10, 25]) — skipping trim")
        else:
            prune(cur)
        validate(cur)
        con.commit()
    except Exception:
        con.rollback()
        raise
    validate_mrp(con)
    con.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
