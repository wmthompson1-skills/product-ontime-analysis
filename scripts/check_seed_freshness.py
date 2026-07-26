#!/usr/bin/env python3
"""
check_seed_freshness.py — Post-merge gate: detect when Ontop seed CSVs are
stale relative to the current manufacturing.db schema or row counts, AND
detect tables in manufacturing.db that have no seed CSV at all.

For every CSV in Utilities/SQLMesh/seeds/ whose basename matches a table in
manufacturing.db, we compare:
  1. Column set  (CSV header vs PRAGMA table_info — order-insensitive)
  2. Row count   (data rows in CSV vs SELECT COUNT(*) FROM table)

Additionally, every table in manufacturing.db must have a corresponding seed
CSV. Tables with no seed file are reported as missing-seed errors so the
DuckDB snapshot used by Ontop never silently drops a table.

Exits 0 if all matched seeds are fresh and no tables are unseeded (or the DB
is absent).
Exits 1 with a clear diff summary if any seed is stale or any table is
missing a seed entirely.

To refresh stale seeds, run:
  python Utilities/SQLMesh/scripts/export_seeds.py

To also create seed CSVs for brand-new (previously unseeded) tables, run:
  python Utilities/SQLMesh/scripts/export_seeds.py --include-new
"""

import csv
import sqlite3
import sys
from pathlib import Path

SEEDS_DIR = Path("Utilities/SQLMesh/seeds")
DB_PATH = Path("hf-space-inventory-sqlgen/app_schema/manufacturing.db")
REFRESH_CMD = "python Utilities/SQLMesh/scripts/export_seeds.py"
INCLUDE_NEW_CMD = "python Utilities/SQLMesh/scripts/export_seeds.py --include-new"

SQLITE_INTERNAL_TABLES = frozenset({
    "sqlite_sequence",
    "sqlite_stat1",
    "sqlite_stat2",
    "sqlite_stat3",
    "sqlite_stat4",
})


def main() -> int:
    if not DB_PATH.exists():
        print(f"check_seed_freshness: DB not found at {DB_PATH} — skipping.")
        return 0

    if not SEEDS_DIR.exists():
        print(f"check_seed_freshness: seeds dir not found at {SEEDS_DIR} — skipping.")
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    db_tables: dict[str, list[str]] = {}
    for (name,) in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        if name in SQLITE_INTERNAL_TABLES or name.startswith("sqlite_"):
            continue
        cols = [row[1] for row in cur.execute(f"PRAGMA table_info('{name}')").fetchall()]
        db_tables[name] = cols

    seeded_tables: set[str] = {p.stem for p in SEEDS_DIR.glob("*.csv")}

    errors: list[str] = []
    missing_seed_errors: list[str] = []
    checked = 0

    for csv_path in sorted(SEEDS_DIR.glob("*.csv")):
        table_name = csv_path.stem
        if table_name not in db_tables:
            continue

        checked += 1
        db_cols = set(db_tables[table_name])
        db_count_row = cur.execute(f"SELECT COUNT(*) FROM '{table_name}'").fetchone()
        db_count = db_count_row[0] if db_count_row else 0

        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                try:
                    header = next(reader)
                except StopIteration:
                    errors.append(f"  {csv_path.name}: empty file (no header)")
                    continue
                csv_cols = set(header)
                csv_count = sum(1 for _ in reader)
        except Exception as exc:
            errors.append(f"  {csv_path.name}: read error — {exc}")
            continue

        col_issues: list[str] = []
        missing_in_csv = db_cols - csv_cols
        extra_in_csv = csv_cols - db_cols
        if missing_in_csv:
            col_issues.append(f"missing from CSV: {sorted(missing_in_csv)}")
        if extra_in_csv:
            col_issues.append(f"extra in CSV (dropped from DB?): {sorted(extra_in_csv)}")

        row_issues: list[str] = []
        if csv_count != db_count:
            row_issues.append(f"row count CSV={csv_count} vs DB={db_count}")

        if col_issues or row_issues:
            all_issues = col_issues + row_issues
            errors.append(f"  {csv_path.name} ({table_name}): {'; '.join(all_issues)}")

    for table_name in sorted(db_tables):
        if table_name not in seeded_tables:
            missing_seed_errors.append(
                f"  {table_name}: exists in manufacturing.db but has no seed CSV "
                f"(DuckDB/Ontop snapshot will be missing this table)"
            )

    conn.close()

    rc = 0

    if errors:
        print(f"check_seed_freshness: {len(errors)} stale seed(s) detected "
              f"(checked {checked} against manufacturing.db):\n")
        for line in errors:
            print(line)
        print(f"\nTo refresh all stale seeds, run:\n  {REFRESH_CMD}")
        rc = 1

    if missing_seed_errors:
        if rc == 0:
            print()
        print(f"check_seed_freshness: {len(missing_seed_errors)} table(s) in "
              f"manufacturing.db have no seed CSV:\n")
        for line in missing_seed_errors:
            print(line)
        print(f"\nTo create seed CSVs for new tables, run:\n  {INCLUDE_NEW_CMD}")
        rc = 1

    if rc == 0:
        print(f"check_seed_freshness: OK — {checked} seed CSV(s) match manufacturing.db "
              "(columns + row counts); no unseeded tables detected")

    return rc


if __name__ == "__main__":
    sys.exit(main())
