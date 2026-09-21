Script works and gives real numbers: of the 74 `legacy_bare` nodes in `manufacturing_graph_node`, 59 are still referenced by at least one edge (must stay until those edges are also handled) and **15 are genuinely orphaned** — the actual safe-to-prune candidates, listed by ID. Also confirms `dbl_colon` never landed in `manufacturing_graph_node` itself (0 found) — that convention lives only in the separate `tables`/`columns`/`contains` collections, as suspected.

Summary:
- **Query safety**: confirmed safe — the gated/canonical write path never touches Arango for resolution, and the best-effort live mirror does exact-key lookups only, so no silent cross-generation ambiguity is possible. Its real limitation (can't resolve 6-slot-canonical or double-colon labels) fails loud, not silently wrong.
- **Future prune pass**: `hf-space-inventory-sqlgen/scripts/audit_legacy_graph_keys.py` is ready — read-only, classifies all four key schemes, and separates "referenced, don't touch" from "orphaned, safe to drop" so nothing gets pruned blind.

Nothing pruned, nothing written — purely diagnostic, as requested.