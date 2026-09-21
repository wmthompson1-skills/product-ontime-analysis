"""
ArangoDB Graph Sync
====================
Synchronizes the SQLite semantic layer (intents, concepts, elevation
weights, binding keys, plus perspective bridge rows) into ArangoDB as
a named graph.

Graph structure:
  Vertex collections: intents, concepts, query_bindings, tables, columns
  Edge collections:   elevates, bound_to, contains

  NOTE: the vertex collection is named `query_bindings`, not `bindings` —
  this Arango instance already has a pre-existing, unrelated `bindings`
  EDGE collection (field_components -> concepts UDF mappings from a
  different real-data pipeline). Reusing that name here would crash on
  ArangoDB's edge-attribute validation (ERR 1233) and must not be revisited
  without first confirming the `bindings` collection's role.
  Bridge document collections (composite-key, not graph edges):
    Perspective_Intents   key = (perspective, intent)
    Perspective_Concepts  key = (perspective, concept)

Graph name: manufacturing_graph  (same value as ARANGO_DB env var)

The legacy `perspectives` vertex plus the `operates_within` and
`uses_definition` edge collections have been retired. Perspective is now
carried as a property on every bridge row in Perspective_Intents and
Perspective_Concepts. Consumers resolve (Intent, Field) -> Concept by
looking up bridge rows keyed by perspective, never by traversing
through a Perspective vertex.

Traversal pattern (current):
  (:Intent) -[:ELEVATES {weight}]-> (:Concept)
  (:Intent) -[:BOUND_TO]-> (:Binding)
Plus bridge-row lookups in Perspective_Intents / Perspective_Concepts.

Structural containment layer (Plan-007):
  (:Table) -[:CONTAINS]-> (:Column)
  Sourced from schema_nodes (tables) + PRAGMA table_info (columns).
  This physical layer is intentionally decoupled from the semantic layer.
  Multi-tier traversal: tables -> contains -> columns -> CAN_MEAN -> concepts
"""

import os
import json
import sqlite3
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

SQLITE_DB_PATH = os.path.join(os.path.dirname(__file__), "app_schema", "manufacturing.db")
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "app_schema", "ground_truth", "reviewer_manifest.json")

GRAPH_NAME = os.environ.get("ARANGO_DB", "manufacturing_graph")

VERTEX_COLLECTIONS = ["intents", "concepts", "query_bindings", "tables", "columns"]
EDGE_COLLECTIONS = ["elevates", "bound_to", "contains", "references"]
BRIDGE_COLLECTIONS = ["Perspective_Intents", "Perspective_Concepts"]

LEGACY_VERTEX_COLLECTIONS = ["perspectives"]
LEGACY_EDGE_COLLECTIONS = ["operates_within", "uses_definition"]

EDGE_DEFINITIONS = [
    {
        "edge_collection": "elevates",
        "from_vertex_collections": ["intents"],
        "to_vertex_collections": ["concepts"],
    },
    {
        "edge_collection": "bound_to",
        "from_vertex_collections": ["intents"],
        "to_vertex_collections": ["query_bindings"],
    },
    {
        "edge_collection": "contains",
        "from_vertex_collections": ["tables"],
        "to_vertex_collections": ["columns"],
    },
    {
        "edge_collection": "references",
        "from_vertex_collections": ["columns"],
        "to_vertex_collections": ["columns"],
    },
]


def _composite_key(*parts: str) -> str:
    """Build an ArangoDB-safe composite key from the bridge identity tuple."""
    import re
    safe = [re.sub(r"[^A-Za-z0-9_\-.]", "_", str(p)) for p in parts]
    return "__".join(safe)


