#!/usr/bin/env python3
"""
export_seeds.py — Refresh Ontop seed CSVs from the current manufacturing.db.

By default only re-exports CSVs whose basename matches a table that already
exists in manufacturing.db.  Seed files that correspond to synthetic/
supplementary data (no matching table) are left untouched.

Use --include-new to also create seed CSVs for tables in manufacturing.db that
currently have no seed CSV at all (i.e. newly added tables).  This is the
recommended fix when check_seed_freshness.py reports "no seed CSV" errors.

Usage:
    python Utilities/SQLMesh/scripts/export_seeds.py [--dry-run] [--include-new]
        [--db PATH] [--seeds-dir PATH]

Options:
    --dry-run       Print what would be refreshed/created without writing any files.
    --include-new   Also export tables that have no existing seed CSV (creates new files).
    --db PATH       Path to manufacturing.db  (default: hf-space-inventory-sqlgen/app_schema/manufacturing.db)
    --seeds-dir PATH  Path to seeds directory (default: Utilities/SQLMesh/seeds)
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB = Path("hf-space-inventory-sqlgen/app_schema/manufacturing.db")
DEFAULT_SEEDS = Path("Utilities/SQLMesh/seeds")

SQLITE_INTERNAL_TABLES = frozenset({
    "sqlite_sequence",
    "sqlite_stat1",
    "sqlite_stat2",
    "sqlite_stat3",
    "sqlite_stat4",
})


def export_table(conn: sqlite3.Connection, table: str, out_path: Path, dry_run: bool) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM '{table}'")
    cols = [desc[0] for desc in cur.description]
    rows = cur.fetchall()
    if dry_run:
        action = "create" if not out_path.exists() else "overwrite"
        print(f"  [dry-run] would {action} {len(rows)} rows → {out_path}")
        return len(rows)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)
    action = "created" if not out_path.exists() else "exported"
    print(f"  {action} {len(rows)} rows → {out_path}")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Ontop seed CSVs from manufacturing.db")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    parser.add_argument(
        "--include-new",
        action="store_true",
        help="Also create seed CSVs for DB tables that currently have no seed file",
    )
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
        if row[0] not in SQLITE_INTERNAL_TABLES and not row[0].startswith("sqlite_")
    }

    seed_csvs = sorted(args.seeds_dir.glob("*.csv"))
    seeded_tables: set[str] = {p.stem for p in seed_csvs}

    to_refresh = [(p, p.stem) for p in seed_csvs if p.stem in db_tables]
    skipped = [p.name for p in seed_csvs if p.stem not in db_tables]

    new_tables: list[str] = []
    if args.include_new:
        new_tables = sorted(db_tables - seeded_tables)

    if not to_refresh and not new_tables:
        if args.include_new:
            print("export_seeds: no seed CSVs to refresh and no new tables to seed — nothing to do.")
        else:
            print("export_seeds: no seed CSVs match any manufacturing.db table — nothing to do.")
        conn.close()
        return 0

    mode = "[dry-run] " if args.dry_run else ""
    total_rows = 0

    if to_refresh:
        print(f"export_seeds: {mode}refreshing {len(to_refresh)} existing seed CSV(s) from {args.db}")
        for csv_path, table in to_refresh:
            total_rows += export_table(conn, table, csv_path, args.dry_run)

    if new_tables:
        print(f"\nexport_seeds: {mode}creating {len(new_tables)} new seed CSV(s) for "
              f"previously-unseeded tables:")
        for table in new_tables:
            out_path = args.seeds_dir / f"{table}.csv"
            total_rows += export_table(conn, table, out_path, args.dry_run)

    conn.close()

    if skipped:
        print(f"\nexport_seeds: {len(skipped)} CSV(s) skipped (no matching DB table — "
              "synthetic/supplementary data):")
        for name in skipped:
            print(f"  {name}")

    refreshed = len(to_refresh)
    created = len(new_tables)
    action = "would handle" if args.dry_run else "handled"
    parts = []
    if refreshed:
        parts.append(f"refreshed {refreshed} existing CSV(s)")
    if created:
        parts.append(f"created {created} new CSV(s)")
    print(f"\nexport_seeds: {action} {'; '.join(parts)}, {total_rows} total rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
