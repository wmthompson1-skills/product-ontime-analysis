"""test_ar_keyword_routing.py

Guards the Receivables (AR) block of the MOCK_ROUTES keyword→intent dispatch
table in production_dispatcher.py.  The AR block covers two intents:

  - order_revenue_recognition — AR aging / invoice / overdue / unpaid queries
  - ar_cash_receipts — cash receipt / installment / customer payment queries
    (added alongside the receivables_cashreceipts_20260724_000002 governed view)

Each test calls extract_via_mock() directly with a natural-language query and
asserts the expected intent and (optionally) the expected primary concept.
SolderEngine is replaced with a MagicMock so no database or manifest file is
needed — the tests are pure routing logic, isolated from SQL assembly.

Coverage:
  - Aging/invoice/overdue/unpaid keywords → order_revenue_recognition
  - Cash receipt/installment/customer payment keywords → ar_cash_receipts
  - Priority order: AR keywords win over the generic "customer" catch-all
    ("which customers have unpaid invoices" must NOT route to
    defect_customer_impact); "customer payment" routes to ar_cash_receipts
  - Concept payloads match the canonical AR concept nodes
  - Pre-existing routes are unaffected (customer/defect, cost, supplier,
    inventory)
  - Unknown queries still return OUT_OF_SCOPE
"""
import os
import sys
from unittest.mock import MagicMock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
HF_DIR    = os.path.dirname(TESTS_DIR)
sys.path.insert(0, HF_DIR)

from production_dispatcher import ProductionDispatcher, MOCK_ROUTES  # noqa: E402

AR_INTENT = "order_revenue_recognition"
AR_RECEIPTS_INTENT = "ar_cash_receipts"  # distinct intent added for cash-receipt queries


def _dispatcher() -> ProductionDispatcher:
    """Return a dispatcher with a mock SolderEngine — no DB or manifest needed."""
    return ProductionDispatcher(solder_engine=MagicMock(), use_live_api=False)


def _route(query: str):
    """Return (intent, concepts) for query via mock routing."""
    result = _dispatcher().extract_via_mock(query)
    return result["intent"], result["concepts"]


# ---------------------------------------------------------------------------
# Core AR routes
# ---------------------------------------------------------------------------

def test_ar_aging_routes_to_receivables_intent():
    intent, concepts = _route("Show me the AR aging report")
    assert intent == AR_INTENT, f"Expected {AR_INTENT}, got {intent}"
    assert "ARInvoiceReference" in concepts


def test_aging_alone_routes_to_receivables_intent():
    intent, _ = _route("How is our invoice aging looking?")
    assert intent == AR_INTENT


def test_receivable_routes_to_receivables_intent():
    intent, concepts = _route("What receivables are still open?")
    assert intent == AR_INTENT
    assert "ARInvoiceReference" in concepts


def test_invoice_routes_to_receivables_intent():
    intent, _ = _route("Which invoices are still open?")
    assert intent == AR_INTENT


def test_past_due_routes_to_receivables_intent():
    intent, _ = _route("What is past due right now?")
    assert intent == AR_INTENT


def test_overdue_routes_to_receivables_intent():
    intent, _ = _route("Show overdue balances by customer")
    assert intent == AR_INTENT


def test_unpaid_routes_to_receivables_intent():
    intent, _ = _route("How much is unpaid?")
    assert intent == AR_INTENT


def test_cash_receipt_routes_with_payment_concepts():
    intent, concepts = _route("List the cash receipts for June")
    assert intent == AR_RECEIPTS_INTENT, f"Expected {AR_RECEIPTS_INTENT}, got {intent}"
    assert "ARCashReceiptAmount" in concepts
    assert "ARPaymentDate" in concepts


def test_installment_routes_with_installment_concepts():
    intent, concepts = _route("Show the installment schedule")
    assert intent == AR_RECEIPTS_INTENT, f"Expected {AR_RECEIPTS_INTENT}, got {intent}"
    assert "ARInstallmentSequence" in concepts


def test_revenue_routes_with_order_accounting_state():
    intent, concepts = _route("How much revenue is recognized vs backlog?")
    assert intent == AR_INTENT
    assert "OrderAccountingState" in concepts


# ---------------------------------------------------------------------------
# Priority order vs generic catch-alls
# ---------------------------------------------------------------------------

def test_unpaid_invoices_beats_customer_catch_all():
    """AR keywords must be checked before the generic 'customer' route."""
    intent, _ = _route("Which customers have unpaid invoices?")
    assert intent == AR_INTENT, (
        f"Expected {AR_INTENT} (AR block must beat 'customer' catch-all), got {intent}"
    )


def test_customer_payment_beats_customer_catch_all():
    intent, concepts = _route("Were any customer payments received this week?")
    assert intent == AR_RECEIPTS_INTENT, (
        f"Expected {AR_RECEIPTS_INTENT} ('customer payment' routes to cash receipts), got {intent}"
    )
    assert "ARCashReceiptAmount" in concepts


def test_ar_block_ordering_in_mock_routes():
    """The AR block must physically precede the 'cost' and 'customer' routes."""
    keys = list(MOCK_ROUTES.keys())
    assert keys.index("ar aging") < keys.index("cost")
    assert keys.index("customer payment") < keys.index("customer")
    assert keys.index("unpaid") < keys.index("customer")


# ---------------------------------------------------------------------------
# Pre-existing routes unaffected
# ---------------------------------------------------------------------------

def test_plain_customer_still_routes_to_defect_customer_impact():
    intent, _ = _route("What problems reach the customer?")
    assert intent == "defect_customer_impact"


def test_cost_still_routes_to_defect_cost_analysis():
    intent, _ = _route("What is the cost impact of defects?")
    assert intent == "defect_cost_analysis"


def test_supplier_still_routes_to_scorecard():
    intent, _ = _route("How are our suppliers performing?")
    assert intent == "supplier_scorecard"


def test_inventory_still_routes_to_stock_status():
    intent, _ = _route("What inventory do we have?")
    assert intent == "inventory_stock_status"


def test_unknown_query_is_out_of_scope():
    intent, _ = _route("What's the weather in Wichita?")
    assert intent == "OUT_OF_SCOPE"


# ---------------------------------------------------------------------------
# Gate-style runner (python tests/test_ar_keyword_routing.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)
