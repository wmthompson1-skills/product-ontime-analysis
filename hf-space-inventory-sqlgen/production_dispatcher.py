"""
ProductionDispatcher - Hybrid RAG Semantic Router
===================================================
Takes natural language questions and routes them through:
  1. LLM (HuggingFace Inference) → Closed-vocabulary intent/concept extraction
  2. SolderEngine → Governed SQL assembly from approved snippets

The LLM acts as a Semantic Router, NOT a SQL generator.
All SQL comes from SME-approved, human-governed ground truth.

Fallback: Mock resolver for demo/testing when API is unavailable.
"""

import os
import json
import sqlite3
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

from solder_engine import SolderEngine, SQLITE_DB_PATH

HF_DEFAULT_MODEL = "mistralai/Mistral-7B-Instruct-v0.3"

MOCK_ROUTES = {
    # Job Costing Ledger (gl_*) routes — MUST come first: "job cost" would
    # otherwise be swallowed by the generic "cost" route below, and "wip for
    # job" must beat the shorter "wip".
    "wip for job": {
        "intent": "ledger_job_cost_summary",
        "concepts": ["GLJobCostSummary"],
        "perspective": "General_Ledger"
    },
    "wip on job": {
        "intent": "ledger_job_cost_summary",
        "concepts": ["GLJobCostSummary"],
        "perspective": "General_Ledger"
    },
    "job cost": {
        "intent": "ledger_job_cost_summary",
        "concepts": ["GLJobCostSummary"],
        "perspective": "General_Ledger"
    },
    "cost element": {
        "intent": "ledger_job_cost_summary",
        "concepts": ["GLJobCostSummary"],
        "perspective": "General_Ledger"
    },
    "wip": {
        "intent": "ledger_inventory_balance",
        "concepts": ["GLInventoryBalance"],
        "perspective": "General_Ledger"
    },
    "work in process": {
        "intent": "ledger_inventory_balance",
        "concepts": ["GLInventoryBalance"],
        "perspective": "General_Ledger"
    },
    "inventory balance": {
        "intent": "ledger_inventory_balance",
        "concepts": ["GLInventoryBalance"],
        "perspective": "General_Ledger"
    },
    "inventory bucket": {
        "intent": "ledger_inventory_balance",
        "concepts": ["GLInventoryBalance"],
        "perspective": "General_Ledger"
    },
    "event trace": {
        "intent": "ledger_event_trace",
        "concepts": ["GLEventTrace"],
        "perspective": "General_Ledger"
    },
    "ledger trace": {
        "intent": "ledger_event_trace",
        "concepts": ["GLEventTrace"],
        "perspective": "General_Ledger"
    },
    "trace": {
        "intent": "ledger_event_trace",
        "concepts": ["GLEventTrace"],
        "perspective": "General_Ledger"
    },
    "material issued": {
        "intent": "ledger_material_issued",
        "concepts": ["GLMaterialIssued"],
        "perspective": "General_Ledger"
    },
    "issued": {
        "intent": "ledger_material_issued",
        "concepts": ["GLMaterialIssued"],
        "perspective": "General_Ledger"
    },
    "finished goods produced": {
        "intent": "ledger_fg_production",
        "concepts": ["GLFGProduced"],
        "perspective": "General_Ledger"
    },
    "goods produced": {
        "intent": "ledger_fg_production",
        "concepts": ["GLFGProduced"],
        "perspective": "General_Ledger"
    },
    "produced": {
        "intent": "ledger_fg_production",
        "concepts": ["GLFGProduced"],
        "perspective": "General_Ledger"
    },
    # Receivables (AR) routes — MUST come before the generic "cost" and
    # "customer" catch-alls: "which customers have unpaid invoices" would
    # otherwise be swallowed by "customer" → defect_customer_impact.
    # NOTE: "invoice" also substring-matches "uninvoiced" — if payables
    # routes are ever added, their "uninvoiced" / "receipt" keywords must
    # be inserted BEFORE this block.
    "ar aging": {
        "intent": "order_revenue_recognition",
        "concepts": ["ARInvoiceReference"],
        "perspective": "Receivables"
    },
    "aging": {
        "intent": "order_revenue_recognition",
        "concepts": ["ARInvoiceReference"],
        "perspective": "Receivables"
    },
    "receivable": {
        "intent": "order_revenue_recognition",
        "concepts": ["ARInvoiceReference"],
        "perspective": "Receivables"
    },
    # Cash-receipt routes point at ar_cash_receipts (governed SQL over
    # receivable_payment ⋈ receivable). They MUST come BEFORE the "invoice"
    # keyword: "show me cash receipts against invoice AR-123" contains both
    # and must resolve to cash receipts. The generic "payment" keyword is
    # deliberately last of the four so the more specific spellings win.
    "cash receipt": {
        "intent": "ar_cash_receipts",
        "concepts": ["ARCashReceiptAmount", "ARPaymentDate"],
        "perspective": "Receivables"
    },
    "installment": {
        "intent": "ar_cash_receipts",
        "concepts": ["ARInstallmentSequence", "ARPaymentDate"],
        "perspective": "Receivables"
    },
    "customer payment": {
        "intent": "ar_cash_receipts",
        "concepts": ["ARCashReceiptAmount", "ARPaymentDate"],
        "perspective": "Receivables"
    },
    "payment": {
        "intent": "ar_cash_receipts",
        "concepts": ["ARCashReceiptAmount", "ARPaymentDate"],
        "perspective": "Receivables"
    },
    "invoice": {
        "intent": "order_revenue_recognition",
        "concepts": ["ARInvoiceReference"],
        "perspective": "Receivables"
    },
    "past due": {
        "intent": "order_revenue_recognition",
        "concepts": ["ARInvoiceReference"],
        "perspective": "Receivables"
    },
    "overdue": {
        "intent": "order_revenue_recognition",
        "concepts": ["ARInvoiceReference"],
        "perspective": "Receivables"
    },
    "unpaid": {
        "intent": "order_revenue_recognition",
        "concepts": ["ARInvoiceReference"],
        "perspective": "Receivables"
    },
    "revenue": {
        "intent": "order_revenue_recognition",
        "concepts": ["OrderAccountingState"],
        "perspective": "Receivables"
    },
    "cost": {
        "intent": "defect_cost_analysis",
        "concepts": ["DefectSeverityCost"],
        "perspective": "Finance"
    },
    "defect": {
        "intent": "defect_quality_trending",
        "concepts": ["DefectSeverityQuality"],
        "perspective": "Quality"
    },
    "customer": {
        "intent": "defect_customer_impact",
        "concepts": ["DefectSeverityCustomer"],
        "perspective": "Customer"
    },
    "supplier": {
        "intent": "supplier_scorecard",
        "concepts": ["DeliveryPerformanceSupplier"],
        "perspective": "Operations"
    },
    "oee": {
        "intent": "oee_operational",
        "concepts": ["OEEOperational"],
        "perspective": "Operations"
    },
    "maintenance": {
        "intent": "maintenance_scheduling",
        "concepts": ["EquipmentStateMaintenance"],
        "perspective": "Operations"
    },
    "schedule": {
        "intent": "schedule_adherence",
        "concepts": ["OrderLifecycleState"],
        "perspective": "Operations"
    },
    "ncm": {
        "intent": "defect_cost_analysis",
        "concepts": ["DefectSeverityCost", "NCMDispositionFinance"],
        "perspective": "Finance"
    },
    "quality": {
        "intent": "defect_quality_trending",
        "concepts": ["DefectSeverityQuality", "DefectSeverityCost"],
        "perspective": "Quality"
    },
    "penalty": {
        "intent": "supplier_cost_penalties",
        "concepts": ["DeliveryPerformanceFinance"],
        "perspective": "Finance"
    },
    "reorder": {
        "intent": "inventory_planning",
        "concepts": ["ReorderPoint"],
        "perspective": "Inventory_Transactions"
    },
    "replenish": {
        "intent": "inventory_planning",
        "concepts": ["ReorderPoint"],
        "perspective": "Inventory_Transactions"
    },
    # "lead time demand" / "demand during lead time" must appear BEFORE
    # the shorter "lead time" keyword — otherwise the substring match on
    # "lead time" wins and the more specific routes are never reached.
    "lead time demand": {
        "intent": "inventory_lead_time_demand",
        "concepts": ["LeadTimeDemand"],
        "perspective": "Inventory_Transactions"
    },
    "demand during lead time": {
        "intent": "inventory_lead_time_demand",
        "concepts": ["LeadTimeDemand"],
        "perspective": "Inventory_Transactions"
    },
    "lead time": {
        "intent": "inventory_planning",
        "concepts": ["LeadTime"],
        "perspective": "Inventory_Transactions"
    },
    "mrp": {
        "intent": "inventory_planning",
        "concepts": ["ReorderPoint", "OnHandQuantity", "LeadTime"],
        "perspective": "Inventory_Transactions"
    },
    "on hand": {
        "intent": "inventory_stock_status",
        "concepts": ["OnHandQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "on-hand": {
        "intent": "inventory_stock_status",
        "concepts": ["OnHandQuantity"],
        "perspective": "Inventory_Transactions"
    },
    # Specific phrases that contain "stock level" as a substring must appear
    # BEFORE "stock level" so the substring match hits the right route first.
    "safety stock": {
        "intent": "inventory_safety_stock",
        "concepts": ["SafetyStock"],
        "perspective": "Inventory_Transactions"
    },
    "buffer stock": {
        "intent": "inventory_safety_stock",
        "concepts": ["SafetyStock"],
        "perspective": "Inventory_Transactions"
    },
    "minimum stock": {
        "intent": "inventory_minimum_stock",
        "concepts": ["MinimumStockQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "min stock": {
        "intent": "inventory_minimum_stock",
        "concepts": ["MinimumStockQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "maximum stock": {
        "intent": "inventory_maximum_stock",
        "concepts": ["MaximumStockQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "max stock": {
        "intent": "inventory_maximum_stock",
        "concepts": ["MaximumStockQuantity"],
        "perspective": "Inventory_Transactions"
    },
    # Generic stock-level and inventory catch-alls — must come after the
    # specific "* stock" phrases above.
    "stock level": {
        "intent": "inventory_stock_status",
        "concepts": ["OnHandQuantity"],
        "perspective": "Inventory_Transactions"
    },
    # "committed" must appear before "inventory" — "committed inventory"
    # would otherwise match the shorter "inventory" catch-all first.
    "allocated": {
        "intent": "inventory_allocated_qty",
        "concepts": ["AllocatedQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "committed": {
        "intent": "inventory_allocated_qty",
        "concepts": ["AllocatedQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "inventory": {
        "intent": "inventory_stock_status",
        "concepts": ["OnHandQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "atp": {
        "intent": "inventory_atp",
        "concepts": ["AvailableToPromise"],
        "perspective": "Inventory_Transactions"
    },
    "available to promise": {
        "intent": "inventory_atp",
        "concepts": ["AvailableToPromise"],
        "perspective": "Inventory_Transactions"
    },
    "available": {
        "intent": "inventory_atp",
        "concepts": ["AvailableToPromise"],
        "perspective": "Inventory_Transactions"
    },
    # "lead time demand" / "demand during lead time" are already positioned
    # before the shorter "lead time" key (see earlier in this dict).
    "lead time demand": {
        "intent": "inventory_lead_time_demand",
        "concepts": ["LeadTimeDemand"],
        "perspective": "Inventory_Transactions"
    },
    "demand during lead time": {
        "intent": "inventory_lead_time_demand",
        "concepts": ["LeadTimeDemand"],
        "perspective": "Inventory_Transactions"
    },
    "eoq": {
        "intent": "inventory_eoq",
        "concepts": ["EconomicOrderQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "economic order quantity": {
        "intent": "inventory_eoq",
        "concepts": ["EconomicOrderQuantity"],
        "perspective": "Inventory_Transactions"
    },
    "economic order": {
        "intent": "inventory_eoq",
        "concepts": ["EconomicOrderQuantity"],
        "perspective": "Inventory_Transactions"
    },
}


@dataclass
class DispatchResult:
    user_query: str
    intent: str
    concepts: List[str]
    perspective: str
    assembled_sql: str
    assembly_report: List[str]
    warnings: List[str]
    routing_mode: str
    routing_confidence: str = ""
    out_of_scope: bool = False
    binding_key: str = ""
    fail_closed: bool = False
    fail_closed_condition: str = ""


class ProductionDispatcher:

    def __init__(self, solder_engine: SolderEngine = None,
                 db_path: str = None, use_live_api: bool = True,
                 hf_model: str = None):
        self.solder = solder_engine or SolderEngine()
        self.db_path = db_path or SQLITE_DB_PATH
        self.use_live_api = use_live_api
        self.hf_model = hf_model or HF_DEFAULT_MODEL

        self.intents = self._load_vocabulary("SELECT intent_name FROM schema_intents")
        self.concepts = self._load_vocabulary("SELECT concept_name FROM schema_concepts")
        self.perspectives = self._load_vocabulary("SELECT perspective_name FROM schema_perspectives")

        self.intent_details = self._load_intent_details()
        self.intent_binding_map = self._load_intent_binding_map()

    def _load_vocabulary(self, query: str) -> List[str]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(query).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def _load_intent_details(self) -> List[Dict[str, str]]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT intent_name, intent_category, description, typical_question,
                       primary_binding_key
                FROM schema_intents ORDER BY intent_category
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _load_intent_binding_map(self) -> Dict[str, str]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT intent_name, primary_binding_key
                FROM schema_intents
                WHERE primary_binding_key IS NOT NULL
            """).fetchall()
            return {r[0]: r[1] for r in rows}
        finally:
            conn.close()

    def _build_system_prompt(self) -> str:
        intent_block = "\n".join(
            f"  - {d['intent_name']} ({d['intent_category']}): {d['description']}"
            + (f'  e.g. "{d["typical_question"]}"' if d.get("typical_question") else "")
            for d in self.intent_details
        )
        concept_block = ", ".join(self.concepts)
        perspective_block = ", ".join(self.perspectives)

        return f"""You are a Semantic Router for a manufacturing intelligence system.
Your ONLY job is to map a user's natural language question to the correct
Intent, Concepts, and Perspective from a CLOSED vocabulary.

AVAILABLE INTENTS:
{intent_block}

AVAILABLE CONCEPTS: {concept_block}

AVAILABLE PERSPECTIVES: {perspective_block}

RULES:
1. You MUST return ONLY a JSON object. No explanation, no markdown, no extra text.
2. The "intent" field MUST be exactly one of the AVAILABLE INTENTS listed above.
3. The "concepts" field MUST be a list of 1-3 items from AVAILABLE CONCEPTS.
4. The "perspective" field MUST be exactly one of the AVAILABLE PERSPECTIVES.
5. If the question is unrelated to manufacturing, return:
   {{"intent": "OUT_OF_SCOPE", "concepts": [], "perspective": "", "confidence": "none"}}
6. Include a "confidence" field: "high", "medium", or "low".

RETURN FORMAT (JSON only, no other text):
{{"intent": "...", "concepts": ["...", "..."], "perspective": "...", "confidence": "..."}}"""

    def extract_via_llm(self, user_query: str) -> Dict[str, Any]:
        try:
            from huggingface_hub import InferenceClient

            hf_token = os.environ.get("HF_TOKEN")
            client = InferenceClient(
                provider="hf-inference",
                api_key=hf_token
            )

            response = client.chat.completions.create(
                model=self.hf_model,
                messages=[
                    {"role": "system", "content": self._build_system_prompt()},
                    {"role": "user", "content": user_query}
                ],
                temperature=0.1,
                max_tokens=200
            )

            content = response.choices[0].message.content

            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            parsed = json.loads(content)

            if parsed.get("intent") not in self.intents and parsed.get("intent") != "OUT_OF_SCOPE":
                parsed["confidence"] = "low"
                parsed["warning"] = f"LLM returned unknown intent: {parsed.get('intent')}"

            filtered_concepts = [c for c in parsed.get("concepts", []) if c in self.concepts]
            if len(filtered_concepts) < len(parsed.get("concepts", [])):
                parsed["warning"] = parsed.get("warning", "") + " Some concepts filtered (not in vocabulary)."
            parsed["concepts"] = filtered_concepts

            return parsed

        except json.JSONDecodeError as e:
            return {
                "intent": "ERROR",
                "concepts": [],
                "perspective": "",
                "confidence": "none",
                "error": f"Failed to parse LLM JSON response: {e}"
            }
        except Exception as e:
            return {
                "intent": "ERROR",
                "concepts": [],
                "perspective": "",
                "confidence": "none",
                "error": str(e)
            }

    def extract_via_mock(self, user_query: str) -> Dict[str, Any]:
        query_lower = user_query.lower()

        for keyword, route in MOCK_ROUTES.items():
            if keyword in query_lower:
                return {
                    "intent": route["intent"],
                    "concepts": route["concepts"],
                    "perspective": route["perspective"],
                    "confidence": "mock"
                }

        return {
            "intent": "OUT_OF_SCOPE",
            "concepts": [],
            "perspective": "",
            "confidence": "mock"
        }

    def dispatch(self, user_query: str,
                 perspective_override: str = None,
                 force_mock: bool = False,
                 base_table: str = "stg_manufacturing_flat",
                 target_dialect: str = "sqlite") -> DispatchResult:

        if force_mock or not self.use_live_api:
            semantic_map = self.extract_via_mock(user_query)
            routing_mode = "mock"
        else:
            semantic_map = self.extract_via_llm(user_query)
            if semantic_map.get("intent") == "ERROR":
                semantic_map = self.extract_via_mock(user_query)
                routing_mode = "mock (API fallback)"
            else:
                routing_mode = f"live (HF: {self.hf_model})"

        intent = semantic_map.get("intent", "OUT_OF_SCOPE")
        concepts = semantic_map.get("concepts", [])
        perspective = perspective_override or semantic_map.get("perspective", "")
        confidence = semantic_map.get("confidence", "")

        if intent == "OUT_OF_SCOPE":
            return DispatchResult(
                user_query=user_query,
                intent="OUT_OF_SCOPE",
                concepts=[],
                perspective="",
                assembled_sql="-- Question is outside manufacturing domain scope",
                assembly_report=["Question could not be mapped to any manufacturing intent."],
                warnings=["OUT_OF_SCOPE: Try asking about defects, costs, suppliers, OEE, scheduling, or inventory/MRP."],
                routing_mode=routing_mode,
                routing_confidence=confidence,
                out_of_scope=True
            )

        binding_key = self.intent_binding_map.get(intent)
        if binding_key:
            binding_result = self.solder.resolve_by_binding_key(binding_key, target_dialect=target_dialect)
            if binding_result.get("sql"):
                report = [f"Resolved via primary_binding_key: `{binding_key}`"]
                report.extend(binding_result.get("report", []))
                warnings = binding_result.get("warnings", [])
                if semantic_map.get("warning"):
                    warnings.insert(0, semantic_map["warning"])
                return DispatchResult(
                    user_query=user_query,
                    intent=intent,
                    concepts=concepts,
                    perspective=perspective,
                    assembled_sql=binding_result["sql"],
                    assembly_report=report,
                    warnings=warnings,
                    routing_mode=routing_mode,
                    routing_confidence=confidence,
                    binding_key=binding_key
                )
            if binding_result.get("fail_closed"):
                # The primary binding-key path failed closed (missing snippet /
                # empty snippet / fingerprint mismatch / parse failure). We must
                # NOT silently fall back to assemble_query and serve different
                # SQL — surface the fail-closed reason and stop here.
                report = [f"Resolved via primary_binding_key: `{binding_key}`"]
                report.extend(binding_result.get("report", []))
                warnings = binding_result.get("warnings", [])
                if semantic_map.get("warning"):
                    warnings.insert(0, semantic_map["warning"])
                return DispatchResult(
                    user_query=user_query,
                    intent=intent,
                    concepts=concepts,
                    perspective=perspective,
                    assembled_sql=binding_result.get("sql", ""),
                    assembly_report=report,
                    warnings=warnings,
                    routing_mode=routing_mode,
                    routing_confidence=confidence,
                    binding_key=binding_key,
                    fail_closed=True,
                    fail_closed_condition=binding_result.get("fail_condition") or ""
                )

        if not concepts:
            return DispatchResult(
                user_query=user_query,
                intent=intent,
                concepts=[],
                perspective=perspective,
                assembled_sql=f"-- No concepts identified for intent '{intent}'",
                assembly_report=[f"Intent '{intent}' identified but no concepts extracted."],
                warnings=["No concepts found. Try being more specific about what data you need."],
                routing_mode=routing_mode,
                routing_confidence=confidence
            )

        assembly_result = self.solder.assemble_query(
            intent=intent,
            perspective=perspective,
            concepts=concepts,
            base_table=base_table,
            target_dialect=target_dialect
        )

        warnings = assembly_result.get("warnings", [])
        assembly_fail_closed = assembly_result.get("fail_closed", False)
        if assembly_fail_closed:
            fc_concepts = assembly_result.get("fail_closed_concepts", [])
            fc_condition = assembly_result.get("fail_closed_condition") or ""
            # Drive the reason off the actual condition code — not every
            # fail-closed is a perspective mismatch (could be a missing snippet,
            # a parse failure, or a structural-fingerprint drift).
            reason = {
                "no_perspective_compatible_snippet": (
                    "no perspective-compatible snippet under perspective "
                    f"'{perspective}'; cross-perspective SQL is never served"
                ),
                "missing_snippet_file": "the approved binding has no snippet SQL to serve",
                "fingerprint_validation_failed": (
                    "the snippet failed structural-fingerprint validation "
                    "(unparseable or drifted from its SME-approved base tables)"
                ),
                "multiple": "multiple fail-closed conditions were triggered",
            }.get(fc_condition, "one or more concepts could not be served")
            warnings.insert(
                0,
                f"FAIL-CLOSED ({fc_condition or 'unknown'}): {reason}. "
                f"Blocked concepts: {', '.join(fc_concepts)}. No SQL is served."
            )
        if semantic_map.get("warning"):
            warnings.insert(0, semantic_map["warning"])

        return DispatchResult(
            user_query=user_query,
            intent=intent,
            concepts=concepts,
            perspective=perspective,
            assembled_sql=assembly_result.get("sql", ""),
            assembly_report=assembly_result.get("report", []),
            warnings=warnings,
            routing_mode=routing_mode,
            routing_confidence=confidence,
            fail_closed=assembly_fail_closed,
            fail_closed_condition=assembly_result.get("fail_closed_condition") or ""
        )
