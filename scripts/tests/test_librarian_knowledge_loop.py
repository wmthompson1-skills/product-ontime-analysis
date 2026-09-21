"""Focused tests for the librarian MCP server (scripts/librarian_server.py) —
the Knowledge Loop's document-ingestion + research-graph-staging tools.

Covers:
  1. list_mrp_documents() / read_document() against the REAL docs/my-mrp-kb
     corpus (not a fixture — this repo's knowledge base already has real
     content; nothing needs "restoring").
  2. commit_to_arangodb()'s isolation guarantees: disabled by default,
     refuses any database/collection name that could touch the certified
     manufacturing_graph, and — on the happy path — writes only to the
     separate mrp_research database while manufacturing_graph_node's count
     stays byte-for-byte unchanged.
  3. stage_terminology() dry-run — regression guard for a missing `import
     sys` that made every call crash (fixed alongside this test file).

Live-ArangoDB tests are skipped when ARANGO_HOST is not set. Tests that
write anything clean up after themselves.

Run individually (gate-style):
    cd scripts
    ARANGO_HOST=... ARANGO_USER=root ARANGO_ROOT_PASSWORD=... ARANGO_DB=manufacturing_graph \\
        python -m pytest tests/test_librarian_knowledge_loop.py -v
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def lib():
    return importlib.import_module("librarian_server")


# ---------------------------------------------------------------------------
# 1. Document ingestion against the real knowledge base
# ---------------------------------------------------------------------------

def test_list_mrp_documents_finds_real_corpus(lib):
    result = lib.list_mrp_documents()
    assert result["count"] > 0
    assert str(lib.DEFAULT_DOC_ROOT) == result["directory"]
    assert any(f.endswith(".docx") for f in result["files"])
    assert any(f.startswith("07-three-way-match/") for f in result["files"])


def test_read_document_reads_real_docx(lib):
    files = lib.list_mrp_documents()["files"]
    docx_files = [f for f in files if f.endswith(".docx")]
    assert docx_files, "Expected at least one real .docx in the knowledge base"

    text = lib.read_document(docx_files[0])
    assert isinstance(text, str) and len(text) > 100


def test_read_document_rejects_non_docx(lib):
    files = lib.list_mrp_documents()["files"]
    md_files = [f for f in files if f.endswith(".md")]
    assert md_files, "Expected at least one .md file to test the extension guard"

    with pytest.raises(ValueError, match="only supports .docx"):
        lib.read_document(md_files[0])


def test_resolve_path_refuses_escape_outside_doc_root(lib):
    with pytest.raises(ValueError, match="escapes"):
        lib._resolve_path("../../../etc/passwd")


# ---------------------------------------------------------------------------
# 2. commit_to_arangodb isolation guarantees
# ---------------------------------------------------------------------------

def test_commit_disabled_by_default(lib, monkeypatch):
    monkeypatch.delenv("MRP_ENABLE_GRAPH_COMMIT", raising=False)
    with pytest.raises(RuntimeError, match="Graph commit is disabled"):
        lib.commit_to_arangodb({"nodes": [], "edges": []})


def test_commit_refuses_certified_database_name(lib, monkeypatch):
    monkeypatch.setenv("MRP_ENABLE_GRAPH_COMMIT", "true")
    monkeypatch.setenv("ARANGO_RESEARCH_DB", "manufacturing_graph")
    monkeypatch.setenv("ARANGO_DB", "manufacturing_graph")
    with pytest.raises(RuntimeError, match="certified database"):
        lib.commit_to_arangodb({"nodes": [], "edges": []})


def test_commit_refuses_unnamespaced_collections(lib, monkeypatch):
    monkeypatch.setenv("MRP_ENABLE_GRAPH_COMMIT", "true")
    monkeypatch.delenv("ARANGO_RESEARCH_DB", raising=False)
    monkeypatch.delenv("ARANGO_DB", raising=False)
    monkeypatch.setenv("ARANGO_NODE_COLLECTION", "manufacturing_graph_node")
    with pytest.raises(RuntimeError, match="must be namespaced"):
        lib.commit_to_arangodb({"nodes": [], "edges": []})


@pytest.mark.skipif(not os.environ.get("ARANGO_HOST"), reason="ARANGO_HOST not set")
def test_commit_happy_path_isolated_from_certified_graph(lib, monkeypatch):
    """The one test that actually writes: confirms it lands ONLY in
    mrp_research/ai_research_node, and manufacturing_graph_node's count is
    unchanged before vs. after."""
    from arango import ArangoClient

    monkeypatch.setenv("MRP_ENABLE_GRAPH_COMMIT", "true")
    monkeypatch.delenv("ARANGO_RESEARCH_DB", raising=False)
    monkeypatch.delenv("MRP_RESEARCH_ARANGO_DB", raising=False)
    monkeypatch.delenv("ARANGO_NODE_COLLECTION", raising=False)
    monkeypatch.delenv("ARANGO_EDGE_COLLECTION", raising=False)

    client = ArangoClient(hosts=lib._normalize_arango_host(os.environ["ARANGO_HOST"]))
    arango_user = os.environ.get("ARANGO_USER", "root")
    arango_password = os.environ.get("ARANGO_ROOT_PASSWORD", "")
    certified_db = client.db(os.environ["ARANGO_DB"], username=arango_user, password=arango_password)
    before = (
        certified_db.collection("manufacturing_graph_node").count()
        if certified_db.has_collection("manufacturing_graph_node")
        else None
    )

    probe_key = "test_knowledge_loop_probe__pytest"
    try:
        result = lib.commit_to_arangodb({
            "nodes": [{"node_type": "concept", "concept_key": probe_key, "label": "pytest probe"}],
            "edges": [],
        })
        assert result["database"] == "mrp_research"
        assert result["node_collection"] == "ai_research_node"
        assert result["nodes_upserted"] == 1

        research_db = client.db("mrp_research", username=arango_user, password=arango_password)
        assert research_db.collection("ai_research_node").get(probe_key) is not None

        after = certified_db.collection("manufacturing_graph_node").count()
        assert after == before, (
            f"manufacturing_graph_node count changed ({before} -> {after}) — "
            "the research commit leaked into the certified graph!"
        )
    finally:
        research_db = client.db("mrp_research", username=arango_user, password=arango_password)
        if research_db.has_collection("ai_research_node"):
            research_db.collection("ai_research_node").delete(probe_key, ignore_missing=True)


# ---------------------------------------------------------------------------
# 3. stage_terminology — regression guard for the missing `import sys` bug
# ---------------------------------------------------------------------------

def test_stage_terminology_dry_run_executes(lib):
    """Was crashing with NameError: name 'sys' is not defined before the fix."""
    result = lib.stage_terminology(commit=False)
    assert result["committed"] is False
    assert result["terms_extracted"] > 0
    assert result["next_step"]["action"] == "review_and_approve"
