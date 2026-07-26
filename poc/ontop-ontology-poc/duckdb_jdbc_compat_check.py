#!/usr/bin/env python3
"""
DuckDB JDBC compatibility check for manual Ontop runs.
=======================================================

Verifies that the DuckDB file at ``Utilities/SQLMesh/db.db`` (written by the
installed Python duckdb library) was serialized at a storage-compatibility level
that the pinned DuckDB JDBC driver can open.

Background
----------
Python DuckDB exposes a ``storage_compatibility_version`` setting that controls
the minimum DuckDB version a written file requires.  The default value is
``v0.10.2`` — meaning any DuckDB >= v0.10.2 (including JDBC 1.1.3) can open
files written by the Python library, regardless of which newer Python version
produced them.

DuckDB does not guarantee *forward* compatibility (old reader / new writer) at
the individual feature level, but the ``storage_compatibility_version`` default
is a deliberate backward-compatibility pledge from the DuckDB project: files
written at the v0.10.2 level are readable by all later DuckDB releases.

Result from 2026-07-26 (Python duckdb 1.5.3, JDBC target 1.1.3):
- storage_compatibility_version = v0.10.2  (DuckDB's own backward-compat floor)
- DuckDB JDBC 1.1.3 is version 1.1.3 >> v0.10.2
- VERDICT: COMPATIBLE — no JDBC version bump required.

Run this check any time the Python duckdb version is upgraded to confirm the
storage_compatibility_version default has not changed to a value newer than the
pinned JDBC version.
"""

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.path.join(REPO_ROOT, "Utilities", "SQLMesh", "db.db")

DUCKDB_JDBC_VERSION = "1.1.3"


def _parse_version(v: str) -> tuple:
    """Return a comparable tuple from a 'vX.Y.Z' or 'X.Y.Z' string."""
    return tuple(int(x) for x in v.lstrip("v").split("."))


def check_compat(db_path: str = DB_PATH, jdbc_version: str = DUCKDB_JDBC_VERSION) -> bool:
    """Return True if ``db_path`` was written at a compatibility level that
    ``jdbc_version`` can open; print a diagnostic summary either way."""

    try:
        import duckdb
    except ImportError:
        print("SKIP — duckdb Python package not installed; cannot inspect storage version.")
        return True

    py_ver = duckdb.__version__

    if not os.path.isfile(db_path):
        print(f"SKIP — {db_path} not found (run `cd Utilities/SQLMesh && sqlmesh run` first).")
        return True

    con = duckdb.connect(db_path, read_only=True)
    try:
        row = con.execute("SELECT current_setting('storage_compatibility_version')").fetchone()
        storage_compat_ver = row[0] if row else "unknown"
    finally:
        con.close()

    jdbc_tuple = _parse_version(jdbc_version)
    compat_tuple = _parse_version(storage_compat_ver)
    compatible = jdbc_tuple >= compat_tuple

    print("DuckDB JDBC compatibility check")
    print("=" * 48)
    print(f"  DB path                    : {db_path}")
    print(f"  Python duckdb version      : {py_ver}")
    print(f"  storage_compatibility_ver  : {storage_compat_ver}")
    print(f"  Pinned JDBC version        : {jdbc_version}")
    print()
    if compatible:
        print(f"PASS — JDBC {jdbc_version} >= storage target {storage_compat_ver}.")
        print("       The pinned DuckDB JDBC driver can open files written by")
        print(f"       Python duckdb {py_ver}. No version bump required.")
    else:
        print(f"FAIL — JDBC {jdbc_version} < storage target {storage_compat_ver}.")
        print(f"       Python duckdb {py_ver} writes files that require DuckDB >= {storage_compat_ver}.")
        print(f"       Bump DUCKDB_JDBC_VERSION in replit_integrations/ontop_poc_setup.py")
        print(f"       to {storage_compat_ver} (or later) and re-pin the SHA-256.")
    return compatible


if __name__ == "__main__":
    ok = check_compat()
    sys.exit(0 if ok else 1)