@dataclass
class SyncReport:
    timestamp: str = ""
    vertices_synced: Dict[str, int] = field(default_factory=dict)
    vertices_new: Dict[str, int] = field(default_factory=dict)
    vertices_updated: Dict[str, int] = field(default_factory=dict)
    edges_synced: Dict[str, int] = field(default_factory=dict)
    edges_new: Dict[str, int] = field(default_factory=dict)
    edges_updated: Dict[str, int] = field(default_factory=dict)
    vertices_pruned: Dict[str, int] = field(default_factory=dict)
    edges_pruned: Dict[str, int] = field(default_factory=dict)
    stale_tables: List[str] = field(default_factory=list)
    bridges_pruned: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    @property
    def total_vertices(self) -> int:
        return sum(self.vertices_synced.values())

    @property
    def total_edges(self) -> int:
        return sum(self.edges_synced.values())

    @property
    def total_pruned_vertices(self) -> int:
        return sum(self.vertices_pruned.values())

    @property
    def total_pruned_edges(self) -> int:
        return sum(self.edges_pruned.values())

    @property
    def total_pruned_bridges(self) -> int:
        return sum(self.bridges_pruned.values())

    def summary(self) -> str:
        mode = "DRY RUN" if self.dry_run else "LIVE SYNC"
        lines = [f"Graph Sync Report ({self.timestamp})"]
        lines.append(f"Mode: {mode}")
        lines.append(f"Status: {'SUCCESS' if self.success else 'FAILED'}")
        lines.append("")
        lines.append(f"Vertices (total synced: {self.total_vertices}):")
        for coll in self.vertices_synced:
            synced = self.vertices_synced.get(coll, 0)
            new = self.vertices_new.get(coll, 0)
            updated = self.vertices_updated.get(coll, 0)
            if self.dry_run:
                lines.append(f"  {coll}: {synced} to sync")
            else:
                lines.append(f"  {coll}: {synced} synced ({new} new, {updated} updated)")
        lines.append("")
        lines.append(f"Edges (total synced: {self.total_edges}):")
        for coll in self.edges_synced:
            synced = self.edges_synced.get(coll, 0)
            new = self.edges_new.get(coll, 0)
            updated = self.edges_updated.get(coll, 0)
            if self.dry_run:
                lines.append(f"  {coll}: {synced} to sync")
            else:
                lines.append(f"  {coll}: {synced} synced ({new} new, {updated} updated)")
        if self.vertices_pruned or self.edges_pruned:
            lines.append("")
            total_pv = self.total_pruned_vertices
            total_pe = self.total_pruned_edges
            prune_label = "would prune" if self.dry_run else "pruned"
            lines.append(f"Stale containment ({prune_label}: {total_pv} vertices, {total_pe} edges):")
            for coll, count in self.vertices_pruned.items():
                lines.append(f"  {coll}: {count} vertices {prune_label}")
            for coll, count in self.edges_pruned.items():
                lines.append(f"  {coll}: {count} edges {prune_label}")
        if self.bridges_pruned:
            lines.append("")
            prune_label = "would prune" if self.dry_run else "pruned"
            lines.append(
                f"Stale bridge rows ({prune_label}: {self.total_pruned_bridges} rows):"
            )
            for coll, count in self.bridges_pruned.items():
                lines.append(f"  {coll}: {count} rows {prune_label}")
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for e in self.errors:
                lines.append(f"  - {e}")
        return "\n".join(lines)


def get_arango_client():
    from arango import ArangoClient

    raw_host = (
        os.environ.get("ARANGO_HOST")
        or os.environ.get("DATABASE_HOST", "http://localhost:8529")
    ).strip()
    if "arangodb.cloud" in raw_host and ":" not in raw_host.split("//", 1)[-1]:
        raw_host = f"{raw_host}:8529"
    client = ArangoClient(hosts=raw_host)
    return client


def get_arango_db(client):
    db_name = os.environ.get("ARANGO_DB")
    username = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_ROOT_PASSWORD", "")

    sys_db = client.db("_system", username=username, password=password)
    if not sys_db.has_database(db_name):
        sys_db.create_database(db_name)

    db = client.db(db_name, username=username, password=password)
    return db


def ensure_graph(db) -> None:
    if db.has_graph(GRAPH_NAME):
        graph = db.graph(GRAPH_NAME)
        existing_edge_defs = {ed["edge_collection"]: ed for ed in graph.edge_definitions()}
        for ed in EDGE_DEFINITIONS:
            existing = existing_edge_defs.get(ed["edge_collection"])
            if existing is None:
                graph.create_edge_definition(**ed)
            elif (sorted(existing["from_vertex_collections"]) != sorted(ed["from_vertex_collections"])
                  or sorted(existing["to_vertex_collections"]) != sorted(ed["to_vertex_collections"])):
                # Vertex collections drifted from what this script declares (e.g. a
                # rename) — bring the live graph definition back in line. This never
                # touches any document data, only the edge definition's collection set.
                graph.replace_edge_definition(**ed)
    else:
        db.create_graph(
            GRAPH_NAME,
            edge_definitions=EDGE_DEFINITIONS,
        )

    for coll_name in BRIDGE_COLLECTIONS:
        if not db.has_collection(coll_name):
            db.create_collection(coll_name)


