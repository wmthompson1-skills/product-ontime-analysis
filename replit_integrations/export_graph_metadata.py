#!/usr/bin/env python3
"""
export_graph_metadata.py — Export the structural containment graph from SQLite
in the canonical, readable **fixed 6-slot composite-key** scheme.

This file is the *canonical anchor* for the graph plan: it is self-describing
(it embeds the full key grammar in a ``key_scheme`` block) and is stamped with a
``schema_version`` / ``milestone`` so canonical drafts can be frozen as versioned
snapshots (e.g. ``graph_metadata.v1.json``).

Fixed 6-slot template (delimiter ``:``; every key has EXACTLY 6 slots; a
component may never be empty or contain ``:`` or ``/``)::

    table : column|entity : family : perspective : predicate|none : unique_id|none
      0          1            2          3              4               5

Reserved tokens (the exporter hard-fails if a source name collides):
    entity   slot 1 — placeholder marking a TABLE node (a table has no column)
    none     slots 4-5 — placeholder marking a NODE (no predicate / unique_id)
    system   slot 3 — perspective reserved for the structural layer

Parse by fixed position — no prefix tag needed:
    NODE  iff slot[4]=='none' and slot[5]=='none'
            table node if slot[1]=='entity', else column node
    EDGE  otherwise; its family is slot[2] ('structural' | 'semantic')

    table node       PAYABLE:entity:structural:system:none:none
    column node      PAYABLE:INVOICE_ID:structural:system:none:none
    structural edge  PAYABLE:INVOICE_ID:structural:system:has_column:SYS_HAS_PAY_INV_001
    semantic edge    PAYABLE:INVOICE_ID:semantic:Payables:resolves_to:PAY_RES_PAY_INV_001  [SCAFFOLDED]

**Unified abbreviated unique_id** (slot 5) — BOTH layers share one grammar::

    perspective(3) _ edge_type(3) _ table(3) _ column|entity(3) _ uniqifier(3, default 001)

    structural  SYS_HAS_PAY_INV_001   (system / has_column / PAYABLE / INVOICE_ID / 001)
    semantic    PAY_RES_PAY_INV_001   (Payables / resolves_to / PAYABLE / INVOICE_ID / 001)

Each part is the first 3 alphanumeric characters of its source token, uppercased.
Abbreviation collisions are EXPECTED (e.g. INVOICE_ID and INVENTORY both -> INV)
and are resolved by the uniqifier: it is *allocated* per
(perspective, edge_type, table, column) prefix, counting up from 001 in a
deterministic sorted order so the same DB always yields the same uids.

Milestone scope (this export): the **structural footprint** — table nodes,
column nodes, the has_column backbone edge (table -> column), and the references
edge (child column -> parent column) built from declared foreign keys. The
semantic ``resolves_to`` layer is format-locked AND wired as node-guarded
scaffolding: it reads SME-curated elevations from SQLite and emits self-loop
edges only for columns that are exported canonical nodes, so it stays at zero
content until an SME maps a real ERP column.

Outputs, written next to this script:
    graph_triples.tsv          — flat (subject, predicate, object) triples
    graph_metadata.json        — canonical (latest) graph document
    graph_metadata.v{N}.json   — frozen milestone snapshot (created once, never clobbered)

Run from the repo root:
    python replit_integrations/export_graph_metadata.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_HF_DIR = os.path.join(_REPO_ROOT, "hf-space-inventory-sqlgen")

DB_PATH = os.path.join(_HF_DIR, "app_schema", "manufacturing.db")
TRIPLES_PATH = os.path.join(_HERE, "graph_triples.tsv")
JSON_PATH = os.path.join(_HERE, "graph_metadata.json")
# The living manifest of SME-approved snippets — wired into the canonical graph
# as binding-key nodes + base-table topology edges (v21).
MANIFEST_PATH = os.path.join(
    _HF_DIR, "app_schema", "ground_truth", "reviewer_manifest.json"
)

# Canonical milestone identity — bump these to freeze a new snapshot.
SCHEMA_VERSION = 34
MILESTONE_NAME = "ar_cash_receipts_governed_view"
SNAPSHOT_PATH = os.path.join(_HERE, f"graph_metadata.v{SCHEMA_VERSION}.json")

# ArangoDB collections (canonical target naming; single node + single edge set).
NODE_COLLECTION = "manufacturing_graph_node"
EDGE_COLLECTION = "manufacturing_graph_edge"

# Fixed 6-slot composite-key vocabulary.
FAMILY_STRUCTURAL = "structural"
FAMILY_SEMANTIC = "semantic"        # slot 2 family: the meaning layer (concept nodes + resolves_to edges)
FAMILY_MANIFEST = "manifest"        # slot 2 family: the living-manifest layer (binding nodes + binds_table edges)
PERSPECTIVE_SYSTEM = "system"       # slot 3, structural-layer scope (tables, columns, FK edges)
PERSPECTIVE_CANONICAL = "canonical" # slot 3, perspective-agnostic scope for concept nodes
PLACEHOLDER_ENTITY = "entity"   # slot 1, marks a table or concept node
NONE_SLOT = "none"              # slots 4-5, mark a node
KEY_DELIMITER = ":"
EDGE_PREDICATE_HAS_COLUMN = "has_column"
EDGE_PREDICATE_REFERENCES = "references"
# Canonical column->concept predicate. The symbol name is kept stable across the
# v16 rename; only the stored token changed to ``resolves_to`` (uid abbrev RES).
EDGE_PREDICATE_ELEVATES = "resolves_to"
# Living-manifest predicate (v21): a binding-key node -> each base table it
# touches (its structural fingerprint, as topology edges).
EDGE_PREDICATE_BINDS_TABLE = "binds_table"
# v22: a ``references`` edge's provenance. Declared foreign keys carry
# ``fk_declared`` (join_type NULL — an FK implies an INNER-joinable relationship).
# Joins discovered in SME-approved snippet fingerprints that the schema's FKs did
# NOT already imply carry ``sql_observed`` + the canonical ``join_type`` — folding
# the graph-aware structural fingerprint into the SAME structural edge layer.
ORIGIN_FK_DECLARED = "fk_declared"
ORIGIN_SQL_OBSERVED = "sql_observed"

# SQLite graph source tables. These persist the exact node/edge rows that the
# graph JSON is serialized FROM, so SQLite is the inspectable source of truth and
# the JSON is provably a dump of these tables (see _materialize_to_sqlite /
# _load_nodes_from_sqlite / _load_edges_from_sqlite). One column per JSON field;
# columns that only apply to one node/edge kind are NULL for the others.
SQL_GRAPH_NODES_TABLE = "sql_graph_nodes"
SQL_GRAPH_EDGES_TABLE = "sql_graph_edges"

# Durable SME-authoring input table (written by the Define Relationship UI via
# POST /mcp/tools/commit_edge). The exporter MERGES these rows into the upstream
# foreign-key / elevation feeds below so authored edges survive the delete+
# reinsert materialization of sql_graph_edges. has_column authored rows are a
# no-op here: the derived has_column backbone already covers every column.
AUTHORED_EDGES_TABLE = "sql_graph_authored_edges"

SQL_GRAPH_DDL = """
CREATE TABLE IF NOT EXISTS sql_graph_nodes (
    ordinal       INTEGER NOT NULL,
    _key          TEXT    NOT NULL PRIMARY KEY,
    _id           TEXT    NOT NULL,
    node_type     TEXT    NOT NULL CHECK(node_type IN ('table', 'column', 'concept', 'binding')),
    node_family   TEXT    NOT NULL,
    perspective   TEXT    NOT NULL,
    table_name    TEXT,
    column_name   TEXT,
    column_slot   TEXT,
    concept_name  TEXT,
    concept_type  TEXT,
    domain        TEXT,
    synonyms      TEXT,
    tags          TEXT,
    computation_template TEXT,
    binding_key   TEXT,
    concept_anchor TEXT,
    logic_type    TEXT,
    predicate     TEXT    NOT NULL,
    unique_id     TEXT    NOT NULL,
    description   TEXT,
    column_type   TEXT,
    "notnull"     INTEGER,
    default_value TEXT,
    primary_key   INTEGER,
    foreign_key   INTEGER
);
CREATE TABLE IF NOT EXISTS sql_graph_edges (
    ordinal           INTEGER NOT NULL,
    _key              TEXT    NOT NULL PRIMARY KEY,
    _id               TEXT    NOT NULL,
    _from             TEXT    NOT NULL,
    _to               TEXT    NOT NULL,
    edge_family       TEXT    NOT NULL,
    edge_type         TEXT    NOT NULL CHECK(edge_type IN ('has_column', 'references', 'resolves_to', 'binds_table')),
    perspective       TEXT    NOT NULL,
    unique_id         TEXT    NOT NULL,
    references_table  TEXT,
    references_column TEXT,
    weight            INTEGER,
    priority_weight   INTEGER,
    field_component   INTEGER,
    variable_name     TEXT,
    origin            TEXT,
    join_type         TEXT
);
"""

# Unified abbreviated unique_id (slot 5): 3 chars per part, '_'-joined.
ABBREV_LEN = 3

# Characters a component may never contain: the delimiter and the ArangoDB
# collection separator (banned inside a _key). Empty components are also banned.
FORBIDDEN_IN_COMPONENT = (KEY_DELIMITER, "/")
# Tokens reserved by the grammar — a real schema name may never equal these,
# or slot-position parsing would become ambiguous.
RESERVED_COLUMN_NAMES = frozenset({PLACEHOLDER_ENTITY, NONE_SLOT})
RESERVED_TABLE_NAMES = frozenset({NONE_SLOT})
# A concept name occupies slot 0 but the node is classified by its family
# (slot 2 == semantic) + perspective (slot 3 == canonical), so the name may never
# equal any grammar token or fixed-slot parsing would be ambiguous.
RESERVED_CONCEPT_NAMES = frozenset({
    PLACEHOLDER_ENTITY, NONE_SLOT, PERSPECTIVE_SYSTEM, PERSPECTIVE_CANONICAL,
    FAMILY_STRUCTURAL, FAMILY_SEMANTIC,
})


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _assert_component_safe(*parts: str) -> None:
    """Fail loud if any name would break the composite-key grammar.

    Anti-drift guard: a name containing ':' or '/', or an empty name, would make
    the fixed-slot parser ambiguous, so we refuse to emit it rather than silently
    sanitising (which would lose fidelity with the source schema).
    """
    for p in parts:
        s = str(p)
        if s == "":
            raise ValueError("composite-key components may not be empty")
        for bad in FORBIDDEN_IN_COMPONENT:
            if bad in s:
                raise ValueError(
                    f"name {p!r} contains reserved character {bad!r}; "
                    "composite keys require ':'- and '/'-free components"
                )


def _assert_name_not_reserved(name: str, reserved: frozenset, role: str) -> None:
    """Reject a source name that collides with a reserved grammar token."""
    if str(name) in reserved:
        raise ValueError(
            f"{role} name {name!r} collides with a reserved token "
            f"({', '.join(sorted(reserved))}); rename it in the source schema"
        )


# ---------------------------------------------------------------------------
# Unified abbreviated unique_id (slot 5)
# ---------------------------------------------------------------------------

def _abbrev(name: str) -> str:
    """First ``ABBREV_LEN`` alphanumeric chars of ``name``, uppercased.

    Collisions are expected (INVOICE_ID / INVENTORY both -> INV) and are resolved
    downstream by the allocated uniqifier, not by the abbreviation itself.
    """
    alnum = "".join(ch for ch in str(name) if ch.isalnum())
    if not alnum:
        raise ValueError(f"cannot abbreviate {name!r}: no alphanumeric characters")
    return alnum[:ABBREV_LEN].upper()


def unified_unique_id(perspective: str, edge_type: str, table: str,
                      column: str, uniqifier: int) -> str:
    """Build the unified slot-5 uid: ``PER_EDG_TBL_COL_NNN`` (all 3-char parts)."""
    return "_".join(
        [_abbrev(perspective), _abbrev(edge_type), _abbrev(table),
         _abbrev(column), f"{uniqifier:03d}"]
    )


def _edge_uid_prefix(perspective: str, edge_type: str, table: str, column: str) -> str:
    """Shared uid prefix for any edge whose anchor (perspective, edge_type,
    table, column|entity) abbreviates alike. The uniqifier disambiguates."""
    return "_".join(
        [_abbrev(perspective), _abbrev(edge_type), _abbrev(table), _abbrev(column)]
    )


def semantic_uid_stable(perspective: str, table: str, column: str,
                        concept: str, field_component) -> str:
    """Concept-stable slot-5 uid for a ``resolves_to`` edge.

    Unlike the structural uid (``unified_unique_id``), which counts up per
    (perspective, edge_type, table, column) prefix, the resolves_to uid is DERIVED
    from the elevation's natural key — perspective, table, column, concept, and
    field_component — via a short content hash. A column may carry several
    meanings under one perspective; deriving (not counting) the uid guarantees
    that adding or removing one meaning never renumbers its siblings, so frozen
    vN snapshots and live ArangoDB keys stay stable under concept churn (the M2
    invariant). The readable prefix is kept for inspection; the hash makes the
    uid unique even when two concept names abbreviate alike.
    """
    prefix = "_".join(
        [_abbrev(perspective), _abbrev(EDGE_PREDICATE_ELEVATES),
         _abbrev(table), _abbrev(column)]
    )
    natural = "|".join(
        [str(perspective), str(table), str(column), str(concept),
         str(field_component if field_component is not None else 1)]
    )
    digest = hashlib.sha1(natural.encode("utf-8")).hexdigest()[:8].upper()
    return f"{prefix}_{digest}"


def _containment_prefix(table: str, column: str) -> str:
    """The uid prefix shared by all containment edges that abbreviate alike."""
    return _edge_uid_prefix(PERSPECTIVE_SYSTEM, EDGE_PREDICATE_HAS_COLUMN, table, column)


def allocate_containment_uids(column_nodes: list[dict]) -> dict[tuple, str]:
    """Deterministically allocate a unique uid to every containment edge.

    Columns are processed in (table_name, column_name) sorted order; within each
    abbreviated prefix the uniqifier counts up from 001. This is reproducible for
    a fixed schema, so re-running the export yields identical uids (no drift).
    """
    ordered = sorted(column_nodes, key=lambda c: (c["table_name"], c["column_name"]))
    counter: dict[str, int] = {}
    uid_map: dict[tuple, str] = {}
    for c in ordered:
        t, col = c["table_name"], c["column_name"]
        prefix = _containment_prefix(t, col)
        n = counter.get(prefix, 0) + 1
        counter[prefix] = n
        uid_map[(t, col)] = f"{prefix}_{n:03d}"
    return uid_map


# ---------------------------------------------------------------------------
# Key builders — fixed 6-slot composite scheme.
# ---------------------------------------------------------------------------

def _slots(*parts: str) -> str:
    return KEY_DELIMITER.join(parts)


def table_key(table_name: str) -> str:
    """Table node: ``table:entity:structural:system:none:none`` (6 slots)."""
    _assert_component_safe(table_name)
    _assert_name_not_reserved(table_name, RESERVED_TABLE_NAMES, "table")
    return _slots(
        table_name, PLACEHOLDER_ENTITY, FAMILY_STRUCTURAL,
        PERSPECTIVE_SYSTEM, NONE_SLOT, NONE_SLOT,
    )


def column_key(table_name: str, column_name: str) -> str:
    """Column node: ``table:column:structural:system:none:none`` (6 slots)."""
    _assert_component_safe(table_name, column_name)
    _assert_name_not_reserved(table_name, RESERVED_TABLE_NAMES, "table")
    _assert_name_not_reserved(column_name, RESERVED_COLUMN_NAMES, "column")
    return _slots(
        table_name, column_name, FAMILY_STRUCTURAL,
        PERSPECTIVE_SYSTEM, NONE_SLOT, NONE_SLOT,
    )


def concept_key(concept_name: str) -> str:
    """Concept node: ``<ConceptName>:entity:semantic:canonical:none:none`` (6 slots).

    The concept name anchors slot 0; slot 1 is the ``entity`` placeholder (a
    concept, like a table, is an entity with no column). A concept lives in the
    ``semantic`` family (slot 2) — it is a meaning-layer node, not structural.
    Slot 3 is ``canonical``: a concept is perspective-agnostic (a perspective
    only attaches later, on the ``resolves_to`` edge), and ``canonical`` keeps the
    structural ``system`` scope distinct from the perspective-spanning concept.
    Because slots 4-5 are ``none`` it is a node, and ``semantic`` + node can only
    mean a concept (a semantic *edge* always carries a predicate + perspective).
    """
    _assert_component_safe(concept_name)
    _assert_name_not_reserved(concept_name, RESERVED_CONCEPT_NAMES, "concept")
    return _slots(
        concept_name, PLACEHOLDER_ENTITY, FAMILY_SEMANTIC,
        PERSPECTIVE_CANONICAL, NONE_SLOT, NONE_SLOT,
    )


def has_column_edge_key(table_name: str, column_name: str, unique_id: str) -> str:
    """Structural edge: ``table:column:structural:system:has_column:uid`` (6 slots)."""
    _assert_component_safe(table_name, column_name, unique_id)
    return _slots(
        table_name, column_name, FAMILY_STRUCTURAL, PERSPECTIVE_SYSTEM,
        EDGE_PREDICATE_HAS_COLUMN, unique_id,
    )


def table_id(table_name: str) -> str:
    return f"{NODE_COLLECTION}/{table_key(table_name)}"


def column_id(table_name: str, column_name: str) -> str:
    return f"{NODE_COLLECTION}/{column_key(table_name, column_name)}"


def concept_id(concept_name: str) -> str:
    return f"{NODE_COLLECTION}/{concept_key(concept_name)}"


def has_column_edge_id(table_name: str, column_name: str, unique_id: str) -> str:
    return f"{EDGE_COLLECTION}/{has_column_edge_key(table_name, column_name, unique_id)}"


def references_edge_key(child_table: str, child_column: str, unique_id: str) -> str:
    """Structural edge: ``childtable:childcol:structural:system:references:uid``."""
    _assert_component_safe(child_table, child_column, unique_id)
    return _slots(
        child_table, child_column, FAMILY_STRUCTURAL, PERSPECTIVE_SYSTEM,
        EDGE_PREDICATE_REFERENCES, unique_id,
    )


def references_edge_id(child_table: str, child_column: str, unique_id: str) -> str:
    return f"{EDGE_COLLECTION}/{references_edge_key(child_table, child_column, unique_id)}"


def semantic_edge_key(table: str, column: str, perspective: str, unique_id: str) -> str:
    """Semantic edge: ``table:column:semantic:{perspective}:resolves_to:uid`` (6 slots).

    A real business perspective may never be a reserved perspective token —
    ``system`` (owned by the structural layer) or ``canonical`` (owned by
    concept nodes) — so we hard-fail on either here to keep fixed-slot parsing
    unambiguous.
    """
    _assert_component_safe(table, column, perspective, unique_id)
    _assert_name_not_reserved(
        perspective, frozenset({PERSPECTIVE_SYSTEM, PERSPECTIVE_CANONICAL}), "perspective"
    )
    return _slots(
        table, column, FAMILY_SEMANTIC, perspective,
        EDGE_PREDICATE_ELEVATES, unique_id,
    )


def semantic_edge_id(table: str, column: str, perspective: str, unique_id: str) -> str:
    return f"{EDGE_COLLECTION}/{semantic_edge_key(table, column, perspective, unique_id)}"


# Reserved perspectives a manifest binding may never claim (they are owned by the
# structural / concept layers), so fixed-slot parsing stays unambiguous.
_RESERVED_PERSPECTIVES = frozenset({PERSPECTIVE_SYSTEM, PERSPECTIVE_CANONICAL})


def binding_key_node_key(binding_key: str, perspective: str) -> str:
    """Binding node: ``<binding_key>:entity:manifest:{perspective}:none:none``.

    The manifest binding key anchors slot 0; slot 1 is the ``entity`` placeholder
    (a binding is an entity with no column). It lives in the ``manifest`` family
    (slot 2) — the living-manifest layer, distinct from structural / semantic —
    and carries its real business perspective in slot 3. Slots 4-5 are ``none``,
    so it is a node, and ``manifest`` + node can only mean a binding node.
    """
    _assert_component_safe(binding_key, perspective)
    _assert_name_not_reserved(perspective, _RESERVED_PERSPECTIVES, "perspective")
    return _slots(
        binding_key, PLACEHOLDER_ENTITY, FAMILY_MANIFEST,
        perspective, NONE_SLOT, NONE_SLOT,
    )


def binding_key_node_id(binding_key: str, perspective: str) -> str:
    return f"{NODE_COLLECTION}/{binding_key_node_key(binding_key, perspective)}"


def binds_table_edge_key(binding_key: str, table_name: str, perspective: str,
                         unique_id: str) -> str:
    """Manifest topology edge: ``<binding_key>:<table>:manifest:{perspective}:binds_table:uid``.

    Connects a binding node (``_from``) to a base table node (``_to``) it touches —
    one edge per table in the snippet's structural fingerprint.
    """
    _assert_component_safe(binding_key, table_name, perspective, unique_id)
    _assert_name_not_reserved(perspective, _RESERVED_PERSPECTIVES, "perspective")
    return _slots(
        binding_key, table_name, FAMILY_MANIFEST, perspective,
        EDGE_PREDICATE_BINDS_TABLE, unique_id,
    )


def binds_table_edge_id(binding_key: str, table_name: str, perspective: str,
                        unique_id: str) -> str:
    return f"{EDGE_COLLECTION}/{binds_table_edge_key(binding_key, table_name, perspective, unique_id)}"


def allocate_binds_table_uids(binding_table_pairs: list[tuple]) -> dict[tuple, str]:
    """Deterministically allocate a slot-5 uid to every binds_table edge.

    Pairs ``(binding_key, table_name, perspective)`` are processed in sorted
    order; within each abbreviated prefix the uniqifier counts up from 001, so
    re-running the export yields identical uids (no drift). Mirrors
    ``allocate_containment_uids`` for the structural backbone.
    """
    ordered = sorted(binding_table_pairs)
    counter: dict[str, int] = {}
    uid_map: dict[tuple, str] = {}
    for binding_key, table_name, perspective in ordered:
        prefix = _edge_uid_prefix(
            perspective, EDGE_PREDICATE_BINDS_TABLE, binding_key, table_name
        )
        n = counter.get(prefix, 0) + 1
        counter[prefix] = n
        uid_map[(binding_key, table_name, perspective)] = f"{prefix}_{n:03d}"
    return uid_map


# ---------------------------------------------------------------------------
# Extraction — tables, then columns, then has_column edges
# ---------------------------------------------------------------------------

def _fetch_structure(conn: sqlite3.Connection):
    """Return (table_nodes, column_nodes, pk_map, integrity) from SQLite.

    Tables are the business ERP tables registered in the schema_nodes registry
    (table_type='Table'). The semantic layer's own metadata tables (schema_*)
    are intentionally excluded: the structural graph models the manufacturing
    domain, not the bookkeeping that drives the graph itself. Columns come from
    PRAGMA table_info run against each table; primary-key columns are tracked so
    foreign keys targeting an implicit PK can be resolved. Tables that cannot be
    PRAGMA'd (views, or dropped between registry and DB) are recorded in the
    integrity report rather than failing the export.
    """
    conn.row_factory = sqlite3.Row
    integrity = {
        "tables_without_columns": [],
        "foreign_keys_skipped": [],
        "semantic_elevations_skipped": [],
    }

    desc_map = {
        r["table_name"]: (r["description"] or "")
        for r in conn.execute(
            "SELECT table_name, description FROM schema_nodes"
        ).fetchall()
    }
    table_names = [
        r["table_name"]
        for r in conn.execute(
            "SELECT table_name FROM schema_nodes WHERE table_type = 'Table' "
            "ORDER BY table_name"
        ).fetchall()
    ]
    # Pure business-only scope: the semantic layer's own metadata tables are
    # ignored entirely, even if one were ever registered as table_type='Table'.
    table_names = [t for t in table_names if not t.startswith("schema_")]

    table_nodes: list[dict] = []
    column_nodes: list[dict] = []
    pk_map: dict[str, list[str]] = {}

    for tname in table_names:
        table_nodes.append(
            {
                "_id": table_id(tname),
                "_key": table_key(tname),
                "node_type": "table",
                "node_family": FAMILY_STRUCTURAL,
                "perspective": PERSPECTIVE_SYSTEM,
                "table_name": tname,
                "column_slot": PLACEHOLDER_ENTITY,
                "predicate": NONE_SLOT,
                "unique_id": NONE_SLOT,
                "description": desc_map.get(tname, ""),
            }
        )

        try:
            # Quote the identifier so dotted/live-format names (e.g. dbo.X)
            # are not parsed as schema.table by PRAGMA. "" escapes a literal ".
            quoted = '"' + tname.replace('"', '""') + '"'
            col_rows = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
        except sqlite3.Error:
            col_rows = []

        if not col_rows:
            integrity["tables_without_columns"].append(tname)
            continue

        pks = [c["name"] for c in col_rows if c["pk"]]
        if pks:
            pk_map[tname] = pks

        # Foreign-key child columns for this table, from the same PRAGMA the
        # references edges are built from — so the node flag and the edge can
        # never drift. Stored as a node-level structural fact, symmetric with
        # primary_key/notnull (which come from PRAGMA table_info).
        try:
            fk_child_cols = {
                r["from"]
                for r in conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
            }
        except sqlite3.Error:
            fk_child_cols = set()

        for col in col_rows:
            cname = col["name"]
            column_nodes.append(
                {
                    "_id": column_id(tname, cname),
                    "_key": column_key(tname, cname),
                    "node_type": "column",
                    "node_family": FAMILY_STRUCTURAL,
                    "perspective": PERSPECTIVE_SYSTEM,
                    "table_name": tname,
                    "column_name": cname,
                    "predicate": NONE_SLOT,
                    "unique_id": NONE_SLOT,
                    "column_type": col["type"] or "TEXT",
                    "notnull": bool(col["notnull"]),
                    "default_value": col["dflt_value"],
                    "primary_key": bool(col["pk"]),
                    "foreign_key": cname in fk_child_cols,
                }
            )

    return table_nodes, column_nodes, pk_map, integrity


def _parse_json_list(raw) -> list:
    """Parse a canonical JSON-array string into a list (NULL / bad value => []).

    Concept ``synonyms`` / ``tags`` are stored as a JSON-array string in SQLite.
    A missing, empty, or malformed value degrades to the empty list so one bad
    row can never break the export. Authored order is preserved (never re-sorted).
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _fetch_concept_nodes(conn: sqlite3.Connection) -> list[dict]:
    """Concept nodes — perspective-agnostic semantic entities from schema_concepts.

    The payload is the concept identity (the concept-anchored key) plus the human
    ``description`` (which serves as the definition) and, from M3 on, richer
    glossary metadata: ``concept_type``, ``domain``, and authored JSON-array
    ``synonyms`` / ``tags``. The perspective stays OFF the node — it attaches on
    the resolves_to edge (the dual-namespace rule). Emitted in a deterministic order
    (by concept_name) so the export stays diff-stable. A database without the
    schema_concepts table yields no concept nodes; databases predating the M3
    columns default them ("" for type/domain, [] for synonyms/tags), so the
    SELECT only asks for columns that actually exist.
    """
    conn.row_factory = sqlite3.Row
    try:
        present = {row[1] for row in conn.execute("PRAGMA table_info(schema_concepts)")}
    except sqlite3.Error:
        return []
    if not present:
        return []
    has_type = "concept_type" in present
    has_domain = "domain" in present
    has_syn = "synonyms" in present
    has_tags = "tags" in present
    has_comp = "computation_template" in present
    cols = ["concept_name", "description"]
    if has_type:
        cols.append("concept_type")
    if has_domain:
        cols.append("domain")
    if has_syn:
        cols.append("synonyms")
    if has_tags:
        cols.append("tags")
    if has_comp:
        cols.append("computation_template")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM schema_concepts ORDER BY concept_name"
    ).fetchall()

    concept_nodes: list[dict] = []
    for r in rows:
        name = r["concept_name"]
        concept_nodes.append(
            {
                "_id": concept_id(name),
                "_key": concept_key(name),
                "node_type": "concept",
                "node_family": FAMILY_SEMANTIC,
                "perspective": PERSPECTIVE_CANONICAL,
                "concept_name": name,
                "concept_type": (r["concept_type"] if has_type else None) or "",
                "domain": (r["domain"] if has_domain else None) or "",
                "synonyms": _parse_json_list(r["synonyms"]) if has_syn else [],
                "tags": _parse_json_list(r["tags"]) if has_tags else [],
                # M4: a metric concept carries its dialect-agnostic
                # computation_template; non-metric concepts stay null.
                "computation_template": (
                    (r["computation_template"] if has_comp else None) or None
                ),
                "predicate": NONE_SLOT,
                "unique_id": NONE_SLOT,
                "description": r["description"] or "",
            }
        )
    return concept_nodes


