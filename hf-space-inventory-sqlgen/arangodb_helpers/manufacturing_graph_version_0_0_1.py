"""
Manufacturing Graph — Schema Model v0.0.1
==========================================
Defines the **Structural Containment** layer of the manufacturing ArangoDB graph.

Architecture
------------
The graph is separated into two layers that are intentionally decoupled:

Physical (structural) layer — this module
    Mirrors the on-premise ERP catalog schema.
    No business meaning is embedded here; just topology.

        tables ──[contains]──> columns

Semantic layer — graph_sync.py / existing collections
    Maps analytical intent to abstract concepts and approved SQL snippets.

        intents ──[elevates]──> concepts
        intents ──[bound_to]──> bindings
        + Perspective_Intents / Perspective_Concepts bridge documents

Multi-tiered traversal for Text-to-SQL agents
----------------------------------------------
A resolver can walk:
    tables/{table} -[contains]-> columns/{table}__{col}
                                 -[CAN_MEAN]-> concepts/{concept}
                                               <-[elevates]- intents/{intent}
                                               <-[BOUND_TO]- bindings/{binding_key}

Key conventions  (aligned with private repo graph_naming_adapters.py)
----------------------------------------------------------------------
- Table vertex key  : ``table::{TABLE_NAME}``          (uppercase, type-prefixed)
                      e.g. ``table::CORRECTIVE_ACTIONS``
- Column vertex key : ``column::{TABLE_NAME}.{COL}``   (uppercase, dot separator)
                      e.g. ``column::CORRECTIVE_ACTIONS.CAPA_ID``
  ArangoDB _key permits dots — the double-colon prefix keeps the type explicit.
- Contains edge key : same as column key (one parent per column → unique)

Collections introduced by this model
-------------------------------------
- ``tables``   — vertex collection
- ``columns``  — vertex collection
- ``contains`` — edge collection  (from: tables, to: columns)
"""

from __future__ import annotations
from typing import Any, Dict

# ── Collection name constants ────────────────────────────────────────────────

TABLES_COLLECTION: str = "tables"
COLUMNS_COLLECTION: str = "columns"
CONTAINS_EDGE_COLLECTION: str = "contains"
REFERENCES_EDGE_COLLECTION: str = "references"

# ── Edge definition for the named graph ─────────────────────────────────────

CONTAINS_EDGE_DEFINITION: Dict[str, Any] = {
    "edge_collection": CONTAINS_EDGE_COLLECTION,
    "from_vertex_collections": [TABLES_COLLECTION],
    "to_vertex_collections": [COLUMNS_COLLECTION],
}

# Foreign-key layer: column → column (child → parent). Both endpoints live in
# the same ``columns`` vertex collection, so from/to are both COLUMNS_COLLECTION.
REFERENCES_EDGE_DEFINITION: Dict[str, Any] = {
    "edge_collection": REFERENCES_EDGE_COLLECTION,
    "from_vertex_collections": [COLUMNS_COLLECTION],
    "to_vertex_collections": [COLUMNS_COLLECTION],
}

# ── Key helpers ──────────────────────────────────────────────────────────────

def table_key(table_name: str) -> str:
    """Return the ArangoDB ``_key`` for a table vertex.

    Format: ``table::{TABLE_NAME}`` (uppercase).

    Example::

        table_key("corrective_actions")
        # → "table::CORRECTIVE_ACTIONS"
    """
    return f"table::{table_name.strip().upper()}"


def column_key(table_name: str, column_name: str) -> str:
    """Return the ArangoDB ``_key`` for a column vertex.

    Format: ``column::{TABLE_NAME}.{COLUMN_NAME}`` (both uppercase).
    ArangoDB ``_key`` permits literal dots; the ``column::`` prefix keeps
    the type unambiguous when browsing the collection.

    Example::

        column_key("corrective_actions", "capa_id")
        # → "column::CORRECTIVE_ACTIONS.CAPA_ID"
    """
    return f"column::{table_name.strip().upper()}.{column_name.strip().upper()}"


def contains_edge_key(table_name: str, column_name: str) -> str:
    """Return the ArangoDB ``_key`` for a contains edge.

    Each column has exactly one parent table, so the column key doubles as the
    edge key.
    """
    return column_key(table_name, column_name)


