#!/usr/bin/env python3
"""
export_seeds.py — Refresh Ontop seed CSVs from the current manufacturing.db.

Only re-exports CSVs whose basename matches a table that actually exists in
manufacturing.db.  Seed files that correspond to synthetic/supplementary data
(no matching table) are left untouched.

Usage:
    python Utilities/SQLMesh/scripts/export_seeds.py [--dry-run] [--db PATH] [--seeds-dir PATH]

Options:
    --dry-run      Print what would be refreshed without writing any files.
    --db PATH      Path to manufacturing.db  (default: hf-space-inventory-sqlgen/app_schema/manufacturing.db)
    --seeds-dir PATH  Path to seeds directory (default: Utilities/SQLMesh/seeds)
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB = Path("hf-space-inventory-sqlgen/app_schema/manufacturing.db")
DEFAULT_SEEDS = Path("Utilities/SQLMesh/seeds")


def export_table(conn: sqlite3.Connection, table: str, out_path: Path, dry_run: bool) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM '{table}'")
    cols = [desc[0] for desc in cur.description]
    rows = cur.fetchall()
    if dry_run:
        print(f"  [dry-run] would write {len(rows)} rows → {out_path}")
        return len(rows)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)
    print(f"  exported {len(rows)} rows → {out_path}")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Ontop seed CSVs from manufacturing.db")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to manufacturing.db")
    parser.add_argument("--seeds-dir", type=Path, default=DEFAULT_SEEDS, help="Path to seeds directory")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"export_seeds: ERROR — DB not found at {args.db}")
        return 1

    if not args.seeds_dir.exists():
        print(f"export_seeds: ERROR — seeds directory not found at {args.seeds_dir}")
        return 1

    conn = sqlite3.connect(str(args.db))
    cur = conn.cursor()
    db_tables: set[str] = {
        row[0]
        for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    seed_csvs = sorted(args.seeds_dir.glob("*.csv"))
    to_export = [(p, p.stem) for p in seed_csvs if p.stem in db_tables]
    skipped = [p.name for p in seed_csvs if p.stem not in db_tables]

    if not to_export:
        print("export_seeds: no seed CSVs match any manufacturing.db table — nothing to do.")
        conn.close()
        return 0

    mode = "[dry-run] " if args.dry_run else ""
    print(f"export_seeds: {mode}refreshing {len(to_export)} seed CSV(s) from {args.db}")
    total_rows = 0
    for csv_path, table in to_export:
        total_rows += export_table(conn, table, csv_path, args.dry_run)

    conn.close()

    if skipped:
        print(f"\nexport_seeds: {len(skipped)} CSV(s) skipped (no matching DB table — "
              "synthetic/supplementary data):")
        for name in skipped:
            print(f"  {name}")

    action = "would refresh" if args.dry_run else "refreshed"
    print(f"\nexport_seeds: {action} {len(to_export)} CSV(s), {total_rows} total rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