def load_schema_containment_data(db_path: str = SQLITE_DB_PATH) -> Dict[str, Any]:
    """Load table and column metadata for the structural containment layer.

    Returns:
        {
          "tables": [{"table_name": str, "description": str}, ...],
          "columns": [{"table_name": str, "column_name": str,
                       "data_type": str, "not_null": bool, "pk": bool}, ...],
        }

    Column data comes from SQLite PRAGMA table_info run against each ERP table
    listed in schema_nodes.  Tables that cannot be PRAGMA'd (e.g. views or
    tables dropped between schema_nodes and the live DB) are skipped with a
    warning in the caller.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    table_rows = conn.execute(
        "SELECT table_name, description FROM schema_nodes "
        "WHERE table_type = 'Table' ORDER BY table_name"
    ).fetchall()

    tables: List[Dict[str, Any]] = [
        {"table_name": r["table_name"], "description": r["description"] or ""}
        for r in table_rows
    ]

    columns: List[Dict[str, Any]] = []
    for tbl in tables:
        tname = tbl["table_name"]
        try:
            col_rows = conn.execute(f"PRAGMA table_info({tname})").fetchall()
            for col in col_rows:
                columns.append({
                    "table_name": tname,
                    "column_name": col["name"],
                    "column_type": col["type"] or "TEXT",
                    "notnull": bool(col["notnull"]),
                    "pk": bool(col["pk"]),
                    "default_value": col["dflt_value"],
                })
        except Exception:
            pass

    conn.close()
    return {"tables": tables, "columns": columns}


def load_schema_foreign_keys(db_path: str = SQLITE_DB_PATH) -> List[Dict[str, Any]]:
    """Read declared foreign keys (PRAGMA foreign_key_list) for every ERP table
    listed in ``schema_nodes``.

    Mirrors the canonical exporter's FK semantics: direction is child → parent,
    and a NULL target column (an FK to the parent's implicit primary key) is
    resolved to the parent's single primary-key column. Ambiguous cases (no PK
    or a composite PK) stay ``None`` so the caller skips them rather than writing
    a dangling references edge. FK *enforcement* is intentionally off in this DB,
    so these are declared-only relationships.

    Returns dicts of {child_table, child_column, parent_table, parent_column}.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    table_rows = conn.execute(
        "SELECT table_name FROM schema_nodes "
        "WHERE table_type = 'Table' ORDER BY table_name"
    ).fetchall()
    table_names = [r["table_name"] for r in table_rows]

    # parent_table -> [primary-key columns], for NULL-target resolution.
    pk_map: Dict[str, List[str]] = {}
    for tname in table_names:
        quoted = '"' + tname.replace('"', '""') + '"'
        try:
            cols = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            pk_map[tname] = [c["name"] for c in cols if c["pk"]]
        except sqlite3.Error:
            pk_map[tname] = []

    fks: List[Dict[str, Any]] = []
    for tname in table_names:
        quoted = '"' + tname.replace('"', '""') + '"'
        try:
            rows = conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            parent_table = r["table"]
            parent_col = r["to"]
            if parent_col is None:
                pks = pk_map.get(parent_table, [])
                parent_col = pks[0] if len(pks) == 1 else None
            fks.append({
                "child_table": tname,
                "child_column": r["from"],
                "parent_table": parent_table,
                "parent_column": parent_col,
            })

    conn.close()
    return fks


def load_sqlite_data(db_path: str = SQLITE_DB_PATH) -> Dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    intents = [dict(r) for r in conn.execute("""
        SELECT intent_id, intent_name, intent_category, description,
               typical_question, primary_binding_key
        FROM schema_intents ORDER BY intent_name
    """).fetchall()]

    perspectives = [dict(r) for r in conn.execute("""
        SELECT perspective_id, perspective_name, description,
               stakeholder_role, priority_focus
        FROM schema_perspectives ORDER BY perspective_name
    """).fetchall()]

    # concept_type was DEPRECATED & REMOVED from schema_concepts. Derive the
    # label for the live graph's concepts vertex by DUCK TYPING the metric
    # template, so the document shape stays stable without the physical column.
    concepts = [dict(r) for r in conn.execute("""
        SELECT concept_id, concept_name,
               CASE WHEN computation_template IS NOT NULL AND computation_template <> ''
                    THEN 'metric' ELSE NULL END AS concept_type,
               description, domain
        FROM schema_concepts ORDER BY concept_name
    """).fetchall()]

    intent_perspectives = [dict(r) for r in conn.execute("""
        SELECT si.intent_name, sp.perspective_name, sip.intent_factor_weight, sip.explanation
        FROM schema_intent_perspectives sip
        JOIN schema_intents si ON sip.intent_id = si.intent_id
        JOIN schema_perspectives sp ON sip.perspective_id = sp.perspective_id
    """).fetchall()]

    intent_concepts = [dict(r) for r in conn.execute("""
        SELECT si.intent_name, sc.concept_name, sic.intent_factor_weight, sic.explanation
        FROM schema_intent_concepts sic
        JOIN schema_intents si ON sic.intent_id = si.intent_id
        JOIN schema_concepts sc ON sic.concept_id = sc.concept_id
    """).fetchall()]

    perspective_concepts = [dict(r) for r in conn.execute("""
        SELECT sp.perspective_name, sc.concept_name,
               spc.relationship_type, spc.priority_weight
        FROM schema_perspective_concepts spc
        JOIN schema_perspectives sp ON spc.perspective_id = sp.perspective_id
        JOIN schema_concepts sc ON spc.concept_id = sc.concept_id
    """).fetchall()]

    conn.close()

    return {
        "intents": intents,
        "perspectives": perspectives,
        "concepts": concepts,
        "intent_perspectives": intent_perspectives,
        "intent_concepts": intent_concepts,
        "perspective_concepts": perspective_concepts,
    }