def _fetch_manifest_bindings(table_names: list[str], integrity: dict,
                             manifest_path: str = MANIFEST_PATH):
    """Binding nodes + base-table topology edges from the living manifest (v21).

    Every APPROVED snippet with a signed-off ``structural_fingerprint`` becomes a
    ``binding`` node (family ``manifest``), and one ``binds_table`` edge is drawn
    from that node to each canonical table node its fingerprint names. The
    base-table set is read from the SME-signed fingerprint (not re-parsed from the
    .sql here), so the graph reflects exactly what was approved. Lowercase
    fingerprint tables are mapped to canonical table nodes case-insensitively; a
    fingerprint table that is not an exported table node is recorded in
    ``integrity['manifest_tables_skipped']`` rather than fabricating a dangling
    edge. Only APPROVED entries participate; an APPROVED entry lacking a
    fingerprint is noted in ``integrity['manifest_snippets_skipped']``.

    Returns ``(binding_nodes, binds_table_edges)`` in deterministic order
    (binding nodes by binding_key; edges by (binding_key, table)).
    """
    integrity.setdefault("manifest_tables_skipped", [])
    integrity.setdefault("manifest_snippets_skipped", [])

    if not os.path.exists(manifest_path):
        return [], []

    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    snippets = manifest.get("approved_snippets", {})

    # Case-insensitive map: lowercase table name -> canonical table node name.
    canonical_by_lower = {t.lower(): t for t in table_names}

    binding_nodes: list[dict] = []
    pending_edges: list[tuple] = []  # (binding_key, canonical_table, perspective)

    for binding_key in sorted(snippets):
        entry = snippets[binding_key]
        if entry.get("validation_status") != "APPROVED":
            continue
        fp = entry.get("structural_fingerprint") or {}
        base_tables = list(fp.get("base_tables") or [])
        if not base_tables:
            integrity["manifest_snippets_skipped"].append(binding_key)
            continue

        perspective = entry.get("perspective") or ""
        binding_nodes.append(
            {
                "_id": binding_key_node_id(binding_key, perspective),
                "_key": binding_key_node_key(binding_key, perspective),
                "node_type": "binding",
                "node_family": FAMILY_MANIFEST,
                "perspective": perspective,
                "binding_key": binding_key,
                "concept_anchor": entry.get("concept_anchor") or "",
                "logic_type": entry.get("logic_type") or "",
                "predicate": NONE_SLOT,
                "unique_id": NONE_SLOT,
                "description": entry.get("sme_justification") or "",
            }
        )

        for base in base_tables:
            canonical = canonical_by_lower.get(base.lower())
            if canonical is None:
                integrity["manifest_tables_skipped"].append(f"{binding_key}:{base}")
                continue
            pending_edges.append((binding_key, canonical, perspective))

    uid_map = allocate_binds_table_uids(pending_edges)
    # Emit edges grouped by binding node (binding_key), then table, for a stable,
    # readable order that mirrors the node emission order.
    binds_table_edges: list[dict] = []
    for binding_key, canonical, perspective in sorted(pending_edges):
        uid = uid_map[(binding_key, canonical, perspective)]
        binds_table_edges.append(
            {
                "_id": binds_table_edge_id(binding_key, canonical, perspective, uid),
                "_key": binds_table_edge_key(binding_key, canonical, perspective, uid),
                "_from": binding_key_node_id(binding_key, perspective),
                "_to": table_id(canonical),
                "edge_family": FAMILY_MANIFEST,
                "edge_type": EDGE_PREDICATE_BINDS_TABLE,
                "perspective": perspective,
                "unique_id": uid,
            }
        )

    return binding_nodes, binds_table_edges


