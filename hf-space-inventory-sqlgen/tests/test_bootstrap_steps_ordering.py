"""Gate test — bootstrap_db.py STEPS ordering (import-only, no DB, no migrations).

Complements test_schema_browser_receivable_payment.py: that gate verifies the
already-built DB, while this one catches an ordering regression in the STEPS
list itself BEFORE any bootstrap runs.

Invariant locked:
    collect_june2026_ar.py must appear AFTER add_receivable_tables.py in
    STEPS — receivable_payment (created by collect_june2026_ar.py) depends
    on the receivable table existing first.

Run gate-style from the repo root:
    python hf-space-inventory-sqlgen/tests/test_bootstrap_steps_ordering.py
"""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_HF_DIR = os.path.dirname(_HERE)
BOOTSTRAP_PATH = os.path.join(_HF_DIR, "scripts", "bootstrap_db.py")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def load_steps() -> list[str]:
    spec = importlib.util.spec_from_file_location("bootstrap_db", BOOTSTRAP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [rel for rel, _args in module.STEPS]


def indices_of(steps: list[str], basename: str) -> list[int]:
    return [i for i, rel in enumerate(steps) if os.path.basename(rel) == basename]


def test_steps_ordering() -> None:
    steps = load_steps()

    receivable_idx = indices_of(steps, "add_receivable_tables.py")
    collect_idx = indices_of(steps, "collect_june2026_ar.py")

    check(
        "STEPS contains add_receivable_tables.py",
        len(receivable_idx) >= 1,
        "add_receivable_tables.py missing from STEPS",
    )
    check(
        "STEPS contains collect_june2026_ar.py",
        len(collect_idx) >= 1,
        "collect_june2026_ar.py missing from STEPS",
    )
    if not receivable_idx or not collect_idx:
        return

    # Every collect_june2026_ar.py occurrence must come after the LAST
    # add_receivable_tables.py occurrence — receivable_payment depends on
    # the receivable table existing first.
    check(
        "collect_june2026_ar.py runs AFTER add_receivable_tables.py",
        min(collect_idx) > max(receivable_idx),
        f"add_receivable_tables at {receivable_idx}, collect_june2026_ar at {collect_idx}",
    )


def main() -> int:
    print("bootstrap_db.py STEPS ordering gate")
    if not os.path.exists(BOOTSTRAP_PATH):
        print(f"  [FAIL] bootstrap_db.py not found at {BOOTSTRAP_PATH!r}")
        return 1
    test_steps_ordering()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All STEPS ordering checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