def references_edge_key(
    child_table: str,
    child_column: str,
    parent_table: str,
    parent_column: str,
) -> str:
    """Return the ArangoDB ``_key`` for a foreign-key (references) edge.

    A child column can declare more than one foreign key, so — unlike the
    contains edge — the column key alone is not unique. The key encodes BOTH
    endpoints so re-sync is a deterministic, idempotent upsert.

    Format: ``fk::{CHILD_TABLE}.{CHILD_COL}__TO__{PARENT_TABLE}.{PARENT_COL}``
    (all uppercase). Uses ``__TO__`` rather than ``->`` because ArangoDB
    document keys may not contain ``<`` or ``>``.

    Example::

        references_edge_key("receiving", "po_id", "purchase_order", "po_id")
        # → "fk::RECEIVING.PO_ID__TO__PURCHASE_ORDER.PO_ID"
    """
    return (
        f"fk::{child_table.strip().upper()}.{child_column.strip().upper()}"
        f"__TO__{parent_table.strip().upper()}.{parent_column.strip().upper()}"
    )


# ── Document builders ────────────────────────────────────────────────────────

def table_vertex(
    table_name: str,
    description: str = "",
    synced_at: str = "",
) -> Dict[str, Any]:
    """Build a ``tables`` vertex document ready for ArangoDB insert/update."""
    return {
        "_key": table_key(table_name),
        "table_name": table_name,
        "qualified_name": f"dbo.{table_name.upper()}",
        "description": description,
        "node_type": "table",
        "synced_at": synced_at,
    }


def column_vertex(
    table_name: str,
    column_name: str,
    column_type: str = "TEXT",
    notnull: bool = False,
    pk: bool = False,
    default_value: Any = None,
    synced_at: str = "",
) -> Dict[str, Any]:
    """Build a ``columns`` vertex document ready for ArangoDB insert/update.

    Field names match the private repo's column node schema (see graph_naming_adapters.py):
      node_type      : "column"
      table_name     : uppercase, e.g. "EMPLOYEE"
      column_name    : uppercase, e.g. "ADDR_1"
      column_type    : SQLite type string, e.g. "TEXT", "INTEGER"
      notnull        : bool
      primary_key    : bool
      default_value  : raw default from PRAGMA (None when absent)
    """
    key = column_key(table_name, column_name)
    return {
        "_key": key,
        "node_type": "column",
        "table_name": table_name.strip().upper(),
        "column_name": column_name.strip().upper(),
        "column_type": column_type or "TEXT",
        "notnull": notnull,
        "primary_key": pk,
        "default_value": default_value,
        "synced_at": synced_at,
    }


def contains_edge(
    table_name: str,
    column_name: str,
    synced_at: str = "",
) -> Dict[str, Any]:
    """Build a ``contains`` edge document ready for ArangoDB insert/update.

    Matches the private repo's edge schema:
      edge_type   : "CONTAINS"
      table_name  : uppercase, e.g. "EMPLOYEE"
      column_name : uppercase, e.g. "ADDR_1"
      _from       : tables/{table_key}
      _to         : columns/{column_key}
    """
    key = contains_edge_key(table_name, column_name)
    return {
        "_key": key,
        "_from": f"{TABLES_COLLECTION}/{table_key(table_name)}",
        "_to": f"{COLUMNS_COLLECTION}/{column_key(table_name, column_name)}",
        "edge_type": "CONTAINS",
        "table_name": table_name.strip().upper(),
        "column_name": column_name.strip().upper(),
        "synced_at": synced_at,
    }


def references_edge(
    child_table: str,
    child_column: str,
    parent_table: str,
    parent_column: str,
    synced_at: str = "",
) -> Dict[str, Any]:
    """Build a ``references`` (foreign-key) edge document for ArangoDB.

    Direction is child column → parent column, matching the canonical exporter's
    ``references`` edges. Both endpoints are ``columns`` vertices that the
    containment sync already created, so this must run AFTER that pass.

      edge_type         : "REFERENCES"  (uppercase, matching the live "CONTAINS")
      table_name        : child table   (uppercase)
      column_name       : child column  (uppercase)
      references_table  : parent table  (uppercase)
      references_column : parent column (uppercase)
      _from             : columns/{child column_key}
      _to               : columns/{parent column_key}
    """
    key = references_edge_key(child_table, child_column, parent_table, parent_column)
    return {
        "_key": key,
        "_from": f"{COLUMNS_COLLECTION}/{column_key(child_table, child_column)}",
        "_to": f"{COLUMNS_COLLECTION}/{column_key(parent_table, parent_column)}",
        "edge_type": "REFERENCES",
        "table_name": child_table.strip().upper(),
        "column_name": child_column.strip().upper(),
        "references_table": parent_table.strip().upper(),
        "references_column": parent_column.strip().upper(),
        "synced_at": synced_at,
    }