def _build_has_column_edges(column_nodes: list[dict], uid_map: dict[tuple, str]) -> list[dict]:
    """One has_column edge per column: parent table --has_column--> column."""
    edges: list[dict] = []
    for c in column_nodes:
        tname = c["table_name"]
        cname = c["column_name"]
        uid = uid_map[(tname, cname)]
        edges.append(
            {
                "_id": has_column_edge_id(tname, cname, uid),
                "_key": has_column_edge_key(tname, cname, uid),
                "_from": table_id(tname),
                "_to": column_id(tname, cname),
                "edge_family": FAMILY_STRUCTURAL,
                "edge_type": EDGE_PREDICATE_HAS_COLUMN,
                "perspective": PERSPECTIVE_SYSTEM,
                "unique_id": uid,
            }
        )
    return edges


def _fetch_foreign_keys(conn: sqlite3.Connection, table_names: list[str],
                        pk_map: dict[str, list[str]]) -> list[dict]:
    """Read declared foreign keys (PRAGMA foreign_key_list) for ``table_names``.

    Returns dicts of {child_table, child_column, parent_table, parent_column}.
    A NULL target column (FK to the parent's implicit PK) is resolved to the
    parent's single primary-key column; ambiguous cases stay None so the edge
    builder can skip them rather than emit a dangling reference.
    """
    fks: list[dict] = []
    for tname in table_names:
        try:
            quoted = '"' + tname.replace('"', '""') + '"'
            rows = conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            parent_table = r["table"]
            parent_col = r["to"]
            if parent_col is None:
                pks = pk_map.get(parent_table, [])
                parent_col = pks[0] if len(pks) == 1 else None
            fks.append(
                {
                    "child_table": tname,
                    "child_column": r["from"],
                    "parent_table": parent_table,
                    "parent_column": parent_col,
                }
            )
    return fks


