"""Structural fingerprints for SME-approved SQL snippets.

A snippet's *structural fingerprint* is the **set of base tables it touches** —
deliberately style-agnostic. Two snippets that read the same base tables are
structurally equivalent even if they rename/reorder CTEs, reorder joins, or
change windowing / bucketing logic. This is the governance gate that lets an SME
rewrite an approved snippet freely, while the Solder Engine still refuses SQL
that reaches into a different set of base tables.

The extraction is a single source of truth (SQLGlot, SQLite dialect, CTE names
excluded) so runtime validation and manifest backfill can never disagree:
``solder_engine._extract_tables_from_sql`` delegates to ``raw_base_tables`` here.

The engine NEVER generates, infers, or mutates SQL — this module only *reads*
and *compares* the SQL an SME already approved.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import sqlglot
from sqlglot import exp

# The dialect the synthetic ground truth is authored in. Fingerprints are always
# extracted in this dialect so extraction is deterministic and reproducible.
FINGERPRINT_DIALECT = "sqlite"

# Stable identifier for the extraction method, stored on each manifest entry so
# an SME can see exactly how the fingerprint was derived.
EXTRACTOR_ID = "sqlglot-sqlite-base-tables-v1"


class FingerprintParseError(ValueError):
    """Raised when SQLGlot cannot parse a snippet, so no fingerprint can be
    established. Callers treat this as a fail-closed condition (condition 4)."""


def raw_base_tables(sql_text: str) -> List[str]:
    """Real base-table names referenced in ``sql_text`` (with duplicates).

    CTE alias names are excluded: SQLGlot surfaces them as ``exp.Table`` nodes in
    the outer SELECT's FROM clause, but they are virtual tables defined within the
    same statement, not schema objects. Names are lowercased so comparison is
    case-insensitive. Parse errors are swallowed here (return best-effort) — the
    strict, fail-closed variant is :func:`base_table_set` with ``strict=True``.
    """
    tables: List[str] = []
    try:
        statements = sqlglot.parse(sql_text, dialect=FINGERPRINT_DIALECT)
    except Exception:
        return tables
    for stmt in statements:
        if stmt is None:
            continue
        cte_names = {
            cte.alias.lower() for cte in stmt.find_all(exp.CTE) if cte.alias
        }
        for node in stmt.find_all(exp.Table):
            name = node.name
            if name and name.lower() not in cte_names:
                tables.append(name.lower())
    return tables


def base_table_set(sql_text: str, strict: bool = False) -> List[str]:
    """The fingerprint: the sorted, de-duplicated set of base tables.

    With ``strict=True`` a SQLGlot parse failure raises
    :class:`FingerprintParseError` instead of degrading to an empty list — used
    by the runtime validator so an unparseable snippet fails closed rather than
    silently fingerprinting as "touches no tables".
    """
    if strict:
        try:
            sqlglot.parse(sql_text, dialect=FINGERPRINT_DIALECT)
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise FingerprintParseError(str(exc)) from exc
    return sorted(set(raw_base_tables(sql_text)))


def validate_fingerprint(
    sql_text: str, approved_base_tables: Optional[List[str]]
) -> Tuple[bool, Optional[str]]:
    """Validate a snippet's *structure* (base-table set) against its approved
    fingerprint. Style — CTE names, join order, windowing/bucketing — is ignored.

    Returns ``(ok, reason)``. ``reason`` is None on success, else a human-readable
    string naming why validation failed (fail-closed condition 4):
      * SQLGlot cannot parse the snippet, OR
      * the current base-table set differs from the approved fingerprint.

    A missing/empty approved fingerprint is NOT a fingerprint failure here — the
    caller decides whether an un-fingerprinted binding may serve (with a warning).
    Parse failure always fails closed: without a parse we cannot establish the
    structure at all.
    """
    try:
        current = base_table_set(sql_text, strict=True)
    except FingerprintParseError as exc:
        return False, f"SQLGlot could not parse the snippet: {exc}"

    if not approved_base_tables:
        return True, None

    approved = sorted(set(t.lower() for t in approved_base_tables))
    if current != approved:
        missing = [t for t in approved if t not in current]
        extra = [t for t in current if t not in approved]
        parts = []
        if extra:
            parts.append(f"unexpected base table(s): {', '.join(extra)}")
        if missing:
            parts.append(f"missing base table(s): {', '.join(missing)}")
        detail = "; ".join(parts) if parts else "base-table set differs"
        return (
            False,
            f"structural fingerprint mismatch ({detail}); "
            f"approved={approved}, actual={current}",
        )
    return True, None


# ---------------------------------------------------------------------------
# Join-edge fingerprint (the join dimension, added to base-table set)
# ---------------------------------------------------------------------------
# The join dimension of the fingerprint is the *set of canonical join edges* a
# snippet uses. A JoinEdge is a column-qualified, alias-free, type-bearing tuple:
#
#     (table_a, column_a, table_b, column_b, join_type)
#
# Canonicalization (so one relationship == one edge regardless of authoring
# style, while optionality stays meaningful):
#   * alias-free  — equi-join columns are resolved back to their owning base
#     table via the FROM/JOIN alias map (the graph knows tables, not query
#     aliases);
#   * endpoints sorted lexicographically for a stable key;
#   * ``join_type`` is expressed RELATIVE to that sorted order — when the sort
#     swaps the two sides, LEFT<->RIGHT are flipped (INNER/FULL/CROSS are
#     symmetric, unaffected), so "A LEFT JOIN B" and the same relationship
#     written "B ... A" collapse to one canonical edge with correct optionality.
#
# Only equi-joins between two BASE tables produce a validated edge. CROSS joins,
# non-equi predicates, joins touching a CTE/subquery, and joins whose columns
# cannot be resolved to a base table are recorded as ``unresolved`` — they warn
# but never block (the base-table set already bounds which tables they reach).
#
# The engine NEVER generates joins from this — it only reads the joins an SME
# already wrote and compares them to the approved fingerprint and the graph.

# The extractor identifier stored on a v2 (join-aware) manifest fingerprint.
EXTRACTOR_ID_V2 = "sqlglot-sqlite-base-tables+join-edges-v2"

JoinEdge = Tuple[str, str, str, str, str]

_SYMMETRIC_JOIN_TYPES = {"INNER", "FULL", "CROSS"}


def _flip_side(join_type: str) -> str:
    """LEFT<->RIGHT; INNER/FULL/CROSS unchanged (they are symmetric)."""
    if join_type == "LEFT":
        return "RIGHT"
    if join_type == "RIGHT":
        return "LEFT"
    return join_type


def _normalize_join_type(side: str, kind: str) -> str:
    side = (side or "").upper()
    kind = (kind or "").upper()
    if "CROSS" in kind:
        return "CROSS"
    if side in ("LEFT", "RIGHT", "FULL"):
        return side
    if "FULL" in kind:
        return "FULL"
    return "INNER"


def _alias_to_table_map(select) -> dict:
    """Map every FROM/JOIN alias AND bare table name in ``select`` to its real
    base-table name (lowercased). Subqueries map to ``None`` (not a base table).
    """
    mapping: dict = {}

    def add(expr):
        if isinstance(expr, exp.Table):
            tname = (expr.name or "").lower()
            if not tname:
                return
            alias = (expr.alias or "").lower()
            if alias:
                mapping[alias] = tname
            mapping.setdefault(tname, tname)
        elif isinstance(expr, exp.Subquery):
            alias = (expr.alias or "").lower()
            if alias:
                mapping[alias] = None

    from_clause = select.args.get("from_")
    if from_clause is not None and from_clause.this is not None:
        add(from_clause.this)
    for join in select.args.get("joins") or []:
        if join.this is not None:
            add(join.this)
    return mapping


def _resolve_column(col, alias_map: dict, cte_names: set):
    """(base_table, column) for a resolvable base-table column, else None.

    Returns None when the column is unqualified, points at a subquery, or
    resolves to a CTE (virtual table) rather than a real base table.
    """
    if not isinstance(col, exp.Column):
        return None
    qualifier = (col.table or "").lower()
    name = (col.name or "").lower()
    if not qualifier or not name:
        return None
    table = alias_map.get(qualifier)
    if table is None:  # unknown alias, or a subquery
        return None
    if table in cte_names:
        return None
    return table, name


def _canonical_edge(
    left: Tuple[str, str],
    right: Tuple[str, str],
    join_type: str,
    joined_table: str,
) -> Optional[JoinEdge]:
    """Sort the two (table, column) endpoints and orient the join type.

    Canonical convention: ``LEFT`` means endpoint **a** (the lexicographically
    smaller one) is the PRESERVED side; ``RIGHT`` means endpoint **b** is
    preserved; INNER/FULL/CROSS are symmetric (no orientation). Optionality is
    derived from ``joined_table`` (the table introduced by the JOIN clause):
    in ``X LEFT JOIN Y`` X is preserved, in ``X RIGHT JOIN Y`` Y is preserved.
    Returns ``None`` when a LEFT/RIGHT join's optionality cannot be oriented
    (neither endpoint is the joined table) so the caller records it unresolved.
    """
    lo, hi = (left, right) if left <= right else (right, left)
    if join_type in _SYMMETRIC_JOIN_TYPES:
        return (lo[0], lo[1], hi[0], hi[1], join_type)

    tables = {left[0], right[0]}
    if joined_table not in tables:
        return None
    others = tables - {joined_table}
    if join_type == "LEFT":
        if len(others) != 1:
            return None
        preserved = next(iter(others))
    else:  # RIGHT
        preserved = joined_table
    canon_type = "LEFT" if lo[0] == preserved else "RIGHT"
    return (lo[0], lo[1], hi[0], hi[1], canon_type)


def join_edges_from_sql(sql_text: str) -> Tuple[List[JoinEdge], List[dict]]:
    """Extract a snippet's canonical join edges and its unresolved joins.

    Returns ``(edges, unresolved)`` where ``edges`` is the sorted, de-duplicated
    list of canonical :data:`JoinEdge` tuples (equi-joins between base tables)
    and ``unresolved`` is a sorted list of ``{"reason", "detail"}`` dicts for
    CROSS / non-equi / CTE / alias-unresolvable joins (warn, never block).
    """
    edges: set = set()
    unresolved: set = set()
    try:
        statements = sqlglot.parse(sql_text, dialect=FINGERPRINT_DIALECT)
    except Exception:
        return [], []

    for stmt in statements:
        if stmt is None:
            continue
        cte_names = {c.alias.lower() for c in stmt.find_all(exp.CTE) if c.alias}
        for select in stmt.find_all(exp.Select):
            alias_map = _alias_to_table_map(select)
            for join in select.args.get("joins") or []:
                side = join.args.get("side") or ""
                kind = join.args.get("kind") or ""
                join_type = _normalize_join_type(side, kind)
                on_expr = join.args.get("on")
                right_name = ""
                if isinstance(join.this, (exp.Table, exp.Subquery)):
                    right_name = (join.this.name or join.this.alias or "").lower()

                if join_type == "CROSS" or on_expr is None:
                    unresolved.add(("cross_join", right_name or "cross"))
                    continue

                eqs = list(on_expr.find_all(exp.EQ))
                if not eqs:
                    unresolved.add(("non_equi", on_expr.sql(dialect=FINGERPRINT_DIALECT)))
                    continue

                joined_table = ""
                if isinstance(join.this, exp.Table):
                    joined_table = (join.this.name or "").lower()

                matched = False
                for eq in eqs:
                    left = _resolve_column(eq.this, alias_map, cte_names)
                    right = _resolve_column(eq.expression, alias_map, cte_names)
                    if left is None or right is None:
                        continue
                    if left[0] == right[0]:  # self-referential column pair, skip
                        continue
                    edge = _canonical_edge(left, right, join_type, joined_table)
                    if edge is None:  # LEFT/RIGHT optionality could not be oriented
                        continue
                    edges.add(edge)
                    matched = True
                if not matched:
                    unresolved.add(("unresolved_columns",
                                    on_expr.sql(dialect=FINGERPRINT_DIALECT)))

    edge_list = sorted(edges)
    unresolved_list = [{"reason": r, "detail": d} for r, d in sorted(unresolved)]
    return edge_list, unresolved_list


def edge_to_dict(edge: JoinEdge) -> dict:
    return {
        "table_a": edge[0], "column_a": edge[1],
        "table_b": edge[2], "column_b": edge[3],
        "join_type": edge[4],
    }


def edge_from_dict(d: dict) -> JoinEdge:
    return (
        (d.get("table_a") or "").lower(),
        (d.get("column_a") or "").lower(),
        (d.get("table_b") or "").lower(),
        (d.get("column_b") or "").lower(),
        (d.get("join_type") or "INNER").upper(),
    )


def validate_join_edges(
    sql_text: str,
    approved_join_edges: Optional[List[dict]],
    graph_join_edges: Optional[set],
) -> Tuple[bool, Optional[str], List[str]]:
    """Validate a snippet's join dimension. Returns ``(ok, reason, warnings)``.

    Two blocking checks (equi-joins only):
      * **drift** — the snippet's current canonical join-edge set (join type
        included) must equal the approved ``approved_join_edges``;
      * **recognition** — every current edge must exist in ``graph_join_edges``
        (the graph's union of known structural relationships).

    ``unresolved`` joins (CROSS / non-equi / CTE / unresolvable) are returned as
    warnings, never as a failure. A parse failure fails closed (no structure).
    """
    warnings: List[str] = []
    try:
        base_table_set(sql_text, strict=True)
    except FingerprintParseError as exc:
        return False, f"SQLGlot could not parse the snippet: {exc}", warnings

    current_edges, unresolved = join_edges_from_sql(sql_text)
    current_set = set(current_edges)
    for u in unresolved:
        warnings.append(
            f"unvalidated {u['reason']} join (warn-only): {u['detail']}"
        )

    approved_set = {edge_from_dict(d) for d in (approved_join_edges or [])}
    if current_set != approved_set:
        added = sorted(current_set - approved_set)
        removed = sorted(approved_set - current_set)
        parts = []
        if added:
            parts.append(f"unexpected join(s): {[edge_to_dict(e) for e in added]}")
        if removed:
            parts.append(f"missing approved join(s): {[edge_to_dict(e) for e in removed]}")
        return False, f"join fingerprint drift ({'; '.join(parts)})", warnings

    if graph_join_edges is not None:
        unknown = sorted(e for e in current_set if e not in graph_join_edges)
        if unknown:
            return (
                False,
                f"join(s) not recognized in graph: {[edge_to_dict(e) for e in unknown]}",
                warnings,
            )

    return True, None, warnings


# ─────────────────────────────────────────────────────────────────────────────
# §4a Topology Gate — scaffolding (docs/plans/graph-aware-fingerprint-plan.md)
#
# Beyond validate_join_edges()'s per-edge set-membership test: two checks that
# reason about the query's join TOPOLOGY as a whole. Both are warn-only —
# neither is wired into validate_join_edges()'s fail-closed return, they are
# separate functions an SME-review surface can call alongside it. Nothing here
# generates or infers a join; both only classify joins an SME already wrote.
# ─────────────────────────────────────────────────────────────────────────────

def load_pk_lookup(conn) -> dict:
    """table_name(lower) -> primary-key column_name(lower), from sql_graph_nodes.

    Returns {} (not a failure) when sql_graph_nodes is absent or empty — callers
    degrade to 'unknown' cardinality rather than raising.
    """
    lookup: dict = {}
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sql_graph_nodes)")}
        if not cols:
            return lookup
        rows = conn.execute(
            "SELECT table_name, column_name FROM sql_graph_nodes "
            "WHERE node_type='column' AND primary_key=1"
        ).fetchall()
    except Exception:
        return lookup
    for table_name, column_name in rows:
        if table_name and column_name:
            lookup[table_name.lower()] = column_name.lower()
    return lookup


def classify_edge_cardinality(edge: JoinEdge, pk_lookup: dict) -> str:
    """Classify a canonical edge's cardinality from A's side using PK lookup.

    Returns one of:
      "one_to_one"   — both column_a and column_b are their table's PK
      "many_to_one"  — column_b is B's PK, column_a is not A's PK (A:B = N:1)
      "one_to_many"  — column_a is A's PK, column_b is not B's PK (A:B = 1:N)
      "unknown"      — neither/PK data unavailable (never asserted as safe)
    """
    table_a, col_a, table_b, col_b, _jt = edge
    a_is_pk = pk_lookup.get(table_a) == col_a
    b_is_pk = pk_lookup.get(table_b) == col_b
    if a_is_pk and b_is_pk:
        return "one_to_one"
    if b_is_pk and not a_is_pk:
        return "many_to_one"
    if a_is_pk and not b_is_pk:
        return "one_to_many"
    return "unknown"


def _table_adjacency(edges) -> dict:
    """table -> set of directly-joined tables, from an iterable of JoinEdge."""
    adj: dict = {}
    for table_a, _ca, table_b, _cb, _jt in edges:
        adj.setdefault(table_a, set()).add(table_b)
        adj.setdefault(table_b, set()).add(table_a)
    return adj


def detect_missing_bridging_entities(
    query_edges: List[JoinEdge], graph_join_edges: set
) -> List[dict]:
    """Classify query edges absent from the graph but bridgeable via one hop.

    For a query edge (A, B) not directly present in ``graph_join_edges`` (a
    table-level check — same table pair, regardless of column, since the
    concern here is topology, not the exact column), look for tables M that
    connect to BOTH A and B in the graph. If any exist, this is a more
    actionable diagnosis than a bare "join not recognized": route the join
    through M instead. Returns [] when every query edge already has a direct
    graph counterpart (including when ``graph_join_edges`` is falsy/None —
    nothing to compare against).
    """
    if not graph_join_edges:
        return []
    adj = _table_adjacency(graph_join_edges)
    graph_table_pairs = {(e[0], e[2]) for e in graph_join_edges} | {
        (e[2], e[0]) for e in graph_join_edges
    }
    findings: List[dict] = []
    seen_pairs = set()
    for table_a, _ca, table_b, _cb, _jt in query_edges:
        if (table_a, table_b) in graph_table_pairs or (table_a, table_b) in seen_pairs:
            continue
        seen_pairs.add((table_a, table_b))
        bridges = sorted((adj.get(table_a, set()) & adj.get(table_b, set())) - {table_a, table_b})
        if bridges:
            findings.append({
                "reason": "missing_bridging_entity",
                "table_a": table_a,
                "table_b": table_b,
                "candidate_bridges": bridges,
                "detail": (
                    f"`{table_a}` and `{table_b}` are not directly connected in "
                    f"the graph, but both connect to {', '.join(f'`{b}`' for b in bridges)} "
                    "— consider routing the join through it instead of a direct edge."
                ),
            })
    return findings


_AGGREGATE_EXPR_TYPES = (exp.Sum, exp.Count, exp.Avg)


def _aggregate_columns(select, alias_map: dict, cte_names: set) -> List[Tuple[str, str]]:
    """(table, column) for every SUM/COUNT/AVG argument in select's projections
    that resolves to a real base-table column. Skips COUNT(*) and anything
    wrapping a subquery/CTE/expression rather than a bare column."""
    found: List[Tuple[str, str]] = []
    for proj in select.expressions or []:
        for agg in proj.find_all(_AGGREGATE_EXPR_TYPES):
            for col in agg.find_all(exp.Column):
                resolved = _resolve_column(col, alias_map, cte_names)
                if resolved:
                    found.append(resolved)
    return found


def detect_fan_out_traps(
    sql_text: str, query_edges: List[JoinEdge], pk_lookup: dict
) -> List[dict]:
    """Warn when a query aggregates across a join graph that fans out.

    Heuristic (deliberately conservative — false negatives over false
    positives): classify every query edge's cardinality via ``pk_lookup``.
    If the query's SELECT list has >= 2 aggregate (SUM/COUNT/AVG) columns AND
    the query's own join graph contains >= 2 distinct ``one_to_many`` edges
    (from either endpoint's perspective — a header joined to two independent
    child tables is the classic double-count shape), flag ONE finding
    summarizing the risk for SME review. This does not attempt to prove
    double-counting occurred (that needs execution, which this module never
    does) — it surfaces the shape that makes it possible, same spirit as the
    "Three-Way Match Coverage" query's own hand-written 1:1-linkage caveat.
    """
    if not query_edges:
        return []
    try:
        statements = sqlglot.parse(sql_text, dialect=FINGERPRINT_DIALECT)
    except Exception:
        return []

    # A relationship is fan-out-relevant regardless of which side the
    # canonical (alphabetically-sorted) edge happens to put first — the same
    # real-world "many rows on one side" fact classifies as "one_to_many" or
    # "many_to_one" purely depending on table name ordering. Both count here.
    one_to_many_edges = [
        e for e in query_edges
        if classify_edge_cardinality(e, pk_lookup) in ("one_to_many", "many_to_one")
    ]
    if len(one_to_many_edges) < 2:
        return []

    agg_tables: set = set()
    for stmt in statements:
        if stmt is None:
            continue
        cte_names = {c.alias.lower() for c in stmt.find_all(exp.CTE) if c.alias}
        for select in stmt.find_all(exp.Select):
            alias_map = _alias_to_table_map(select)
            for table, _col in _aggregate_columns(select, alias_map, cte_names):
                agg_tables.add(table)

    if len(agg_tables) < 2:
        # A single aggregated table can still fan out through chained
        # one-to-many hops, but scaffolding stays conservative: only flag
        # the unambiguous "aggregates diverge across branches" shape for now.
        return []

    return [{
        "reason": "fan_out_trap",
        "one_to_many_edges": [edge_to_dict(e) for e in sorted(one_to_many_edges)],
        "aggregated_tables": sorted(agg_tables),
        "detail": (
            f"Query aggregates columns from {len(agg_tables)} different tables "
            f"({', '.join(f'`{t}`' for t in sorted(agg_tables))}) across "
            f"{len(one_to_many_edges)} one-to-many join(s) — verify each branch "
            "is pre-aggregated/deduplicated before the join, or the SUM/COUNT/AVG "
            "may double-count rows fanned out by the other branch."
        ),
    }]
