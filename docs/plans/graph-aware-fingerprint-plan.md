# Plan: Graph-aware structural fingerprint (base tables → base tables + join edges)

## Objective
Make the graph an **active join validator** without ever generating SQL. Extend the
structural fingerprint from "the set of base tables a snippet touches" to also cover
"the set of join edges a snippet uses", and fail closed when a snippet joins two
tables via a relationship the graph does not recognize. Feed the extractor's
discovered join lineage back into the graph so the recognition check is correct
(not over-rejecting).

## Invariant guardrail
SME-approved SQL stays the sole source of joins. The graph never supplies or infers
a join — it only **recognizes / rejects** the joins already written in approved SQL.

---

## 1. Join-edge canonical form (the unit being fingerprinted)
Each equi-join in a snippet reduces to a **column-qualified, alias-free,
type-bearing** tuple:

    JoinEdge = (table_a, column_a, table_b, column_b, join_type)

Derivation from the extractor's `JoinRelationship`:
- Resolve `left_key`/`right_key` back to their owning **table** using the alias→table
  map built from the FROM/JOIN clause (aliases are query-local, the graph knows tables).
- Canonical order: sort the two `(table, column)` endpoints lexicographically for a
  stable key, AND express `join_type` **relative to that order** — when the sort swaps
  the sides, flip `LEFT`↔`RIGHT` (INNER/FULL/CROSS are symmetric, unaffected). This
  yields one canonical edge per relationship while preserving optionality semantics.
- Lowercase table + column names (matches base-table casing rule).

Design decisions (per user, revised):
- **Column-qualified, not table-pair** — fixes the "lossy dedup" concern; two
  different relationships between the same table pair are distinct edges.
- **Join TYPE IS part of the validated key** — a LEFT vs INNER join is ontological
  meaning (optional vs mandatory participation), not cosmetic style. So an SME's
  INNER→LEFT rewrite DOES change the fingerprint and requires re-approval. (DECIDED)
- **Equi-joins only for validation** — only `exp.EQ` column=column predicates yield a
  validated JoinEdge. Non-equi / CROSS / alias-unresolvable joins go into
  `unresolved_joins` (warn, never block — see §4).

## 2. Expanded fingerprint stored on the manifest entry
```jsonc
"structural_fingerprint": {
  "base_tables": ["customer_order_line", "part"],            // unchanged
  "join_edges": [                                            // NEW (sorted, deduped)
    {"table_a":"customer_order_line","column_a":"part_id",
     "table_b":"part","column_b":"part_id","join_type":"INNER"}
  ],
  "unresolved_joins": [                                      // NEW: cross/non-equi/unresolved
    // {"reason":"cross_join","tables":["a","b"]}
  ],
  "extractor_id": "sqlglot-sqlite-base-tables+join-edges-v2" // bumped v1 -> v2
}
```
`extractor_id` is bumped v1→v2 for EVERY entry in the same migration — **no grace
period, hard cutover** (§6). There is no v1/v2 coexistence at runtime: after the
migration every entry carries a join dimension and the join gate enforces for all.

## 3. Graph adjacency the fingerprint validates against
Build a cached set `graph_join_edges` in the SAME canonical form, from the graph's
`references` edges: each edge gives child (`_from` node → table:col) and parent
(`references_table` / `references_column`).

**Verified baseline (2026-09-21, live):** the canonical pipeline —
`export_graph_metadata.py` → `sql_graph_nodes`/`sql_graph_edges` → `graph_metadata.json`
→ `sql_graph_parity_check.py` (authoritative) → `load_canonical_to_arango.py` — was run
end-to-end against the current schema and confirmed consistent across SQLite, the
frozen JSON, and ArangoDB's `manufacturing_graph_node`/`manufacturing_graph_edge`:
**34 physical tables, 340 columns, 71 `references` (declared-FK) edges.** This
supersedes the earlier "39 FK-derived edges" figure, which predated both the Wave-4
traceability-spine tables and the `references_edge_key()` bug fix (the key format used
`->`, an illegal ArangoDB `_key` character, so most declared-FK edges silently failed
to sync before that fix — the true declared-FK count was always closer to 71 than 39).
`graph_sync.py`'s live-sync path independently reproduces the same 34/340/71 figures,
confirming the two pipelines agree.

