"""Load the canonical graph metadata into ArangoDB.

Reads ``graph_metadata.json`` (SQLite-sourced canonical, the source of truth)
and loads it into the two flat collections named by the canonical itself:
``manufacturing_graph_node`` and ``manufacturing_graph_edge``.

This is the downstream sync step: SQLite -> canonical JSON -> ArangoDB.
It is idempotent (truncate-then-load keyed on ``_key``) and only ever touches
the two canonical collections -- it never alters the legacy named graph
(intents/concepts/contains/elevates) that shares the database.

WARNING -- do not run this file directly against the live instance: a real
extract from an external SQL Server (1,106+ ``table_x5F_Live_x2E_dbo_x2E_*``
nodes, referenced by the ``ATOMIC_FK`` edge collection) physically coexists
INSIDE ``manufacturing_graph_node`` / ``manufacturing_graph_edge`` themselves --
the two collections this script truncates. That data is not derived from
anything in this repo and cannot be regenerated if lost. Use
``safe_load_canonical_to_arango.py`` instead, which backs up both collections,
runs this same truncate-then-load, then additively restores every non-canonical
doc the truncate would otherwise destroy.

Connection note: ``ARANGO_HOST`` points at the managed web endpoint (port 443,
serves the HTML UI). The arangod HTTP API lives on port 8529, so we rewrite the
URL to that port here.

Usage:
    python replit_integrations/load_canonical_to_arango.py --dry-run
    python replit_integrations/load_canonical_to_arango.py
"""
import os
import sys
import json
from urllib.parse import urlparse

from arango import ArangoClient

HERE = os.path.dirname(os.path.abspath(__file__))
CANONICAL_PATH = os.path.join(HERE, "graph_metadata.json")
API_PORT = 8529


def arango_api_url() -> str:
    raw = (os.environ.get("ARANGO_HOST") or "").rstrip("/")
    if not raw:
        raise SystemExit("ARANGO_HOST is not set")
    p = urlparse(raw)
    scheme = p.scheme or "https"
    host = p.hostname or raw
    port = p.port or API_PORT
    return f"{scheme}://{host}:{port}"


def open_db():
    url = arango_api_url()
    client = ArangoClient(hosts=url)
    db = client.db(
        os.environ.get("ARANGO_DB", "manufacturing_graph"),
        username=os.environ["ARANGO_USER"],
        password=os.environ["ARANGO_ROOT_PASSWORD"],
    )
    return db, url


def _clean(doc: dict) -> dict:
    # Drop illustrative comment keys and the server-derived _id; keep _key/_from/_to.
    return {k: v for k, v in doc.items() if not k.startswith("//") and k != "_id"}


def main(dry_run: bool = False) -> int:
    data = json.load(open(CANONICAL_PATH))
    nodes = data["nodes"]
    edges = data["edges"]
    node_col = data["graph"]["node_collection"]
    edge_col = data["graph"]["edge_collection"]

    # Referential integrity: every edge endpoint must be a declared node.
    node_ids = {n["_id"] for n in nodes}
    missing = [
        (e["_key"], e.get("_from"), e.get("_to"))
        for e in edges
        if e.get("_from") not in node_ids or e.get("_to") not in node_ids
    ]
    if missing:
        print(f"ABORT: {len(missing)} edges reference missing nodes:")
        for m in missing[:10]:
            print("   ", m)
        return 1
    print(f"validation OK: {len(nodes)} nodes, {len(edges)} edges; all endpoints resolve")

    # Edge-family / edge_type breakdown + the perspective invariant: structural
    # edges are always perspective 'system'; semantic edges never are.
    by_family: dict = {}
    by_type: dict = {}
    semantic_bad: list = []
    structural_bad: list = []
    for e in edges:
        fam = e.get("edge_family", "?")
        et = e.get("edge_type", "?")
        by_family[fam] = by_family.get(fam, 0) + 1
        by_type[et] = by_type.get(et, 0) + 1
        persp = e.get("perspective")
        if fam == "semantic" and persp == "system":
            semantic_bad.append(e.get("_key"))
        if fam == "structural" and persp != "system":
            structural_bad.append(e.get("_key"))
    print(f"edge families: {by_family}")
    print(f"edge types   : {by_type}")
    if semantic_bad or structural_bad:
        print(
            f"ABORT: perspective invariant violated "
            f"(semantic-with-system={len(semantic_bad)}, "
            f"structural-non-system={len(structural_bad)})"
        )
        for k in (semantic_bad + structural_bad)[:10]:
            print("   ", k)
        return 1

    db, url = open_db()
    existing = sorted(c["name"] for c in db.collections() if not c["name"].startswith("_"))
    print(f"connected to {url} / db={data['graph'].get('node_collection','').split('_node')[0] or os.environ.get('ARANGO_DB')}")
    print(f"existing collections: {existing}")

    if dry_run:
        print(
            f"DRY-RUN: would load {len(nodes)} -> {node_col}, "
            f"{len(edges)} -> {edge_col} (truncate-then-import, on_duplicate=replace)"
        )
        return 0

    if db.has_collection(node_col):
        print(f"{node_col} exists -> truncating")
        db.collection(node_col).truncate()
    else:
        db.create_collection(node_col)
        print(f"created {node_col}")

    if db.has_collection(edge_col):
        print(f"{edge_col} exists -> truncating")
        db.collection(edge_col).truncate()
    else:
        db.create_collection(edge_col, edge=True)
        print(f"created {edge_col} (edge collection)")

    rn = db.collection(node_col).import_bulk([_clean(n) for n in nodes], on_duplicate="replace")
    re_ = db.collection(edge_col).import_bulk([_clean(e) for e in edges], on_duplicate="replace")
    print("nodes import:", {k: rn.get(k) for k in ("created", "errors", "empty", "updated")})
    print("edges import:", {k: re_.get(k) for k in ("created", "errors", "empty", "updated")})

    nc = db.collection(node_col).count()
    ec = db.collection(edge_col).count()
    print(f"FINAL: {node_col}={nc} (expect {len(nodes)}), {edge_col}={ec} (expect {len(edges)})")
    if nc != len(nodes) or ec != len(edges):
        print("COUNT MISMATCH")
        return 1
    print("PARITY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
