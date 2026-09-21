"""Audit (read-only): catalog manufacturing_graph_node docs by key scheme.

Four key conventions currently coexist in manufacturing_graph_node:
  - canonical   6-slot colon form, e.g. "ARCashReceiptAmount:entity:semantic:canonical:none:none"
                (written by replit_integrations/export_graph_metadata.py)
  - dbl_colon   "table::X" / "column::X.Y"
                (written by graph_sync.py's structural-containment sync, but
                lives in the separate tables/columns/contains collections —
                flagged here too in case any land in manufacturing_graph_node)
  - live_dbo    hex-encoded, e.g. "table_x5F_Live_x2E_dbo_x2E_ACCOUNT"
                (real sql-lab-1/LIVE SQL Server extract — see ddl/README.md)
  - legacy_bare everything else, e.g. "EMPLOYEE", "corrective_actions"
                (earliest graph_sync.py / pre-ERP-schema generation)

This script only reports counts and, for legacy_bare / dbl_colon keys (the
two candidate-for-pruning buckets), whether any edge in any collection still
references them via _from/_to — so a future prune pass can tell "orphaned,
safe to drop" from "still referenced, do not touch" without guessing.

Never deletes anything. Run:
    ARANGO_HOST=... ARANGO_USER=root ARANGO_ROOT_PASSWORD=... ARANGO_DB=manufacturing_graph \\
        python hf-space-inventory-sqlgen/scripts/audit_legacy_graph_keys.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import graph_sync as gs  # noqa: E402

NODE_COLLECTION = "manufacturing_graph_node"

_CANONICAL_RE = re.compile(r".+:(structural|semantic):.+:.+:.+$")


def classify(key: str) -> str:
    if "_x5F_" in key or "_x2E_" in key:
        return "live_dbo"
    if "::" in key:
        return "dbl_colon"
    if _CANONICAL_RE.match(key):
        return "canonical"
    return "legacy_bare"


def main() -> None:
    client = gs.get_arango_client()
    db = gs.get_arango_db(client)

    if not db.has_collection(NODE_COLLECTION):
        print(f"{NODE_COLLECTION} does not exist — nothing to audit.")
        return

    edge_collections = sorted(
        c["name"] for c in db.collections()
        if not c["name"].startswith("_") and db.collection(c["name"]).properties()["edge"]
    )
    print(f"Edge collections scanned for references: {edge_collections}\n")

    buckets: dict = {"canonical": [], "dbl_colon": [], "live_dbo": [], "legacy_bare": []}
    for doc in db.collection(NODE_COLLECTION).all():
        buckets[classify(doc["_key"])].append(doc["_id"])

    for style, ids in buckets.items():
        print(f"{style:<12} {len(ids):>6} node(s)")

    for style in ("legacy_bare", "dbl_colon"):
        ids = set(buckets[style])
        if not ids:
            continue
        referenced = set()
        for ec in edge_collections:
            cursor = db.aql.execute(
                f"FOR e IN {ec} "
                "FILTER e._from IN @ids OR e._to IN @ids "
                "RETURN DISTINCT (e._from IN @ids ? e._from : e._to)",
                bind_vars={"ids": list(ids)},
            )
            referenced.update(cursor)
        orphaned = ids - referenced
        print(
            f"\n{style}: {len(ids)} total, {len(referenced)} referenced by "
            f"at least one edge, {len(orphaned)} orphaned (no incoming/outgoing edge)"
        )
        if orphaned:
            sample = sorted(orphaned)[:10]
            print(f"  sample orphaned _id(s): {sample}")

    print(
        "\nNo changes made. Orphaned counts above are candidates for a future "
        "prune pass — referenced nodes must not be pruned without also "
        "handling their edges."
    )


if __name__ == "__main__":
    main()