def _emit_reference_edge(edges: list[dict], counter: dict[str, int],
                         from_table: str, from_col: str,
                         to_table: str, to_col: str,
                         origin: str, join_type: str | None) -> None:
    """Append one ``references``-family structural edge (from_col -> to_col).

    Both declared FKs and SME-observed joins live in this single edge layer,
    distinguished only by ``origin``/``join_type``. The uniqifier is allocated
    from the SHARED ``counter`` keyed by the (system, references, from_table,
    from_col) prefix, so an FK edge and a sql_observed edge that happen to anchor
    on the same column never collide on ``_key``.
    """
    prefix = _edge_uid_prefix(PERSPECTIVE_SYSTEM, EDGE_PREDICATE_REFERENCES,
                              from_table, from_col)
    n = counter.get(prefix, 0) + 1
    counter[prefix] = n
    uid = f"{prefix}_{n:03d}"
    edges.append(
        {
            "_id": references_edge_id(from_table, from_col, uid),
            "_key": references_edge_key(from_table, from_col, uid),
            "_from": column_id(from_table, from_col),
            "_to": column_id(to_table, to_col),
            "edge_family": FAMILY_STRUCTURAL,
            "edge_type": EDGE_PREDICATE_REFERENCES,
            "perspective": PERSPECTIVE_SYSTEM,
            "unique_id": uid,
            "references_table": to_table,
            "references_column": to_col,
            "origin": origin,
            "join_type": join_type,
        }
    )


def _build_references_edges(fk_rows: list[dict], node_index: set,
                            integrity: dict) -> tuple[list[dict], dict, set]:
    """One references edge per FK column pair: child column -> parent column.

    Edges are emitted in a deterministic sorted order; the uniqifier counts up
    within each abbreviated uid prefix (collision-safe, identical to the
    containment allocator). A foreign key whose child or parent column is not an
    exported node is skipped and recorded in the integrity report rather than
    emitting a dangling edge.

    Returns ``(edges, counter, fk_inner_set)`` — the shared uniqifier counter (so
    the sql_observed join builder can continue the SAME allocation) and the set
    of canonical ``(a, ca, b, cb, 'INNER')`` tuples the FKs already imply (so an
    observed INNER join on a declared FK is not stored a second time). Every FK
    edge carries ``origin='fk_declared'`` and ``join_type=None``.
    """
    edges: list[dict] = []
    counter: dict[str, int] = {}
    fk_inner_set: set = set()
    ordered = sorted(
        fk_rows,
        key=lambda f: (
            f["child_table"], f["child_column"],
            f["parent_table"], str(f["parent_column"]),
        ),
    )
    for fk in ordered:
        ct, cc = fk["child_table"], fk["child_column"]
        pt, pc = fk["parent_table"], fk["parent_column"]
        if pc is None or (ct, cc) not in node_index or (pt, pc) not in node_index:
            integrity["foreign_keys_skipped"].append(f"{ct}.{cc} -> {pt}.{pc}")
            continue
        _emit_reference_edge(edges, counter, ct, cc, pt, pc,
                             ORIGIN_FK_DECLARED, None)
        (a, b) = sorted([(ct, cc), (pt, pc)])
        fk_inner_set.add((a[0], a[1], b[0], b[1], "INNER"))
    return edges, counter, fk_inner_set


def _canonical_join_tuple(jd: dict) -> tuple:
    """Normalize a manifest ``join_edges`` dict to the canonical 5-tuple.

    The manifest already stores each edge canonically (endpoints sorted, join
    type oriented relative to that sort — see structural_fingerprint), so this is
    a pure case/shape normalization, NOT a re-derivation.
    """
    return (
        (jd.get("table_a") or "").lower(),
        (jd.get("column_a") or "").lower(),
        (jd.get("table_b") or "").lower(),
        (jd.get("column_b") or "").lower(),
        (jd.get("join_type") or "INNER").upper(),
    )


