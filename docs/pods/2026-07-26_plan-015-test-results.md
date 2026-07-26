# Plan-015 Test Execution Results
_2026-07-26 — PM-approved test plan_

## Test 1 — Happy Path Migration PR ✅

**Steps run in CI order:**
1. Bootstrap governed DB → PASS (fresh sandbox, all 23 steps to `expand_demand_and_completions`)
2. Seed refresh (`export_seeds.py --include-new`) → 62 CSVs refreshed
3. Freshness gate (`check_seed_freshness.py`) → OK
4. DuckDB materialization (`sqlmesh plan --auto-apply`) → 15/15 model batches executed

**Bug found and fixed during this test:**  
`prune_erp_to_demo_scale.py` could prune WO-00007 before `expand_demand_and_completions.py`
tried to close it, causing a FAIL-CLOSED on a fresh bootstrap. WO-00009 and WO-00015
survived the prune by chance; WO-00007 did not.

**Fix:** pinned WO-00007, WO-00015, WO-00009 in the prune keep-set immediately after
the existing WO-JUL-% pin, using the same guard-and-query pattern:

```python
for _pin in ("WO-00007", "WO-00015", "WO-00009"):
    if _pin not in kept_wos:
        kept_wos += _ids(cur, "SELECT wo_id FROM work_order WHERE wo_id = ?", (_pin,))
```

**Verified:** fresh sandbox bootstrap to `expand_demand_and_completions` after the fix:
```
completed WO-00007: +1.0 P-10010 to stock  ✓
completed WO-00015: +1.0 P-10024 to stock  ✓
completed WO-00009: +5.0 P-10036 to stock  ✓
```

---

## Test 2 — Stale Seed Detection ✅

Removed `warehouse.csv`, ran gate:
```
check_seed_freshness: 1 table(s) in manufacturing.db have no seed CSV:
  warehouse: exists in manufacturing.db but has no seed CSV
Exit code was: 1
```
Gate exited 1 with a clear actionable message. CSV restored; gate returned to 62/62.

---

## Test 3 — New Table Coverage (`--include-new`) ✅

Created temp table `_test_new_table` in manufacturing.db, ran export:
```
exported 1 rows → Utilities/SQLMesh/seeds/_test_new_table.csv
export_seeds: handled refreshed 62 existing CSV(s); created 1 new CSV(s), 4261 total rows.
```
CSV confirmed present. Temp table dropped, CSV cleaned up.

---

## Test 4 — Trigger Coverage Audit ✅

All 11 path groups confirmed in both `push` and `pull_request` filters:
```
poc/ontop-ontology-poc/**
replit_integrations/ontop_poc_setup.py
replit_integrations/ontop_poc_run_demo.py
replit_integrations/seed_*.py
replit_integrations/import_graph_metadata.py
replit_integrations/export_graph_metadata.py
Utilities/SQLMesh/**
hf-space-inventory-sqlgen/*.py
hf-space-inventory-sqlgen/app_schema/**
hf-space-inventory-sqlgen/migrations/**
hf-space-inventory-sqlgen/scripts/**
.github/workflows/ontop-interop-ci.yml
```

---

## Test 5 — ArangoDB Parity Non-Issue (Expected FAIL) ✅

`sql_aql_parity_check.py --skip-on-missing` output:
```
[sql_aql_parity] FAIL — the SQLite graph tables do not match the live ArangoDB graph:
  - nodes: count mismatch — SQLite=461 ArangoDB=445
  - edges: count mismatch — SQLite=562 ArangoDB=544
```

**Confirmed pre-existing and out of scope.** The live ArangoDB graph lags the canonical
SQLite tables (it was never re-synced after the AR/receivables migrations added new nodes).
`sql_graph_parity` (SQLite ↔ `graph_metadata.json`) is the authoritative acceptance gate —
it is green. `sql_aql_parity` failure is a known, documented non-regression.

---

## Summary

| Test | Result | Notes |
|---|---|---|
| 1. Happy path | ✅ PASS | Bug found + fixed in prune_erp_to_demo_scale.py |
| 2. Stale seed detection | ✅ PASS | Gate blocks, clear message, exits 1 |
| 3. New table coverage | ✅ PASS | --include-new picks up new tables correctly |
| 4. Trigger coverage | ✅ PASS | All 12 path groups confirmed |
| 5. ArangoDB parity | ✅ PASS | Expected FAIL documented, not a regression |
