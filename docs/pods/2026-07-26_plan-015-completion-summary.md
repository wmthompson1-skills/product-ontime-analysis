# Plan-015 Ontop CI Infrastructure — Completion Summary
_2026-07-26_

## What was built

**Goal:** Give the Ontop interoperability CI workflow a reliable, self-healing pipeline so that any change to the database — migrations, scripts, seeders, schema SQL, or SQLMesh models — automatically triggers a full seed refresh and parity check before DuckDB materialization runs.

---

## Tasks completed (in merge order)

| Task | What it delivered |
|---|---|
| #300 | SQLMesh/DuckDB migration — 28 seed models, DuckDB materialization, multi-dialect parity scripts |
| #301 | Bootstrap step wired into CI workflow |
| #302 | Two bug fixes: FK PRAGMA fallback + missing `schema_perspectives`; all 8 parity checks green |
| #303 | Seed freshness gate added to `post-merge.sh` (detects stale/missing seeds) |
| #304 | DuckDB JDBC 1.1.3 ↔ Python duckdb 1.5.3 compatibility verified |
| #305 | Timeouts added to CI job and bootstrap step |
| #308 | Reverse coverage check added — gate fails if any DB table has no seed CSV |
| **Seed fix** | 30 missing seed CSVs exported; gate confirmed at 62/62 |
| #309 | CI step order corrected: bootstrap → seed refresh → freshness gate → DuckDB materialization |
| #314 | CI now triggers on `hf-space-inventory-sqlgen/migrations/**` changes |
| #315 | CI now triggers on any file in `hf-space-inventory-sqlgen/scripts/**` |
| #316 | CI now triggers on top-level `hf-space-inventory-sqlgen/*.py` and `replit_integrations/seed_*.py` |
| #317 | CI now triggers on `hf-space-inventory-sqlgen/app_schema/**` (DDL/schema SQL) |

---

## Current gate status (all green)

```
post-merge: OK
22 passed, 0 failed
check_seed_freshness: OK — 62 seed CSV(s) match manufacturing.db
```

---

## What the PM should plan testing for

1. **Happy path — migration PR**: add a new migration file, confirm CI triggers, seeds refresh automatically, freshness gate passes, DuckDB materializes cleanly.

2. **Stale seed detection**: modify a seeder script without re-exporting seeds, confirm the freshness gate exits 1 and blocks the pipeline.

3. **New table coverage**: add a migration that creates a new table, confirm `--include-new` picks it up and the gate still passes.

4. **Trigger coverage**: make a change to each watched path group (`scripts/**`, `app_schema/**`, `*.py`, `seed_*.py`, `migrations/**`) and verify the CI workflow fires for each.

5. **Pre-existing known non-issue**: `sql_aql_parity` will show FAIL/SKIP against live ArangoDB — this is documented and out of scope; `sql_graph_parity` (SQLite ↔ JSON) is the authoritative acceptance gate.