def _fetch_manifest_join_edges(fk_inner_set: set,
                               manifest_path: str = MANIFEST_PATH) -> list[tuple]:
    """Union of canonical join edges across every APPROVED snippet fingerprint,
    minus those a declared FK already covers (its INNER-joinable relationship).

    Returns a sorted list of canonical ``(a, ca, b, cb, join_type)`` tuples to
    fold into the graph as ``sql_observed`` references edges — exactly the joins
    the schema's FKs did not already imply (non-INNER optionality on an FK pair,
    or a genuine business join with no FK at all). Tolerant of a missing or
    unreadable manifest (returns []). Read-only.
    """
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    union: set = set()
    for entry in (manifest.get("approved_snippets") or {}).values():
        if entry.get("validation_status") != "APPROVED":
            continue
        fp = entry.get("structural_fingerprint") or {}
        for jd in fp.get("join_edges") or []:
            union.add(_canonical_join_tuple(jd))
    return sorted(union - fk_inner_set)


def _build_sql_observed_join_edges(join_rows: list[tuple], node_index: set,
                                   counter: dict[str, int],
                                   integrity: dict) -> list[dict]:
    """Emit ``sql_observed`` references edges for the manifest's discovered joins.

    ``join_rows`` are canonical ``(a, ca, b, cb, join_type)`` tuples (a<=b), so
    ``_from`` is always the lexicographically smaller endpoint and the stored
    edge is already in canonical form — the engine reads it back without
    re-sorting. Endpoints must be exported column nodes or the edge is skipped
    (recorded in the integrity report), exactly like a dangling FK. The shared
    ``counter`` continues the FK uniqifier allocation.
    """
    edges: list[dict] = []
    for (ta, ca, tb, cb, jt) in sorted(join_rows):
        if (ta, ca) not in node_index or (tb, cb) not in node_index:
            integrity.setdefault("sql_observed_joins_skipped", []).append(
                f"{ta}.{ca} <-> {tb}.{cb} [{jt}]"
            )
            continue
        _emit_reference_edge(edges, counter, ta, ca, tb, cb,
                             ORIGIN_SQL_OBSERVED, jt)
    return edges


