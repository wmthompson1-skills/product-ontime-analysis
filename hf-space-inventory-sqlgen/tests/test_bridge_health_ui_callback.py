"""Focused tests against the Bridge Health tab's actual bound UI callback.

Extracts the exact function Gradio has registered against the "Check Now"
button (fn=run_bridge_health_check, passed directly — no lambda wrapper here,
unlike the Graph Sync tab) by monkeypatching gr.Button.click at import time.

This is a regression guard for the tables count false-mismatch bug fixed in
bridge_health.py (CANONICAL_KEY_PREFIX / STARTS_WITH filtering): unlike the
Graph Sync vertex/edge counts, which are expected to grow as the synthetic
dataset grows, "bridge health in sync" is a state this test should actively
enforce — if it goes out of sync again, this test must fail.

Skipped entirely when ARANGO_HOST is not set, matching this repo's existing
test convention.

Run individually (gate-style):
    cd hf-space-inventory-sqlgen
    ARANGO_HOST=... ARANGO_USER=root ARANGO_ROOT_PASSWORD=... ARANGO_DB=manufacturing_graph \\
        python -m pytest tests/test_bridge_health_ui_callback.py -v
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARANGO_HOST"),
    reason="ARANGO_HOST not set — Bridge Health UI callback needs live ArangoDB",
)


def _extract_run_bridge_health_check():
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

    targets = [fn for fn in captured if getattr(fn, "__name__", "") == "run_bridge_health_check"]
    if not targets:
        pytest.fail(
            "No Button.click(fn=run_bridge_health_check) was captured — "
            "the Bridge Health tab's button wiring may have changed (import regression)."
        )
    return targets[0]


@pytest.fixture(scope="module")
def run_bridge_health_check():
    return _extract_run_bridge_health_check()


def test_callback_executes_and_returns_five_outputs(run_bridge_health_check):
    """Matches health_check_btn.click(outputs=[health_status, health_timestamp,
    health_detail, coverage_badge, coverage_detail]) — five string outputs."""
    result = run_bridge_health_check()
    assert isinstance(result, tuple) and len(result) == 5
    overall, timestamp, detail, badge, coverage_text = result
    for name, val in [
        ("overall", overall), ("timestamp", timestamp), ("detail", detail),
        ("badge", badge), ("coverage_text", coverage_text),
    ]:
        assert isinstance(val, str) and val, f"{name} was empty or non-string: {val!r}"


def test_callback_reports_in_sync(run_bridge_health_check):
    """Regression guard for the tables-count false-mismatch bug: this must
    report IN SYNC, not just 'ran without exception'."""
    overall, _timestamp, detail, _badge, _coverage_text = run_bridge_health_check()
    assert overall.startswith("✅"), f"Expected IN SYNC, got: {overall!r}\n{detail}"
    assert "IN SYNC" in overall
    assert "❌" not in detail, f"Detail shows a mismatch row despite overall IN SYNC:\n{detail}"


def test_tables_row_uses_canonical_filtered_count(run_bridge_health_check):
    """The 'tables' row specifically must compare canonical-key-filtered
    ArangoDB count to SQLite schema_nodes — both sides equal and non-zero
    (not a coincidental 0==0)."""
    _overall, _timestamp, detail, _badge, _coverage_text = run_bridge_health_check()

    tables_line = next((ln for ln in detail.splitlines() if ln.strip().startswith("tables")), None)
    assert tables_line is not None, f"No 'tables' row in detail:\n{detail}"

    parts = tables_line.split()
    # Format: "tables  <arango_n>  <sqlite_n>  <match_icon>"
    arango_n, sqlite_n = int(parts[1]), int(parts[2])
    assert arango_n == sqlite_n, f"tables row mismatch: {tables_line!r}"
    assert arango_n > 0, "tables count is 0 — schema_nodes likely not seeded"


def test_coverage_badge_status_is_recognized(run_bridge_health_check):
    """Sweep 1 coverage badge must be one of the four known states, proving
    _get_sweep1_coverage_gaps() ran and its result was formatted, not swallowed."""
    _overall, _timestamp, _detail, badge, _coverage_text = run_bridge_health_check()
    known_prefixes = ("✅", "⚠️", "—", "❌")
    assert badge.startswith(known_prefixes), f"Unrecognized coverage badge: {badge!r}"
