"""
SolderEngine - Semantic Transpilation Engine
=============================================
Takes APPROVED SQL snippets from the Reviewer Manifest and assembles
final executable queries using SQLGlot AST manipulation, guided by
perspective elevation weights from the semantic graph.

The Solder Pattern:
  1. Path Resolution: Find APPROVED binding for requested Concept
  2. Elevation Check: Use ELEVATES/SUPPRESSES weights to select snippet
  3. AST Manipulation: Parse SQL via SQLGlot, rename aliases, handle joins
  4. Final Generation: Output sanitized, optimized SQL for target dialect
"""

import os
import json
import sqlite3
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

import re
from collections import Counter

import structural_fingerprint as sfp

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "app_schema")
SQLITE_DB_PATH = os.path.join(SCHEMA_DIR, "manufacturing.db")
GROUND_TRUTH_DIR = os.path.join(SCHEMA_DIR, "ground_truth")
MANIFEST_PATH = os.path.join(GROUND_TRUTH_DIR, "reviewer_manifest.json")
QUERIES_DIR = os.path.join(SCHEMA_DIR, "queries")
QUERIES_INDEX_PATH = os.path.join(QUERIES_DIR, "index.json")

_QUERY_HEADER_RE = re.compile(r"(?m)^--\s*={10,}.*?$")
_QUERY_NAME_RE   = re.compile(r"--\s*Query\s+\d+\s*[—\-]+\s*(\S+)", re.IGNORECASE)


@dataclass
class SolderBinding:
    binding_key: str
    perspective: str
    concept_anchor: str
    logic_type: str
    sql_snippet_path: str
    sme_justification: str
    validation_status: str
    sql_text: str = ""
    # Structural fingerprint: the SME-approved set of base tables this snippet
    # touches. Empty when the manifest entry has not been fingerprinted yet.
    base_tables: List[str] = field(default_factory=list)
    # Graph-aware fingerprint (v2): the SME-approved canonical join edges this
    # snippet uses. ``join_aware`` is True only when the manifest entry carries a
    # v2 (join-aware) fingerprint; a pre-v2 entry serves without join validation.
    join_edges: List[dict] = field(default_factory=list)
    join_aware: bool = False


@dataclass
class ElevationEdge:
    intent_name: str
    concept_name: str
    weight: float
    explanation: str
    perspective_name: str = ""
    table_name: str = ""
    field_name: str = ""
    context_hint: str = ""


@dataclass
class SolderResult:
    original_sql: str
    soldered_sql: str
    dialect: str
    binding_key: str
    perspective: str
    concept: str
    logic_type: str
    elevation_weight: float
    ast_operations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class MetricAssemblyError(ValueError):
    """Raised when a metric cannot be assembled into SQL under the fail-closed
    guardrail: a missing computation_template, a missing/extra variable binding,
    a conflicting binding (one variable resolving to two different physical
    columns), or an unresolvable join between the tables a metric spans."""


@dataclass
class MetricBinding:
    variable_name: str
    table_name: str
    column_name: str