def _fetch_semantic_elevations(conn: sqlite3.Connection) -> list[dict]:
    """Read SME-approved column elevations from the SQLite semantic tables.

    A ``resolves_to`` edge marks a column as semantically meaningful under a
    business perspective (the Solder Pattern: meaning is SME-curated, never
    generated). The source of truth is the join:

        schema_concept_fields  (table_name, field_name) -> concept
          x schema_perspective_concepts  concept -> perspective (+ priority_weight)
          x schema_perspectives          perspective_id -> perspective_name
          x schema_concepts              concept_id -> concept_name

    This is intentionally read-only scaffolding: it emits an edge only when the
    elevated column is an exported canonical node (see ``_build_elevates_edges``).
    Today the curated rows target a staging table that is not part of the
    business graph, so zero edges are produced until an SME maps a real ERP
    column — the format is locked and the plumbing is live, the content is not.
    """
    # M4: ``variable_name`` names which metric template {variable} this column
    # fills (NULL for categorical/measure elevations). It only exists on databases
    # seeded at M4+, so select it conditionally — an older DB simply omits it and
    # every edge gets variable_name=None downstream.
    try:
        scf_cols = {row[1] for row in conn.execute("PRAGMA table_info(schema_concept_fields)")}
    except sqlite3.Error:
        return []
    var_select = "cf.variable_name AS variable_name" if "variable_name" in scf_cols else "NULL AS variable_name"
    try:
        rows = conn.execute(
            f"""
            SELECT cf.table_name              AS table_name,
                   cf.field_name              AS column_name,
                   p.perspective_name         AS perspective,
                   pc.priority_weight         AS priority_weight,
                   c.concept_name             AS concept,
                   pc.relationship_type       AS relationship,
                   cf.component_index         AS field_component,
                   {var_select}
            FROM schema_concept_fields cf
            JOIN schema_perspective_concepts pc ON pc.concept_id = cf.concept_id
            JOIN schema_perspectives p          ON p.perspective_id = pc.perspective_id
            JOIN schema_concepts c              ON c.concept_id = cf.concept_id
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _build_elevates_edges(elevation_rows: list[dict], node_index: set,
                          concept_index: set, integrity: dict) -> list[dict]:
    """One ``resolves_to`` edge per curated elevation: column node -> concept node.

    M2 re-point: ``_from`` is the elevated column, ``_to`` is the CONCEPT NODE
    the column expresses (it was a self-loop on the column in M1, carrying the
    concept as a string). The concept's identity now lives on the target node,
    so the edge no longer stores a ``concept`` string — the name is encoded in
    ``_to`` and is still an input to the stable uid.

    Both endpoints are node-guarded: an elevation whose column is not an exported
    node, OR whose concept has no concept node, is skipped and recorded in the
    integrity report rather than emitting a dangling edge. ``weight`` is the
    binary gate normalized from ``priority_weight`` (1 iff priority_weight > 0);
    the raw ``priority_weight`` is kept as non-gating metadata. The uid is
    derived from the elevation's natural key (see ``semantic_uid_stable``), so it
    is reproducible and stable when sibling concepts are added or removed.
    """
    edges: list[dict] = []
    ordered = sorted(
        elevation_rows,
        key=lambda r: (
            str(r["perspective"]), r["table_name"], r["column_name"], str(r["concept"]),
        ),
    )
    for r in ordered:
        t, col, persp = r["table_name"], r["column_name"], r["perspective"]
        concept = r["concept"]
        if (t, col) not in node_index:
            integrity["semantic_elevations_skipped"].append(
                f"{persp}:{t}.{col} (column not a canonical node)"
            )
            continue
        if concept not in concept_index:
            integrity["semantic_elevations_skipped"].append(
                f"{persp}:{t}.{col} -> {concept} (concept not a canonical node)"
            )
            continue
        field_component = r.get("field_component", 1) or 1
        priority_weight = r.get("priority_weight")
        weight = 1 if (priority_weight or 0) > 0 else 0
        uid = semantic_uid_stable(persp, t, col, concept, field_component)
        edges.append(
            {
                "_id": semantic_edge_id(t, col, persp, uid),
                "_key": semantic_edge_key(t, col, persp, uid),
                "_from": column_id(t, col),
                "_to": concept_id(concept),
                "edge_family": FAMILY_SEMANTIC,
                "edge_type": EDGE_PREDICATE_ELEVATES,
                "perspective": persp,
                "unique_id": uid,
                "weight": weight,
                "priority_weight": priority_weight,
                "field_component": field_component,
                # M4: only metric bindings name a template variable; categorical /
                # measure elevations stay null.
                "variable_name": r.get("variable_name"),
            }
        )
    return edges


def _fetch_authored_edges(conn: sqlite3.Connection) -> list[dict]:
    """Read SME-authored canonical edges from ``sql_graph_authored_edges``.

    Returns one dict per row. Tolerant if the table does not exist yet (older
    databases that predate the authoring table) — returns an empty list so the
    export still runs. Absent columns are stored as '' in the table.
    """
    try:
        rows = conn.execute(
            f"""
            SELECT edge_type, from_table, from_column, to_table, to_column,
                   perspective, weight, concept
            FROM {AUTHORED_EDGES_TABLE}
            ORDER BY authored_id
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _merge_authored_into_sources(
    authored: list[dict],
    fk_rows: list[dict],
    elevation_rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Fold SME-authored edges into the derived foreign-key / elevation feeds.

    Authored rows flow through the SAME node-guarded builders as the derived
    edges (``_build_references_edges`` / ``_build_elevates_edges``), so an
    authored edge whose endpoints are not canonical nodes is skipped exactly
    like a derived one — no special-casing, no dangling edges. Returns the
    augmented (fk_rows, elevation_rows). De-duplicated against what the derived
    feeds already contain so authoring an edge that the schema already implies
    is a no-op.

    Mapping:
      references → fk row   {child_table, child_column, parent_table, parent_column}
      resolves_to → elevation row {table_name, column_name, perspective,
                                  priority_weight, concept, relationship,
                                  field_component} — the authored SME weight maps
                                  to the raw non-gating ``priority_weight``; the
                                  builder derives the binary ``weight`` from it.
      has_column → ignored (the derived has_column backbone already covers every
                   column; authoring one is recorded for audit but emits nothing)
    """
    merged_fks = list(fk_rows)
    merged_elevations = list(elevation_rows)

    fk_seen = {
        (r["child_table"], r["child_column"], r["parent_table"], r["parent_column"])
        for r in merged_fks
    }
    elevation_seen = {
        (r["table_name"], r["column_name"], str(r["perspective"]), str(r["concept"]))
        for r in merged_elevations
    }

    for a in authored:
        etype = a["edge_type"]
        if etype == EDGE_PREDICATE_REFERENCES:
            child_col = a["from_column"] or None
            parent_col = a["to_column"] or None
            # A column-less FK cannot become a canonical column->column edge.
            if not (child_col and parent_col):
                continue
            key = (a["from_table"], child_col, a["to_table"], parent_col)
            if key in fk_seen:
                continue
            fk_seen.add(key)
            merged_fks.append(
                {
                    "child_table": a["from_table"],
                    "child_column": child_col,
                    "parent_table": a["to_table"],
                    "parent_column": parent_col,
                }
            )
        elif etype == EDGE_PREDICATE_ELEVATES:
            col = a["from_column"] or None
            if not col:
                continue
            key = (a["from_table"], col, str(a["perspective"]), str(a["concept"]))
            if key in elevation_seen:
                continue
            elevation_seen.add(key)
            merged_elevations.append(
                {
                    "table_name": a["from_table"],
                    "column_name": col,
                    "perspective": a["perspective"],
                    "priority_weight": a["weight"],
                    "concept": a["concept"],
                    "relationship": "RESOLVES_TO",
                    "field_component": 1,
                    # M4: authored edges never bind a metric template variable.
                    "variable_name": None,
                }
            )
        # has_column authored rows: intentionally ignored (no-op).

    return merged_fks, merged_elevations


# ---------------------------------------------------------------------------
# Canonical key-grammar spec (embedded so the artifact is self-describing)
# ---------------------------------------------------------------------------

def _key_scheme_spec() -> dict:
    """The full, locked fixed 6-slot composite-key grammar — incl. deferred layer.

    Embedding this in the artifact makes graph_metadata.json self-documenting:
    a reader can recover the entire naming plan from the file alone, which is
    the anti-drift anchor for this milestone.
    """
    return {
        "template": "table|concept : column|entity : family : perspective : predicate|none : unique_id|none",
        "slots": 6,
        "fixed_width": True,
        "delimiter": KEY_DELIMITER,
        "forbidden_in_component": list(FORBIDDEN_IN_COMPONENT) + [""],
        "node_collection": NODE_COLLECTION,
        "edge_collection": EDGE_COLLECTION,
        "reserved_tokens": {
            PLACEHOLDER_ENTITY: "slot 2 (index 1): placeholder marking a TABLE or CONCEPT node (the entity itself — no column)",
            NONE_SLOT: "slots 5-6 (index 4-5): placeholder marking a NODE (no predicate / no unique_id)",
            PERSPECTIVE_SYSTEM: "slot 4 (index 3): reserved structural-layer scope (tables, columns, FK edges)",
            PERSPECTIVE_CANONICAL: "slot 4 (index 3): reserved perspective-agnostic scope for CONCEPT nodes (a concept spans all perspectives; the perspective attaches on the resolves_to edge, not the concept)",
        },
        "name_constraints": (
            "A real column may never be named 'entity' or 'none'; a real table "
            "may never be named 'none'; a business perspective may never be "
            "'system' or 'canonical'; a concept may never be named any grammar "
            "token ('entity', 'none', 'system', 'canonical', 'structural', "
            "'semantic'). The exporter hard-fails if any source name collides."
        ),
        "parse_by": (
            "fixed slot positions: NODE iff slot[4]=='none' and slot[5]=='none' "
            "(concept node if slot[2]=='semantic'; binding node if "
            "slot[2]=='manifest'; else table node if slot[1]=='entity'; else "
            "column node — both structural); otherwise EDGE, whose family is "
            "slot[2] (a manifest edge is binds_table)."
        ),
        "rules": [
            {
                "kind": "table_node",
                "slots": 6,
                "marker": "slot[2]=='structural' and slot[1]=='entity' and slot[4:6]==['none','none']",
                "form": "table:entity:structural:system:none:none",
                "example": "PAYABLE:entity:structural:system:none:none",
                "status": "active",
            },
            {
                "kind": "column_node",
                "slots": 6,
                "marker": "slot[2]=='structural' and slot[1]!='entity' and slot[4:6]==['none','none']",
                "form": "table:column:structural:system:none:none",
                "example": "PAYABLE:INVOICE_ID:structural:system:none:none",
                "status": "active",
            },
            {
                "kind": "concept_node",
                "slots": 6,
                "marker": "slot[2]=='semantic' and slot[1]=='entity' and slot[4:6]==['none','none']",
                "form": "<ConceptName>:entity:semantic:canonical:none:none",
                "example": "OrderLifecycleState:entity:semantic:canonical:none:none",
                "payload": (
                    "M3: concept_type, domain, description (the definition), and "
                    "authored JSON-array synonyms / tags. M4: a metric concept "
                    "(concept_type=='metric') also carries a dialect-agnostic "
                    "computation_template with named {variable} placeholders (NULL "
                    "for non-metric concepts). The perspective stays OFF the node "
                    "(it attaches on the resolves_to edge — dual-namespace). "
                    "A concept may be a glossary-only node with no resolves_to edge yet."
                ),
                "status": "active",
            },
            {
                "kind": "structural_edge",
                "slots": 6,
                "marker": "slot[2]=='structural' and slot[4]!='none'",
                "form": "table:column:structural:system:predicate:unique_id",
                "predicates": [EDGE_PREDICATE_HAS_COLUMN, EDGE_PREDICATE_REFERENCES],
                "examples": {
                    EDGE_PREDICATE_HAS_COLUMN: "PAYABLE:INVOICE_ID:structural:system:has_column:SYS_HAS_PAY_INV_001  (table -> column)",
                    EDGE_PREDICATE_REFERENCES: "schema_concepts:parent_concept_id:structural:system:references:SYS_REF_SCH_PAR_001  (FK child column -> parent column)",
                },
                "example": "PAYABLE:INVOICE_ID:structural:system:has_column:SYS_HAS_PAY_INV_001",
                "status": "active",
            },
            {
                "kind": "semantic_edge",
                "slots": 6,
                "marker": "slot[2]=='semantic' and slot[4]!='none'",
                "form": "table:column:semantic:perspective:resolves_to:unique_id  (_from=column node, _to=concept node)",
                "example": "PAYABLE:INVOICE_ID:semantic:Payables:resolves_to:PAY_RES_PAY_INV_1A2B3C4D",
                "status": "active (node-guarded; column -> concept node, M2 re-point; uid is concept-stable)",
                "payload": (
                    "M4: an edge that binds a column to a metric concept also carries "
                    "variable_name — the template {variable} this column fills (NULL "
                    "for categorical / measure elevations)."
                ),
            },
            {
                "kind": "binding_node",
                "slots": 6,
                "marker": "slot[2]=='manifest' and slot[1]=='entity' and slot[4:6]==['none','none']",
                "form": "<binding_key>:entity:manifest:{perspective}:none:none",
                "example": "inventory_atp_20260703_000004:entity:manifest:Inventory_Transactions:none:none",
                "payload": (
                    "v21 living-manifest binding: an SME-approved snippet identity. "
                    "Carries binding_key, concept_anchor, logic_type, its business "
                    "perspective, and the sme_justification as description. Its "
                    "structural fingerprint (base-table set) is expressed as "
                    "binds_table edges, not stored on the node. Only APPROVED "
                    "snippets become binding nodes."
                ),
                "status": "active",
            },
            {
                "kind": "manifest_edge",
                "slots": 6,
                "marker": "slot[2]=='manifest' and slot[4]!='none'",
                "form": "<binding_key>:<table>:manifest:{perspective}:binds_table:unique_id  (_from=binding node, _to=table node)",
                "predicates": [EDGE_PREDICATE_BINDS_TABLE],
                "example": "inventory_atp_20260703_000004:part:manifest:Inventory_Transactions:binds_table:INV_BIN_INV_PAR_001",
                "status": "active (one edge per base table in the snippet's signed structural fingerprint)",
            },
        ],
        "unique_id_grammar": {
            "structural": "structural edges (has_column, references) share one COUNTED slot-5 grammar",
            "form": "perspective(3)_edge_type(3)_table(3)_column|entity(3)_uniqifier(3, default 001)",
            "abbrev": "first 3 alphanumeric chars of each part, uppercased; collisions are expected and resolved by the uniqifier",
            "uniqifier": "structural only: allocated (not derived) per (perspective, edge_type, table, column|entity) prefix; default '001'",
            "edge_type_key_scope": "one edge_type key per perspective — the 3-char edge_type abbreviation is namespaced within its perspective, not global",
            "structural_example": "SYS_HAS_PAY_INV_001 (system / has_column / PAYABLE / INVOICE_ID / 001)",
            "structural_references_example": "SYS_REF_SCH_PAR_001 (system / references / schema_concepts / parent_concept_id / 001)",
            "resolves_to_uid": "the resolves_to uid is concept-STABLE and DERIVED (not counted): <perspective>_RES_<table>_<column>_<8-hex SHA1 of perspective|table|column|concept|field_component>; deriving it from the edge's natural key means adding/removing one meaning never renumbers a column's other resolves_to edges (M2 invariant)",
            "semantic_example": "PAY_RES_PAY_INV_1A2B3C4D (Payables / resolves_to / PAYABLE / INVOICE_ID / concept-hash)",
        },
    }


# ---------------------------------------------------------------------------
# SQLite graph source tables — persist the graph, then read the JSON back FROM it
# ---------------------------------------------------------------------------

def _bool_to_int(value):
    """Map a JSON boolean (or None) to its SQLite INTEGER storage form."""
    if value is None:
        return None
    return 1 if value else 0


def _json_or_none(value):
    """Serialize a list to a canonical JSON-array string; None stays NULL.

    Concept nodes carry ``synonyms`` / ``tags`` lists (stored as JSON text);
    table / column nodes have no such key, so they store SQL NULL.
    """
    if value is None:
        return None
    return json.dumps(value)


def _sql_graph_nodes_is_stale(conn: sqlite3.Connection) -> bool:
    """True if ``sql_graph_nodes`` predates the concept node type and must be rebuilt.

    The concept node shape needs THREE changes SQLite cannot ALTER in place: the
    ``node_type`` CHECK must admit ``'concept'``, ``table_name`` must be nullable,
    and the ``concept_name`` column must exist. We must detect ALL THREE — not
    just the column — because ``app.py``'s additive boot guard can add
    ``concept_name`` to an old table while leaving the old CHECK and the old
    ``table_name NOT NULL`` intact. Keying the rebuild only on the column would
    then mistake that half-migrated table for an up-to-date one and concept
    inserts would fail (NOT NULL on ``table_name`` / CHECK on ``node_type``).
    """
    info = {row[1]: row for row in conn.execute("PRAGMA table_info(sql_graph_nodes)")}
    if "concept_name" not in info:
        return True
    # M3 widened the concept payload. The table must carry ALL four new columns to
    # round-trip; the app boot guard can bolt some on, so rebuild if any is absent.
    if any(c not in info for c in ("concept_type", "domain", "synonyms", "tags")):
        return True
    # M4 added the metric ``computation_template`` node column. Rebuild when absent
    # so the concept round-trip carries it.
    if "computation_template" not in info:
        return True
    # v21 added the living-manifest binding node columns. Rebuild when any is
    # absent so the binding round-trip carries them.
    if any(c not in info for c in ("binding_key", "concept_anchor", "logic_type")):
        return True
    table_name_col = info.get("table_name")
    if table_name_col is not None and table_name_col[3]:  # PRAGMA "notnull" flag
        return True
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sql_graph_nodes'"
    ).fetchone()
    ddl = (ddl_row[0] if ddl_row else "") or ""
    # The modern CHECK literal is the only place ``'concept'`` (quoted) appears;
    # the concept_name column is unquoted, so this marker is unambiguous.
    if "'concept'" not in ddl:
        return True
    # v21 widened the node_type CHECK to admit ``'binding'``; a table created
    # before v21 admits only the older tokens, so a binding insert would fail.
    if "'binding'" not in ddl:
        return True
    return False


def _sql_graph_edges_is_stale(conn: sqlite3.Connection) -> bool:
    """True if sql_graph_edges predates the current shape and must rebuild.

    Two shape changes SQLite cannot ALTER in place are detected here:
      * M2 dropped the edge ``concept`` string (the concept identity now lives on
        the ``_to`` concept node) and added ``priority_weight`` as non-gating
        metadata. Dropping a column is not an in-place SQLite ALTER, and app.py's
        additive boot guard can add ``priority_weight`` while leaving the old
        ``concept`` column behind — so rebuild when EITHER the legacy ``concept``
        column is still present OR ``priority_weight`` is missing.
      * v16 renamed the canonical column->concept predicate to ``resolves_to``.
        The ``edge_type`` CHECK is fixed at CREATE time, so a table created before
        the rename still admits only the old token and a ``resolves_to`` insert
        would fail its CHECK — rebuild when the live DDL lacks the current token.
    The table is fully regenerated from schema_* on every export, so the drop is
    safe. An absent table is not stale (CREATE handles it).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sql_graph_edges)")}
    if not cols:
        return False
    if "concept" in cols or "priority_weight" not in cols:
        return True
    # M4 added the metric ``variable_name`` edge column. Rebuild when absent so the
    # resolves_to round-trip carries it.
    if "variable_name" not in cols:
        return True
    # v22 added references-edge provenance (``origin``/``join_type``) so joins
    # discovered in snippet fingerprints fold into the structural edge layer.
    # Rebuild when a pre-v22 table lacks them so the round-trip carries them.
    if "origin" not in cols or "join_type" not in cols:
        return True
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sql_graph_edges'"
    ).fetchone()
    ddl = (ddl_row[0] if ddl_row else "") or ""
    if "'resolves_to'" not in ddl:
        return True
    # v21 widened the edge_type CHECK to admit ``'binds_table'``; rebuild when a
    # pre-v21 table lacks the token so a binds_table insert does not fail its CHECK.
    return "'binds_table'" not in ddl


