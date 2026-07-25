---
name: SQLMesh/DuckDB Ontop CI
description: Architecture decisions for replacing manufacturing.db in Ontop CI with DuckDB via SQLMesh
---

# SQLMesh/DuckDB Ontop CI Infrastructure

## The Rule
Ontop CI no longer depends on `manufacturing.db`. `sqlmesh plan --auto-apply` (run from `Utilities/SQLMesh/`) materialises:
- `mfg_data.*` — 19 ERP tables (18 Ontop-mapped + `payables` for 3WM check)
- `mfg_metadata.*` — 9 semantic-layer tables needed by SolderEngine

`make_snapshot()` in `parity_check.py` exports from DuckDB → SQLite at `tools/tmp/manufacturing_snapshot.db`. Both Ontop and SolderEngine read the SQLite snapshot; the DuckDB source is NEVER opened for writing.

**Why:** DuckDB JDBC version mismatch risk (Python duckdb 1.5.3 vs JDBC 1.1.3) and OBDA source SQL uses unqualified table names that work with SQLite JDBC but would need `mfg_data.` qualification for DuckDB JDBC. SQLite snapshot sidesteps both issues. The `.properties` files and `ontop_poc_setup.py` are still updated for manual DuckDB JDBC use.

## How to Apply
- Run `cd Utilities/SQLMesh && sqlmesh plan --auto-apply` before any Ontop parity check
- Seeds are at `Utilities/SQLMesh/seeds/*.csv` (exported from manufacturing.db)
- Models: `models/mfg_data/mfg_data_{table}.sql` and `models/mfg_metadata/mfg_meta_{table}.sql`
- `parity_check.py`: `LIVE_DB = DUCKDB_DB` (alias for backward compat); make_snapshot uses duckdb + pandas to export
- Guard in `sparql_endpoint.py` now checks `pc.DUCKDB_DB` not in runtime props (not `pc.LIVE_DB`)

## Schema in DuckDB
- `mfg_data`: customer_order, customer_order_line, gl_events, gl_finished_goods_inventory, gl_job_cost_detail, gl_raw_materials_inventory, gl_wip_inventory, inventory_transaction, operation, part, payable_line, payables, po_line, purchase_order, receiving, receiving_line, shop_resource, suppliers, work_order
- `mfg_metadata`: ground_truth_table_usage, schema_concept_fields, schema_concepts, schema_intent_concepts, schema_intents, schema_perspective_concepts, sql_graph_authored_edges, sql_graph_edges, sql_graph_nodes

## Pitfalls
- Prior session created conflicting `raw/raw_schema_*.sql` and `staging/stg_schema_*.sql` models — these were removed (they referenced non-existent raw seeds and wrong column names)
- `stg_suppliers.sql` (staging) was also removed; `mfg_data.suppliers` is the canonical ERP model now
- DuckDB JDBC 1.1.3 SHA256: `7bdfe781bb101e2e807397c460c1840955b40aecbaacf2a5e6fc490d80e4f7cd`
- Python duckdb installed as v1.5.3; JDBC is 1.1.3 (latest stable Maven); version gap means manual DuckDB JDBC use needs validation before production use
