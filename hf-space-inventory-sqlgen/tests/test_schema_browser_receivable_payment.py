"""Gate test — receivable_payment visible in Schema Browser after bootstrap.

Locks two invariants that prevent a future migration-ordering change from
silently dropping receivable_payment from the graph:

  1. sql_graph_nodes has ≥ 1 row with table_name='receivable_payment'
     (proves collect_june2026_ar.py ran AND the graph export captured the table;
     the Schema Browser sources its table list directly from sql_graph_nodes).

  2. sql_graph_parity_check passes (graph_metadata.json ↔ sql_graph_nodes).
     Ensures the committed JSON and the live SQLite tables agree, so the table
     is visible in both the Schema Browser tab AND in any offline graph tooling.

Run gate-style from the repo root:
    python hf-space-inventory-sqlgen/tests/test_schema_browser_receivable_payment.py

Or from within the hf-space-inventory-sqlgen directory:
    python tests/test_schema_browser_receivable_payment.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_HF_DIR = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_HF_DIR)

DB_PATH = os.path.join(_HF_DIR, "app_schema", "manufacturing.db")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# Gate 1 — sql_graph_nodes contains receivable_payment rows
# ---------------------------------------------------------------------------

def test_sql_graph_nodes_has_receivable_payment() -> None:
    """sql_graph_nodes must contain at least one row for receivable_payment.

    This is the direct source of truth for the Schema Browser tab: the tab
    queries sql_graph_nodes to build its table/column tree.  If the row is
    absent the table is silently invisible in the UI without any error.
    """
    if not os.path.exists(DB_PATH):
        check(
            "sql_graph_nodes — receivable_payment present",
            False,
            f"DB not found at {DB_PATH!r} — run bootstrap_db.py first",
        )
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        # Confirm the table itself exists before querying it.
        tbl_exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='sql_graph_nodes'"
        ).fetchone()[0]
        if not tbl_exists:
            check(
                "sql_graph_nodes — receivable_payment present",
                False,
                "sql_graph_nodes table does not exist — "
                "run bootstrap_db.py then the graph export",
            )
            return

        n_rows = conn.execute(
            "SELECT COUNT(*) FROM sql_graph_nodes WHERE table_name = 'receivable_payment'"
        ).fetchone()[0]
        check(
            "sql_graph_nodes — receivable_payment present",
            n_rows >= 1,
            f"found {n_rows} rows; expected ≥ 1 — "
            "collect_june2026_ar.py may not have run or the graph was not re-exported",
        )

        # Also confirm the physical table exists so we know the migration ran.
        tbl_physical = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='receivable_payment'"
        ).fetchone()[0]
        check(
            "receivable_payment physical table exists",
            tbl_physical == 1,
            "table missing in sqlite_master — collect_june2026_ar.py did not run",
        )

        if n_rows >= 1:
            # Show the column names captured, for diagnostic visibility.
            cols = [
                r[0] or "(table node)"
                for r in conn.execute(
                    "SELECT column_name FROM sql_graph_nodes "
                    "WHERE table_name = 'receivable_payment' "
                    "ORDER BY ordinal"
                ).fetchall()
            ]
            print(
                f"        receivable_payment columns in sql_graph_nodes ({n_rows}): "
                + ", ".join(cols)
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Gate 2 — sql_graph_parity_check passes (JSON ↔ SQLite)
# ---------------------------------------------------------------------------

def test_sql_graph_parity() -> None:
    """graph_metadata.json must be in parity with sql_graph_nodes/sql_graph_edges.

    This is the authoritative acceptance gate (run by post-merge.sh).  Running
    it here confirms that the committed JSON reflects the bootstrapped DB state
    — so a stale JSON would fail this test before reaching the post-merge gate.
    """
    # Locate the parity checker (lives under replit_integrations/ at repo root).
    parity_mod_dir = os.path.join(_REPO_ROOT, "replit_integrations")
    if parity_mod_dir not in sys.path:
        sys.path.insert(0, parity_mod_dir)

    try:
        import sql_graph_parity_check as parity  # noqa: PLC0415
    except ImportError as exc:
        check(
            "sql_graph_parity — module importable",
            False,
            f"cannot import sql_graph_parity_check: {exc}",
        )
        return

    check("sql_graph_parity — module importable", True)

    if not os.path.exists(DB_PATH):
        check(
            "sql_graph_parity — DB present",
            False,
            f"DB not found at {DB_PATH!r}",
        )
        return

    check("sql_graph_parity — DB present", True)

    # Resolve the committed graph_metadata.json alongside the parity module.
    json_path = os.path.join(parity_mod_dir, "graph_metadata.json")

    # Run the parity check (skip-on-missing so a brand-new env fails gracefully).
    rc = parity.check_parity(DB_PATH, json_path, skip_on_missing=True)
    check(
        "sql_graph_parity — graph_metadata.json matches sql_graph_nodes/edges",
        rc == 0,
        f"parity check returned exit code {rc} — "
        "graph_metadata.json is out of sync with sql_graph_nodes/sql_graph_edges",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    print("Schema Browser — receivable_payment post-bootstrap gate\n")

    for fn in (
        test_sql_graph_nodes_has_receivable_payment,
        test_sql_graph_parity,
    ):
        try:
            fn()
        except Exception as exc:  # pragma: no cover
            check(f"{fn.__name__} (unexpected error)", False, f"{type(exc).__name__}: {exc}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
