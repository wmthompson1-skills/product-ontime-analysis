"""
test_bridge_collection_health.py
=================================
Two test groups in this file:

1. Unit tests for ``run_bridge_health_check_impl`` (bridge_health.py)
   ---------------------------------------------------------------
   These tests use dependency-injection seams so they run without a real
   SQLite DB or ArangoDB instance.  They cover:

   - IN SYNC        — ArangoDB counts equal SQLite counts → "✅  IN SYNC"
   - OUT OF SYNC    — counts differ                      → "❌  OUT OF SYNC"
   - MISSING COLL.  — collection absent in ArangoDB      → "❌  OUT OF SYNC"
                       (detail contains "MISSING")
   - NO ARANGO_HOST — env var not set                    → "⚠️  SKIP — ARANGO_HOST not set"
   - ARANGO CONN ERR— factory raises                     → "⚠️  SKIP — ArangoDB connection failed"
   - SCHEMA_NODES IN SYNC  — tables vertex count == schema_nodes row count
   - SCHEMA_NODES OUT OF SYNC — tables vertex count != schema_nodes row count

2. Integration tests
   ------------------
   Skipped when ARANGO_HOST is not set.
   Writes a machine-readable drift report to DRIFT_REPORT_PATH on failure.
   Covers both Perspective bridge tables AND schema_nodes ↔ tables vertex.

Run directly:
    python hf-space-inventory-sqlgen/tests/test_bridge_collection_health.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
HF_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(HF_DIR)
sys.path.insert(0, HF_DIR)

from bridge_health import BRIDGE_HEALTH_MAP, run_bridge_health_check_impl

SQLITE_DB_PATH = os.path.join(HF_DIR, "app_schema", "manufacturing.db")

BRIDGE_MAP = {
    "Perspective_Intents": "schema_intent_perspectives",
    "Perspective_Concepts": "schema_perspective_concepts",
}

SCHEMA_NODES_MAP = {
    "tables": "schema_nodes",
}

DRIFT_REPORT_PATH = os.environ.get(
    "DRIFT_REPORT_PATH",
    os.path.join(REPO_ROOT, "drift_report.txt"),
)


def _sqlite_counts(db_path: str) -> dict[str, int]:
    """Return {sqlite_table: row_count} for both bridge source tables."""
    conn = sqlite3.connect(db_path)
    counts: dict[str, int] = {}
    try:
        for table in BRIDGE_MAP.values():
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = row[0] if row else 0
    finally:
        conn.close()
    return counts


def _arango_counts(db) -> dict[str, int]:
    """Return {arango_collection: document_count} for both bridge collections."""
    counts: dict[str, int] = {}
    for coll_name in BRIDGE_MAP:
        if db.has_collection(coll_name):
            counts[coll_name] = db.collection(coll_name).count()
        else:
            counts[coll_name] = -1
    return counts


def _reset_drift_report() -> None:
    """Remove any drift report left over from a previous run.

    Called once at the start of the test run so that the append-mode writes
    in _write_drift_report() accumulate only the failures from THIS run, not
    stale entries from a prior invocation in the same workspace.
    """
    try:
        if os.path.exists(DRIFT_REPORT_PATH):
            os.remove(DRIFT_REPORT_PATH)
    except OSError as exc:
        print(f"WARNING: could not reset drift report at {DRIFT_REPORT_PATH}: {exc}")


def _write_drift_report(lines: list[str]) -> None:
    """Append drift details to DRIFT_REPORT_PATH for CI notification steps.

    Opened in append ("a") mode so that when multiple health checks fail in the
    same run (e.g. both the bridge-collection check and the schema_nodes/tables
    check), every failing check's entries are preserved.  Write ("w") mode would
    let the second writer silently clobber the first, hiding earlier failures
    from the Slack alert.  The file is cleared once per run via
    _reset_drift_report().
    """
    if not lines:
        return
    try:
        report_dir = os.path.dirname(DRIFT_REPORT_PATH)
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)
        with open(DRIFT_REPORT_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"WARNING: could not write drift report to {DRIFT_REPORT_PATH}: {exc}")


# ---------------------------------------------------------------------------
# Unit tests for run_bridge_health_check_impl (no real DB or ArangoDB needed)
# ---------------------------------------------------------------------------

_SMALL_BRIDGE_MAP = {
    "Perspective_Intents": "schema_intent_perspectives",
    "Perspective_Concepts": "schema_perspective_concepts",
}

_FAKE_DB_PATH = "/fake/manufacturing.db"


def _make_sqlite_conn(counts: dict):
    """Return a fake sqlite3 connection whose execute().fetchone() returns counts."""
    class _Cursor:
        def __init__(self, table):
            self._table = table

        def fetchone(self):
            for tbl, n in counts.items():
                if tbl in self._table:
                    return (n,)
            return (0,)

    class _Conn:
        def execute(self, sql, *_):
            return _Cursor(sql)

        def close(self):
            pass

    return _Conn()


def _make_arango_db(collection_counts: dict):
    """Return a fake ArangoDB db object with has_collection / collection.count()
    and an .aql.execute() that resolves the LENGTH(FOR d IN <coll> ...) queries
    bridge_health.py issues for key-prefix-filtered collections (e.g. 'tables').
    The fake doesn't model raw-vs-filtered counts separately — it just returns
    whatever count the test configured for that collection either way, since
    these tests are about the in-sync/out-of-sync comparison, not the AQL
    filtering itself (that's exercised live against real ArangoDB).
    """
    import re

    class _Coll:
        def __init__(self, n):
            self._n = n

        def count(self):
            return self._n

    class _Aql:
        def execute(self, query, bind_vars=None):
            m = re.search(r"FOR d IN (\w+)", query)
            coll_name = m.group(1) if m else None
            return iter([collection_counts.get(coll_name, 0)])

    class _DB:
        aql = _Aql()

        def has_collection(self, name):
            return name in collection_counts

        def collection(self, name):
            return _Coll(collection_counts[name])

    return _DB()


def test_bridge_health_in_sync() -> None:
    """When ArangoDB and SQLite counts match, status is IN SYNC."""
    sqlite_counts = {
        "schema_intent_perspectives": 10,
        "schema_perspective_concepts": 5,
    }
    arango_counts = {
        "Perspective_Intents": 10,
        "Perspective_Concepts": 5,
    }

    overall, timestamp, detail = run_bridge_health_check_impl(
        _FAKE_DB_PATH,
        _SMALL_BRIDGE_MAP,
        _os_path_exists=lambda _: True,
        _sqlite_connect=lambda _: _make_sqlite_conn(sqlite_counts),
        _arango_env_getter=lambda key: "http://localhost:8529" if key == "ARANGO_HOST" else None,
        _arango_factory=lambda: _make_arango_db(arango_counts),
    )

    assert "IN SYNC" in overall, f"Expected IN SYNC, got: {overall!r}"
    assert "✅" in overall
    assert "✅" in detail
    print(f"PASS: test_bridge_health_in_sync — {overall!r}")


def test_bridge_health_out_of_sync() -> None:
    """When ArangoDB and SQLite counts differ, status is OUT OF SYNC."""
    sqlite_counts = {
        "schema_intent_perspectives": 10,
        "schema_perspective_concepts": 5,
    }
    arango_counts = {
        "Perspective_Intents": 7,
        "Perspective_Concepts": 3,
    }

    overall, timestamp, detail = run_bridge_health_check_impl(
        _FAKE_DB_PATH,
        _SMALL_BRIDGE_MAP,
        _os_path_exists=lambda _: True,
        _sqlite_connect=lambda _: _make_sqlite_conn(sqlite_counts),
        _arango_env_getter=lambda key: "http://localhost:8529" if key == "ARANGO_HOST" else None,
        _arango_factory=lambda: _make_arango_db(arango_counts),
    )

    assert "OUT OF SYNC" in overall, f"Expected OUT OF SYNC, got: {overall!r}"
    assert "❌" in overall
    assert "❌" in detail
    print(f"PASS: test_bridge_health_out_of_sync — {overall!r}")


def test_bridge_health_missing_collection() -> None:
    """When an ArangoDB collection is absent, status is OUT OF SYNC and detail says MISSING."""
    sqlite_counts = {
        "schema_intent_perspectives": 10,
        "schema_perspective_concepts": 5,
    }
    arango_counts: dict = {}

    overall, timestamp, detail = run_bridge_health_check_impl(
        _FAKE_DB_PATH,
        _SMALL_BRIDGE_MAP,
        _os_path_exists=lambda _: True,
        _sqlite_connect=lambda _: _make_sqlite_conn(sqlite_counts),
        _arango_env_getter=lambda key: "http://localhost:8529" if key == "ARANGO_HOST" else None,
        _arango_factory=lambda: _make_arango_db(arango_counts),
    )

    assert "OUT OF SYNC" in overall, f"Expected OUT OF SYNC, got: {overall!r}"
    assert "MISSING" in detail, f"Expected MISSING in detail, got:\n{detail}"
    print(f"PASS: test_bridge_health_missing_collection — {overall!r}")


def test_bridge_health_arango_host_not_set() -> None:
    """When ARANGO_HOST is not set, status reports SKIP with clear message."""
    sqlite_counts = {
        "schema_intent_perspectives": 8,
        "schema_perspective_concepts": 4,
    }

    overall, timestamp, detail = run_bridge_health_check_impl(
        _FAKE_DB_PATH,
        _SMALL_BRIDGE_MAP,
        _os_path_exists=lambda _: True,
        _sqlite_connect=lambda _: _make_sqlite_conn(sqlite_counts),
        _arango_env_getter=lambda key: None,
    )

    assert "ARANGO_HOST not set" in overall, f"Expected ARANGO_HOST not set, got: {overall!r}"
    assert "⚠️" in overall
    assert "ARANGO_HOST" in detail
    assert "N/A" in detail
    print(f"PASS: test_bridge_health_arango_host_not_set — {overall!r}")


def test_bridge_health_arango_connection_failed() -> None:
    """When ArangoDB factory raises, status reports the connection failure."""
    sqlite_counts = {
        "schema_intent_perspectives": 3,
        "schema_perspective_concepts": 2,
    }

    def _bad_factory():
        raise RuntimeError("connection refused")

    overall, timestamp, detail = run_bridge_health_check_impl(
        _FAKE_DB_PATH,
        _SMALL_BRIDGE_MAP,
        _os_path_exists=lambda _: True,
        _sqlite_connect=lambda _: _make_sqlite_conn(sqlite_counts),
        _arango_env_getter=lambda key: "http://localhost:8529" if key == "ARANGO_HOST" else None,
        _arango_factory=_bad_factory,
    )

    assert "ArangoDB connection failed" in overall, f"Expected connection failed, got: {overall!r}"
    assert "⚠️" in overall
    assert "connection refused" in detail
    print(f"PASS: test_bridge_health_arango_connection_failed — {overall!r}")


_SCHEMA_NODES_BRIDGE_MAP = {
    "tables": "schema_nodes",
}


def test_schema_nodes_tables_in_sync() -> None:
    """When ArangoDB 'tables' vertex count equals SQLite schema_nodes row count, status is IN SYNC."""
    sqlite_counts = {"schema_nodes": 20}
    arango_counts = {"tables": 20}

    overall, timestamp, detail = run_bridge_health_check_impl(
        _FAKE_DB_PATH,
        _SCHEMA_NODES_BRIDGE_MAP,
        _os_path_exists=lambda _: True,
        _sqlite_connect=lambda _: _make_sqlite_conn(sqlite_counts),
        _arango_env_getter=lambda key: "http://localhost:8529" if key == "ARANGO_HOST" else None,
        _arango_factory=lambda: _make_arango_db(arango_counts),
    )

    assert "IN SYNC" in overall, f"Expected IN SYNC, got: {overall!r}"
    assert "✅" in overall
    assert "✅" in detail
    print(f"PASS: test_schema_nodes_tables_in_sync — {overall!r}")


def test_schema_nodes_tables_out_of_sync() -> None:
    """When ArangoDB 'tables' vertex count differs from schema_nodes, status is OUT OF SYNC."""
    sqlite_counts = {"schema_nodes": 20}
    arango_counts = {"tables": 15}

    overall, timestamp, detail = run_bridge_health_check_impl(
        _FAKE_DB_PATH,
        _SCHEMA_NODES_BRIDGE_MAP,
        _os_path_exists=lambda _: True,
        _sqlite_connect=lambda _: _make_sqlite_conn(sqlite_counts),
        _arango_env_getter=lambda key: "http://localhost:8529" if key == "ARANGO_HOST" else None,
        _arango_factory=lambda: _make_arango_db(arango_counts),
    )

    assert "OUT OF SYNC" in overall, f"Expected OUT OF SYNC, got: {overall!r}"
    assert "❌" in overall
    assert "❌" in detail
    print(f"PASS: test_schema_nodes_tables_out_of_sync — {overall!r}")


# ---------------------------------------------------------------------------
# Integration test (existing — skipped when ARANGO_HOST not set)
# ---------------------------------------------------------------------------


def test_bridge_collection_counts_match() -> None:
    """Assert Perspective_Intents and Perspective_Concepts document counts
    equal the corresponding SQLite row counts.

    Skipped when ARANGO_HOST is not set.
    Writes a structured drift_report.txt on failure for CI notification steps.
    """
    if not os.environ.get("ARANGO_HOST"):
        print("SKIP: ARANGO_HOST not set — bridge collection health check skipped")
        return

    if not os.path.exists(SQLITE_DB_PATH):
        print(f"SKIP: SQLite DB not found at {SQLITE_DB_PATH}")
        return

    try:
        from graph_sync import get_arango_client, get_arango_db
    except Exception as exc:
        print(f"SKIP: could not import ArangoDB helpers: {exc}")
        return

    try:
        client = get_arango_client()
        db = get_arango_db(client)
    except Exception as exc:
        print(f"SKIP: could not connect to ArangoDB: {exc}")
        return

    sqlite_counts = _sqlite_counts(SQLITE_DB_PATH)
    arango_counts = _arango_counts(db)

    failures: list[str] = []
    report_lines: list[str] = []

    for arango_coll, sqlite_table in BRIDGE_MAP.items():
        arango_n = arango_counts[arango_coll]
        sqlite_n = sqlite_counts[sqlite_table]

        if arango_n == -1:
            msg = (
                f"Collection '{arango_coll}' does not exist in ArangoDB "
                f"(expected {sqlite_n} docs from SQLite '{sqlite_table}'). "
                "Run graph_sync.py to create and populate it."
            )
            failures.append(f"  {msg}")
            report_lines.append(
                f"MISSING_COLLECTION collection={arango_coll} "
                f"sqlite_table={sqlite_table} sqlite_rows={sqlite_n}"
            )
            continue

        if arango_n != sqlite_n:
            diff = arango_n - sqlite_n
            direction = "extra" if diff > 0 else "missing"
            msg = (
                f"COUNT MISMATCH — '{arango_coll}': "
                f"ArangoDB={arango_n}, SQLite '{sqlite_table}'={sqlite_n} "
                f"({abs(diff)} {direction}). "
                "Re-run graph_sync.py to bring ArangoDB in sync."
            )
            failures.append(f"  {msg}")
            report_lines.append(
                f"COUNT_MISMATCH collection={arango_coll} "
                f"sqlite_table={sqlite_table} "
                f"arango_docs={arango_n} sqlite_rows={sqlite_n} "
                f"delta={diff} direction={direction}"
            )

    if failures:
        _write_drift_report(report_lines)

    assert not failures, (
        "Bridge collection health check FAILED:\n" + "\n".join(failures)
    )

    for arango_coll, sqlite_table in BRIDGE_MAP.items():
        print(
            f"PASS: '{arango_coll}' ({arango_counts[arango_coll]} docs) "
            f"== SQLite '{sqlite_table}' ({sqlite_counts[sqlite_table]} rows)"
        )
    print("PASS: all bridge collection counts match SQLite source data")


def test_schema_nodes_tables_count_match() -> None:
    """Assert ArangoDB 'tables' vertex collection count equals SQLite schema_nodes row count.

    schema_nodes is the canonical registry of ERP tables that have been synced
    into the graph as 'tables' vertices.  A count mismatch means some tables
    were added or removed in SQLite but graph_sync.py has not been re-run.

    Skipped when ARANGO_HOST is not set.
    Writes a structured entry to drift_report.txt on failure for CI alert steps.
    """
    if not os.environ.get("ARANGO_HOST"):
        print("SKIP: ARANGO_HOST not set — schema_nodes / tables count check skipped")
        return

    if not os.path.exists(SQLITE_DB_PATH):
        print(f"SKIP: SQLite DB not found at {SQLITE_DB_PATH}")
        return

    try:
        from graph_sync import get_arango_client, get_arango_db
    except Exception as exc:
        print(f"SKIP: could not import ArangoDB helpers: {exc}")
        return

    try:
        client = get_arango_client()
        db = get_arango_db(client)
    except Exception as exc:
        print(f"SKIP: could not connect to ArangoDB: {exc}")
        return

    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        row = conn.execute("SELECT COUNT(*) FROM schema_nodes").fetchone()
        schema_nodes_n = row[0] if row else 0
    finally:
        conn.close()

    if db.has_collection("tables"):
        # Raw .count() includes legacy pre-canonical-key documents that
        # coexist with the current table::X-keyed ones (see
        # docs/plans/arango-key-guardrail.md) — filter to the canonical
        # prefix so this compares current state to current state, matching
        # bridge_health.py's CANONICAL_KEY_PREFIX handling.
        tables_n = next(iter(db.aql.execute(
            "RETURN LENGTH(FOR d IN tables "
            "FILTER STARTS_WITH(d._key, 'table::') RETURN 1)"
        )))
    else:
        tables_n = -1

    failures: list[str] = []
    report_lines: list[str] = []

    if tables_n == -1:
        msg = (
            f"ArangoDB collection 'tables' does not exist "
            f"(expected {schema_nodes_n} vertices from SQLite 'schema_nodes'). "
            "Run graph_sync.py to create and populate it."
        )
        failures.append(f"  {msg}")
        report_lines.append(
            f"MISSING_COLLECTION collection=tables "
            f"sqlite_table=schema_nodes sqlite_rows={schema_nodes_n}"
        )
    elif tables_n != schema_nodes_n:
        diff = tables_n - schema_nodes_n
        direction = "extra" if diff > 0 else "missing"
        msg = (
            f"COUNT MISMATCH — 'tables': "
            f"ArangoDB={tables_n}, SQLite 'schema_nodes'={schema_nodes_n} "
            f"({abs(diff)} {direction}). "
            "Re-run graph_sync.py to bring ArangoDB in sync."
        )
        failures.append(f"  {msg}")
        report_lines.append(
            f"COUNT_MISMATCH collection=tables "
            f"sqlite_table=schema_nodes "
            f"arango_docs={tables_n} sqlite_rows={schema_nodes_n} "
            f"delta={diff} direction={direction}"
        )

    if failures:
        _write_drift_report(report_lines)

    assert not failures, (
        "schema_nodes / tables vertex count check FAILED:\n" + "\n".join(failures)
    )

    print(
        f"PASS: 'tables' ({tables_n} docs) == SQLite 'schema_nodes' ({schema_nodes_n} rows)"
    )


def main() -> int:
    _reset_drift_report()
    tests = [
        test_bridge_health_in_sync,
        test_bridge_health_out_of_sync,
        test_bridge_health_missing_collection,
        test_bridge_health_arango_host_not_set,
        test_bridge_health_arango_connection_failed,
        test_schema_nodes_tables_in_sync,
        test_schema_nodes_tables_out_of_sync,
        test_bridge_collection_counts_match,
        test_schema_nodes_tables_count_match,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            print(f"FAIL: {t.__name__}:\n{exc}")
            failed += 1
        except Exception as exc:
            print(f"ERROR: {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print()
    print(f"{'PASS' if failed == 0 else 'FAIL'}: {len(tests) - failed}/{len(tests)} tests")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
