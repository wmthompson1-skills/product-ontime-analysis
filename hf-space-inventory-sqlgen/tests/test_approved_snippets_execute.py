"""Executes every APPROVED SQL snippet against the live SQLite database.

Closes the gap where a snippet can be registered as APPROVED in
reviewer_manifest.json yet fail at query time because of a schema change,
a dropped table, or a typo.  For each APPROVED entry we:

  1. Resolve its SQL file (mirroring SolderEngine's path resolution so the
     two stay in agreement on where snippets live).
  2. Read the SQL text.
  3. Run it against app_schema/manufacturing.db in read-only mode.
  4. Assert no exception is raised.

The database is opened read-only (mode=ro) so the test can never mutate
manufacturing.db, even though every approved snippet is a SELECT.

Run: python hf-space-inventory-sqlgen/tests/test_approved_snippets_execute.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HF_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(HF_DIR)

MANIFEST_PATH = os.path.join(
    HF_DIR, "app_schema", "ground_truth", "reviewer_manifest.json"
)
MANIFEST_DIR = os.path.dirname(MANIFEST_PATH)
DB_PATH = os.path.join(HF_DIR, "app_schema", "manufacturing.db")


def _resolve_sql_path(file_path: str) -> str | None:
    """Find a snippet's SQL file on disk.

    Manifest file_path values use two conventions (relative to the HF Space
    dir, and relative to the repo root), so try the documented candidates in
    the same order SolderEngine.load_approved_bindings uses.
    """
    if not file_path:
        return None
    candidates = [
        file_path if os.path.isabs(file_path) else None,
        os.path.join(MANIFEST_DIR, os.path.basename(file_path)),
        os.path.join(HF_DIR, file_path),
        os.path.join(REPO_ROOT, file_path),
        file_path,
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return None


def _all_entries() -> list[tuple[str, dict]]:
    with open(MANIFEST_PATH, "r") as f:
        manifest = json.load(f)
    return list(manifest.get("approved_snippets", {}).items())


def _approved_entries() -> list[tuple[str, dict]]:
    return [
        (key, entry)
        for key, entry in _all_entries()
        if entry.get("validation_status") == "APPROVED"
    ]


def test_manifest_present_with_approved_snippets() -> None:
    assert os.path.exists(MANIFEST_PATH), f"manifest missing: {MANIFEST_PATH}"
    entries = _approved_entries()
    assert entries, "no APPROVED snippets found in reviewer_manifest.json"
    print(f"PASS: found {len(entries)} APPROVED snippet(s) to execute")


def test_database_present() -> None:
    assert os.path.exists(DB_PATH), f"database missing: {DB_PATH}"
    print(f"PASS: database present at {DB_PATH}")


def test_every_approved_snippet_resolves_to_a_file() -> None:
    missing = []
    for key, entry in _approved_entries():
        if _resolve_sql_path(entry.get("file_path", "")) is None:
            missing.append((key, entry.get("file_path", "")))
    assert not missing, "APPROVED snippets with no SQL file on disk: " + ", ".join(
        f"{k} ({fp})" for k, fp in missing
    )
    print("PASS: every APPROVED snippet resolves to a SQL file")


def test_every_approved_snippet_executes() -> None:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        failures = []
        executed = 0
        for key, entry in _approved_entries():
            path = _resolve_sql_path(entry.get("file_path", ""))
            if path is None:
                failures.append(f"{key}: SQL file not found")
                continue
            with open(path, "r") as f:
                sql = f.read().strip()
            if not sql:
                failures.append(f"{key}: SQL file is empty")
                continue
            try:
                # Temporal-contract snippets carry NULL-guarded named params.
                # Binding them all to NULL reproduces the full unfiltered
                # population; snippets that don't reference a key ignore it.
                conn.execute(
                    sql,
                    {
                        "start_date": None,
                        "end_date": None,
                        "supplier_id": None,
                        "as_of_date": None,
                        "job_id": None,
                        "invoice_number": None,
                    },
                ).fetchall()
                executed += 1
            except Exception as exc:  # noqa: BLE001 - report any execution error
                failures.append(f"{key}: {type(exc).__name__}: {exc}")
        assert not failures, (
            "APPROVED snippets failed to execute against manufacturing.db:\n  "
            + "\n  ".join(failures)
        )
        print(f"PASS: all {executed} APPROVED snippet(s) executed without errors")
    finally:
        conn.close()


def test_all_manifest_entries_path_resolution_smoke() -> None:
    """Smoke-test path resolution for every manifest entry, not just APPROVED.

    APPROVED entries with an unresolvable file_path are a hard failure —
    the Solder Pattern guarantee is broken.

    ARCHIVED entries with unresolvable paths are printed as warnings only;
    files may be intentionally removed when a snippet is retired.
    """
    hard_failures: list[str] = []
    archived_missing: list[str] = []

    for key, entry in _all_entries():
        status = entry.get("validation_status", "UNKNOWN")
        fp = entry.get("file_path", "")
        resolved = _resolve_sql_path(fp)
        if resolved is None:
            if status == "APPROVED":
                hard_failures.append(f"{key} (file_path={fp!r})")
            else:
                archived_missing.append(f"{key} [{status}] (file_path={fp!r})")

    for msg in archived_missing:
        print(f"WARNING: unresolvable path for non-APPROVED entry: {msg}")

    assert not hard_failures, (
        "APPROVED manifest entries with no SQL file on disk:\n  "
        + "\n  ".join(hard_failures)
    )

    total = len(list(_all_entries()))
    approved_ok = sum(
        1 for k, e in _all_entries()
        if e.get("validation_status") == "APPROVED"
        and _resolve_sql_path(e.get("file_path", "")) is not None
    )
    print(
        f"PASS: all {approved_ok} APPROVED paths resolve; "
        f"{len(archived_missing)} ARCHIVED path(s) missing (expected); "
        f"{total} total entries checked"
    )


def main() -> int:
    tests = [
        test_manifest_present_with_approved_snippets,
        test_database_present,
        test_every_approved_snippet_resolves_to_a_file,
        test_all_manifest_entries_path_resolution_smoke,
        test_every_approved_snippet_executes,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"{'PASS' if failed == 0 else 'FAIL'}: {len(tests) - failed}/{len(tests)} tests")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
