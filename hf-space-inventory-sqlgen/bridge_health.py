"""
bridge_health.py
================
Standalone helper for the Bridge Health panel.

Extracted from app.py so that the logic can be unit-tested without importing
the full FastAPI + Gradio application stack.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

BRIDGE_HEALTH_MAP: dict[str, str] = {
    "Perspective_Intents": "schema_intent_perspectives",
    "Perspective_Concepts": "schema_perspective_concepts",
}

# ArangoDB vertex collection ↔ SQLite registry table for the schema-node /
# tables drift check.  ``schema_nodes`` is the canonical registry of ERP tables
# that have been synced into the graph as ``tables`` vertices; a count mismatch
# means tables were added/removed in SQLite but graph_sync.py was not re-run.
SCHEMA_NODES_HEALTH_MAP: dict[str, str] = {
    "tables": "schema_nodes",
}

# Some ArangoDB collections (``tables`` in particular) carry legacy documents
# from earlier schema/key-scheme generations alongside the current canonical
# ones (see docs/plans/arango-key-guardrail.md). A raw collection .count()
# would compare stale + current against SQLite's current-only count and
# always report a false mismatch. Collections listed here are counted via an
# AQL query filtered to the given key prefix instead of a raw count.
CANONICAL_KEY_PREFIX: dict[str, str] = {
    "tables": "table::",
}


def run_bridge_health_check_impl(
    sqlite_db_path: str,
    bridge_map: dict,
    *,
    _os_path_exists=None,
    _sqlite_connect=None,
    _arango_env_getter=None,
    _arango_factory=None,
) -> tuple[str, str, str]:
    """Core logic for the Bridge Health panel.

    Parameters
    ----------
    sqlite_db_path:
        Filesystem path to the SQLite database.
    bridge_map:
        Mapping of ``{arango_collection_name: sqlite_table_name}``.
    _os_path_exists:
        Injection seam — replaces ``os.path.exists`` in tests.
    _sqlite_connect:
        Injection seam — replaces ``sqlite3.connect`` in tests.
    _arango_env_getter:
        Injection seam — replaces ``os.environ.get`` for the ARANGO_HOST
        lookup in tests.
    _arango_factory:
        Injection seam — callable that returns a mock ArangoDB ``db`` object
        in tests. When *None* (production), the real ``graph_sync`` helpers
        are used.

    Returns
    -------
    tuple[str, str, str]
        ``(overall_status, timestamp, detail_text)``
    """
    import sqlite3 as _sqlite3
    import datetime as _dt

    _exists = _os_path_exists if _os_path_exists is not None else os.path.exists
    _connect = _sqlite_connect if _sqlite_connect is not None else _sqlite3.connect
    _env_get = _arango_env_getter if _arango_env_getter is not None else (
        lambda key: os.environ.get(key)
    )

    timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not _exists(sqlite_db_path):
        return (
            "⚠️  SKIP — SQLite DB not found",
            timestamp,
            f"SQLite DB not found at: {sqlite_db_path}",
        )

    sqlite_counts: dict = {}
    conn = _connect(sqlite_db_path)
    try:
        for sqlite_table in bridge_map.values():
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {sqlite_table}"
                ).fetchone()
                sqlite_counts[sqlite_table] = row[0] if row else 0
            except Exception as exc:
                sqlite_counts[sqlite_table] = f"ERROR: {exc}"
    finally:
        conn.close()

    if not _env_get("ARANGO_HOST"):
        lines = ["ArangoDB: NOT CONFIGURED (ARANGO_HOST not set)", ""]
        lines.append(
            f"{'Collection':<28} {'ArangoDB':>12} {'SQLite':>12} {'Match':>8}"
        )
        lines.append("-" * 64)
        for arango_coll, sqlite_table in bridge_map.items():
            sqlite_n = sqlite_counts.get(sqlite_table, "?")
            lines.append(
                f"{arango_coll:<28} {'N/A':>12} {str(sqlite_n):>12} {'—':>8}"
            )
        lines.append("")
        lines.append("Set ARANGO_HOST to enable the ArangoDB side of this check.")
        return (
            "⚠️  SKIP — ARANGO_HOST not set",
            timestamp,
            "\n".join(lines),
        )

    if _arango_factory is not None:
        try:
            db = _arango_factory()
        except Exception as exc:
            return (
                f"⚠️  SKIP — ArangoDB connection failed: {exc}",
                timestamp,
                f"Could not connect to ArangoDB:\n{exc}",
            )
    else:
        try:
            from graph_sync import get_arango_client, get_arango_db
            client = get_arango_client()
            db = get_arango_db(client)
        except Exception as exc:
            return (
                f"⚠️  SKIP — ArangoDB connection failed: {exc}",
                timestamp,
                f"Could not connect to ArangoDB:\n{exc}",
            )

    arango_counts: dict = {}
    for coll_name in bridge_map:
        try:
            if not db.has_collection(coll_name):
                arango_counts[coll_name] = -1
                continue
            prefix = CANONICAL_KEY_PREFIX.get(coll_name)
            if prefix:
                cursor = db.aql.execute(
                    f"RETURN LENGTH(FOR d IN {coll_name} "
                    "FILTER STARTS_WITH(d._key, @prefix) RETURN 1)",
                    bind_vars={"prefix": prefix},
                )
                arango_counts[coll_name] = next(iter(cursor))
            else:
                arango_counts[coll_name] = db.collection(coll_name).count()
        except Exception as exc:
            arango_counts[coll_name] = f"ERROR: {exc}"

    lines = []
    lines.append(
        f"{'Collection':<28} {'ArangoDB':>12} {'SQLite':>12} {'Match':>8}"
    )
    lines.append("-" * 64)

    all_ok = True
    for arango_coll, sqlite_table in bridge_map.items():
        arango_n = arango_counts.get(arango_coll)
        sqlite_n = sqlite_counts.get(sqlite_table, "?")

        if isinstance(arango_n, str):
            match_icon = "ERROR"
            all_ok = False
        elif arango_n == -1:
            match_icon = "MISSING"
            all_ok = False
        elif arango_n == sqlite_n:
            match_icon = "✅"
        else:
            match_icon = "❌"
            all_ok = False

        arango_display = "MISSING" if arango_n == -1 else str(arango_n)
        lines.append(
            f"{arango_coll:<28} {arango_display:>12} {str(sqlite_n):>12} {match_icon:>8}"
        )

    lines.append("")
    if all_ok:
        lines.append("All bridge collection counts match the SQLite source data.")
        overall = "✅  IN SYNC — all counts match"
    else:
        lines.append(
            "Count mismatch detected. Re-run 'Sync to ArangoDB' on the Graph Sync tab."
        )
        overall = "❌  OUT OF SYNC — counts differ (see details below)"

    return overall, timestamp, "\n".join(lines)


# ---------------------------------------------------------------------------
# Sweep 1 coverage gaps — concepts in ELEVATES edges without approved SQL
# ---------------------------------------------------------------------------

def get_sweep1_coverage_gaps(
    manifest_path: str,
    *,
    _arango_env_getter=None,
    _arango_factory=None,
) -> Dict[str, Any]:
    """Identify ELEVATES edge concepts that lack an APPROVED SQL binding.

    Mirrors the Sweep 1 logic from ``scripts/verify_metadata_meaning.py`` so
    the Bridge Health panel can surface coverage gaps without running the CLI.

    Parameters
    ----------
    manifest_path:
        Filesystem path to ``reviewer_manifest.json``.
    _arango_env_getter:
        Injection seam — replaces ``os.environ.get`` for the ARANGO_HOST
        lookup in tests.
    _arango_factory:
        Injection seam — callable that returns a mock ArangoDB ``db`` object
        in tests.

    Returns
    -------
    dict with keys:
        ``status``        — "ok" | "gaps" | "skip" | "error"
        ``total_edges``   — int, number of ELEVATES edges found
        ``pass_count``    — int, edges with an approved binding
        ``gap_concepts``  — list of dicts: {intent_name, concept_name, concept_anchor}
        ``skip_count``    — int, edges with dangling vertex references (skipped)
        ``message``       — human-readable summary string
    """
    from metadata_query_templates import get_ground_truth_bindings

    _env_get = _arango_env_getter if _arango_env_getter is not None else (
        lambda key: os.environ.get(key)
    )

    if not _env_get("ARANGO_HOST"):
        return {
            "status": "skip",
            "total_edges": 0,
            "pass_count": 0,
            "gap_concepts": [],
            "skip_count": 0,
            "message": "ArangoDB not configured (ARANGO_HOST not set) — coverage check skipped.",
        }

    if _arango_factory is not None:
        try:
            db = _arango_factory()
        except Exception as exc:
            return {
                "status": "error",
                "total_edges": 0,
                "pass_count": 0,
                "gap_concepts": [],
                "skip_count": 0,
                "message": f"ArangoDB connection failed: {exc}",
            }
    else:
        try:
            from graph_sync import get_arango_client, get_arango_db
            client = get_arango_client()
            db = get_arango_db(client)
        except Exception as exc:
            return {
                "status": "error",
                "total_edges": 0,
                "pass_count": 0,
                "gap_concepts": [],
                "skip_count": 0,
                "message": f"ArangoDB connection failed: {exc}",
            }

    aql = """
    FOR e IN elevates
        LET intent_doc  = DOCUMENT(e._from)
        LET concept_doc = DOCUMENT(e._to)
        RETURN {
            intent_key  : intent_doc._key,
            intent_name : intent_doc.intent_name,
            concept_key : concept_doc._key,
            concept_name: concept_doc.concept_name,
            weight      : e.weight
        }
    """
    try:
        cursor = db.aql.execute(aql, batch_size=200)
        edges: List[Dict[str, Any]] = list(cursor)
    except Exception as exc:
        return {
            "status": "error",
            "total_edges": 0,
            "pass_count": 0,
            "gap_concepts": [],
            "skip_count": 0,
            "message": f"Could not query ELEVATES edges: {exc}",
        }

    if not edges:
        return {
            "status": "skip",
            "total_edges": 0,
            "pass_count": 0,
            "gap_concepts": [],
            "skip_count": 0,
            "message": "No ELEVATES edges found — graph may be empty or unsynced.",
        }

    bindings = get_ground_truth_bindings(manifest_path)
    approved_anchors: Dict[str, str] = {}
    for b in bindings:
        anchor = (b.get("concept_anchor") or "").upper().strip()
        if anchor and anchor not in approved_anchors:
            approved_anchors[anchor] = b.get("file_path") or b["binding_key"]

    gap_concepts: List[Dict[str, str]] = []
    pass_count = 0
    skip_count = 0

    for edge in edges:
        intent_label = edge.get("intent_name") or edge.get("intent_key") or ""
        concept_label = edge.get("concept_name") or edge.get("concept_key") or ""

        if not intent_label or not concept_label:
            skip_count += 1
            continue

        concept_raw = concept_label
        if concept_raw.lower().startswith("concept::"):
            concept_raw = concept_raw[len("concept::"):]
        concept_anchor = concept_raw.upper().strip()

        if concept_anchor in approved_anchors:
            pass_count += 1
        else:
            gap_concepts.append({
                "intent_name": intent_label,
                "concept_name": concept_label,
                "concept_anchor": concept_anchor,
            })

    total_checked = pass_count + len(gap_concepts)
    if gap_concepts:
        status = "gaps"
        message = (
            f"{len(gap_concepts)} concept(s) without an approved SQL snippet "
            f"(out of {total_checked} triples checked)"
        )
    else:
        status = "ok"
        message = (
            f"All {pass_count} semantic triple(s) have an approved SQL binding."
        )

    return {
        "status": status,
        "total_edges": len(edges),
        "pass_count": pass_count,
        "gap_concepts": gap_concepts,
        "skip_count": skip_count,
        "message": message,
    }