**Coupling (why write-back is required, not optional):** 71 declared-FK edges is
already an undercount relative to what approved snippets actually join on — e.g. the
"Three-Way Match Coverage" query (`payables_threewaymatchcoverage_20260708_000005`)
joins `po_line → purchase_order` (declared FK, present) but also
`purchase_order → suppliers`, `po_line → receiving_line`, `receiving_line → receiving`,
`receiving_line → payable_line`, `payable_line → payables` — several of which are not
declared foreign keys in `schema_sqlite.sql` today. So the extractor's discovered
`join_edges` are upserted back into the graph (idempotent UPSERT, mirroring existing
duplicate-edge protection) as **first-class STRUCTURAL edges** — same
`edge_family = structural` layer as `references`, part of ONE ontology mosaic (NOT a
segregated `join_lineage` provenance silo). Provenance is kept as an edge **property**
(`origin`: `fk_declared` | `sql_observed`) plus `join_type`, so we never falsely assert
referential integrity we don't have, while still unifying joins and FKs into one
structural graph. `graph_join_edges` = every structural relationship edge (FK
references + observed joins) in the canonical §1 form. Without write-back a correct
snippet joining on a non-FK column would wrongly fail closed.

## 4. New validation function (additive, alongside validate_fingerprint)
    validate_join_edges(sql_text, approved_join_edges, graph_join_edges) -> (ok, reason, warnings)

Two blocking checks (equi-joins only):
- (a) **Drift**: snippet's current equi-join-edge set (incl. `join_type`) must equal
  the manifest's approved `join_edges` — adding/removing a join, or changing its type,
  is a structural change needing re-approval.
- (b) **Recognition**: every snippet equi-join edge must exist in `graph_join_edges`;
  unrecognized → fail closed. This is the "graph as active validator" chosen.

Non-blocking:
- `unresolved_joins` (CROSS / non-equi / alias-unresolvable) → **warn, never block**.
  These are legitimate in approved SQL (e.g. time-phasing range joins). The base-table
  fingerprint still bounds which tables they can reach, so the invariant holds; the
  warning surfaces them for SME visibility.

### 4a. Beyond single-edge membership: topology-level checks
Recognition (check (b) above) is a per-edge set-membership test — it answers "does this exact
join exist somewhere in the graph?" but not "does this *set* of joins, taken together,
form a topologically sound path?" Two failure modes slip through pure edge-membership
and need their own classification, both derived from `graph_join_edges` treated as a
graph `G` (nodes = tables, edges = join relationships, each edge annotated with a
cardinality hint — see below):

**Missing bridging entity.** A query edge `(A, B)` that is *not* in `G` directly, but
`A` and `B` *are* connected in `G` via exactly one intermediate table `M` (a 2-hop path
`A–M–B` exists, no direct `A–B` edge does), gets classified as `missing_bridging_entity`
rather than a generic `join_not_in_graph`. This is a more actionable diagnosis: the fix
is "route the join through `M`," not "this join is wrong." Worked counter-example from
the live schema: a hypothetical query joining `po_line` directly to `payables` would
fail this way — the real path is `po_line → receiving_line → payable_line → payables`;
skipping `receiving_line`/`payable_line` bypasses the receipt/voucher legs the
three-way-match logic depends on. (The real "Three-Way Match Coverage" query gets this
right — see §3's join list — which is exactly why it should validate clean once this
check exists.)