def _ensure_sql_graph_tables(conn: sqlite3.Connection) -> None:
    """Create sql_graph_nodes / sql_graph_edges if they do not yet exist.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so both source
    tables are rebuilt in place when they predate the current shape (each is
    fully regenerated from schema_* on every export, so the drops are safe):
      * sql_graph_nodes — rebuilt when it predates the concept node type (see
        ``_sql_graph_nodes_is_stale``).
      * sql_graph_edges — rebuilt when it predates the current shape (see
        ``_sql_graph_edges_is_stale``): the legacy ``concept`` column is dropped,
        ``priority_weight`` is added, and the edge_type CHECK is refreshed for the
        v16 ``resolves_to`` rename.
    """
    conn.executescript(SQL_GRAPH_DDL)
    if _sql_graph_nodes_is_stale(conn):
        conn.execute("DROP TABLE sql_graph_nodes")
        conn.executescript(SQL_GRAPH_DDL)
    if _sql_graph_edges_is_stale(conn):
        conn.execute("DROP TABLE sql_graph_edges")
        conn.executescript(SQL_GRAPH_DDL)


def _materialize_to_sqlite(conn: sqlite3.Connection, table_nodes: list[dict],
                           column_nodes: list[dict], edges: list[dict],
                           concept_nodes: list[dict] | None = None,
                           binding_nodes: list[dict] | None = None) -> None:
    """Write the canonical nodes/edges into the SQLite graph source tables.

    The tables are emptied and re-filled in a single transaction so the result
    is idempotent (re-running on an unchanged schema yields identical rows). An
    ``ordinal`` column records the exact emission order — table nodes, then
    column nodes, then concept nodes, then binding nodes, then has_column /
    references / resolves_to / binds_table edges — so the JSON read back from
    these tables preserves byte-for-byte ordering.
    """
    _ensure_sql_graph_tables(conn)
    all_nodes = (
        table_nodes + column_nodes + (concept_nodes or []) + (binding_nodes or [])
    )
    with conn:
        conn.execute(f"DELETE FROM {SQL_GRAPH_NODES_TABLE}")
        conn.execute(f"DELETE FROM {SQL_GRAPH_EDGES_TABLE}")
        for i, n in enumerate(all_nodes, start=1):
            conn.execute(
                f"INSERT INTO {SQL_GRAPH_NODES_TABLE} "
                "(ordinal, _key, _id, node_type, node_family, perspective, "
                "table_name, column_name, column_slot, concept_name, concept_type, "
                "domain, synonyms, tags, computation_template, binding_key, "
                "concept_anchor, logic_type, predicate, "
                "unique_id, description, column_type, \"notnull\", default_value, "
                "primary_key, foreign_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    i, n["_key"], n["_id"], n["node_type"], n["node_family"],
                    n["perspective"], n.get("table_name"),
                    n.get("column_name"), n.get("column_slot"),
                    n.get("concept_name"), n.get("concept_type"),
                    n.get("domain"), _json_or_none(n.get("synonyms")),
                    _json_or_none(n.get("tags")), n.get("computation_template"),
                    n.get("binding_key"), n.get("concept_anchor"),
                    n.get("logic_type"),
                    n["predicate"], n["unique_id"], n.get("description"),
                    n.get("column_type"), _bool_to_int(n.get("notnull")),
                    n.get("default_value"), _bool_to_int(n.get("primary_key")),
                    _bool_to_int(n.get("foreign_key")),
                ),
            )
        for i, e in enumerate(edges, start=1):
            conn.execute(
                f"INSERT INTO {SQL_GRAPH_EDGES_TABLE} "
                "(ordinal, _key, _id, _from, _to, edge_family, edge_type, "
                "perspective, unique_id, references_table, references_column, "
                "weight, priority_weight, field_component, variable_name, "
                "origin, join_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    i, e["_key"], e["_id"], e["_from"], e["_to"],
                    e["edge_family"], e["edge_type"], e["perspective"],
                    e["unique_id"], e.get("references_table"),
                    e.get("references_column"), e.get("weight"),
                    e.get("priority_weight"), e.get("field_component"),
                    e.get("variable_name"), e.get("origin"),
                    e.get("join_type"),
                ),
            )


def _node_dict_from_row(row: sqlite3.Row) -> dict:
    """Reconstruct a JSON node dict from a sql_graph_nodes row (exact field set)."""
    if row["node_type"] == "table":
        return {
            "_id": row["_id"],
            "_key": row["_key"],
            "node_type": "table",
            "node_family": row["node_family"],
            "perspective": row["perspective"],
            "table_name": row["table_name"],
            "column_slot": row["column_slot"],
            "predicate": row["predicate"],
            "unique_id": row["unique_id"],
            "description": row["description"] if row["description"] is not None else "",
        }
    if row["node_type"] == "concept":
        return {
            "_id": row["_id"],
            "_key": row["_key"],
            "node_type": "concept",
            "node_family": row["node_family"],
            "perspective": row["perspective"],
            "concept_name": row["concept_name"],
            "concept_type": row["concept_type"] if row["concept_type"] is not None else "",
            "domain": row["domain"] if row["domain"] is not None else "",
            "synonyms": _parse_json_list(row["synonyms"]),
            "tags": _parse_json_list(row["tags"]),
            # M4: metric concepts carry a computation_template; non-metric concepts
            # store NULL and round-trip as None.
            "computation_template": row["computation_template"],
            "predicate": row["predicate"],
            "unique_id": row["unique_id"],
            "description": row["description"] if row["description"] is not None else "",
        }
    if row["node_type"] == "binding":
        # v21 living-manifest binding node: the SME-approved snippet identity. Its
        # base-table topology lives on binds_table edges, not on the node.
        return {
            "_id": row["_id"],
            "_key": row["_key"],
            "node_type": "binding",
            "node_family": row["node_family"],
            "perspective": row["perspective"],
            "binding_key": row["binding_key"],
            "concept_anchor": row["concept_anchor"] if row["concept_anchor"] is not None else "",
            "logic_type": row["logic_type"] if row["logic_type"] is not None else "",
            "predicate": row["predicate"],
            "unique_id": row["unique_id"],
            "description": row["description"] if row["description"] is not None else "",
        }
    return {
        "_id": row["_id"],
        "_key": row["_key"],
        "node_type": "column",
        "node_family": row["node_family"],
        "perspective": row["perspective"],
        "table_name": row["table_name"],
        "column_name": row["column_name"],
        "predicate": row["predicate"],
        "unique_id": row["unique_id"],
        "column_type": row["column_type"],
        "notnull": bool(row["notnull"]),
        "default_value": row["default_value"],
        "primary_key": bool(row["primary_key"]),
        "foreign_key": bool(row["foreign_key"]),
    }


