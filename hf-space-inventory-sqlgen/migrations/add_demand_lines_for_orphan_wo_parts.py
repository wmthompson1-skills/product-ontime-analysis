"""
Migration: add customer_order_line demand coverage for work-order parts that
have zero customer_order_line rows.

Why: migrations/add_demand_linkage_and_forecast.py links each non-planned
work order to a customer_order_line on the same part_id
(link_work_orders() -> by_part.get(part_id)) and fails closed if fewer than
MIN_LINK_RATIO (0.5) of work orders end up linked. A work order whose part_id
has NO customer_order_line at all can never link, regardless of scoring —
this is a "no demand traceability" gap, not a threshold-tuning problem. On a
freshly bootstrapped DB (after the receiving NOT-IN-NULL fix in
seed_erp_synthetic.py made seed_receiving() actually populate rows, changing
downstream part/status selection), exactly 4 parts land in this gap
(P-10010, P-10024, P-10025, P-10036), leaving 9/20 (45%) linked — just under
the 50% gate.

This migration is DETERMINISTIC and GROUNDED (no randomness): for each
orphaned part, it adds one customer_order_line per WO status family that
_status_compatible() in add_demand_linkage_and_forecast.py actually needs
(closed WOs need a Closed/Shipped order; everything else needs an Open
order), attached to a fixed existing customer_order header so no new CO
headers are created and the demo band (CO 10-20) is untouched. Quantity is
the work order's own quantity (grounded in the real WO, not invented); unit
price is the part's unit_cost with a fixed 1.3x markup, matching the markup
band seed_erp_synthetic.py's seed_customer_order_lines() already uses
(1.15-1.45x).

Run once (registered in scripts/bootstrap_db.py before
add_demand_linkage_and_forecast.py):
    cd hf-space-inventory-sqlgen
    python migrations/add_demand_lines_for_orphan_wo_parts.py

Safe to re-run: only inserts a line for a (order_id, part_id) pair that
doesn't already have one.
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app_schema", "manufacturing.db")

# Fixed, existing, non-MRP-critical CO headers — never a new header.
OPEN_CO = "CO-00015"      # status = Open
CLOSED_CO = "CO-00002"    # status = Closed

MARKUP = 1.3


def _status_family(wo_status: str) -> str:
    return "closed" if wo_status == "closed" else "open"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        open_status, closed_status = cur.execute(
            "SELECT status FROM customer_order WHERE order_id=?", (OPEN_CO,)
        ).fetchone()[0], cur.execute(
            "SELECT status FROM customer_order WHERE order_id=?", (CLOSED_CO,)
        ).fetchone()[0]
        if open_status != "Open":
            raise RuntimeError(f"{OPEN_CO} expected status Open, got {open_status!r}")
        if closed_status not in ("Closed", "Shipped"):
            raise RuntimeError(f"{CLOSED_CO} expected Closed/Shipped, got {closed_status!r}")

        orphan_wos = cur.execute(
            """
            SELECT w.wo_id, w.part_id, w.status, w.quantity
            FROM work_order w
            WHERE w.wo_id NOT LIKE 'WO-PLN-%'
              AND NOT EXISTS (
                    SELECT 1 FROM customer_order_line l WHERE l.part_id = w.part_id
              )
            ORDER BY w.wo_id
            """
        ).fetchall()

        # One line per (part_id, status_family) — covers every orphaned WO's
        # part with a status-compatible order, per _status_family() above.
        needed: dict[tuple[str, str], float] = {}
        for wo_id, part_id, status, qty in orphan_wos:
            key = (part_id, _status_family(status))
            needed[key] = max(needed.get(key, 0.0), qty or 1)

        inserted = 0
        for (part_id, family), qty in sorted(needed.items()):
            order_id = CLOSED_CO if family == "closed" else OPEN_CO

            exists = cur.execute(
                "SELECT 1 FROM customer_order_line WHERE order_id=? AND part_id=?",
                (order_id, part_id),
            ).fetchone()
            if exists:
                continue

            unit_cost = cur.execute(
                "SELECT unit_cost FROM part WHERE part_id=?", (part_id,)
            ).fetchone()
            if unit_cost is None:
                raise RuntimeError(f"part {part_id} not found in part master")
            unit_price = round(unit_cost[0] * MARKUP, 2)

            next_line_no = cur.execute(
                "SELECT COALESCE(MAX(line_no), 0) + 1 FROM customer_order_line WHERE order_id=?",
                (order_id,),
            ).fetchone()[0]

            cur.execute(
                "INSERT INTO customer_order_line "
                "(order_id, line_no, part_id, site_id, order_qty, unit_price) "
                "SELECT ?, ?, ?, site_id, ?, ? FROM customer_order WHERE order_id=?",
                (order_id, next_line_no, part_id, qty, unit_price, order_id),
            )
            inserted += 1
            print(f"  {order_id} line {next_line_no}: part={part_id} qty={qty} "
                  f"unit_price={unit_price} (family={family})")

        conn.commit()
        print(f"customer_order_line: {inserted} new row(s) inserted")

        # Fail closed: every non-planned WO's part must now have >= 1 line.
        still_orphaned = cur.execute(
            """
            SELECT COUNT(*) FROM work_order w
            WHERE w.wo_id NOT LIKE 'WO-PLN-%'
              AND NOT EXISTS (
                    SELECT 1 FROM customer_order_line l WHERE l.part_id = w.part_id
              )
            """
        ).fetchone()[0]
        if still_orphaned:
            raise RuntimeError(
                f"{still_orphaned} non-planned work order(s) still have a part_id "
                "with zero customer_order_line rows after backfill"
            )

        print("Done. Every non-planned work order's part now has demand coverage.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
