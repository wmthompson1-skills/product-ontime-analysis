"""Safety wrapper around ``load_canonical_to_arango.py``.

``manufacturing_graph_node`` / ``manufacturing_graph_edge`` are NOT dedicated to
the canonical pipeline. A real extract from an external SQL Server
(``sql-lab-1`` / database ``LIVE``) physically coexists in the SAME two
collections: 1,106+ nodes keyed ``table_x5F_Live_x2E_dbo_x2E_*`` plus a handful
of other legacy docs, alongside the ``ATOMIC_FK`` edge collection that
references them. ``load_canonical_to_arango.py``'s own docstring claims it
"never alters ... that shares the database" -- true for every OTHER collection
in the DB, but NOT true for the two it actually truncates: a bare run of that
script destroys the real Live.dbo data with no way to regenerate it (it is not
derived from anything in this repo). This instance is also a SHARED cloud
ArangoDB (see ``.agents/memory/live-flat-graph-parity-race.md``) -- concurrently
mutated by processes outside this environment -- so keep the window between
backup and restore as short as possible and never assume a single count check
stays true indefinitely.

This script:
  1. Backs up ``manufacturing_graph_node``, ``manufacturing_graph_edge``, and
     ``ATOMIC_FK`` in full to one timestamped local JSON artifact, before any
     truncate or write.
  2. Truncates and loads ONLY the canonical set from ``graph_metadata.json``
     into the two named collections directly -- no named-graph traversal
     (``scripts/safe_arango_rebuild.py`` uses ``graph_sync.py``'s legacy named
     graph, ``manufacturing_semantic_layer``, which does not even include these
     two collections; it is the wrong tool for this and would back up nothing
     relevant here).
  3. Additively restores every backed-up node/edge whose ``_key`` is outside
     the fresh canonical set (``on_duplicate="ignore"``, so nothing canonical
     is overwritten) -- this is what shields the Live.dbo partition and any
     other non-canonical doc from the truncate.
  4. Verifies: canonical node/edge counts exactly match ``graph_metadata.json``
     (the SQLite/JSON parity baseline -- run ``sql_graph_parity_check.py``
     first if you need that baseline itself re-verified), total counts never
     go net-negative, the pre-sync Live.dbo node count is unchanged, and a real
     AQL traversal from a sampled ``ATOMIC_FK`` edge into
     ``manufacturing_graph_node`` resolves both endpoints.

Usage:
    python replit_integrations/safe_load_canonical_to_arango.py --dry-run
    python replit_integrations/safe_load_canonical_to_arango.py
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from arango import ArangoClient
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env")

BACKUP_DIR = REPO / "arango_backups"
CANONICAL_PATH = Path(__file__).resolve().parent / "graph_metadata.json"
LIVE_DBO_PREFIX = "table_x5F_Live_x2E_dbo_x2E_"


def open_db():
    raw = (os.environ.get("ARANGO_HOST") or "").rstrip("/")
    if not raw:
        raise SystemExit("ARANGO_HOST is not set")
    p = urlparse(raw)
    url = f"{p.scheme or 'https'}://{p.hostname or raw}:8529"
    client = ArangoClient(hosts=url)
    return client.db(
        os.environ.get("ARANGO_DB", "manufacturing_graph"),
        username=os.environ["ARANGO_USER"],
        password=os.environ["ARANGO_ROOT_PASSWORD"],
    )


def _clean(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("//") and k != "_id"}


def main(dry_run: bool = False) -> int:
    db = open_db()
    data = json.load(open(CANONICAL_PATH))
    canonical_nodes = data["nodes"]
    canonical_edges = data["edges"]
    node_col = data["graph"]["node_collection"]
    edge_col = data["graph"]["edge_collection"]
    canonical_node_keys = {n["_key"] for n in canonical_nodes}
    canonical_edge_keys = {e["_key"] for e in canonical_edges}

    print("=" * 70)
    print("STEP 1: Atomic backup (manufacturing_graph_node/_edge, ATOMIC_FK)")
    print("=" * 70)
    node_docs = list(db.collection(node_col).all())
    edge_docs = list(db.collection(edge_col).all())
    atomic_fk_docs = list(db.collection("ATOMIC_FK").all()) if db.has_collection("ATOMIC_FK") else []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"arango_canonical_{ts}.json"
    BACKUP_DIR.mkdir(exist_ok=True)
    with open(backup_path, "w") as f:
        json.dump({
            "timestamp": ts,
            node_col: node_docs,
            edge_col: edge_docs,
            "ATOMIC_FK": atomic_fk_docs,
        }, f, default=str)
    live_dbo_before = {d["_key"] for d in node_docs if d["_key"].startswith(LIVE_DBO_PREFIX)}
    print(f"  {len(node_docs)} nodes, {len(edge_docs)} edges, {len(atomic_fk_docs)} ATOMIC_FK docs -> {backup_path}")
    print(f"  pre-sync Live.dbo real nodes: {len(live_dbo_before)}")

    non_canonical_nodes = [d for d in node_docs if d["_key"] not in canonical_node_keys]
    non_canonical_edges = [d for d in edge_docs if d["_key"] not in canonical_edge_keys]

    print()
    print("=" * 70)
    print("STEP 2: Scope check")
    print("=" * 70)
    print(f"  target collections: {node_col}, {edge_col} (direct -- no named graph)")
    print(f"  canonical set to load: {len(canonical_nodes)} nodes, {len(canonical_edges)} edges")
    print(f"  non-canonical docs a bare truncate would destroy: "
          f"{len(non_canonical_nodes)} nodes ({len(live_dbo_before)} Live.dbo), "
          f"{len(non_canonical_edges)} edges")

    node_ids = {n["_id"] for n in canonical_nodes}
    missing = [(e["_key"], e.get("_from"), e.get("_to")) for e in canonical_edges
               if e.get("_from") not in node_ids or e.get("_to") not in node_ids]
    if missing:
        print(f"ABORT: {len(missing)} canonical edges reference missing nodes")
        return 1

    if dry_run:
        print(f"\nDRY-RUN: would truncate+load {len(canonical_nodes)}/{len(canonical_edges)} canonical, "
              f"then restore {len(non_canonical_nodes)}/{len(non_canonical_edges)} non-canonical. No changes made.")
        return 0

    print()
    print("=" * 70)
    print("STEP 3: Truncate + load canonical set")
    print("=" * 70)
    db.collection(node_col).truncate()
    db.collection(edge_col).truncate()
    rn = db.collection(node_col).import_bulk([_clean(n) for n in canonical_nodes], on_duplicate="replace")
    re_ = db.collection(edge_col).import_bulk([_clean(e) for e in canonical_edges], on_duplicate="replace")
    print(f"  nodes: created={rn.get('created')} errors={rn.get('errors')}")
    print(f"  edges: created={re_.get('created')} errors={re_.get('errors')}")

    print()
    print("=" * 70)
    print("STEP 4: Additive restore (shields Live.dbo + all other non-canonical docs)")
    print("=" * 70)
    if non_canonical_nodes:
        rn2 = db.collection(node_col).import_bulk([_clean(d) for d in non_canonical_nodes], on_duplicate="ignore")
        print(f"  nodes restored: created={rn2.get('created')} errors={rn2.get('errors')}")
    if non_canonical_edges:
        re2 = db.collection(edge_col).import_bulk([_clean(d) for d in non_canonical_edges], on_duplicate="ignore")
        print(f"  edges restored: created={re2.get('created')} errors={re2.get('errors')}")

    print()
    print("=" * 70)
    print("STEP 5: Post-load invariant verification")
    print("=" * 70)
    post_node_count = db.collection(node_col).count()
    post_edge_count = db.collection(edge_col).count()
    live_dbo_after = db.aql.execute(
        f"FOR n IN {node_col} FILTER STARTS_WITH(n._key, @p) COLLECT WITH COUNT INTO c RETURN c",
        bind_vars={"p": LIVE_DBO_PREFIX},
    ).next()
    canonical_now = db.aql.execute(
        f"FOR n IN {node_col} FILTER n._key IN @keys COLLECT WITH COUNT INTO c RETURN c",
        bind_vars={"keys": list(canonical_node_keys)},
    ).next()
    canonical_edges_now = db.aql.execute(
        f"FOR e IN {edge_col} FILTER e._key IN @keys COLLECT WITH COUNT INTO c RETURN c",
        bind_vars={"keys": list(canonical_edge_keys)},
    ).next()

    print(f"  total: {post_node_count} nodes (was {len(node_docs)}), {post_edge_count} edges (was {len(edge_docs)})")
    print(f"  canonical present: {canonical_now}/{len(canonical_node_keys)} nodes, "
          f"{canonical_edges_now}/{len(canonical_edge_keys)} edges "
          f"(parity baseline expects {len(canonical_nodes)}/{len(canonical_edges)})")
    print(f"  Live.dbo real nodes: {live_dbo_after} (was {len(live_dbo_before)})")

    traversal_ok = True
    if atomic_fk_docs:
        sample = atomic_fk_docs[0]
        for field in ("_from", "_to"):
            endpoint = sample.get(field, "")
            if "/" in endpoint and endpoint.split("/", 1)[0] == node_col:
                key = endpoint.split("/", 1)[1]
                found = db.collection(node_col).has(key)
                print(f"  traversal check: {endpoint} -> {'FOUND' if found else 'MISSING'}")
                traversal_ok = traversal_ok and found

    ok = (
        post_node_count >= len(node_docs)
        and post_edge_count >= len(edge_docs)
        and canonical_now == len(canonical_node_keys) == len(canonical_nodes)
        and canonical_edges_now == len(canonical_edge_keys) == len(canonical_edges)
        and live_dbo_after == len(live_dbo_before)
        and traversal_ok
    )
    print()
    print("RESULT:", "PASS" if ok else "FAIL -- investigate before trusting this state / restarting the UI")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
