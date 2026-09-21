---
name: Syncing canonical graph into the live ArangoDB
description: Which script pushes canonical graph nodes/edges into the live graph, and how to keep committed parity reports honest.
---

# Syncing the canonical graph into live ArangoDB

When you add/remove nodes or edges in the canonical `graph_metadata.json` (via the
exporter + a SCHEMA_VERSION bump), the live ArangoDB graph does NOT update itself.
Push the canonical graph into the two canonical live collections
(`manufacturing_graph_node` / `manufacturing_graph_edge`) with
**`replit_integrations/safe_load_canonical_to_arango.py`** (supports `--dry-run`;
idempotent truncate-then-load, `on_duplicate=replace`; rewrites the host to the DB
port 8529).

**Do not call `load_canonical_to_arango.py` directly.** A real extract from an
external SQL Server (1,106+ `table_x5F_Live_x2E_dbo_x2E_*` nodes, referenced by
the `ATOMIC_FK` edge collection) physically coexists INSIDE
`manufacturing_graph_node` / `manufacturing_graph_edge` themselves — the two
collections the loader truncates. That data is not derived from anything in
this repo and is unrecoverable if wiped (confirmed 2026-09-21: a bare run would
have destroyed it). `safe_load_canonical_to_arango.py` backs up both
collections first, runs the same truncate-then-load, then additively restores
every non-canonical doc (`on_duplicate="ignore"`) so the Live.dbo partition and
anything else outside the canonical set survives.

**Why:** `graph_sync.py` is the WRONG tool for this — it only syncs the *bridge*
collections (Perspective_Intents / Perspective_Concepts / etc.), not the canonical
node/edge collections. Using it leaves the live graph stale, so the SQL-vs-AQL
parity gate (`sql_aql_parity_check.py --skip-on-missing`) fails with a
node/edge count mismatch.

**How to apply:** After any canonical graph change: (1) re-export + freeze
`graph_metadata.v{N}.json`; (2) run `load_canonical_to_arango.py --dry-run` then
apply; (3) re-verify with `sql_aql_parity_check.py`.

## Parity reports are committed artifacts — regenerate them, don't just re-check
`sql_aql_parity_report.txt` and `sql_graph_parity_report.txt` are committed. A bare
re-run of the checker prints OK to stdout but does NOT rewrite the file unless you
pass `--report-file <path>` (and `--csv-dir <dir>` for the columnar CSVs). If you
fix a mismatch and re-check without those flags, the committed report still records
the OLD failure state. Always regenerate with `--report-file`/`--csv-dir` (or run
the full `scripts/post-merge.sh`) after fixing parity.
