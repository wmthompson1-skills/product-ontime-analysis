"""Focused tests against the Graph Sync tab's actual bound UI callback.

Unlike calling graph_sync.sync_graph() directly, this extracts the *exact*
function object Gradio has registered against the "Dry Run" / "Sync to
ArangoDB" buttons (a lambda closing over create_gradio_interface()'s local
run_graph_sync()), by monkeypatching gr.Button.click to record fn= at import
time and then resolving the lambda's closure cell. This catches import
regressions, signature drift, or status-string-formatting bugs in the UI
wrapper itself — not just in the underlying sync_graph() engine.

Live-ArangoDB assertions (target collections, key format) are skipped when
ARANGO_HOST is not set, matching this repo's existing test convention (see
tests/test_bridge_collection_health.py).

Run individually (gate-style):
    cd hf-space-inventory-sqlgen
    ARANGO_HOST=... ARANGO_USER=root ARANGO_ROOT_PASSWORD=... ARANGO_DB=manufacturing_graph \\
        python -m pytest tests/test_graph_sync_ui_callback.py -v
"""

from __future__ import annotations

import os
import re

import pytest


def _extract_run_graph_sync():
    """Import app.py (building its Gradio demo at module import time) while
    spying on gr.Button.click, then resolve the real run_graph_sync function
    object from the registered lambda's closure. Returns (dry_run_fn, live_fn)
    — both resolve to the SAME underlying function; kept separate because
    that's what the UI actually has bound to two different buttons.
    """
    import gradio as gr

    captured: list = []
    orig_click = gr.Button.click

    def spy_click(self, fn=None, **kwargs):
        if fn is not None:
            captured.append(fn)
        return orig_click(self, fn=fn, **kwargs)

    gr.Button.click = spy_click
    try:
        import app as appmod  # noqa: F401  (import executes module-level demo build)
    finally:
        gr.Button.click = orig_click

    targets = [
        fn for fn in captured
        if getattr(fn, "__code__", None) and "run_graph_sync" in fn.__code__.co_freevars
    ]
    if not targets:
        pytest.fail(
            "No Button.click(fn=...) closing over run_graph_sync was captured — "
            "the Graph Sync tab's button wiring may have changed (import regression)."
        )

    resolved = []
    for fn in targets:
        for cell, name in zip(fn.__closure__, fn.__code__.co_freevars):
            if name == "run_graph_sync":
                resolved.append((fn, cell.cell_contents))
    dry_run_fn = next(real for lam, real in resolved if True in lam.__code__.co_consts and False not in lam.__code__.co_consts)
    live_fn = next(real for lam, real in resolved if False in lam.__code__.co_consts)
    return dry_run_fn, live_fn


@pytest.fixture(scope="module")
def run_graph_sync():
    dry_run_fn, live_fn = _extract_run_graph_sync()
    assert dry_run_fn is live_fn, (
        "Expected the dry-run and live buttons to close over the SAME "
        "run_graph_sync function object"
    )
    return dry_run_fn


# ---------------------------------------------------------------------------
# 1. UI callback executes cleanly with mock inputs
# ---------------------------------------------------------------------------

def test_callback_dry_run_executes_without_error(run_graph_sync):
    """The exact bound callback runs with mock (dry_run=True, purge_stale) inputs."""
    status, report_text, stale_md = run_graph_sync(dry_run=True, purge_stale=False)
    assert isinstance(status, str) and status
    assert isinstance(report_text, str) and report_text
    assert isinstance(stale_md, str) and stale_md
    assert "ERROR" not in status, f"Callback raised inside its own try/except: {status}"
    assert "DRY RUN" in status


def test_callback_dry_run_with_purge_executes_without_error(run_graph_sync):
    """purge_stale=True is a valid mock input too (checkbox on the tab)."""
    status, _report_text, _stale_md = run_graph_sync(dry_run=True, purge_stale=True)
    assert "ERROR" not in status


# ---------------------------------------------------------------------------
# 2. Return payload: counts are consistent and status formats cleanly
# ---------------------------------------------------------------------------