def _edge_dict_from_row(row: sqlite3.Row) -> dict:
    """Reconstruct a JSON edge dict from a sql_graph_edges row (exact field set)."""
    et = row["edge_type"]
    doc = {
        "_id": row["_id"],
        "_key": row["_key"],
        "_from": row["_from"],
        "_to": row["_to"],
        "edge_family": row["edge_family"],
        "edge_type": et,
        "perspective": row["perspective"],
        "unique_id": row["unique_id"],
    }
    if et == EDGE_PREDICATE_REFERENCES:
        doc["references_table"] = row["references_table"]
        doc["references_column"] = row["references_column"]
        # v22: provenance of the relationship. fk_declared edges carry
        # join_type=None; sql_observed edges carry the canonical join type.
        doc["origin"] = row["origin"]
        doc["join_type"] = row["join_type"]
    elif et == EDGE_PREDICATE_ELEVATES:
        doc["weight"] = row["weight"]
        doc["priority_weight"] = row["priority_weight"]
        doc["field_component"] = row["field_component"]
        # M4: metric bindings name a template variable; others round-trip as None.
        doc["variable_name"] = row["variable_name"]
    # v21 binds_table edges carry no payload beyond the shared fields above.
    return doc


def _load_nodes_from_sqlite(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM {SQL_GRAPH_NODES_TABLE} ORDER BY ordinal"
    ).fetchall()
    return [_node_dict_from_row(r) for r in rows]


def _load_edges_from_sqlite(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM {SQL_GRAPH_EDGES_TABLE} ORDER BY ordinal"
    ).fetchall()
    return [_edge_dict_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------

def _build_graph_document(
    nodes: list[dict],
    edges: list[dict],
    integrity: dict,
) -> dict:
    edges_by_type: dict[str, int] = {}
    for e in edges:
        edges_by_type[e["edge_type"]] = edges_by_type.get(e["edge_type"], 0) + 1
    nodes_by_type: dict[str, int] = {}
    for n in nodes:
        nodes_by_type[n["node_type"]] = nodes_by_type.get(n["node_type"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": MILESTONE_NAME,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "source": "sqlite:hf-space-inventory-sqlgen/app_schema/manufacturing.db",
        "description": (
            "Canonical structural graph exported from SQLite in the fixed 6-slot "
            "composite-key scheme: table + column + concept nodes, has_column "
            "edges (table -> column), and references edges (child column -> parent "
            "column) built from declared foreign keys, all with unified "
            "abbreviated unique_ids. Covers the business ERP tables "
            "(schema_* metadata tables excluded — the graph models the domain, "
            "not its own bookkeeping); the semantic ``resolves_to`` layer is active "
            "and node-guarded — each SME-curated elevation is an edge from a "
            "column node to the CONCEPT node it expresses under a business "
            "perspective (M2 re-point), carrying the binary weight gate and the "
            "raw priority_weight. Concept nodes are perspective-agnostic and carry "
            "the M3 payload (concept_type, domain, description as the definition, "
            "and the synonyms/tags arrays, emitted from schema_concepts); "
            "perspective is never stored on the node — it lives only on the "
            "resolves_to edge. M4 adds metrics: a metric concept carries a "
            "dialect-agnostic computation_template, and the resolves_to edges that "
            "bind its template {variable} placeholders to physical columns carry "
            "variable_name. A concept may exist without a resolves_to edge (an orphan "
            "glossary term) until a real ERP column is onboarded."
        ),
        "key_scheme": _key_scheme_spec(),
        "graph": {
            "node_collection": NODE_COLLECTION,
            "edge_collection": EDGE_COLLECTION,
        },
        "counts": {
            "nodes_total": len(nodes),
            "edges_total": len(edges),
            "nodes_by_type": {
                "table": nodes_by_type.get("table", 0),
                "column": nodes_by_type.get("column", 0),
                "concept": nodes_by_type.get("concept", 0),
                "binding": nodes_by_type.get("binding", 0),
            },
            "edges_by_type": edges_by_type,
        },
        "integrity": integrity,
        "nodes": nodes,
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_triples(edges: list[dict], path: str) -> None:
    lines = ["subject\tpredicate\tobject"]
    for e in edges:
        lines.append("\t".join([e["_from"], e["edge_type"], e["_to"]]))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_json(doc: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"ERROR: manufacturing.db not found at {DB_PATH}", file=sys.stderr)
        return 1

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            table_nodes, column_nodes, pk_map, integrity = _fetch_structure(conn)
            concept_nodes = _fetch_concept_nodes(conn)
            table_names = [t["table_name"] for t in table_nodes]
            fk_rows = _fetch_foreign_keys(conn, table_names, pk_map)
            elevation_rows = _fetch_semantic_elevations(conn)

            # Fold SME-authored edges (Define Relationship UI) into the derived
            # feeds so they survive the delete+reinsert of sql_graph_edges. They
            # pass through the same node-guarded builders below.
            authored_rows = _fetch_authored_edges(conn)
            fk_rows, elevation_rows = _merge_authored_into_sources(
                authored_rows, fk_rows, elevation_rows
            )

            uid_map = allocate_containment_uids(column_nodes)
            has_column_edges = _build_has_column_edges(column_nodes, uid_map)
            node_index = {(c["table_name"], c["column_name"]) for c in column_nodes}
            references_edges, ref_counter, fk_inner_set = _build_references_edges(
                fk_rows, node_index, integrity
            )
            # v22: fold graph-aware structural fingerprints in — the joins SME
            # snippets actually use, minus those a declared FK already implies,
            # emitted as sql_observed references edges sharing the FK uniqifier.
            join_rows = _fetch_manifest_join_edges(fk_inner_set)
            sql_observed_join_edges = _build_sql_observed_join_edges(
                join_rows, node_index, ref_counter, integrity
            )
            concept_index = {c["concept_name"] for c in concept_nodes}
            elevates_edges = _build_elevates_edges(
                elevation_rows, node_index, concept_index, integrity
            )

            # v21: fold the living manifest in — binding nodes + base-table
            # topology edges (the SME-signed structural fingerprints).
            binding_nodes, binds_table_edges = _fetch_manifest_bindings(
                table_names, integrity
            )
            edges = (
                has_column_edges + references_edges + sql_observed_join_edges
                + elevates_edges + binds_table_edges
            )

            # Persist the graph into the SQLite source tables, then read it back
            # so the JSON we emit is provably a serialization of those tables
            # (SQLite is the source of truth; the JSON is a dump of it).
            _materialize_to_sqlite(
                conn, table_nodes, column_nodes, edges, concept_nodes,
                binding_nodes,
            )
            nodes = _load_nodes_from_sqlite(conn)
            edges = _load_edges_from_sqlite(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"ERROR: failed to read schema from SQLite: {exc}", file=sys.stderr)
        return 1

    doc = _build_graph_document(nodes, edges, integrity)

    try:
        _write_triples(edges, TRIPLES_PATH)
        _write_json(doc, JSON_PATH)
        # Freeze the milestone snapshot once; never clobber a frozen canonical.
        if os.path.exists(SNAPSHOT_PATH):
            snapshot_action = f"snapshot kept (already frozen): {SNAPSHOT_PATH}"
        else:
            _write_json(doc, SNAPSHOT_PATH)
            snapshot_action = f"snapshot frozen: {SNAPSHOT_PATH}"
    except OSError as exc:
        print(f"ERROR: failed to write export artifacts: {exc}", file=sys.stderr)
        return 1

    print(f"Canonical graph exported (fixed 6-slot scheme, {MILESTONE_NAME})")
    print(f"  triples : {TRIPLES_PATH}  ({len(edges)} rows)")
    print(f"  graph   : {JSON_PATH}")
    print(f"  {snapshot_action}")
    print(f"  nodes   : {doc['counts']['nodes_total']}  ({len(table_nodes)} tables, {len(column_nodes)} columns, {len(concept_nodes)} concepts, {len(binding_nodes)} bindings)")
    print(
        f"  edges   : {doc['counts']['edges_total']}  "
        f"({len(has_column_edges)} {EDGE_PREDICATE_HAS_COLUMN}, "
        f"{len(references_edges)} {EDGE_PREDICATE_REFERENCES} fk_declared "
        f"+ {len(sql_observed_join_edges)} sql_observed, "
        f"{len(elevates_edges)} {EDGE_PREDICATE_ELEVATES}, "
        f"{len(binds_table_edges)} {EDGE_PREDICATE_BINDS_TABLE}) in {EDGE_COLLECTION}"
    )
    # Report any abbreviation collisions that the uniqifier had to disambiguate.
    bumped = sorted({uid.rsplit("_", 1)[0] for uid in uid_map.values()
                     if not uid.endswith("_001")})
    if bumped:
        print(f"  uid     : {len(bumped)} abbreviated prefix(es) needed >1 uniqifier (collisions resolved)")
    if integrity["tables_without_columns"]:
        print(
            f"  WARN    : {len(integrity['tables_without_columns'])} table(s) "
            f"had no PRAGMA columns: {', '.join(integrity['tables_without_columns'])}"
        )
    if integrity["foreign_keys_skipped"]:
        print(
            f"  WARN    : {len(integrity['foreign_keys_skipped'])} foreign key(s) "
            f"skipped (endpoint not an exported node): "
            f"{', '.join(integrity['foreign_keys_skipped'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