@dataclass
class MetricResult:
    metric: str
    template: str            # dialect-agnostic template with {variable} placeholders
    expression: str          # template with {vars} -> real table.column (SQLite form)
    sql: str                 # full runnable SELECT in the target dialect
    dialect: str
    bindings: List[MetricBinding] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    join_path: List[str] = field(default_factory=list)
    perspectives: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class SolderEngine:

    def __init__(self, db_path: str = None, manifest_path: str = None):
        self.db_path = db_path or SQLITE_DB_PATH
        self.manifest_path = manifest_path or MANIFEST_PATH

    def load_approved_bindings(self) -> List[SolderBinding]:
        if not os.path.exists(self.manifest_path):
            return []

        try:
            with open(self.manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception:
            return []

        snippets = manifest.get("approved_snippets", {})
        manifest_dir = os.path.dirname(os.path.abspath(self.manifest_path))

        bindings = []
        for binding_key, entry in snippets.items():
            if entry.get("validation_status") != "APPROVED":
                continue

            sql_text = ""
            sql_path = entry.get("file_path", "")
            resolved_path = sql_path
            if sql_path and not os.path.isabs(sql_path):
                hf_dir = os.path.dirname(os.path.abspath(__file__))
                candidate = os.path.join(manifest_dir, os.path.basename(sql_path))
                if os.path.exists(candidate):
                    resolved_path = candidate
                elif os.path.exists(os.path.join(hf_dir, sql_path)):
                    resolved_path = os.path.join(hf_dir, sql_path)
                elif not os.path.exists(sql_path):
                    repo_root = os.path.dirname(hf_dir)
                    candidate2 = os.path.join(repo_root, sql_path)
                    if os.path.exists(candidate2):
                        resolved_path = candidate2
            if resolved_path and os.path.exists(resolved_path):
                try:
                    with open(resolved_path, "r") as f:
                        sql_text = f.read().strip()
                except Exception:
                    pass

            fingerprint = entry.get("structural_fingerprint") or {}
            base_tables = list(fingerprint.get("base_tables") or [])
            join_edges = list(fingerprint.get("join_edges") or [])
            join_aware = fingerprint.get("extractor") == sfp.EXTRACTOR_ID_V2

            bindings.append(SolderBinding(
                binding_key=binding_key,
                perspective=entry.get("perspective", ""),
                concept_anchor=entry.get("concept_anchor", ""),
                logic_type=entry.get("logic_type", "DIRECT"),
                sql_snippet_path=sql_path,
                sme_justification=entry.get("sme_justification", ""),
                validation_status=entry.get("validation_status", ""),
                sql_text=sql_text,
                base_tables=base_tables,
                join_edges=join_edges,
                join_aware=join_aware,
            ))

        return bindings

    def _graph_join_edges(self) -> Optional[set]:
        """Union of known structural relationships from the SQL graph edge tables.

        Reads the ``references``-family edges from ``sql_graph_edges`` and returns
        them as a set of canonical :data:`structural_fingerprint.JoinEdge` tuples
        — the graph's recognition set for join validation:

          * ``fk_declared`` edges (and any pre-v22 table with no ``origin`` column)
            contribute the INNER-joinable relationship — endpoints sorted, type
            ``INNER`` (an FK always supports an inner join across it);
          * ``sql_observed`` edges contribute their stored canonical join edge
            (``_from`` is already the lexicographically smaller endpoint and the
            join type is oriented relative to it — see structural_fingerprint).

        Returns ``None`` when the edge table is absent so recognition is SKIPPED
        (drift is still enforced) rather than locking the engine out of a DB that
        has not yet materialized the graph tables. Read-only.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return None
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sql_graph_edges)")}
            if not cols:
                return None
            has_origin = "origin" in cols and "join_type" in cols
            select = (
                "SELECT _from, references_table, references_column"
                + (", origin, join_type" if has_origin else "")
                + " FROM sql_graph_edges WHERE edge_type = 'references'"
            )
            rows = conn.execute(select).fetchall()
        except sqlite3.Error:
            return None
        finally:
            conn.close()

        edges: set = set()
        for r in rows:
            tail = (r["_from"] or "").split("/", 1)[-1]
            parts = tail.split(":")
            if len(parts) < 2:
                continue
            ft, fc = parts[0].lower(), parts[1].lower()
            tt = (r["references_table"] or "").lower()
            tc = (r["references_column"] or "").lower()
            if not (ft and fc and tt and tc):
                continue
            origin = r["origin"] if has_origin else None
            if origin == "sql_observed":
                jt = (r["join_type"] or "INNER").upper() if has_origin else "INNER"
                edges.add((ft, fc, tt, tc, jt))
            else:  # fk_declared / legacy: an FK implies an INNER-joinable pair
                (a, b) = sorted([(ft, fc), (tt, tc)])
                edges.add((a[0], a[1], b[0], b[1], "INNER"))
        return edges

    def validate_temporal_contract(
        self, sql_text: str, binding_key: str = ""
    ) -> Tuple[bool, str, List[str]]:
        """Passive fail-closed check that every tokenized (:named) parameter
        baked into a snippet is a recognized member of its Temporal Parameter
        Contract.

        A snippet may carry named placeholders (``:start_date``, ``:end_date``,
        ``:supplier_id``). Each one must appear in the machine-readable contract
        produced by :mod:`view_ontology_extractor` — i.e. it must sit in a
        comparison that binds it to a physical column and classifies it
        (``horizon`` / ``netting`` / ``filter``). A placeholder the extractor
        cannot resolve to a contract entry (e.g. one used in ``LIMIT``, a
        function call, or an ``IN`` / ``BETWEEN`` list) is unrecognized drift and
        is never served (fail closed).

        Pure SQLGlot AST analysis — nothing is executed and no value is bound.
        Snippets with no placeholders pass trivially. Returns
        ``(ok, reason, warnings)``.
        """
        text = (sql_text or "").strip()
        if not text:
            return True, "", []
        try:
            ast = sqlglot.parse_one(text, read="sqlite")
        except Exception:
            # A parse failure is already caught (and failed closed) by the
            # parse / fingerprint guards on the serve path; do not double-report.
            return True, "", []

        # Mirror the extractor's recognition rule (view_ontology_extractor.
        # _extract_temporal_parameters), but check it per OCCURRENCE — not per
        # token name — so the same token used once in a valid guard and again in
        # an invalid context (e.g. `LIMIT :end_date`) is still caught. The
        # SME-approved idiom is `(:param IS NULL OR <column> <op> :param)`, so a
        # placeholder occurrence is valid in exactly two shapes:
        #   1. a comparison (EQ / NEQ / range) that carries a physical column, or
        #   2. the `:param IS NULL` guard arm (a bare null check — the extractor
        #      skips it, and it carries no injection / semantic risk).
        # Anything else (LIMIT, function arg, `:a = :b` with no column) is drift.
        range_ops = (exp.LT, exp.LTE, exp.GT, exp.GTE)
        comparison_types = (exp.EQ, exp.NEQ) + range_ops

        def _occurrence_reason(placeholder) -> Optional[str]:
            """Return ``None`` when this occurrence is a valid contract member,
            otherwise a short, SME-actionable reason naming the exact context
            that broke the contract (e.g. ``used in a LIMIT clause``). Mirrors
            the accept rules in ``_occurrence_is_valid`` — the accept outcomes
            are byte-identical; only the reject path is enriched with a reason.
            """
            node = placeholder.parent
            while node is not None:
                if isinstance(node, comparison_types):
                    # The column-bearing arm must exist to bind the parameter.
                    if list(node.find_all(exp.Column)):
                        return None
                    return (
                        "compared to another parameter with no physical column "
                        "to bind it to"
                    )
                if isinstance(node, exp.Is):
                    # Accept ONLY the exact positive `:param IS NULL` guard arm:
                    # the right side must be NULL and it must not be negated
                    # (`IS NOT NULL`, `IS TRUE`, etc. are not the approved idiom).
                    if isinstance(node.expression, exp.Null) and not isinstance(
                        node.parent, exp.Not
                    ):
                        return None
                    return (
                        "used in an `IS NOT NULL` / non-`IS NULL` check (only the "
                        "`:param IS NULL` guard arm is allowed)"
                    )
                if isinstance(node, exp.Limit):
                    return "used in a LIMIT clause (not a column-bound comparison)"
                if isinstance(node, exp.In):
                    return "used in an IN list (not a column-bound comparison)"
                if isinstance(node, exp.Between):
                    return (
                        "used in a BETWEEN range (not the approved guarded "
                        "comparison idiom)"
                    )
                if isinstance(node, (exp.Func, exp.Anonymous)):
                    return "used as a function argument (not a column-bound comparison)"
                if isinstance(node, (exp.Where, exp.Select)):
                    return "not used inside a column-bound comparison predicate"
                node = node.parent
            return "not used inside a column-bound comparison predicate"

        occurrences = 0
        unrecognized: set = set()
        # token -> ordered list of distinct human reasons it broke the contract.
        details: Dict[str, List[str]] = {}

        def _record(token: str, why: str) -> None:
            unrecognized.add(token)
            reasons = details.setdefault(token, [])
            if why not in reasons:
                reasons.append(why)

        for ph in ast.find_all(exp.Placeholder):
            name = ph.name or (ph.this if isinstance(ph.this, str) else "")
            if not name:
                continue
            occurrences += 1
            why = _occurrence_reason(ph)
            if why is not None:
                _record(f":{name}", why)
        if occurrences == 0:
            return True, "", []

        # Authoritative cross-check: the surviving tokens must also appear in the
        # machine-readable contract the extractor emits (the single source of
        # truth for what actually resolved to a governed column / classification).
        from view_ontology_extractor import extract_view_ontology
        try:
            contract = extract_view_ontology(
                text, binding_key or "", "", ""
            ).temporal_parameters
        except Exception as exc:
            # Placeholders exist but the contract could not be derived, so their
            # safety cannot be proven — fail closed rather than serve blindly.
            return (
                False,
                f"could not derive a Temporal Parameter Contract to validate the "
                f"baked-in parameter(s) ({exc})",
                [],
            )
        recognized = {p["token"] for p in contract}
        bound_tokens = {
            f":{(ph.name or (ph.this if isinstance(ph.this, str) else ''))}"
            for ph in ast.find_all(exp.Placeholder)
            if (ph.name or (ph.this if isinstance(ph.this, str) else ""))
        } - unrecognized
        # Tokens that survived the per-occurrence check but the extractor's
        # contract never recognized (e.g. bound to a column the graph does not
        # classify) — record a distinct, actionable reason for each.
        for token in sorted(bound_tokens - recognized):
            _record(
                token,
                "resolved to a predicate but is absent from the machine-readable "
                "Temporal Parameter Contract (not classified as horizon / netting "
                "/ filter)",
            )

        if unrecognized:
            per_token = "; ".join(
                f"`{token}` {' and '.join(details.get(token, ['unrecognized parameter']))}"
                for token in sorted(unrecognized)
            )
            reason = (
                "tokenized parameter(s) not part of the snippet's Temporal "
                "Parameter Contract (each must be a column-bound, classifiable "
                "guarded predicate) — " + per_token
            )
            return False, reason, []
        return True, "", []

    def resolve_by_binding_key(self, binding_key: str, target_dialect: str = "sqlite") -> Dict[str, Any]:
        bindings = self.load_approved_bindings()
        binding = None
        for b in bindings:
            if b.binding_key == binding_key:
                binding = b
                break

        if not binding:
            # Fail-closed condition (1): no binding key in the manifest.
            return {
                "sql": "",
                "report": [f"Binding key `{binding_key}` not found or not APPROVED in manifest."],
                "warnings": [f"Missing ground truth: `{binding_key}.sql`"],
                "fail_closed": True,
                "fail_condition": "missing_binding_key",
            }

        if not (binding.sql_text or "").strip():
            # Fail-closed condition (2): the binding exists but its snippet file
            # is missing or empty, so there is no SME-approved SQL to serve.
            return {
                "sql": "",
                "report": [
                    f"Binding key `{binding_key}` has no snippet file "
                    f"(`{binding.sql_snippet_path}`); nothing to serve."
                ],
                "warnings": [f"Missing ground truth: `{binding_key}.sql`"],
                "fail_closed": True,
                "fail_condition": "missing_snippet_file",
            }

        # Fail-closed condition (4): structural fingerprint validation. The
        # snippet's base-table set must match the SME-approved fingerprint (style
        # — CTE names, join order, windowing/bucketing — is deliberately ignored).
        # A parse failure also fails closed. A binding with no approved
        # fingerprint yet serves, with a warning, rather than being locked out.
        fp_warnings: List[str] = []
        fp_ok, fp_reason = sfp.validate_fingerprint(binding.sql_text, binding.base_tables)
        if not fp_ok:
            return {
                "sql": "",
                "report": [
                    f"Binding key `{binding_key}` failed structural fingerprint "
                    f"validation: {fp_reason}"
                ],
                "warnings": [
                    "Snippet structure does not match its SME-approved fingerprint; "
                    "re-register the snippet if this change is intended."
                ],
                "fail_closed": True,
                "fail_condition": "fingerprint_validation_failed",
            }
        if not binding.base_tables:
            fp_warnings.append(
                f"Binding `{binding_key}` has no approved structural fingerprint; "
                "serving without base-table validation."
            )

        # Fail-closed condition (4, graph-aware): join-edge validation. The
        # snippet's canonical join edges (join type included) must match the
        # SME-approved set (drift) AND every edge must be a relationship the graph
        # recognizes (recognition). Only enforced for v2 (join-aware) entries; a
        # pre-v2 entry serves without it. A missing graph skips recognition (drift
        # still enforced) rather than locking the engine out.
        if binding.join_aware:
            graph_join_edges = self._graph_join_edges()
            je_ok, je_reason, je_warnings = sfp.validate_join_edges(
                binding.sql_text, binding.join_edges, graph_join_edges
            )
            fp_warnings.extend(je_warnings)
            if not je_ok:
                return {
                    "sql": "",
                    "report": [
                        f"Binding key `{binding_key}` failed join validation: {je_reason}"
                    ],
                    "warnings": [
                        "Snippet join structure does not match its SME-approved / "
                        "graph-recognized joins; re-register the snippet if this "
                        "change is intended."
                    ],
                    "fail_closed": True,
                    "fail_condition": "join_validation_failed",
                }
            if graph_join_edges is None:
                fp_warnings.append(
                    f"Binding `{binding_key}`: SQL graph edge tables unavailable; "
                    "served without join-recognition validation."
                )

        # Fail-closed: any tokenized (:named) parameter baked into the snippet
        # must be a recognized member of its Temporal Parameter Contract; an
        # unrecognized placeholder is drift and is never served. Passive —
        # nothing is executed and no value is bound.
        tc_ok, tc_reason, tc_warnings = self.validate_temporal_contract(
            binding.sql_text, binding_key
        )
        fp_warnings.extend(tc_warnings)
        if not tc_ok:
            return {
                "sql": "",
                "report": [
                    f"Binding key `{binding_key}` failed temporal-parameter "
                    f"contract validation: {tc_reason}"
                ],
                "warnings": fp_warnings + [
                    "Snippet carries a tokenized parameter that is not part of "
                    "its Temporal Parameter Contract; re-register the snippet if "
                    "this change is intended."
                ],
                "fail_closed": True,
                "fail_condition": "temporal_contract_validation_failed",
            }

        sql = binding.sql_text
        if target_dialect and target_dialect != "sqlite":
            try:
                import sqlglot
                sql = sqlglot.transpile(sql, read="sqlite", write=target_dialect)[0]
            except Exception as e:
                return {
                    "sql": binding.sql_text,
                    "report": [f"Bound via `{binding_key}` (transpilation failed: {e})"],
                    "warnings": fp_warnings + [f"Transpilation to {target_dialect} failed, returning SQLite dialect"]
                }

        return {
            "sql": sql,
            "report": [
                f"**{binding.concept_anchor}**: Bound via `{binding_key}` ({binding.logic_type})",
                f"Perspective: {binding.perspective}"
            ],
            "warnings": fp_warnings
        }

    def get_elevation_edges(self, intent_name: str) -> List[ElevationEdge]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT 
                    i.intent_name,
                    c.concept_name,
                    ic.intent_factor_weight,
                    ic.explanation,
                    p.perspective_name,
                    cf.table_name,
                    cf.field_name,
                    cf.context_hint
                FROM schema_intent_concepts ic
                JOIN schema_intents i ON ic.intent_id = i.intent_id
                JOIN schema_concepts c ON ic.concept_id = c.concept_id
                LEFT JOIN schema_intent_perspectives ip 
                    ON i.intent_id = ip.intent_id AND ip.intent_factor_weight = 1.0
                LEFT JOIN schema_perspectives p ON ip.perspective_id = p.perspective_id
                LEFT JOIN schema_concept_fields cf ON c.concept_id = cf.concept_id
                WHERE i.intent_name = ?
                ORDER BY ic.intent_factor_weight DESC
            """, (intent_name,)).fetchall()

            edges = []
            for r in rows:
                edges.append(ElevationEdge(
                    intent_name=r["intent_name"],
                    concept_name=r["concept_name"],
                    weight=r["intent_factor_weight"],
                    explanation=r["explanation"] or "",
                    perspective_name=r["perspective_name"] or "",
                    table_name=r["table_name"] or "",
                    field_name=r["field_name"] or "",
                    context_hint=r["context_hint"] or ""
                ))
            return edges
        finally:
            conn.close()

    def get_available_intents(self) -> List[Dict[str, str]]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT intent_name, intent_category, description, typical_question
                FROM schema_intents ORDER BY intent_category, intent_name
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def find_binding_for_concept(self, concept: str, perspective: str = None) -> Optional[SolderBinding]:
        bindings = self.load_approved_bindings()
        concept_upper = concept.upper()

        has_perspective = perspective and perspective.strip()

        if has_perspective:
            # Fail-closed condition (3): when a perspective is explicitly
            # requested, resolution NEVER falls back to a snippet approved under a
            # different perspective — serving cross-perspective SQL silently would
            # violate the manifest's perspective contract. If nothing matches the
            # requested perspective (exact then partial concept), return None.
            persp = perspective.lower()
            exact_with_perspective = [
                b for b in bindings
                if b.concept_anchor == concept_upper
                and b.perspective.lower() == persp
            ]
            if exact_with_perspective:
                return exact_with_perspective[-1]

            partial_with_perspective = [
                b for b in bindings
                if b.perspective.lower() == persp
                and (concept_upper in b.concept_anchor or b.concept_anchor in concept_upper)
            ]
            if partial_with_perspective:
                return partial_with_perspective[-1]

            return None

        exact_any_perspective = [
            b for b in bindings
            if b.concept_anchor == concept_upper
        ]
        if exact_any_perspective:
            return exact_any_perspective[-1]

        partial_matches = [
            b for b in bindings
            if concept_upper in b.concept_anchor or b.concept_anchor in concept_upper
        ]
        if partial_matches:
            return partial_matches[-1]

        return None

    def parse_sql_ast(self, sql: str, dialect: str = "sqlite") -> Optional[exp.Expression]:
        try:
            parsed = sqlglot.parse(sql, read=dialect)
            if parsed and parsed[0]:
                return parsed[0]
        except Exception:
            pass
        return None

    def apply_alias_rename(self, ast: exp.Expression, old_alias: str, new_alias: str) -> Tuple[exp.Expression, str]:
        def rename_node(node):
            if isinstance(node, exp.Column) and node.table == old_alias:
                return exp.Column(
                    this=node.this,
                    table=exp.to_identifier(new_alias)
                )
            if isinstance(node, exp.TableAlias) and node.this and node.this.name == old_alias:
                return exp.TableAlias(this=exp.to_identifier(new_alias))
            if isinstance(node, exp.Subquery) and node.alias == old_alias:
                node.set("alias", exp.TableAlias(this=exp.to_identifier(new_alias)))
                return node
            return node

        transformed = ast.transform(rename_node)
        return transformed, f"Renamed alias '{old_alias}' -> '{new_alias}' (columns + table definitions)"

    def apply_table_qualification(self, ast: exp.Expression, table_name: str) -> Tuple[exp.Expression, str]:
        transformed = ast.transform(
            lambda node: exp.Column(
                this=node.this,
                table=exp.to_identifier(table_name)
            ) if isinstance(node, exp.Column) and not node.table else node
        )
        return transformed, f"Qualified unaliased columns with table '{table_name}'"

    def apply_where_injection(self, ast: exp.Expression, condition_sql: str) -> Tuple[exp.Expression, str]:
        try:
            condition = sqlglot.parse_one(f"SELECT * WHERE {condition_sql}", read="sqlite")
            where_clause = condition.find(exp.Where)
            if where_clause and isinstance(ast, exp.Select):
                existing_where = ast.find(exp.Where)
                if existing_where:
                    new_condition = exp.And(
                        this=existing_where.this,
                        expression=where_clause.this
                    )
                    ast.set("where", exp.Where(this=new_condition))
                    return ast, f"Injected AND condition: {condition_sql}"
                else:
                    ast.set("where", where_clause)
                    return ast, f"Added WHERE clause: {condition_sql}"
        except Exception:
            pass
        return ast, f"Warning: Could not inject condition: {condition_sql}"

    def transpile(self, sql: str, from_dialect: str = "sqlite", to_dialect: str = "tsql") -> str:
        try:
            result = sqlglot.transpile(sql, read=from_dialect, write=to_dialect)
            if result:
                return result[0]
        except Exception:
            pass
        return sql

    def solder(self, intent_name: str, target_concept: str = None,
               target_dialect: str = "sqlite",
               context_overrides: Dict[str, str] = None) -> SolderResult:

        elevation_edges = self.get_elevation_edges(intent_name)

        if not elevation_edges:
            return SolderResult(
                original_sql="",
                soldered_sql="-- No elevation edges found for intent: " + intent_name,
                dialect=target_dialect,
                binding_key="",
                perspective="",
                concept="",
                logic_type="",
                elevation_weight=0.0,
                warnings=[f"No elevation data found for intent '{intent_name}'"]
            )

        elevated = [e for e in elevation_edges if e.weight == 1.0]
        suppressed = [e for e in elevation_edges if e.weight == 0.0]

        if target_concept:
            target_edges = [e for e in elevated if e.concept_name.upper() == target_concept.upper()]
            if not target_edges:
                target_edges = [e for e in elevated if target_concept.upper() in e.concept_name.upper()]
            if target_edges:
                elevated = target_edges

        if not elevated:
            return SolderResult(
                original_sql="",
                soldered_sql=f"-- No ELEVATED concepts for intent '{intent_name}'",
                dialect=target_dialect,
                binding_key="",
                perspective="",
                concept="",
                logic_type="",
                elevation_weight=0.0,
                warnings=["No elevated concepts found"]
            )

        primary_edge = elevated[0]
        perspective = primary_edge.perspective_name
        concept = primary_edge.concept_name

        intent_bk = self._get_intent_binding_key(intent_name)
        if intent_bk:
            binding_result = self.resolve_by_binding_key(intent_bk, target_dialect=target_dialect)
            if binding_result.get("sql"):
                bindings = self.load_approved_bindings()
                for b in bindings:
                    if b.binding_key == intent_bk:
                        binding = b
                        break
                else:
                    binding = self.find_binding_for_concept(concept, perspective)
            else:
                binding = self.find_binding_for_concept(concept, perspective)
        else:
            binding = self.find_binding_for_concept(concept, perspective)

        ast_operations = []
        warnings = []

        if binding and binding.sql_text:
            original_sql = binding.sql_text
            ast = self.parse_sql_ast(original_sql)

            if ast:
                if context_overrides:
                    for old_alias, new_alias in context_overrides.items():
                        ast, op = self.apply_alias_rename(ast, old_alias, new_alias)
                        ast_operations.append(op)

                if primary_edge.table_name:
                    ast, op = self.apply_table_qualification(ast, primary_edge.table_name)
                    ast_operations.append(op)

                ast_operations.append(f"Elevation: {concept} (weight={primary_edge.weight})")
                ast_operations.append(f"Perspective: {perspective}")
                ast_operations.append(f"Context hint: {primary_edge.context_hint}")

                if binding.logic_type == "SPATIAL_ALIAS":
                    ast_operations.append("Logic type: SPATIAL_ALIAS (coordinate/bin mapping - verify spatial accuracy)")
                    warnings.append("SPATIAL_ALIAS detected: SME-defined spatial logic requires manual coordinate verification")

                for s in suppressed:
                    ast_operations.append(f"Suppressed: {s.concept_name} (weight={s.weight}) - {s.explanation}")

                if target_dialect != "sqlite":
                    soldered_sql = self.transpile(ast.sql(dialect="sqlite"), "sqlite", target_dialect)
                    ast_operations.append(f"Transpiled: sqlite -> {target_dialect}")
                else:
                    soldered_sql = ast.sql(dialect="sqlite", pretty=True)
            else:
                soldered_sql = original_sql
                warnings.append("Could not parse SQL into AST; returning raw SQL")

            return SolderResult(
                original_sql=original_sql,
                soldered_sql=soldered_sql,
                dialect=target_dialect,
                binding_key=binding.binding_key,
                perspective=perspective,
                concept=concept,
                logic_type=binding.logic_type,
                elevation_weight=primary_edge.weight,
                ast_operations=ast_operations,
                warnings=warnings
            )

        base_sql = self._generate_base_query(primary_edge)
        ast_operations.append("Generated base query from elevation metadata (no approved snippet)")
        ast_operations.append(f"Elevation: {concept} (weight={primary_edge.weight})")
        ast_operations.append(f"Perspective: {perspective}")

        for s in suppressed:
            ast_operations.append(f"Suppressed: {s.concept_name} (weight={s.weight})")

        if target_dialect != "sqlite":
            soldered_sql = self.transpile(base_sql, "sqlite", target_dialect)
            ast_operations.append(f"Transpiled: sqlite -> {target_dialect}")
        else:
            soldered_sql = base_sql

        return SolderResult(
            original_sql=base_sql,
            soldered_sql=soldered_sql,
            dialect=target_dialect,
            binding_key="(auto-generated)",
            perspective=perspective,
            concept=concept,
            logic_type="DIRECT",
            elevation_weight=primary_edge.weight,
            ast_operations=ast_operations,
            warnings=["No approved SME snippet found; using auto-generated base query"]
        )

    def get_elevation_weight(self, intent_name: str, concept_name: str) -> float:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("""
                SELECT ic.intent_factor_weight
                FROM schema_intent_concepts ic
                JOIN schema_intents i ON ic.intent_id = i.intent_id
                JOIN schema_concepts c ON ic.concept_id = c.concept_id
                WHERE i.intent_name = ? AND c.concept_name = ?
            """, (intent_name, concept_name)).fetchone()
            return row["intent_factor_weight"] if row else 0
        finally:
            conn.close()

    def resolve_concept_snippet(self, perspective: str, concept: str,
                                 intent: str = None) -> Optional[SolderBinding]:
        weight = self.get_elevation_weight(intent, concept) if intent else 0

        all_approved = self.load_approved_bindings()

        candidates = [
            b for b in all_approved
            if b.concept_anchor == concept.upper()
            and (not perspective or b.perspective.lower() == perspective.lower())
            and b.validation_status == "APPROVED"
        ]

        # Fail-closed condition (3): when a perspective is explicitly requested and
        # no APPROVED snippet matches it, do NOT broaden to other perspectives —
        # serving cross-perspective SQL silently would violate the perspective
        # contract. Return None so the caller skips (fails closed) instead.
        if not candidates:
            return None

        if weight <= -1:
            return SolderBinding(
                binding_key="SUPPRESSED",
                perspective=perspective or "",
                concept_anchor=concept.upper(),
                logic_type="SUPPRESSED",
                sql_snippet_path="",
                sme_justification="Suppressed by intent elevation weight",
                validation_status="APPROVED",
                sql_text="NULL"
            )

        if intent and len(candidates) > 1:
            intent_bk = self._get_intent_binding_key(intent)
            if intent_bk:
                for c in candidates:
                    if c.binding_key == intent_bk:
                        return c

        return sorted(candidates, key=lambda x: x.binding_key, reverse=True)[0]

    def _get_intent_binding_key(self, intent: str) -> Optional[str]:
        """Look up primary_binding_key for an intent from SQLite"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "SELECT primary_binding_key FROM schema_intents WHERE intent_name = ?",
                (intent,)
            )
            row = cursor.fetchone()
            conn.close()
            return row[0] if row and row[0] else None
        except Exception:
            return None

    def _perspectives_for_concept(self, concept: str) -> List[str]:
        """Distinct perspectives an APPROVED snippet exists under for ``concept``.

        Used to distinguish fail-closed condition (3) — "the concept IS approved,
        just not for the requested perspective" — from a concept that has no
        approved snippet at all.
        """
        cu = concept.upper()
        return sorted({
            b.perspective for b in self.load_approved_bindings()
            if b.concept_anchor == cu
            and b.validation_status == "APPROVED"
            and b.perspective
        })

    def assemble_query(self, intent: str, perspective: str,
                       concepts: List[str], base_table: str,
                       target_dialect: str = "sqlite") -> Dict[str, Any]:
        cte_parts = []
        select_refs = []
        report = []
        warnings = []
        fail_closed_concepts: List[str] = []
        fail_closed_conditions: List[str] = []
        resolved_count = 0

        # Build the graph join-edge recognition set once for the whole assembly
        # (None when the graph tables are absent → recognition is skipped, drift
        # still enforced per concept below).
        graph_join_edges = self._graph_join_edges()

        for concept in concepts:
            binding = self.resolve_concept_snippet(perspective, concept, intent)

            if not binding:
                # Distinguish fail-closed condition (3) from a genuinely unknown
                # concept. If the concept IS approved but only under other
                # perspectives, we fail closed rather than silently serving
                # cross-perspective SQL — and we surface exactly why.
                available = self._perspectives_for_concept(concept)
                if perspective and perspective.strip() and available:
                    msg = (
                        f"{concept}: no snippet approved for perspective "
                        f"'{perspective}'. Approved under: {', '.join(available)}. "
                        f"Failing closed — cross-perspective SQL is never served."
                    )
                    report.append(f"- **{concept}**: ⛔ FAIL-CLOSED — {msg}")
                    warnings.append(msg)
                    fail_closed_concepts.append(concept)
                    if "no_perspective_compatible_snippet" not in fail_closed_conditions:
                        fail_closed_conditions.append("no_perspective_compatible_snippet")
                else:
                    report.append(f"- **{concept}**: No approved snippet found (skipped)")
                continue

            if binding.logic_type == "SUPPRESSED":
                select_refs.append(f"NULL AS {concept}")
                report.append(f"- ~~{concept}~~: SUPPRESSED by intent weight → NULL")
                resolved_count += 1
                continue

            snippet_sql = binding.sql_text.strip().rstrip(";")
            if not snippet_sql:
                # Fail-closed condition (2): approved binding with no snippet SQL.
                msg = (
                    f"{concept}: binding `{binding.binding_key}` has no snippet SQL. "
                    f"Failing closed — nothing SME-approved to serve."
                )
                report.append(f"- **{concept}**: ⛔ FAIL-CLOSED — {msg}")
                warnings.append(msg)
                fail_closed_concepts.append(concept)
                if "missing_snippet_file" not in fail_closed_conditions:
                    fail_closed_conditions.append("missing_snippet_file")
                continue

            try:
                sqlglot.parse_one(snippet_sql, read="sqlite")
            except Exception as e:
                # Fail-closed condition (4): a snippet that will not parse cannot
                # be structurally validated, so it is never served.
                msg = (
                    f"{concept}: snippet for binding `{binding.binding_key}` failed "
                    f"to parse ({e}). Failing closed — unparseable SQL is never served."
                )
                report.append(f"- **{concept}**: ⛔ FAIL-CLOSED — {msg}")
                warnings.append(msg)
                fail_closed_concepts.append(concept)
                if "fingerprint_validation_failed" not in fail_closed_conditions:
                    fail_closed_conditions.append("fingerprint_validation_failed")
                continue

            # Fail-closed condition (4): structural fingerprint. The snippet's
            # base-table set must match the SME-approved fingerprint (style is
            # deliberately ignored). A binding with no approved fingerprint yet
            # serves with a warning rather than being locked out.
            fp_ok, fp_reason = sfp.validate_fingerprint(binding.sql_text, binding.base_tables)
            if not fp_ok:
                msg = (
                    f"{concept}: snippet for binding `{binding.binding_key}` failed "
                    f"structural fingerprint validation ({fp_reason}). Failing closed — "
                    f"re-register the snippet if this change is intended."
                )
                report.append(f"- **{concept}**: ⛔ FAIL-CLOSED — {msg}")
                warnings.append(msg)
                fail_closed_concepts.append(concept)
                if "fingerprint_validation_failed" not in fail_closed_conditions:
                    fail_closed_conditions.append("fingerprint_validation_failed")
                continue
            if not binding.base_tables:
                warnings.append(
                    f"Binding `{binding.binding_key}` has no approved structural "
                    f"fingerprint; serving {concept} without base-table validation."
                )

            # Fail-closed condition (4, graph-aware): join-edge validation. The
            # snippet's canonical join edges must match the SME-approved set
            # (drift) AND every edge must be recognized by the graph. Only for v2
            # (join-aware) entries; a missing graph skips recognition (drift still
            # enforced). The recognition set is built once per assembly below.
            if binding.join_aware:
                je_ok, je_reason, je_warnings = sfp.validate_join_edges(
                    binding.sql_text, binding.join_edges, graph_join_edges
                )
                warnings.extend(je_warnings)
                if not je_ok:
                    msg = (
                        f"{concept}: snippet for binding `{binding.binding_key}` "
                        f"failed join validation ({je_reason}). Failing closed — "
                        f"re-register the snippet if this change is intended."
                    )
                    report.append(f"- **{concept}**: ⛔ FAIL-CLOSED — {msg}")
                    warnings.append(msg)
                    fail_closed_concepts.append(concept)
                    if "join_validation_failed" not in fail_closed_conditions:
                        fail_closed_conditions.append("join_validation_failed")
                    continue
                if graph_join_edges is None:
                    warnings.append(
                        f"Binding `{binding.binding_key}`: SQL graph edge tables "
                        f"unavailable; serving {concept} without join-recognition "
                        f"validation."
                    )

            # Fail-closed condition (5): temporal-parameter contract (see
            # resolve_by_binding_key). Any tokenized (:named) parameter baked into
            # the snippet must be a recognized member of its Temporal Parameter
            # Contract; an unrecognized placeholder is drift and is never served.
            # Passive — nothing is executed and no value is bound.
            tc_ok, tc_reason, tc_warnings = self.validate_temporal_contract(
                binding.sql_text, binding.binding_key
            )
            warnings.extend(tc_warnings)
            if not tc_ok:
                msg = (
                    f"{concept}: snippet for binding `{binding.binding_key}` "
                    f"failed temporal-parameter contract validation ({tc_reason}). "
                    f"Failing closed — re-register the snippet if this change is "
                    f"intended."
                )
                report.append(f"- **{concept}**: ⛔ FAIL-CLOSED — {msg}")
                warnings.append(msg)
                fail_closed_concepts.append(concept)
                if "temporal_contract_validation_failed" not in fail_closed_conditions:
                    fail_closed_conditions.append("temporal_contract_validation_failed")
                continue

            cte_parts.append(f"{concept} AS (\n{snippet_sql}\n)")
            select_refs.append(f"{concept}.*")
            resolved_count += 1

            report.append(
                f"- **{concept}**: Bound via `{binding.binding_key}` "
                f"({binding.logic_type})"
            )

            if binding.logic_type == "SPATIAL_ALIAS":
                warnings.append(
                    f"SPATIAL_ALIAS on {concept}: verify coordinate/bin accuracy"
                )

        # Hard fail-closed: if ANY requested concept failed closed (conditions
        # 2/3/4), refuse the WHOLE assembly. Serving SQL for the resolved subset
        # would silently drop a concept the caller asked for — and, worse, could
        # answer a question with partial data that looks complete. Fail-closed
        # means no runnable SQL, not partial SQL.
        if fail_closed_concepts:
            condition = self._summarize_fail_condition(fail_closed_conditions)
            base_warning = (
                "Failing closed: one or more requested concepts could not be "
                f"served ({', '.join(fail_closed_concepts)}). No SQL is returned."
            )
            return {
                "sql": (
                    f"-- FAIL-CLOSED ({condition}): refusing to serve partial SQL "
                    f"for intent '{intent}'. Blocked concepts: "
                    f"{', '.join(fail_closed_concepts)}."
                ),
                "dialect": target_dialect,
                "report": report,
                "warnings": [base_warning] + warnings,
                "concept_count": 0,
                "intent": intent,
                "perspective": perspective,
                "base_table": base_table,
                "fail_closed": True,
                "fail_closed_concepts": fail_closed_concepts,
                "fail_closed_condition": condition,
            }

        if not select_refs:
            base_warning = "No concepts could be resolved to approved snippets"
            return {
                "sql": f"-- No projections resolved for intent '{intent}'",
                "dialect": target_dialect,
                "report": report,
                "warnings": ([base_warning] + warnings) if warnings else [base_warning],
                "fail_closed": False,
                "fail_closed_concepts": [],
                "fail_closed_condition": None,
            }

        cte_clause = "WITH " + ",\n".join(cte_parts) + "\n" if cte_parts else ""
        cte_names = [c.split(" AS ")[0] for c in cte_parts]

        if cte_names:
            from_clause = cte_names[0]
            join_clauses = ""
            for cte_name in cte_names[1:]:
                join_clauses += f"\nCROSS JOIN {cte_name}"
            assembled_sql = (
                f"{cte_clause}"
                f"SELECT {', '.join(select_refs)}\n"
                f"FROM {from_clause}{join_clauses}"
            )
        else:
            assembled_sql = f"SELECT {', '.join(select_refs)}\nFROM {base_table}"

        if target_dialect != "sqlite":
            output_sql = self.transpile(assembled_sql, "sqlite", target_dialect)
        else:
            output_sql = assembled_sql

        return {
            "sql": output_sql,
            "dialect": target_dialect,
            "report": report,
            "warnings": warnings,
            "concept_count": resolved_count,
            "intent": intent,
            "perspective": perspective,
            "base_table": base_table,
            "fail_closed": bool(fail_closed_concepts),
            "fail_closed_concepts": fail_closed_concepts,
            "fail_closed_condition": self._summarize_fail_condition(fail_closed_conditions),
        }

    @staticmethod
    def _summarize_fail_condition(conditions: List[str]) -> Optional[str]:
        """Collapse the distinct fail-closed condition codes into one label.

        None when nothing failed closed; the single code when only one kind of
        failure occurred; ``"multiple"`` when different kinds were mixed.
        """
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return "multiple"

    def _generate_base_query(self, edge: ElevationEdge) -> str:
        if edge.table_name and edge.field_name:
            return f"SELECT {edge.field_name} FROM {edge.table_name}"
        return f"-- Cannot generate query: missing table/field metadata for {edge.concept_name}"

    def _get_field_description(self, table_name: str, field_name: str) -> Dict[str, Any]:
        """SME field description (api_field_descriptions) for a column.

        This SME-meaningful overlay (local stand-in for DAB) takes precedence
        over the internal, abstract concept name whenever a meaning is shown to
        a human. Returns {} when no description has been authored for the column.
        """
        if not table_name or not field_name:
            return {}
        source_db = os.environ.get("SQL_MCP_SOURCE_DATABASE", "manufacturing")
        schema = os.environ.get("SQL_MCP_DEFAULT_SCHEMA", "dbo")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT display_name, description, example_value "
                "FROM api_field_descriptions "
                "WHERE source_database=? AND schema_name=? "
                "  AND table_name=? AND column_name=?",
                (source_db, schema, table_name, field_name),
            ).fetchone()
            return dict(row) if row else {}
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    # ── Metrics: dialect-agnostic computation templates ───────────────────────
    #
    # A metric is identified by DUCK TYPING: a concept is a metric iff it carries
    # a non-empty computation_template (concept_type is deprecated and never
    # consulted). The template is dialect-agnostic with named {variable}
    # placeholders — NEVER static SQL. Each variable binds to a physical column via a
    # schema_concept_fields row carrying variable_name (the SQLite source of the
    # graph's resolves_to edges). assemble_metric_sql() substitutes the variables
    # with real table-qualified columns and transpiles, so a metric is DEFINED
    # ONCE and yields identical SQL no matter which perspective surfaced it.
    #
    # Bindings are resolved at CONCEPT scope and deduplicated by
    # variable_name -> table.column. The same metric is intentionally visible
    # under several existing perspectives, so the resolves_to edges fan out; the
    # deduplication collapses that fan-out to one binding per variable. The
    # assembler FAILS CLOSED (raises MetricAssemblyError) on a missing template,
    # a missing/extra binding, a conflicting binding (one variable -> two
    # different columns), or an unresolvable join — never fabricated SQL.

    _PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

    @staticmethod
    def _metric_alias(metric_name: str) -> str:
        """CamelCase concept name -> snake_case column alias for the SELECT."""
        s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", metric_name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()

    def _metric_template(self, conn, metric: str) -> Optional[str]:
        # Duck typing: a concept is a metric iff it carries a non-empty
        # computation_template. concept_type is deprecated and never consulted.
        row = conn.execute(
            "SELECT computation_template FROM schema_concepts "
            "WHERE concept_name = ? "
            "  AND computation_template IS NOT NULL "
            "  AND computation_template <> ''",
            (metric,),
        ).fetchone()
        return row["computation_template"] if row else None

    def _metric_bindings(self, conn, metric: str) -> Dict[str, Tuple[str, str]]:
        """Concept-scoped variable -> (table, column), deduplicated.

        The resolves_to edges fan out across every perspective the metric is
        visible under, so the same (variable -> column) row appears multiple
        times. Identical duplicates collapse silently; a variable that resolves
        to two *different* columns is a hard conflict and fails closed.
        """
        rows = conn.execute(
            "SELECT cf.variable_name, cf.table_name, cf.field_name "
            "FROM schema_concept_fields cf "
            "JOIN schema_concepts c ON c.concept_id = cf.concept_id "
            "WHERE c.concept_name = ? AND cf.variable_name IS NOT NULL "
            "  AND cf.variable_name <> ''",
            (metric,),
        ).fetchall()
        resolved: Dict[str, Tuple[str, str]] = {}
        for r in rows:
            var = r["variable_name"]
            pair = (r["table_name"], r["field_name"])
            if var in resolved and resolved[var] != pair:
                a = ".".join(resolved[var])
                b = ".".join(pair)
                raise MetricAssemblyError(
                    f"Conflicting binding for metric '{metric}' variable "
                    f"'{{{var}}}': resolves to both {a} and {b}. A variable must "
                    f"map to exactly one physical column."
                )
            resolved[var] = pair
        return resolved

    def _table_foreign_keys(self, conn, table: str):
        """[(local_col, referenced_table, referenced_col)] from the live schema.

        Primary source: ``PRAGMA foreign_key_list`` — present when the database
        was created with FOREIGN KEY clauses in the DDL (as ``manufacturing.db``
        is).  Fallback: ``sql_graph_edges`` ``references`` edges — used when the
        database is a read-only snapshot created from DuckDB via
        ``pandas.to_sql``, which does not preserve SQLite FK declarations.  The
        ``_from`` / ``_to`` composite node keys encode table and column as
        ``{table}:{col}:…``, so parsing them reproduces the PRAGMA result.
        """
        rows = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        fks = [(r["from"], r["table"], r["to"]) for r in rows]
        if fks:
            return fks
        try:
            edges = conn.execute(
                "SELECT _from, _to FROM sql_graph_edges"
                " WHERE edge_type = 'references'"
            ).fetchall()
            for r in edges:
                f_key = r["_from"] if isinstance(r, sqlite3.Row) else r[0]
                t_key = r["_to"] if isinstance(r, sqlite3.Row) else r[1]
                f_parts = f_key.split("/")[-1].split(":")
                t_parts = t_key.split("/")[-1].split(":")
                if len(f_parts) < 2 or len(t_parts) < 2:
                    continue
                if f_parts[0].lower() == table.lower():
                    fks.append((f_parts[1], t_parts[0], t_parts[1]))
        except Exception:
            pass
        return fks

    def _resolve_from_join(self, conn, tables: List[str]) -> Tuple[str, List[str]]:
        """Build a FROM/JOIN clause from declared foreign keys only.

        Single table -> ``FROM t``. Two tables -> a JOIN on the declared FK in
        either direction. No declared FK (or >2 tables) fails closed rather than
        fabricating an unsupported join.
        """
        tables = sorted(set(tables))
        if not tables:
            raise MetricAssemblyError("Metric resolves to no physical table.")
        if len(tables) == 1:
            return tables[0], []
        if len(tables) == 2:
            a, b = tables
            for child, parent in ((a, b), (b, a)):
                for frm, reftbl, refcol in self._table_foreign_keys(conn, child):
                    if reftbl == parent:
                        clause = (f"{child}\n  JOIN {parent} "
                                  f"ON {child}.{frm} = {parent}.{refcol}")
                        return clause, [f"{child}.{frm} = {parent}.{refcol}"]
            raise MetricAssemblyError(
                f"No declared foreign key links '{a}' and '{b}'; refusing to "
                f"fabricate a join (fail closed)."
            )
        raise MetricAssemblyError(
            f"Metric spans {len(tables)} tables ({', '.join(tables)}); multi-hop "
            f"join resolution is not supported (fail closed)."
        )

    def _metric_perspectives(self, conn, metric: str) -> List[str]:
        rows = conn.execute(
            "SELECT p.perspective_name FROM schema_perspective_concepts pc "
            "JOIN schema_perspectives p ON p.perspective_id = pc.perspective_id "
            "JOIN schema_concepts c ON c.concept_id = pc.concept_id "
            "WHERE c.concept_name = ? ORDER BY p.perspective_name",
            (metric,),
        ).fetchall()
        return [r["perspective_name"] for r in rows]

    def list_metrics(self) -> List[Dict[str, Any]]:
        """Concept rows that are defined metrics (have a computation_template)."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT concept_name, description, domain, computation_template "
                "FROM schema_concepts "
                "WHERE computation_template IS NOT NULL "
                "  AND computation_template <> '' "
                "ORDER BY concept_name"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_metric_template(self, metric: str) -> Optional[str]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return self._metric_template(conn, metric)
        finally:
            conn.close()

    def get_metric_bindings(self, metric: str) -> List[MetricBinding]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            resolved = self._metric_bindings(conn, metric)
            return [MetricBinding(v, t, c)
                    for v, (t, c) in sorted(resolved.items())]
        finally:
            conn.close()

    def assemble_metric_sql(self, metric: str,
                            target_dialect: str = "sqlite") -> MetricResult:
        """Substitute a metric's template variables with real table-qualified
        columns and return a runnable SELECT in ``target_dialect``.

        Fails closed (MetricAssemblyError) on a missing/empty template, a static
        (variable-free) template, a missing or extra binding, a conflicting
        binding, or an unresolvable join.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            template = self._metric_template(conn, metric)
            if not template or not template.strip():
                raise MetricAssemblyError(
                    f"Metric '{metric}' has no computation_template "
                    f"(not a defined metric concept)."
                )
            placeholders: List[str] = []
            for m in self._PLACEHOLDER_RE.finditer(template):
                if m.group(1) not in placeholders:
                    placeholders.append(m.group(1))
            if not placeholders:
                raise MetricAssemblyError(
                    f"Metric '{metric}' template has no {{variable}} "
                    f"placeholders; a metric template must never be static SQL."
                )
            resolved = self._metric_bindings(conn, metric)
            missing = [p for p in placeholders if p not in resolved]
            if missing:
                raise MetricAssemblyError(
                    f"Metric '{metric}' is missing variable bindings for "
                    f"{missing}; cannot assemble SQL (fail closed)."
                )
            extra = sorted(set(resolved) - set(placeholders))
            if extra:
                raise MetricAssemblyError(
                    f"Metric '{metric}' has bindings {extra} with no matching "
                    f"template placeholder (fail closed)."
                )
            expression = template
            for var in placeholders:
                tbl, col = resolved[var]
                expression = expression.replace("{" + var + "}", f"{tbl}.{col}")
            tables = sorted({resolved[v][0] for v in placeholders})
            from_clause, join_path = self._resolve_from_join(conn, tables)
            alias = self._metric_alias(metric)
            sqlite_sql = f"SELECT {expression} AS {alias}\nFROM {from_clause}"
            warnings: List[str] = []
            out_sql = sqlite_sql
            if target_dialect and target_dialect != "sqlite":
                transpiled = self.transpile(sqlite_sql, "sqlite", target_dialect)
                if transpiled == sqlite_sql:
                    warnings.append(
                        f"Transpilation to {target_dialect} returned the SQLite "
                        f"form unchanged."
                    )
                out_sql = transpiled
            bindings = [MetricBinding(v, resolved[v][0], resolved[v][1])
                        for v in placeholders]
            return MetricResult(
                metric=metric,
                template=template,
                expression=expression,
                sql=out_sql,
                dialect=target_dialect or "sqlite",
                bindings=bindings,
                tables=tables,
                join_path=join_path,
                perspectives=self._metric_perspectives(conn, metric),
                warnings=warnings,
            )
        finally:
            conn.close()

    def get_metric_lineage(self, metric: str) -> Dict[str, Any]:
        """Human-facing lineage: each variable -> physical column (with its SME
        meaning), the tables involved, and the perspectives the metric serves."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            template = self._metric_template(conn, metric)
            if template is None:
                return {}
            resolved = self._metric_bindings(conn, metric)
            variables = []
            for v, (t, c) in sorted(resolved.items()):
                fd = self._get_field_description(t, c)
                variables.append({
                    "variable": v,
                    "table": t,
                    "column": c,
                    "display_name": fd.get("display_name") or "",
                    "meaning": fd.get("description") or "",
                })
            tables = sorted({t for (t, _c) in resolved.values()})
            return {
                "metric": metric,
                "template": template,
                "variables": variables,
                "tables": tables,
                "perspectives": self._metric_perspectives(conn, metric),
            }
        finally:
            conn.close()

    def get_solder_report(self, intent_name: str, target_dialect: str = "sqlite") -> str:
        edges = self.get_elevation_edges(intent_name)

        if not edges:
            return f"No elevation data found for intent '{intent_name}'."

        elevated = [e for e in edges if e.weight == 1.0]
        suppressed = [e for e in edges if e.weight == 0.0]
        neutral = [e for e in edges if e.weight not in (0.0, 1.0)]

        report = f"## Solder Report: `{intent_name}`\n\n"

        if elevated:
            report += "### ELEVATED Fields (weight = 1.0)\n"
            for e in elevated:
                binding = self.find_binding_for_concept(e.concept_name, e.perspective_name)
                status = f"Approved: `{binding.binding_key}`" if binding else "No approved snippet"
                fd = self._get_field_description(e.table_name, e.field_name)
                # SME field description takes precedence over the abstract concept
                # name; fall back to the qualified column when none is authored.
                title = fd.get("display_name") or f"{e.table_name}.{e.field_name}"
                report += f"- **{title}** ({e.perspective_name})\n"
                report += f"  - Field: `{e.table_name}.{e.field_name}`\n"
                if fd.get("description"):
                    report += f"  - Meaning: {fd['description']}\n"
                if e.context_hint:
                    report += f"  - Applies when: {e.context_hint}\n"
                report += f"  - SME Snippet: {status}\n"
                if e.explanation:
                    report += f"  - *{e.explanation}*\n"
                report += f"  - _internal concept key: {e.concept_name}_\n\n"

        if suppressed:
            report += "### SUPPRESSED Concepts (weight = 0.0)\n"
            for s in suppressed:
                report += f"- ~~{s.concept_name}~~ - {s.explanation}\n"
            report += "\n"

        if neutral:
            report += "### NEUTRAL Concepts\n"
            for n in neutral:
                report += f"- {n.concept_name} (weight={n.weight}) - {n.explanation}\n"
            report += "\n"

        approved_bindings = self.load_approved_bindings()
        if approved_bindings:
            report += f"### Manifest: {len(approved_bindings)} approved snippets available\n"
            for b in approved_bindings:
                report += f"- `{b.binding_key}` ({b.perspective} / {b.concept_anchor}) - {b.logic_type}\n"

        return report

    # ── Table-usage index ─────────────────────────────────────────────────────

    def _split_queries(self, sql_text: str) -> List[Tuple[str, str]]:
        """Split a multi-query SQL file into (query_name, sql_text) pairs.

        Splits on the '-- ===…===' separator lines used in all ground-truth
        files.  Returns [(name, sql), …]; name is taken from the
        '-- Query N — name' line immediately after each separator.
        """
        parts = _QUERY_HEADER_RE.split(sql_text)
        blocks: List[str] = []
        current: List[str] = []
        for part in sql_text.splitlines(keepends=True):
            if _QUERY_HEADER_RE.match(part.rstrip()):
                if current:
                    blocks.append("".join(current))
                current = [part]
            else:
                current.append(part)
        if current:
            blocks.append("".join(current))

        result = []
        for block in blocks:
            name_match = _QUERY_NAME_RE.search(block)
            query_name = name_match.group(1) if name_match else "unnamed"
            sql_lines = [
                ln for ln in block.splitlines()
                if not ln.strip().startswith("--")
            ]
            sql_clean = "\n".join(sql_lines).strip()
            if sql_clean:
                result.append((query_name, sql_clean))
        return result or [("all", sql_text)]

    def _count_select_participation(self, sql_text: str) -> Dict[str, int]:
        """Count how many distinct SELECT nodes each base table participates in.

        A table with ``select_count > 1`` in the same query block is being
        sliced N times (discriminator / polymorphic pattern) where a single
        pivot (CASE WHEN) could reduce it to one scan.

        CTE aliases are excluded — only real schema objects are counted.
        Nested SELECT nodes (subqueries) are counted independently so that
        a subquery referencing the same table as an outer SELECT contributes
        an additional participation hit.
        """
        participation: Counter = Counter()
        try:
            for stmt in sqlglot.parse(sql_text, dialect="sqlite"):
                if stmt is None:
                    continue
                cte_names: set = {
                    cte.alias.lower()
                    for cte in stmt.find_all(exp.CTE)
                    if cte.alias
                }
                for select_node in stmt.find_all(exp.Select):
                    seen_in_this_select: set = set()
                    # Walk tables inside this SELECT but stop at nested SELECT
                    # nodes (subqueries) to avoid double-counting.
                    for node in select_node.walk():
                        if node is select_node:
                            continue
                        if isinstance(node, exp.Select):
                            # Don't recurse into a subquery — it will be
                            # counted when find_all reaches it as its own item.
                            break
                        if isinstance(node, exp.Table):
                            name = node.name
                            if name and name.lower() not in cte_names:
                                seen_in_this_select.add(name.lower())
                    for t in seen_in_this_select:
                        participation[t] += 1
        except Exception:
            pass
        return dict(participation)

    def _extract_tables_from_sql(self, sql_text: str) -> List[str]:
        """Return real base-table names referenced in a SQL string (SQLGlot walk).

        CTE alias names are excluded: SQLGlot surfaces them as ``exp.Table``
        nodes in the outer SELECT's FROM clause, but they are virtual tables
        defined within the same statement, not schema objects.

        Delegates to ``structural_fingerprint.raw_base_tables`` so runtime
        structural validation and manifest fingerprint backfill share one
        extraction implementation and can never disagree.
        """
        return sfp.raw_base_tables(sql_text)

    def build_table_usage_index(self, verbose: bool = True) -> Dict[str, Any]:
        """Parse every ground-truth SQL file in QUERIES_DIR with SQLGlot,
        count table references per query, and persist:

          - SQLite  ``ground_truth_table_usage``  (per-query, per-table rows)
          - ``app_schema/queries/index.json``      (table_usage per category)

        Returns a summary dict: {category_id: {totals, per_query}, global: Counter}.
        """
        if not os.path.exists(QUERIES_INDEX_PATH):
            return {"error": f"index.json not found at {QUERIES_INDEX_PATH}"}

        with open(QUERIES_INDEX_PATH) as fh:
            index = json.load(fh)

        category_map: Dict[str, str] = {
            c["file"]: c["id"] for c in index.get("categories", [])
        }

        conn = sqlite3.connect(self.db_path)
        cur  = conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS ground_truth_table_usage (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                query_file      TEXT    NOT NULL,
                category_id     TEXT    NOT NULL,
                query_name      TEXT    NOT NULL,
                table_name      TEXT    NOT NULL,
                reference_count INTEGER NOT NULL DEFAULT 1,
                select_count    INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_gt_usage
                ON ground_truth_table_usage (category_id, query_name, table_name);
            DELETE FROM ground_truth_table_usage;
        """)
        # Add select_count column if upgrading an existing table without it.
        try:
            cur.execute("ALTER TABLE ground_truth_table_usage ADD COLUMN select_count INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        except Exception:
            pass  # column already exists

        global_counter: Counter = Counter()
        summary: Dict[str, Any] = {}

        for sql_file in sorted(
            (f for f in os.scandir(QUERIES_DIR) if f.name.endswith(".sql")),
            key=lambda e: e.name,
        ):
            file_name   = sql_file.name
            category_id = category_map.get(file_name, os.path.splitext(file_name)[0])
            sql_text    = open(sql_file.path).read()
            queries     = self._split_queries(sql_text)

            file_counter: Counter = Counter()
            per_query_list: List[Dict] = []

            for query_name, q_sql in queries:
                tables      = self._extract_tables_from_sql(q_sql)
                q_counts    = Counter(tables)
                sel_counts  = self._count_select_participation(q_sql)
                file_counter.update(q_counts)
                global_counter.update(q_counts)

                for table_name, ref_count in q_counts.items():
                    s_count = sel_counts.get(table_name, 1)
                    cur.execute(
                        """INSERT OR REPLACE INTO ground_truth_table_usage
                           (query_file, category_id, query_name, table_name,
                            reference_count, select_count)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (file_name, category_id, query_name, table_name,
                         ref_count, s_count),
                    )
                per_query_list.append({
                    "query_name": query_name,
                    "tables": dict(q_counts),
                    "select_counts": sel_counts,
                })

            summary[category_id] = {"file": file_name, "per_query": per_query_list,
                                    "totals": dict(file_counter)}
            if verbose:
                print(f"  {file_name}: {len(queries)} queries, "
                      f"{len(file_counter)} tables → {dict(file_counter)}")

        # Also index every APPROVED snippet in the reviewer manifest. These live
        # in ground_truth/sql_snippets/ (outside QUERIES_DIR) and would otherwise
        # be invisible to ground_truth_table_usage, under-reporting their tables.
        snippet_summary = self.index_snippet_table_usage(conn=conn, verbose=verbose)
        for binding_key, info in snippet_summary.items():
            for tbl, cnt in info.get("tables", {}).items():
                global_counter[tbl] += cnt
            summary[binding_key] = {
                "file": info.get("query_file", ""),
                "per_query": [{
                    "query_name": info.get("query_name", binding_key),
                    "tables": info.get("tables", {}),
                    "select_counts": info.get("select_counts", {}),
                }],
                "totals": info.get("tables", {}),
            }

        conn.commit()
        conn.close()

        # ── Update index.json ------------------------------------------------
        for cat in index.get("categories", []):
            usage = summary.get(cat["id"])
            if usage:
                cat["table_usage"] = usage
        index["tables_referenced"]       = sorted(global_counter.keys())
        index["table_reference_counts"]  = dict(global_counter.most_common())
        with open(QUERIES_INDEX_PATH, "w") as fh:
            json.dump(index, fh, indent=2)

        summary["_global"] = dict(global_counter.most_common())
        return summary

    def _resolve_snippet_path(self, file_path: str) -> str:
        """Resolve a manifest snippet file_path to an existing absolute path.

        Mirrors the resolution logic in ``load_approved_bindings`` so the
        table-usage index and the runtime binder agree on which file backs a
        snippet.
        """
        if not file_path:
            return ""
        if os.path.isabs(file_path) and os.path.exists(file_path):
            return file_path
        manifest_dir = os.path.dirname(os.path.abspath(self.manifest_path))
        candidate = os.path.join(manifest_dir, os.path.basename(file_path))
        if os.path.exists(candidate):
            return candidate
        # Also try ground_truth/sql_snippets/<basename> (current snippet home).
        candidate2 = os.path.join(manifest_dir, "sql_snippets", os.path.basename(file_path))
        if os.path.exists(candidate2):
            return candidate2
        if os.path.exists(file_path):
            return file_path
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidate3 = os.path.join(repo_root, file_path)
        if os.path.exists(candidate3):
            return candidate3
        return ""

    def index_snippet_table_usage(self, conn: sqlite3.Connection = None,
                                  verbose: bool = True) -> Dict[str, Any]:
        """Record per-table usage for every APPROVED reviewer-manifest snippet.

        SME-approved snippets live in ``ground_truth/sql_snippets/`` — outside
        ``QUERIES_DIR`` — so ``build_table_usage_index`` never sees them and the
        ``ground_truth_table_usage`` log silently under-reports the tables they
        touch (e.g. ``shop_resource``, ``operation``, ``receiving``,
        ``certification``).  This method parses each snippet with SQLGlot and
        upserts one row per (binding_key, concept_anchor, table) into the log.

        Pass ``conn`` to reuse an open connection/transaction (the caller
        commits); otherwise a connection is opened and committed here. Returns a
        dict keyed by binding_key with the tables/select counts that were
        recorded.
        """
        bindings = self.load_approved_bindings()

        owns_conn = conn is None
        if owns_conn:
            conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS ground_truth_table_usage (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                query_file      TEXT    NOT NULL,
                category_id     TEXT    NOT NULL,
                query_name      TEXT    NOT NULL,
                table_name      TEXT    NOT NULL,
                reference_count INTEGER NOT NULL DEFAULT 1,
                select_count    INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_gt_usage
                ON ground_truth_table_usage (category_id, query_name, table_name);
        """)

        summary: Dict[str, Any] = {}
        for binding in bindings:
            sql_text = binding.sql_text
            if not sql_text:
                resolved = self._resolve_snippet_path(binding.sql_snippet_path)
                if resolved:
                    try:
                        sql_text = open(resolved).read()
                    except Exception:
                        sql_text = ""
            if not sql_text:
                if verbose:
                    print(f"  [skip] {binding.binding_key}: no SQL text")
                continue

            q_counts   = Counter(self._extract_tables_from_sql(sql_text))
            if not q_counts:
                if verbose:
                    print(f"  [skip] {binding.binding_key}: no tables parsed")
                continue
            sel_counts = self._count_select_participation(sql_text)
            category_id = binding.binding_key
            query_name  = binding.concept_anchor or binding.binding_key
            query_file  = os.path.basename(binding.sql_snippet_path) or binding.binding_key

            for table_name, ref_count in q_counts.items():
                s_count = sel_counts.get(table_name, 1)
                cur.execute(
                    """INSERT OR REPLACE INTO ground_truth_table_usage
                       (query_file, category_id, query_name, table_name,
                        reference_count, select_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (query_file, category_id, query_name, table_name,
                     ref_count, s_count),
                )

            summary[binding.binding_key] = {
                "query_file": query_file,
                "query_name": query_name,
                "tables": dict(q_counts),
                "select_counts": sel_counts,
            }
            if verbose:
                print(f"  {binding.binding_key}: {dict(q_counts)}")

        if owns_conn:
            conn.commit()
            conn.close()
        return summary

    # ── Perspective affinity index ────────────────────────────────────────────

    #: Maps table name → perspectives it primarily signals.
    #: Multi-perspective entries distribute affinity weight equally.
    TABLE_PERSPECTIVE_MAP: Dict[str, List[str]] = {
        # Payables — supplier side of accounting
        "purchase_order":           ["Payables"],
        "po_line":                  ["Payables"],
        "payables":           ["Payables"],
        "receiving":                ["Payables", "Inventory_Transactions"],
        "certification":            ["Payables", "Quality"],
        "suppliers":                ["Payables", "Quality"],
        # Receivables / CRM / Customer_Order — customer/sales side
        "customer":                 ["Receivables", "CRM", "Customer_Order"],
        "customer_address":         ["Receivables", "CRM"],
        "sales":                    ["Receivables", "Customer_Order"],
        "daily_deliveries":         ["Payables", "Receivables", "Customer_Order"],
        # General_Ledger — RM/WIP/FG/COGS cost flow
        "financial_impact":         ["General_Ledger"],
        "quality_costs":            ["General_Ledger", "Quality"],
        # Quality — defect prevention, NCM, corrective actions
        "product_defects":          ["Quality"],
        "non_conformant_materials": ["Quality"],
        "corrective_actions":       ["Quality"],
        "quality_incidents":        ["Quality"],
        "production_quality":       ["Quality", "Manufacturing"],
        "manufacturing_acronyms":   ["Quality"],
        # Work_Orders — routing of resources in sequence (the operation table concept)
        "operation":                ["Work_Orders"],
        "shop_resource":            ["Work_Orders"],
        "service":                  ["Work_Orders"],
        "labor_ticket":             ["Work_Orders"],
        # Manufacturing — production execution, WIP, equipment, schedule
        "work_order":               ["Manufacturing"],
        "production_schedule":      ["Manufacturing", "Customer_Order"],
        "production_lines":         ["Manufacturing"],
        "product_lines":            ["Manufacturing", "Customer_Order"],
        "equipment_metrics":        ["Manufacturing"],
        "downtime_events":          ["Manufacturing"],
        "failure_events":           ["Manufacturing"],
        "effectiveness_metrics":    ["Manufacturing"],
        "maintenance_targets":      ["Manufacturing"],
        "equipment_reliability":    ["Manufacturing"],
        # Inventory_Transactions — material movements and stock
        "material_issue":           ["Inventory_Transactions"],
    }

    _PERSP_LINE_RE = re.compile(r"^--\s*Perspectives?:\s*(.+)", re.IGNORECASE)
    _CANON_PERSPECTIVES = frozenset(
        ["Payables", "Receivables", "General_Ledger",
         "Quality", "Work_Orders", "Manufacturing", "Inventory_Transactions",
         "Customer_Order", "Demand_Forecast", "Engineering", "Parts",
         "CRM", "Visual_Admin"]
    )

    def _parse_declared_perspectives(self, sql_text: str) -> List[str]:
        """Extract canonical perspective names from '-- Perspective: …' header lines
        (first 20 lines only).  Multi-perspective headers like 'Accounts_Payable · Quality'
        return both names in order.
        """
        declared: List[str] = []
        # Sort longest-first so Customer_Order matches before Customer
        canons_sorted = sorted(self._CANON_PERSPECTIVES, key=len, reverse=True)
        for line in sql_text.splitlines()[:20]:
            m = self._PERSP_LINE_RE.match(line)
            if not m:
                continue
            for tok in re.split(r"[·/,]", m.group(1)):
                tok = re.sub(r"\(.*?\)", "", tok).strip().replace(" ", "_")
                tok_norm = tok.lower()
                # Try exact match first, then prefix (both length-sorted)
                for canon in canons_sorted:
                    if tok_norm == canon.lower() or tok_norm.startswith(canon.lower()):
                        if canon not in declared:
                            declared.append(canon)
                        break
        return declared

    def _score_perspectives(self, table_refs: List[Tuple[str, int]]) -> Dict[str, float]:
        """Return normalised perspective scores for a list of (table_name, ref_count) pairs."""
        scores: Dict[str, float] = {}
        total_w = 0.0
        for tbl, cnt in table_refs:
            mapping = self.TABLE_PERSPECTIVE_MAP.get(tbl.lower(), [])
            if not mapping:
                continue
            w = cnt / len(mapping)
            for p in mapping:
                scores[p] = scores.get(p, 0.0) + w
            total_w += cnt
        if not total_w:
            return {}
        return {p: round(s / total_w, 3) for p, s in scores.items()}

    def build_perspective_affinity_index(self, verbose: bool = True) -> Dict[str, Any]:
        """Score every ground-truth SQL file against the perspective affinity map and
        persist the result to SQLite ``query_shape_perspective``.

        Resolution precedence:
          1. Declared perspective(s) from ``-- Perspective:`` file header (SME-ground-truth)
          2. Table affinity inference as fallback / conflict signal

        Returns a summary dict keyed by category_id.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.executescript("""
            CREATE TABLE IF NOT EXISTS query_shape_perspective (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id           TEXT NOT NULL,
                query_name            TEXT NOT NULL,
                declared_perspectives TEXT,
                inferred_primary      TEXT NOT NULL,
                inferred_scores_json  TEXT,
                agreed                INTEGER,
                resolved_perspective  TEXT NOT NULL,
                created_at            TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category_id, query_name)
            );
            DELETE FROM query_shape_perspective;
        """)

        # Load table usage grouped by (category_id, query_name)
        usage_rows = cur.execute(
            "SELECT category_id, query_name, table_name, reference_count "
            "FROM ground_truth_table_usage"
        ).fetchall()
        groups: Dict[tuple, List] = {}
        for r in usage_rows:
            key = (r["category_id"], r["query_name"])
            groups.setdefault(key, []).append((r["table_name"], r["reference_count"]))

        # Build file→declared map
        file_declared: Dict[str, List[str]] = {}
        if os.path.isdir(QUERIES_DIR):
            for entry in os.scandir(QUERIES_DIR):
                if entry.name.endswith(".sql"):
                    stem = os.path.splitext(entry.name)[0]
                    file_declared[stem] = self._parse_declared_perspectives(
                        open(entry.path).read()
                    )

        if verbose:
            print(f"\n{'Category':<40} {'Declared':<28} {'Inferred':<16} {'Match'} Resolved")
            print("─" * 108)

        summary: Dict[str, Any] = {}

        for (cat_id, q_name), table_refs in sorted(groups.items()):
            norm = self._score_perspectives(table_refs)
            ranked = sorted(norm.items(), key=lambda x: -x[1])
            inferred_primary = ranked[0][0] if ranked else "UNMAPPED"

            declared = file_declared.get(cat_id, [])
            declared_str = " · ".join(declared)

            if declared:
                resolved = declared[0]
                agreed = 1 if inferred_primary in declared else 0
            else:
                resolved = inferred_primary
                agreed = -1   # no header declaration

            cur.execute("""
                INSERT OR REPLACE INTO query_shape_perspective
                  (category_id, query_name, declared_perspectives, inferred_primary,
                   inferred_scores_json, agreed, resolved_perspective)
                VALUES (?,?,?,?,?,?,?)
            """, (cat_id, q_name, declared_str, inferred_primary,
                  json.dumps(norm), agreed, resolved))

            summary[cat_id] = {
                "query_name": q_name,
                "declared": declared,
                "inferred_primary": inferred_primary,
                "inferred_scores": norm,
                "resolved": resolved,
                "agreed": agreed,
            }

            if verbose:
                flag = "✓" if agreed == 1 else ("?" if agreed == -1 else "✗")
                print(f"{cat_id:<40} {declared_str:<28} {inferred_primary:<16} {flag:<6} {resolved}")

        conn.commit()
        conn.close()
        return summary

    def get_table_usage(self, table_name: str = None,
                        category_id: str = None) -> List[Dict[str, Any]]:
        """Query the ground_truth_table_usage index.

        Filters by table_name and/or category_id (both optional).
        Returns rows ordered by reference_count DESC.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            clauses, params = [], []
            if table_name:
                clauses.append("table_name = ?")
                params.append(table_name.lower())
            if category_id:
                clauses.append("category_id = ?")
                params.append(category_id)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT query_file, category_id, query_name, table_name, reference_count "
                f"FROM ground_truth_table_usage {where} "
                f"ORDER BY reference_count DESC, category_id, query_name",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