def test_callback_status_message_matches_report_counts(run_graph_sync):
    """The formatted status string's counts must match the underlying
    sync_graph() report exactly — proves the UI wrapper doesn't mangle
    numbers when building the display string. Asserting against the live
    report (not a hardcoded literal) because the synthetic dataset's size
    is expected to grow as more migrations/seeders are added; a pinned
    count would go stale on the next bootstrap.
    """
    from graph_sync import sync_graph

    status, _report_text, _stale_md = run_graph_sync(dry_run=True, purge_stale=False)
    independent_report = sync_graph(dry_run=True)

    m = re.search(r"DRY RUN — (\d+) vertices, (\d+) edges ready to sync", status)
    assert m, f"Status string format changed, could not parse counts from: {status!r}"
    ui_vertices, ui_edges = int(m.group(1)), int(m.group(2))

    assert ui_vertices == independent_report.total_vertices
    assert ui_edges == independent_report.total_edges
    assert ui_vertices > 0 and ui_edges > 0, "Expected a non-empty sync (dataset should not be empty)"


def test_callback_live_sync_reports_success(run_graph_sync):
    """Live sync (mock purge_stale=False) succeeds end-to-end through the UI path."""
    if not os.environ.get("ARANGO_HOST"):
        pytest.skip("ARANGO_HOST not set — live sync check skipped")

    status, report_text, _stale_md = run_graph_sync(dry_run=False, purge_stale=False)
    assert status.startswith("SUCCESS"), f"Expected SUCCESS, got: {status!r}\n{report_text}"
    assert "vertices" in status and "edges" in status


# ---------------------------------------------------------------------------
# 3. Target collections + key-format rules
# ---------------------------------------------------------------------------

def test_references_edge_key_uses_to_separator_not_arrow():
    """references_edge_key() must never emit '->' (illegal ArangoDB _key char)."""
    from arangodb_helpers.manufacturing_graph_version_0_0_1 import references_edge_key

    key = references_edge_key("receiving", "po_id", "purchase_order", "po_id")
    assert key == "fk::RECEIVING.PO_ID__TO__PURCHASE_ORDER.PO_ID"
    assert "__TO__" in key
    assert "->" not in key


def test_live_sync_writes_query_bindings_not_bindings(run_graph_sync):
    """After a live sync, ground-truth query bindings land in query_bindings —
    never in the pre-existing, unrelated 'bindings' edge collection."""
    if not os.environ.get("ARANGO_HOST"):
        pytest.skip("ARANGO_HOST not set — live collection check skipped")

    from graph_sync import get_arango_client, get_arango_db

    run_graph_sync(dry_run=False, purge_stale=False)

    client = get_arango_client()
    db = get_arango_db(client)

    assert db.has_collection("query_bindings"), "query_bindings collection was not created"
    qb_props = db.collection("query_bindings").properties()
    assert qb_props["edge"] is False, "query_bindings must be a document (vertex) collection"
    assert db.collection("query_bindings").count() > 0

    if db.has_collection("bindings"):
        b_props = db.collection("bindings").properties()
        assert b_props["edge"] is True, (
            "'bindings' changed from an edge collection — it should remain the "
            "pre-existing field_components->concepts collection, untouched by graph_sync"
        )


def test_live_sync_all_references_edges_use_to_separator(run_graph_sync):
    """Every references edge currently in Arango must use __TO__, not '->',
    in its _key — not just the one example in the unit test above."""
    if not os.environ.get("ARANGO_HOST"):
        pytest.skip("ARANGO_HOST not set — live collection check skipped")

    from graph_sync import get_arango_client, get_arango_db

    run_graph_sync(dry_run=False, purge_stale=False)

    client = get_arango_client()
    db = get_arango_db(client)
    assert db.has_collection("references")

    keys = [d["_key"] for d in db.collection("references").all()]
    assert keys, "references collection is empty — expected declared-FK edges from the ERP schema"
    bad = [k for k in keys if "->" in k]
    assert not bad, f"Found {len(bad)} references edge key(s) still using '->': {bad[:5]}"
    assert all("__TO__" in k for k in keys)