**Fan-out trap.** Classify each edge in `G` by cardinality using the schema's declared
keys: an edge `(A.col_a, B.col_b)` is `many_to_one` in the `A → B` direction when
`col_b` is `B`'s primary key (the reverse direction is `one_to_many`). A fan-out risk
exists when a query aggregates (`SUM`/`COUNT`/`AVG` — not wrapping a `GROUP BY` grain
column) a column anchored at or before the query's grain root, while the join graph
reaches that aggregate's table through a *different* `one_to_many` branch than another
aggregate in the same `SELECT`, or through more than one `one_to_many` hop from the
grain root without an intervening `DISTINCT`/pre-aggregation. Concretely: joining a
header table to two independent child tables (e.g. `work_order` to both
`labor_ticket` and `material_issue`) and summing a column from each in the same
`SELECT` double/triple-counts unless each branch is pre-aggregated before the join.
This is exactly the risk the "Three-Way Match Coverage" query's own SME header comment
already reasons about by hand: *"In the synthetic twin the linkage is 1:1 (no PO line
has multiple receipt lines, no receipt line has multiple voucher lines), so row totals
equal line totals; the flat grain stays honest if that ever changes."* — i.e. the SME
already identified this exact fan-out trap and documented the (currently-true, not
schema-enforced) 1:1 assumption that keeps the flat join safe. Formalizing this check
turns that prose caveat into something the validator can actually re-verify.

Both checks are **warn, never block** (like `unresolved_joins` in §4 above) — they
surface a specific, actionable classification for SME review rather than adding a new
fail-closed condition on top of Recognition. Promoting either to blocking is a
follow-on decision, not part of this plan.

## 5. Wiring into assemble_query / dispatch (extends fail-closed condition 4)
Condition 4 today = base-table mismatch. Extend it to also fire on join-edge drift or
unrecognized join, reusing the fail-closed hard-refusal path hardened this session.
New `fail_closed_condition` values: `join_fingerprint_drift`, `join_not_in_graph`.
Enforced for ALL entries immediately (hard cutover — no v1 skip). `unresolved_joins`
attach as warnings on the served result, not as fail-closed conditions.

## 6. Backfill / migration (hard cutover, ends fail-closed)
- Re-fingerprint EVERY manifest entry in one pass: parse → extract canonical join
  edges (with `join_type`) → write `join_edges` + `unresolved_joins`, bump
  `extractor_id` to v2.
- Graph write-back: union of all discovered equi-join edges → idempotent UPSERT as
  structural edges (`origin=sql_observed`) into the references/structural layer;
  re-export bumps SCHEMA_VERSION; parity + coverage gates re-run in post-merge.
- **Migration ends with a fail-closed validation** (same pattern as
  `validate_planning_inputs`): assert every approved snippet's equi-join edges are now
  recognized in `graph_join_edges`. If any are not, the migration ABORTS loudly rather
  than shipping a gate that would refuse on boot. This is what makes an immediate
  hard cutover safe.
- New tests: canonical-form normalization (incl. LEFT↔RIGHT flip on endpoint swap),
  drift (incl. type change), recognition, unresolved-join warn-not-block, alias→table
  resolution, one-canonical-edge-per-relationship, migration completeness assertion.

## Decisions locked (from user review)
1. Join TYPE **is** part of the validated key (optionality is ontology).
2. Discovered joins fold into the **structural** layer (one mosaic), provenance as a
   property — not a distinct `join_lineage` edge type.
3. **No grace period** — hard cutover; every entry re-fingerprinted + enforced at once.
4. Non-equi/CROSS joins **warn, never block**.

5. Directionality: canonicalize by sorting endpoints lexicographically and flipping
   `LEFT`↔`RIGHT` when the sort swaps sides (INNER/FULL/CROSS symmetric) — one
   canonical edge per relationship, with join type + order specified on it.
   **Confirmed live (2026-09-21):** observed directly in the Ontology Mosaic's
   "SQL Semantics" lens for the "Three-Way Match Coverage" query. The raw SQL writes
   `receiving_line rl LEFT JOIN payable_line pyl ON pyl.receipt_line_id = rl.receipt_line_id`
   and `receiving r LEFT JOIN receiving_line rl` (i.e. `receiving_line` keeps all rows
   in both). The extracted canonical join-edge table renders these as
   `payable_line.receipt_line_id RIGHT receiving_line.receipt_line_id` and
   `receiving.receipt_id RIGHT receiving_line.receipt_id` — `payable_line` sorts before
   `receiving_line` and `receiving` sorts before `receiving_line` alphabetically, so the
   endpoint swap correctly flips `LEFT`→`RIGHT` in both cases, preserving "keep all
   `receiving_line` rows" under the swapped order. This is `structural_fingerprint.py`'s
   `join_edges_from_sql()` (via `view_ontology_extractor.py`), already live in
   production — this part of §1 is not aspirational, it is running code today.