def load_manifest(manifest_path: str = MANIFEST_PATH) -> Dict[str, Any]:
    if not os.path.exists(manifest_path):
        return {}
    with open(manifest_path, "r") as f:
        return json.load(f)


def _upsert_vertex(collection, key: str, doc: Dict[str, Any]) -> bool:
    doc["_key"] = key
    if collection.has(key):
        collection.update(doc)
        return False
    else:
        collection.insert(doc)
        return True


def _upsert_edge(collection, from_id: str, to_id: str, key: str, doc: Dict[str, Any]) -> bool:
    doc["_key"] = key
    doc["_from"] = from_id
    doc["_to"] = to_id
    if collection.has(key):
        collection.update(doc)
        return False
    else:
        collection.insert(doc)
        return True


def prune_stale_containment(
    db,
    db_path: str = SQLITE_DB_PATH,
    report: Optional["SyncReport"] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove table/column vertices (and their CONTAINS edges) that no longer
    exist in the SQLite ``schema_nodes`` table.

    This reconcile pass is intentionally gated — call it only when the caller
    explicitly requests a purge (``--purge-stale`` / ``GRAPH_PRUNE_STALE=1``).

    Returns a dict with keys:
        tables_pruned     : int   — table vertices removed (or would remove in dry-run)
        columns_pruned    : int   — column vertices removed
        edges_pruned      : int   — contains edges removed
        references_pruned : int   — foreign-key (references) edges removed
        stale_table_names : List[str]  — names of the stale tables detected
    """
    from arangodb_helpers.manufacturing_graph_version_0_0_1 import (
        table_key, TABLES_COLLECTION, COLUMNS_COLLECTION, CONTAINS_EDGE_COLLECTION,
        REFERENCES_EDGE_COLLECTION,
    )

    result: Dict[str, Any] = {
        "tables_pruned": 0,
        "columns_pruned": 0,
        "edges_pruned": 0,
        "references_pruned": 0,
        "stale_table_names": [],
    }

    conn = sqlite3.connect(db_path)
    live_rows = conn.execute(
        "SELECT table_name FROM schema_nodes WHERE table_type = 'Table'"
    ).fetchall()
    conn.close()
    live_keys: set = {table_key(r[0]) for r in live_rows}

    tables_coll = db.collection(TABLES_COLLECTION)
    cursor = db.aql.execute(
        f"FOR t IN {TABLES_COLLECTION} RETURN t._key",
        batch_size=500,
    )
    arango_keys: set = set(cursor)

    stale_keys = arango_keys - live_keys
    if not stale_keys:
        return result

    for stale_key in sorted(stale_keys):
        tbl_id = f"{TABLES_COLLECTION}/{stale_key}"
        stale_name = stale_key.replace("table::", "", 1)
        result["stale_table_names"].append(stale_name)

        edges_deleted = 0
        cols_deleted = 0

        if not dry_run:
            edge_cursor = db.aql.execute(
                f"FOR e IN {CONTAINS_EDGE_COLLECTION} "
                f"FILTER e._from == @tbl_id "
                f"REMOVE e IN {CONTAINS_EDGE_COLLECTION} "
                f"RETURN 1",
                bind_vars={"tbl_id": tbl_id},
                batch_size=500,
            )
            edges_deleted = sum(1 for _ in edge_cursor)

            col_cursor = db.aql.execute(
                f"FOR c IN {COLUMNS_COLLECTION} "
                f"FILTER c.table_name == @tname "
                f"REMOVE c IN {COLUMNS_COLLECTION} "
                f"RETURN 1",
                bind_vars={"tname": stale_name},
                batch_size=500,
            )
            cols_deleted = sum(1 for _ in col_cursor)

            if tables_coll.has(stale_key):
                tables_coll.delete(stale_key)
        else:
            edge_cursor = db.aql.execute(
                f"FOR e IN {CONTAINS_EDGE_COLLECTION} "
                f"FILTER e._from == @tbl_id "
                f"RETURN 1",
                bind_vars={"tbl_id": tbl_id},
                batch_size=500,
            )
            edges_deleted = sum(1 for _ in edge_cursor)

            col_cursor = db.aql.execute(
                f"FOR c IN {COLUMNS_COLLECTION} "
                f"FILTER c.table_name == @tname "
                f"RETURN 1",
                bind_vars={"tname": stale_name},
                batch_size=500,
            )
            cols_deleted = sum(1 for _ in col_cursor)

        result["tables_pruned"] += 1
        result["columns_pruned"] += cols_deleted
        result["edges_pruned"] += edges_deleted

        if report is not None:
            action = "would prune" if dry_run else "pruned"
            report.warnings.append(
                f"Stale table {action}: {stale_name!r} "
                f"({cols_deleted} columns, {edges_deleted} contains edges)"
            )

    # Foreign-key edges dangle when EITHER endpoint table is pruned — the child
    # (table_name) or the parent (references_table). A single edge can connect
    # two stale tables, so this is handled ONCE over all stale names (not inside
    # the per-table loop) to avoid double-counting in the dry-run accounting.
    stale_names = result["stale_table_names"]
    if stale_names:
        if not dry_run:
            ref_cursor = db.aql.execute(
                f"FOR e IN {REFERENCES_EDGE_COLLECTION} "
                f"FILTER e.table_name IN @names OR e.references_table IN @names "
                f"REMOVE e IN {REFERENCES_EDGE_COLLECTION} "
                f"RETURN 1",
                bind_vars={"names": stale_names},
                batch_size=500,
            )
        else:
            ref_cursor = db.aql.execute(
                f"FOR e IN {REFERENCES_EDGE_COLLECTION} "
                f"FILTER e.table_name IN @names OR e.references_table IN @names "
                f"RETURN 1",
                bind_vars={"names": stale_names},
                batch_size=500,
            )
        refs_deleted = sum(1 for _ in ref_cursor)
        result["references_pruned"] += refs_deleted

        if report is not None and refs_deleted:
            action = "would prune" if dry_run else "pruned"
            report.warnings.append(
                f"Stale references {action}: {refs_deleted} edges "
                f"touching {len(stale_names)} stale table(s)"
            )

    return result


def prune_stale_bridges(db, data: Dict[str, Any],
                        report: Optional["SyncReport"] = None,
                        dry_run: bool = False) -> Dict[str, int]:
    """Remove ArangoDB bridge rows that no longer exist in the SQLite source.

    Bridge collections (Perspective_Intents, Perspective_Concepts) are pure
    projections of the SQLite source-of-truth tables. Unlike structural
    containment (tables/columns), there is no scenario where an Arango bridge
    row should outlive its SQLite source, so this reconcile runs on every
    sync — not gated behind ``--purge-stale``.

    A row is stale when its ``_key`` is absent from the set of composite keys
    derived from the freshly-loaded SQLite data. Returns a per-collection
    count of removed (or, in dry-run, would-be-removed) rows.
    """
    live_keys = {
        "Perspective_Intents": {
            _composite_key(ip["perspective_name"], ip["intent_name"])
            for ip in data.get("intent_perspectives", [])
        },
        "Perspective_Concepts": {
            _composite_key(pc["perspective_name"], pc["concept_name"])
            for pc in data.get("perspective_concepts", [])
        },
    }

    pruned: Dict[str, int] = {}
    for coll_name, keep in live_keys.items():
        if not db.has_collection(coll_name):
            continue
        coll = db.collection(coll_name)
        arango_keys = set(db.aql.execute(
            f"FOR d IN {coll_name} RETURN d._key", batch_size=500
        ))
        stale = arango_keys - keep
        if not stale:
            continue
        if not dry_run:
            for k in stale:
                try:
                    coll.delete(k)
                except Exception as ex:
                    if report is not None:
                        report.warnings.append(
                            f"Bridge prune {coll_name}/{k!r}: {ex}"
                        )
        pruned[coll_name] = len(stale)
        if report is not None:
            action = "would prune" if dry_run else "pruned"
            report.warnings.append(
                f"Stale {coll_name} {action}: {len(stale)} orphan row(s)"
            )

    return pruned


def sync_graph(db_path: str = SQLITE_DB_PATH,
               manifest_path: str = MANIFEST_PATH,
               dry_run: bool = False,
               purge_stale: bool = False) -> SyncReport:
    report = SyncReport(timestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))

    try:
        data = load_sqlite_data(db_path)
        manifest = load_manifest(manifest_path)
    except Exception as e:
        report.errors.append(f"Failed to load source data: {e}")
        return report

    if dry_run:
        report.dry_run = True
        report.vertices_synced = {
            "intents": len(data["intents"]),
            "concepts": len(data["concepts"]),
            "query_bindings": len(manifest.get("approved_snippets", {})),
            "Perspective_Intents": len(data["intent_perspectives"]),
            "Perspective_Concepts": len(data["perspective_concepts"]),
        }
        report.edges_synced = {
            "elevates": len(data["intent_concepts"]),
            "bound_to": sum(1 for i in data["intents"] if i.get("primary_binding_key")),
        }
        report.warnings.append("DRY RUN — no changes written to ArangoDB")
        return report

    try:
        client = get_arango_client()
        db = get_arango_db(client)
        ensure_graph(db)
    except Exception as e:
        report.errors.append(f"ArangoDB connection failed: {e}")
        return report

    graph = db.graph(GRAPH_NAME)

    bridge_keys = ["intents", "concepts", "query_bindings", "Perspective_Intents", "Perspective_Concepts"]
    v_synced = {k: 0 for k in bridge_keys}
    v_new = {k: 0 for k in bridge_keys}
    v_updated = {k: 0 for k in bridge_keys}

    intent_coll = graph.vertex_collection("intents")
    for intent in data["intents"]:
        key = intent["intent_name"]
        doc = {
            "intent_name": intent["intent_name"],
            "intent_category": intent["intent_category"],
            "description": intent.get("description", ""),
            "typical_question": intent.get("typical_question", ""),
            "primary_binding_key": intent.get("primary_binding_key", ""),
            "synced_at": report.timestamp,
        }
        try:
            is_new = _upsert_vertex(intent_coll, key, doc)
            v_synced["intents"] += 1
            if is_new:
                v_new["intents"] += 1
            else:
                v_updated["intents"] += 1
        except Exception as e:
            report.warnings.append(f"Intent '{key}': {e}")

    concept_coll = graph.vertex_collection("concepts")
    for concept in data["concepts"]:
        key = concept["concept_name"]
        doc = {
            "concept_name": concept["concept_name"],
            "concept_type": concept.get("concept_type") or "",
            "description": concept.get("description", ""),
            "domain": concept.get("domain", ""),
            "synced_at": report.timestamp,
        }
        try:
            is_new = _upsert_vertex(concept_coll, key, doc)
            v_synced["concepts"] += 1
            if is_new:
                v_new["concepts"] += 1
            else:
                v_updated["concepts"] += 1
        except Exception as e:
            report.warnings.append(f"Concept '{key}': {e}")

    bindings_coll = graph.vertex_collection("query_bindings")
    approved = manifest.get("approved_snippets", {})
    for bk, entry in approved.items():
        doc = {
            "binding_key": bk,
            "concept_anchor": entry.get("concept_anchor", ""),
            "perspective": entry.get("perspective", ""),
            "category": entry.get("category", ""),
            "logic_type": entry.get("logic_type", ""),
            "file_path": entry.get("file_path", ""),
            "sme_justification": entry.get("sme_justification", ""),
            "validation_status": entry.get("validation_status", ""),
            "approved_by": entry.get("approved_by", ""),
            "synced_at": report.timestamp,
        }
        try:
            is_new = _upsert_vertex(bindings_coll, bk, doc)
            v_synced["query_bindings"] += 1
            if is_new:
                v_new["query_bindings"] += 1
            else:
                v_updated["query_bindings"] += 1
        except Exception as e:
            report.warnings.append(f"Binding '{bk}': {e}")

    pi_coll = db.collection("Perspective_Intents")
    for ip in data["intent_perspectives"]:
        persp = ip["perspective_name"]
        intent = ip["intent_name"]
        key = _composite_key(persp, intent)
        doc = {
            "perspective": persp,
            "intent": intent,
            "weight": ip.get("intent_factor_weight", 1),
            "explanation": ip.get("explanation", ""),
            "synced_at": report.timestamp,
        }
        try:
            is_new = _upsert_vertex(pi_coll, key, doc)
            v_synced["Perspective_Intents"] += 1
            if is_new:
                v_new["Perspective_Intents"] += 1
            else:
                v_updated["Perspective_Intents"] += 1
        except Exception as e:
            report.warnings.append(f"Perspective_Intents '{key}': {e}")

    report.vertices_synced = v_synced
    report.vertices_new = v_new
    report.vertices_updated = v_updated

    e_synced = {"elevates": 0, "bound_to": 0}
    e_new = {"elevates": 0, "bound_to": 0}
    e_updated = {"elevates": 0, "bound_to": 0}

    # Build intent → [perspective_names] lookup from already-loaded bridge data.
    # Used to stamp perspectives onto elevates and bound_to edges so consumers
    # can filter/route by perspective without a separate bridge-collection lookup.
    _intent_perspectives: Dict[str, list] = {}
    for ip in data["intent_perspectives"]:
        _intent_perspectives.setdefault(ip["intent_name"], []).append(
            ip["perspective_name"]
        )

    el_coll = graph.edge_collection("elevates")
    for ic in data["intent_concepts"]:
        from_id = f"intents/{ic['intent_name']}"
        to_id = f"concepts/{ic['concept_name']}"
        key = f"{ic['intent_name']}__{ic['concept_name']}"
        weight = ic.get("intent_factor_weight", 0)
        doc = {
            "weight": weight,
            "explanation": ic.get("explanation", ""),
            "relationship": "ELEVATES" if weight >= 1 else "NEUTRAL" if weight == 0 else "SUPPRESSES",
            "perspectives": sorted(_intent_perspectives.get(ic["intent_name"], [])),
            "synced_at": report.timestamp,
        }
        try:
            is_new = _upsert_edge(el_coll, from_id, to_id, key, doc)
            e_synced["elevates"] += 1
            if is_new:
                e_new["elevates"] += 1
            else:
                e_updated["elevates"] += 1
        except Exception as e:
            report.warnings.append(f"elevates '{key}': {e}")

    pc_coll = db.collection("Perspective_Concepts")
    for pc in data["perspective_concepts"]:
        persp = pc["perspective_name"]
        concept = pc["concept_name"]
        key = _composite_key(persp, concept)
        doc = {
            "perspective": persp,
            "concept": concept,
            "relationship_type": pc.get("relationship_type", "USES_DEFINITION"),
            "priority_weight": pc.get("priority_weight", 1),
            "synced_at": report.timestamp,
        }
        try:
            is_new = _upsert_vertex(pc_coll, key, doc)
            v_synced["Perspective_Concepts"] += 1
            if is_new:
                v_new["Perspective_Concepts"] += 1
            else:
                v_updated["Perspective_Concepts"] += 1
        except Exception as e:
            report.warnings.append(f"Perspective_Concepts '{key}': {e}")

    report.vertices_synced = v_synced
    report.vertices_new = v_new
    report.vertices_updated = v_updated

    # Bridge collections mirror the SQLite source-of-truth exactly. Remove any
    # Arango rows whose source row was deleted/changed so the bridge-health
    # check stays IN SYNC. Runs every sync (not gated) — see prune_stale_bridges.
    try:
        bridge_pruned = prune_stale_bridges(db, data, report=report, dry_run=dry_run)
        if bridge_pruned:
            report.bridges_pruned.update(bridge_pruned)
    except Exception as ex:
        report.warnings.append(f"Stale bridge prune skipped: {ex}")

    bt_coll = graph.edge_collection("bound_to")
    for intent in data["intents"]:
        bk = intent.get("primary_binding_key")
        if bk and bk in approved:
            from_id = f"intents/{intent['intent_name']}"
            to_id = f"query_bindings/{bk}"
            key = f"{intent['intent_name']}__{bk}"
            doc = {
                "relationship": "BOUND_TO",
                "perspectives": sorted(_intent_perspectives.get(intent["intent_name"], [])),
                "synced_at": report.timestamp,
            }
            try:
                is_new = _upsert_edge(bt_coll, from_id, to_id, key, doc)
                e_synced["bound_to"] += 1
                if is_new:
                    e_new["bound_to"] += 1
                else:
                    e_updated["bound_to"] += 1
            except Exception as e:
                report.warnings.append(f"bound_to '{key}': {e}")

    report.edges_synced = e_synced
    report.edges_new = e_new
    report.edges_updated = e_updated

    # ── Structural Containment Layer (Plan-007) ───────────────────────────────
    # tables (vertex) → contains (edge) → columns (vertex)
    # Source: schema_nodes + PRAGMA table_info per ERP table.
    # These collections are intentionally separate from the semantic layer.
    try:
        from arangodb_helpers.manufacturing_graph_version_0_0_1 import (
            table_key, table_vertex, column_vertex, contains_edge, references_edge,
            TABLES_COLLECTION, COLUMNS_COLLECTION, CONTAINS_EDGE_COLLECTION,
            REFERENCES_EDGE_COLLECTION,
        )

        containment_data = load_schema_containment_data(db_path)
        tables_coll = graph.vertex_collection(TABLES_COLLECTION)
        columns_coll = graph.vertex_collection(COLUMNS_COLLECTION)
        contains_coll = graph.edge_collection(CONTAINS_EDGE_COLLECTION)

        sc_v_synced: Dict[str, int] = {TABLES_COLLECTION: 0, COLUMNS_COLLECTION: 0}
        sc_v_new: Dict[str, int] = {TABLES_COLLECTION: 0, COLUMNS_COLLECTION: 0}
        sc_v_updated: Dict[str, int] = {TABLES_COLLECTION: 0, COLUMNS_COLLECTION: 0}
        sc_e_synced: Dict[str, int] = {CONTAINS_EDGE_COLLECTION: 0}
        sc_e_new: Dict[str, int] = {CONTAINS_EDGE_COLLECTION: 0}
        sc_e_updated: Dict[str, int] = {CONTAINS_EDGE_COLLECTION: 0}

        for tbl in containment_data["tables"]:
            tname = tbl["table_name"]
            doc = table_vertex(tname, description=tbl["description"],
                               synced_at=report.timestamp)
            tbl_key = table_key(tname)   # e.g. "table::CORRECTIVE_ACTIONS"
            try:
                is_new = _upsert_vertex(tables_coll, tbl_key, doc)
                sc_v_synced[TABLES_COLLECTION] += 1
                (sc_v_new if is_new else sc_v_updated)[TABLES_COLLECTION] += 1
            except Exception as ex:
                report.warnings.append(f"tables '{tname}': {ex}")

        for col in containment_data["columns"]:
            tname = col["table_name"]
            cname = col["column_name"]
            col_doc = column_vertex(
                tname, cname,
                column_type=col["column_type"],
                notnull=col["notnull"],
                pk=col["pk"],
                default_value=col.get("default_value"),
                synced_at=report.timestamp,
            )
            edge_doc = contains_edge(tname, cname, synced_at=report.timestamp)
            col_key = col_doc["_key"]
            edge_key = edge_doc["_key"]
            try:
                is_new = _upsert_vertex(columns_coll, col_key, col_doc)
                sc_v_synced[COLUMNS_COLLECTION] += 1
                (sc_v_new if is_new else sc_v_updated)[COLUMNS_COLLECTION] += 1
            except Exception as ex:
                report.warnings.append(f"columns '{col_key}': {ex}")
            try:
                from_id = edge_doc["_from"]
                to_id = edge_doc["_to"]
                clean_doc = {k: v for k, v in edge_doc.items()
                             if k not in ("_key", "_from", "_to")}
                is_new = _upsert_edge(contains_coll, from_id, to_id, edge_key, clean_doc)
                sc_e_synced[CONTAINS_EDGE_COLLECTION] += 1
                (sc_e_new if is_new else sc_e_updated)[CONTAINS_EDGE_COLLECTION] += 1
            except Exception as ex:
                report.warnings.append(f"contains '{edge_key}': {ex}")

        # ── Structural Foreign-Key Layer ──────────────────────────────────────
        # columns (vertex) → references (edge) → columns (vertex)
        # Source: PRAGMA foreign_key_list per ERP table (declared FKs only —
        # enforcement is intentionally off). Same child→parent direction and
        # NULL-target resolution as the canonical exporter. Runs AFTER the column
        # upserts above so both endpoint vertices already exist.
        references_coll = graph.edge_collection(REFERENCES_EDGE_COLLECTION)
        node_index = {(c["table_name"], c["column_name"])
                      for c in containment_data["columns"]}
        sc_e_synced[REFERENCES_EDGE_COLLECTION] = 0
        sc_e_new[REFERENCES_EDGE_COLLECTION] = 0
        sc_e_updated[REFERENCES_EDGE_COLLECTION] = 0

        for fk in load_schema_foreign_keys(db_path):
            ct, cc = fk["child_table"], fk["child_column"]
            pt, pc = fk["parent_table"], fk["parent_column"]
            # Skip dangling/ambiguous FKs rather than write an edge whose
            # endpoint column vertex does not exist (mirrors the exporter).
            if pc is None or (ct, cc) not in node_index or (pt, pc) not in node_index:
                report.warnings.append(
                    f"references skipped (dangling): {ct}.{cc} -> {pt}.{pc}"
                )
                continue
            ref_doc = references_edge(ct, cc, pt, pc, synced_at=report.timestamp)
            ref_key = ref_doc["_key"]
            try:
                from_id = ref_doc["_from"]
                to_id = ref_doc["_to"]
                clean_doc = {k: v for k, v in ref_doc.items()
                             if k not in ("_key", "_from", "_to")}
                is_new = _upsert_edge(references_coll, from_id, to_id, ref_key, clean_doc)
                sc_e_synced[REFERENCES_EDGE_COLLECTION] += 1
                (sc_e_new if is_new else sc_e_updated)[REFERENCES_EDGE_COLLECTION] += 1
            except Exception as ex:
                report.warnings.append(f"references '{ref_key}': {ex}")

        report.vertices_synced.update(sc_v_synced)
        report.vertices_new.update(sc_v_new)
        report.vertices_updated.update(sc_v_updated)
        report.edges_synced.update(sc_e_synced)
        report.edges_new.update(sc_e_new)
        report.edges_updated.update(sc_e_updated)

        if purge_stale:
            try:
                prune_result = prune_stale_containment(
                    db, db_path=db_path, report=report, dry_run=dry_run
                )
                if prune_result["tables_pruned"] > 0:
                    report.vertices_pruned[TABLES_COLLECTION] = prune_result["tables_pruned"]
                    report.vertices_pruned[COLUMNS_COLLECTION] = prune_result["columns_pruned"]
                    report.edges_pruned[CONTAINS_EDGE_COLLECTION] = prune_result["edges_pruned"]
                    report.edges_pruned[REFERENCES_EDGE_COLLECTION] = prune_result["references_pruned"]
                    report.stale_tables = list(prune_result["stale_table_names"])
            except Exception as ex:
                report.warnings.append(f"Stale containment prune failed: {ex}")

    except Exception as ex:
        report.warnings.append(f"Structural containment sync skipped: {ex}")

    return report


if __name__ == "__main__":
    import sys

    dry = "--dry-run" in sys.argv
    purge = "--purge-stale" in sys.argv or os.environ.get("GRAPH_PRUNE_STALE", "").strip() == "1"

    print("=" * 60)
    print("ArangoDB GRAPH SYNC")
    print("=" * 60)

    if dry:
        print("MODE: DRY RUN (no changes will be written)\n")
    else:
        print("MODE: LIVE SYNC\n")

    if purge:
        print("PRUNE: stale containment vertices/edges will be removed\n")

    report = sync_graph(dry_run=dry, purge_stale=purge)
    print(report.summary())

    sys.exit(0 if report.success else 1)