## Status
Schema fully specified and locked. No open questions on the design.

**Revised status (2026-09-21) — correcting the previous revision, which understated
what exists:** §1 through §6 are **already implemented and live**, not merely
"designed":
- §1/§2: `structural_fingerprint.py` has the full v2 extractor
  (`join_edges_from_sql()`, canonical `JoinEdge` tuples, LEFT/RIGHT normalization) and
  the manifest already stores `join_edges`/`unresolved_joins`/`extractor:
  "sqlglot-sqlite-base-tables+join-edges-v2"` per entry — confirmed **20 of 56**
  approved snippets are join-aware today (the rest predate the backfill or were added
  since).
- §3: `SolderEngine._graph_join_edges()` (`solder_engine.py:181`) already reads
  `fk_declared`-equivalent edges from `sql_graph_edges` for recognition, AND already
  has a code path for `sql_observed` edges (an `origin`/`join_type` column pair) —
  **but** the live `sql_graph_edges` table in this environment does not yet have those
  columns (`sqlite3.OperationalError: no such column: origin`, checked directly). So
  today recognition only checks against the 71 declared-FK edges from §3's verified
  baseline; the write-back **migration** that would populate `sql_observed` rows is
  the one piece of §3 not yet run/built as a script, even though the read side already
  supports it.
- §4: `validate_join_edges(sql_text, approved_join_edges, graph_join_edges)` is fully
  implemented (`structural_fingerprint.py:346`) — both blocking checks (drift,
  recognition) exactly as specified.
- §5: wired into the real dispatch path — `solder_engine.py` calls
  `validate_join_edges()` at two call sites (~line 464, ~line 935/1026), returning
  `fail_condition: "join_validation_failed"` on failure, exactly matching the planned
  `fail_closed_condition` extension.
- §6: `migrations/backfill_structural_fingerprints.py` exists and has run (hence the
  20/56 figure above) — though not against every snippet, so it is not yet the "hard
  cutover, no v1/v2 coexistence" the plan called for; some snippets still serve without
  join validation.

**§4a status (2026-09-21) — scaffolded and verified:** `structural_fingerprint.py`
now has `load_pk_lookup()`, `classify_edge_cardinality()`, `_table_adjacency()`,
`detect_missing_bridging_entities()`, and `detect_fan_out_traps()`. Per the "beyond
single-edge membership" framing above, these are deliberately **not** merged into
`validate_join_edges()` — they are standalone, warn-only functions an SME-review
surface calls alongside it, exactly as §4a specifies ("Both checks are warn, never
block... Promoting either to blocking is a follow-on decision, not part of this
plan"). Verified against two cases:
- The real "Three-Way Match Coverage" query (§3's baseline) — both checks return `[]`
  (clean), confirming the check doesn't false-positive on approved, graph-recognized SQL.
- Constructed bad queries — `po_line` joined directly to `payables` (skipping
  `receiving_line`/`payable_line`) correctly classifies as `missing_bridging_entity`
  with both real bridge tables listed as candidates; `work_order` joined to both
  `labor_ticket` and `material_issue` with a `SUM()` on each correctly classifies as
  `fan_out_trap`. The latter required one fix: `classify_edge_cardinality()`'s
  `one_to_many` vs `many_to_one` label depends on which table name sorts first in the
  canonical edge tuple, so `detect_fan_out_traps()` treats both labels as
  fan-out-relevant (the underlying "many rows on one side" fact is identical either way).

**Not yet done:** wiring §4a's output into an actual SME-facing surface (the Ontology
Mosaic UI has no lens for these findings yet — today they're only reachable by calling
the functions directly), closing the join-aware backfill gap (36 of 56 snippets predate
v2), and the `sql_observed` write-back migration §3 already has a read path for.
