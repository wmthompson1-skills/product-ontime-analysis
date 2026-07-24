"""Seed the SME-approved semantic elevations into SQLite (authoritative manifest).

This is the *content* that lights up the node-guarded ``resolves_to`` scaffolding in
``export_graph_metadata.py``. SQLite is the source of truth; running this then
re-running the exporter + loader produces the corresponding ``resolves_to`` edges.

Idempotent: every table carries a UNIQUE natural key
(schema_concepts.concept_name; schema_perspective_concepts(perspective_id,
concept_id); schema_concept_fields(table_name, field_name, concept_id)), so
re-running is a no-op via INSERT OR IGNORE / name lookups. This file holds the
full approved list across all batches — run it once to reproduce every elevation.

Curation rule: M1-M2 elevations are bounded categorical discriminators
(status / type / class / location) on a canonical business column — not a unique
id, date, free-text label, or continuous measure. M3 adds two further kinds:
  * canonical named MEASURES — a column that directly realizes a single named
    business metric (e.g. part.reorder_point -> ReorderPoint). These are
    continuous, but the elevation maps the column to ONE canonical measure
    concept, so the categorical-discriminator rule does not apply.
  * glossary-only concept NODES — business vocabulary seeded as perspective-
    agnostic concept nodes with NO resolves_to edge yet, because no physical column
    anchors them (the ontology can hold a term before the ETL pipeline catches
    up). They become elevations later when a column is mapped.

Concept identity is perspective-agnostic: the perspective is stamped ONLY on the
resolves_to edge, never on the concept node (the dual-namespace rule).
"""
import json
import os
import sqlite3

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hf-space-inventory-sqlgen", "app_schema", "manufacturing.db",
)

# concept_name -> (concept_type, domain, description). Concepts to ensure exist.
# Concepts NOT listed here (e.g. OrderAccountingState) are existing SME vocabulary
# that we only *reference* — we never recreate shipped concepts.
NEW_CONCEPTS = {
    "WarehouseLocation": (
        "classification", "operations",
        "Warehouse/site a stock move is distributed to (inventory traced by move)",
    ),
    "ThreeWayMatchState": (
        "state", "finance",
        "PO/receipt/invoice three-way match status used to validate AP invoices",
    ),
    "WorkOrderLifecycleState": (
        "state", "operations",
        "Work order lifecycle stage on the shop floor (Open->Released->In "
        "Process->Complete->Closed)",
    ),
    "PartSourcingClass": (
        "classification", "operations",
        "How a part is sourced: engineered/made vs bought vs raw vs hardware vs "
        "outside service (engineering make/buy intent)",
    ),
    "StockMovementDirection": (
        "classification", "operations",
        "Direction of an inventory transaction: stock receipt (in) vs issue (out)",
    ),
    "OperationExecutionState": (
        "state", "operations",
        "Execution state of a routing operation (queued -> started -> complete)",
    ),
    "QuantityBasisEngineering": (
        "metric", "operations",
        "Quantity read through the engineering lens: a per-unit, as-designed "
        "requirement basis (engineering normalizes material needs to a single unit)",
    ),
    "QuantityBasisManufacturing": (
        "metric", "operations",
        "Quantity read through the manufacturing lens: a production batch / lot "
        "size to build or fulfill (qty > 1)",
    ),
    "PurchaseOrderLifecycleState": (
        "state", "procurement",
        "Lifecycle state of a purchase order (open -> approved -> received -> closed)",
    ),
    "ReceivingInspectionState": (
        "state", "quality",
        "Inspection disposition of a received shipment (pending -> pass / fail)",
    ),
    "CertificationStatusState": (
        "state", "quality",
        "Validity state of a certification (active / expired / revoked / pending)",
    ),
    "CertificationType": (
        "classification", "quality",
        "Kind of certification held (material / process / quality-system / etc.)",
    ),
    "RequirementBasisEngineering": (
        "classification", "operations",
        "Requirement read through the engineering lens: an as-designed routing "
        "standard on a PART component (per-unit basis); component_type='PART'",
    ),
    "RequirementBasisManufacturing": (
        "classification", "operations",
        "Requirement read through the manufacturing lens: as-built actuals on a "
        "WORK_ORDER component (batch basis); component_type='WORK_ORDER'",
    ),
    # --- Receivable_Payment (AR cash-receipt layer). Four concepts anchor the
    # key columns of receivable_payment so the table surfaces in the Semantic
    # Graph (not just the Schema Browser). Receivables perspective; finance
    # domain. Pattern mirrors the AP payables elevations (batch 4).
    "ARInvoiceReference": (
        "classification", "finance",
        "FK link that identifies which AR invoice a cash-receipt installment "
        "is applied against (receivable_payment.invoice_id -> receivable)",
    ),
    "ARCashReceiptAmount": (
        "metric", "finance",
        "Dollar amount of cash received in a single payment installment "
        "against an accounts-receivable invoice",
    ),
    "ARPaymentDate": (
        "state", "finance",
        "Date on which a cash payment was received and applied to an AR "
        "invoice installment (temporal cash-settlement marker)",
    ),
    "ARInstallmentSequence": (
        "classification", "finance",
        "Ordinal position of this payment within a multi-installment payment "
        "schedule for an AR invoice (1 = first installment)",
    ),
    # --- General_Ledger (job-costing ledger) vocabulary. Mirrors the five
    # SKOS anchors of the Job-Costing Ledger Concept Scheme; each concept
    # anchors one governed ledger query intent (ledger_* in schema_intents).
    "LedgerPostingEventClass": (
        "classification", "finance",
        "Kind of ledger posting event in the gl_events audit trail "
        "(RM_ISSUE / LABOR / BURDEN / FG_COMPLETION)",
    ),
    "JobCostElement": (
        "classification", "finance",
        "Cost element of a job-cost ledger line (labor / material / burden / "
        "service) rolled up per work order in gl_job_cost_detail",
    ),
    "InventoryBucketBalance": (
        "metric", "finance",
        "Signed running balance of a perpetual inventory bucket (Raw Materials "
        "/ WIP / Finished Goods) from the gl_* sub-ledgers; the approved "
        "ledger_inventorybalance snippet is the authoritative derivation",
    ),
    "MaterialIssueFlow": (
        "metric", "finance",
        "Material issued to jobs over a period: RM_ISSUE outflow from the Raw "
        "Materials bucket, reported as positive issued value per part and job",
    ),
    "FinishedGoodsProductionFlow": (
        "metric", "finance",
        "Finished goods produced over a period: FG_COMPLETION inflow into the "
        "Finished Goods bucket per part and job",
    ),
}

# M3 — MRP / inventory-planning vocabulary, seeded as perspective-AGNOSTIC
# concept NODES enriched with synonyms + tags. Only the 3 terms with a real
# physical column get a resolves_to edge (see batch 6 below); the other 7 are
# intentional glossary-only nodes — the ontology can securely hold a business
# term before a column / ETL pipeline maps it.
# concept_name -> (concept_type, domain, description, synonyms, tags)
MRP_CONCEPTS = {
    # --- the 3 column-anchored measures (get a resolves_to edge in batch 6) ---
    "ReorderPoint": (
        "metric", "operations",
        "Inventory level at which a replenishment order is triggered "
        "(lead-time demand + safety stock)",
        ["ROP", "reorder level", "order point"],
        ["mrp", "inventory", "replenishment"],
    ),
    "LeadTime": (
        "metric", "operations",
        "Elapsed time from placing a replenishment order to receiving the stock "
        "(in days)",
        ["replenishment lead time", "procurement lead time", "supplier lead time"],
        ["mrp", "inventory", "planning"],
    ),
    "OnHandQuantity": (
        "metric", "operations",
        "Physical quantity of a part currently in stock (book inventory on hand)",
        ["on hand", "QOH", "quantity on hand", "stock on hand"],
        ["mrp", "inventory", "stock"],
    ),
    # --- the 7 glossary-only nodes (no anchoring column yet) ---
    "SafetyStock": (
        "metric", "operations",
        "Buffer inventory held to absorb demand and lead-time variability and "
        "prevent stockouts",
        ["buffer stock", "safety inventory"],
        ["mrp", "inventory", "buffer"],
    ),
    "LeadTimeDemand": (
        "metric", "operations",
        "Expected demand for a part over its replenishment lead time "
        "(average daily demand x lead time)",
        ["demand during lead time", "DLT"],
        ["mrp", "inventory", "demand"],
    ),
    "MinimumStockQuantity": (
        "metric", "operations",
        "Lowest stock level allowed before replenishment is required "
        "(min in a min/max policy)",
        ["min stock", "minimum level", "min"],
        ["mrp", "inventory", "min-max"],
    ),
    "MaximumStockQuantity": (
        "metric", "operations",
        "Highest stock level a part is replenished up to (max in a min/max policy)",
        ["max stock", "maximum level", "max"],
        ["mrp", "inventory", "min-max"],
    ),
    "EconomicOrderQuantity": (
        "metric", "operations",
        "Order quantity that minimizes combined ordering and holding cost "
        "(classic EOQ formula)",
        ["EOQ", "economic order qty", "optimal order quantity"],
        ["mrp", "inventory", "ordering"],
    ),
    "AvailableToPromise": (
        "metric", "operations",
        "Uncommitted inventory available to promise to new customer orders "
        "(on hand minus allocated)",
        ["ATP", "available to promise quantity"],
        ["mrp", "inventory", "availability"],
    ),
    "AllocatedQuantity": (
        "metric", "operations",
        "Quantity of on-hand stock already committed / reserved to existing orders",
        ["allocated", "reserved quantity", "committed quantity"],
        ["mrp", "inventory", "allocation"],
    ),
}

# (perspective_name, concept_name, table, column, weight, context_hint)
ELEVATIONS = [
    # --- batch 1 ---
    ("Inventory_Transactions", "WarehouseLocation",
     "inventory_transaction", "site_id", 3,
     "Inventory move traced to its warehouse site"),
    ("Payables", "ThreeWayMatchState",
     "payables", "three_way_match_status", 3,
     "AP three-way match validation state"),
    ("Receivables", "OrderAccountingState",
     "customer_order", "status", 3,
     "Order status at shipment = revenue recognition (AR lens)"),
    # --- batch 2 ---
    ("Work_Orders", "WorkOrderLifecycleState",
     "work_order", "status", 3,
     "Work order shop-floor lifecycle stage"),
    ("Parts", "PartSourcingClass",
     "part", "part_class", 3,
     "Make/buy/raw sourcing class (engineering design intent)"),
    ("Inventory_Transactions", "StockMovementDirection",
     "inventory_transaction", "type", 3,
     "Stock in vs out direction of the move"),
    ("Manufacturing", "OperationExecutionState",
     "operation", "status", 3,
     "Routing operation execution state"),
    # --- batch 3: same quantity column, two perspective lenses ---
    # No engineering quantity column exists (no BOM table); the engineering vs
    # manufacturing contrast is modeled as dual meaning on the batch-quantity
    # columns. order_qty / work_order.quantity are the multiplier that bridges a
    # per-unit (engineering, =1) requirement to a batch (manufacturing, >1).
    ("Engineering", "QuantityBasisEngineering",
     "work_order", "quantity", 3,
     "Per-unit design basis; batch = N x per-unit requirement"),
    ("Manufacturing", "QuantityBasisManufacturing",
     "work_order", "quantity", 3,
     "Production batch / lot size (qty > 1)"),
    ("Engineering", "QuantityBasisEngineering",
     "customer_order_line", "order_qty", 3,
     "Per-unit design basis multiplier for the ordered line"),
    ("Manufacturing", "QuantityBasisManufacturing",
     "customer_order_line", "order_qty", 3,
     "Ordered quantity driving the manufacturing batch (qty > 1)"),
    # --- batch 4: procurement + quality status/type discriminators ---
    ("Payables", "PurchaseOrderLifecycleState",
     "purchase_order", "status", 3,
     "Purchase order lifecycle state"),
    ("Quality", "ReceivingInspectionState",
     "receiving", "inspection_status", 3,
     "Inspection disposition of a received shipment"),
    ("Quality", "CertificationStatusState",
     "certification", "status", 3,
     "Validity state of a certification"),
    ("Quality", "CertificationType",
     "certification", "cert_type", 3,
     "Kind of certification held"),
    # --- batch 5: requirement.component_type — the value-level engineering vs
    # manufacturing differentiator. Unlike the quantity dual-lens (batch 3),
    # here the column VALUE selects the perspective: PART routes to Engineering,
    # WORK_ORDER routes to Manufacturing. The value condition is carried in the
    # context_hint (the model's "when this meaning applies" slot).
    ("Engineering", "RequirementBasisEngineering",
     "requirement", "component_type", 3,
     "component_type = 'PART' — as-designed routing standard (per-unit, engineering)"),
    ("Manufacturing", "RequirementBasisManufacturing",
     "requirement", "component_type", 3,
     "component_type = 'WORK_ORDER' — as-built actuals (batch, manufacturing)"),
    # --- batch 6: M3 canonical MRP measures. Unlike batches 1-5 (categorical
    # discriminators), these map a part column to ONE named inventory metric.
    # The perspective (Inventory_Transactions) is stamped on the edge only; the
    # concept node itself stays perspective-agnostic.
    ("Inventory_Transactions", "ReorderPoint",
     "part", "reorder_point", 3,
     "Part reorder point — replenishment trigger level"),
    ("Inventory_Transactions", "LeadTime",
     "part", "lead_time_days", 3,
     "Part replenishment lead time (days)"),
    ("Inventory_Transactions", "OnHandQuantity",
     "part", "on_hand_qty", 3,
     "Part on-hand stock quantity"),
    # --- batch 7: dataset-derived concepts (ATP and AllocatedQuantity).
    # No single column IS the metric; the approved SQL snippet is the
    # authoritative definition. The column anchor here captures the PRIMARY
    # INPUT to each derivation so the graph stays navigable and the
    # schema_concept_fields row exists as required by the curation rule.
    # ATP = on_hand_qty − allocated; AllocatedQty = SUM(order_qty) per part.
    ("Inventory_Transactions", "AvailableToPromise",
     "part", "on_hand_qty", 3,
     "Primary stock input to ATP: on-hand qty reduced by open customer-order commitments"),
    ("Inventory_Transactions", "AllocatedQuantity",
     "customer_order_line", "order_qty", 3,
     "Per-line order quantity — the raw demand commitment allocated against on-hand stock"),
    # --- batch 8: five remaining MRP glossary concepts. No dedicated column
    # exists for any of these; each is anchored to the closest available
    # physical input column. The approved SQL snippet is the authoritative
    # definition of the derived / computed measure.
    ("Inventory_Transactions", "SafetyStock",
     "part", "reorder_point", 3,
     "ROP encodes the safety buffer: ROP ≈ SS + lead-time demand"),
    ("Inventory_Transactions", "LeadTimeDemand",
     "part", "lead_time_days", 3,
     "Lead-time days — the planning-window factor in the LT demand derivation"),
    ("Inventory_Transactions", "MinimumStockQuantity",
     "part", "reorder_point", 3,
     "Min-max minimum level = reorder_point (the replenishment floor in this ERP)"),
    ("Inventory_Transactions", "MaximumStockQuantity",
     "po_line", "quantity", 3,
     "Average PO replenishment quantity — upper bound input for the max-stock derivation"),
    ("Inventory_Transactions", "EconomicOrderQuantity",
     "po_line", "quantity", 3,
     "Average observed PO order quantity — empirical EOQ proxy (no D/S/H cost params in schema)"),
    # --- batch 9: General_Ledger (job-costing ledger). The bounded categorical
    # is event_type (the posting event class); the balance/flow metrics follow
    # the batch-7 pattern — anchored to their PRIMARY INPUT column, with the
    # approved governed snippet as the authoritative derivation. Value-routed
    # flows carry the event_type condition in context_hint (batch-5 pattern).
    ("General_Ledger", "LedgerPostingEventClass",
     "gl_events", "event_type", 3,
     "Posting event class in the ledger audit trail (RM_ISSUE / LABOR / BURDEN / FG_COMPLETION)"),
    ("General_Ledger", "JobCostElement",
     "gl_job_cost_detail", "event_type", 3,
     "Cost element of the job-cost line (labor / material / burden per work order)"),
    ("General_Ledger", "InventoryBucketBalance",
     "gl_raw_materials_inventory", "amount", 3,
     "Signed RM-bucket posting amount — primary input to the running bucket balance"),
    ("General_Ledger", "InventoryBucketBalance",
     "gl_wip_inventory", "amount", 3,
     "Signed WIP-bucket posting amount — primary input to the running bucket balance"),
    ("General_Ledger", "InventoryBucketBalance",
     "gl_finished_goods_inventory", "amount", 3,
     "Signed FG-bucket posting amount — primary input to the running bucket balance"),
    ("General_Ledger", "MaterialIssueFlow",
     "gl_raw_materials_inventory", "event_type", 3,
     "event_type = 'RM_ISSUE' — material issued out of the RM bucket to a job"),
    ("General_Ledger", "FinishedGoodsProductionFlow",
     "gl_finished_goods_inventory", "event_type", 3,
     "event_type = 'FG_COMPLETION' — finished goods completed into the FG bucket"),
    # --- batch 10: AR cash-receipt layer (receivable_payment). The Receivables
    # perspective covers AR invoicing at the receivable grain; batch 10 extends
    # coverage to the cash-receipt grain so payment columns surface in the
    # Semantic Graph. ARCashReceiptAmount follows the batch-6 named-measure
    # pattern; ARInvoiceReference / ARInstallmentSequence follow the batch-1/4
    # bounded-categorical pattern; ARPaymentDate follows the batch-5 value-
    # condition / temporal-marker pattern.
    ("Receivables", "ARInvoiceReference",
     "receivable_payment", "invoice_id", 3,
     "AR invoice this payment installment is applied against (FK to receivable)"),
    ("Receivables", "ARCashReceiptAmount",
     "receivable_payment", "amount", 3,
     "Dollar amount of cash received in this payment installment"),
    ("Receivables", "ARPaymentDate",
     "receivable_payment", "payment_date", 3,
     "Date the cash payment was received and applied to the AR invoice"),
    ("Receivables", "ARInstallmentSequence",
     "receivable_payment", "installment_no", 3,
     "Ordinal position of this installment within the payment schedule (1-based)"),
]

# --- M4: METRIC computation templates -------------------------------------
# A metric concept carries a single dialect-AGNOSTIC ``computation_template``:
# a scalar expression whose named ``{variable}`` placeholders are bound to
# physical columns by the resolves_to edges in METRIC_ELEVATIONS below. The
# SolderEngine substitutes each {variable} with its real ``table.column`` and
# transpiles the result per dialect — so the metric is DEFINED ONCE here and
# every caller gets identical SQL. Templates are faithful derivations of the
# SME-APPROVED ground-truth snippets (the snippets' own ``WHEN ... > 0`` guards
# become NULLIF; the per-row on-time CASE becomes an AVG on-time rate); we never
# author free SQL. concept_name -> computation_template.
METRIC_TEMPLATES = {
    # OEE Operational — availability/performance proxy: actual vs scheduled run
    # hours (from operations_oeeoperational_*.sql performance_ratio column).
    "OEEOperational":
        "SUM({act_run_hrs}) / NULLIF(SUM({run_hrs}), 0)",
    # OEE Strategic — cost-per-actual-run-hour (from operations_oeestrategic_*.sql
    # cost_per_actual_hr column): (labour + burden actual cost) / actual run hrs.
    "OEEStrategic":
        "SUM({act_atl_lab_cost} + {act_atl_bur_cost}) / NULLIF(SUM({act_run_hrs}), 0)",
    # Delivery on-time rate — the share of receipts that landed on/before the PO
    # required date (derived from operations_deliveryperformanceops_*.sql on-time
    # CASE). ONE approved on-time definition, reused across the three delivery
    # perspectives (operational / supplier / finance) — the perspective lives on
    # the resolves_to edge, never in the formula.
    "DeliveryPerformanceOps":
        "AVG(CASE WHEN {receipt_date} IS NOT NULL AND {receipt_date} <= {required_date} "
        "THEN 1.0 WHEN {receipt_date} IS NOT NULL AND {receipt_date} > {required_date} "
        "THEN 0.0 ELSE NULL END)",
    "DeliveryPerformanceSupplier":
        "AVG(CASE WHEN {receipt_date} IS NOT NULL AND {receipt_date} <= {required_date} "
        "THEN 1.0 WHEN {receipt_date} IS NOT NULL AND {receipt_date} > {required_date} "
        "THEN 0.0 ELSE NULL END)",
    "DeliveryPerformanceFinance":
        "AVG(CASE WHEN {receipt_date} IS NOT NULL AND {receipt_date} <= {required_date} "
        "THEN 1.0 WHEN {receipt_date} IS NOT NULL AND {receipt_date} > {required_date} "
        "THEN 0.0 ELSE NULL END)",
}

# (perspective_name, concept_name, table, column, variable_name, weight, hint)
# Each row binds ONE template {variable} to a physical column via a resolves_to
# edge that additionally carries ``variable_name``. Every variable referenced by
# a METRIC_TEMPLATES entry must have exactly one binding here (the engine fails
# closed otherwise). Perspectives are EXISTING ones only (no new perspective):
# delivery is shown through Manufacturing / Quality / Finance lenses over the
# same two columns; OEE through Manufacturing (shift) and Finance (capital).
METRIC_ELEVATIONS = [
    # --- OEE Operational (Manufacturing lens) ---
    ("Manufacturing", "OEEOperational", "operation", "act_run_hrs", "act_run_hrs", 3,
     "Actual run hours logged against the operation (numerator)"),
    ("Manufacturing", "OEEOperational", "operation", "run_hrs", "run_hrs", 3,
     "Scheduled/standard run hours for the operation (denominator)"),
    # --- OEE Strategic (Finance lens) ---
    ("Finance", "OEEStrategic", "operation", "act_atl_lab_cost", "act_atl_lab_cost", 3,
     "Actual at-the-operation labour cost (numerator term)"),
    ("Finance", "OEEStrategic", "operation", "act_atl_bur_cost", "act_atl_bur_cost", 3,
     "Actual at-the-operation burden/overhead cost (numerator term)"),
    ("Finance", "OEEStrategic", "operation", "act_run_hrs", "act_run_hrs", 3,
     "Actual run hours (denominator — cost per actual hour)"),
    # --- Delivery on-time, three perspective lenses over the same two columns ---
    ("Manufacturing", "DeliveryPerformanceOps", "receiving", "receipt_date", "receipt_date", 3,
     "Actual goods receipt date (on-time comparison, operational lens)"),
    ("Manufacturing", "DeliveryPerformanceOps", "purchase_order", "required_date", "required_date", 3,
     "PO required-by date (on-time comparison, operational lens)"),
    ("Quality", "DeliveryPerformanceSupplier", "receiving", "receipt_date", "receipt_date", 3,
     "Actual goods receipt date (supplier scorecard lens)"),
    ("Quality", "DeliveryPerformanceSupplier", "purchase_order", "required_date", "required_date", 3,
     "PO required-by date (supplier scorecard lens)"),
    ("Finance", "DeliveryPerformanceFinance", "receiving", "receipt_date", "receipt_date", 3,
     "Actual goods receipt date (cost/penalty lens)"),
    ("Finance", "DeliveryPerformanceFinance", "purchase_order", "required_date", "required_date", 3,
     "PO required-by date (cost/penalty lens)"),
]


# Perspectives this manifest references that are NOT part of the canonical
# schema_sqlite.sql seed (which ships the 13 ERP-module perspectives). A fresh
# bootstrap database therefore lacks them, and _perspective_id would fail
# closed. Seed them here (INSERT OR IGNORE, keyed by name) so this manifest
# stays self-contained — mirroring the additive-guard philosophy above.
NEW_PERSPECTIVES = {
    "Finance": (
        "Financial lens over operations: cost, penalty exposure, and capital effectiveness.",
        "Controller, Finance Analyst",
        "Cost variances, delivery penalty exposure, capital utilization (OEE strategic)",
    ),
}


def _concept_id(cur, name: str) -> int:
    row = cur.execute(
        "SELECT concept_id FROM schema_concepts WHERE concept_name = ?", (name,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"ERROR: concept '{name}' not found and not in NEW_CONCEPTS")
    return row[0]


def _perspective_id(cur, name: str) -> int:
    row = cur.execute(
        "SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?",
        (name,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"ERROR: perspective '{name}' not found")
    return row[0]


def main() -> int:
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"ERROR: manufacturing.db not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        # Additive guard: older databases predate ``component_index``. Add it in
        # place so this manifest stays self-contained (idempotent).
        scf_cols = {row[1] for row in cur.execute("PRAGMA table_info(schema_concept_fields)")}
        if "component_index" not in scf_cols:
            cur.execute(
                "ALTER TABLE schema_concept_fields "
                "ADD COLUMN component_index INTEGER NOT NULL DEFAULT 1"
            )
        # M4 additive guard: a metric resolves_to edge names which template
        # {variable} the bound column fills. Categorical/measure elevations leave
        # it NULL.
        if "variable_name" not in scf_cols:
            cur.execute(
                "ALTER TABLE schema_concept_fields ADD COLUMN variable_name TEXT"
            )

        # concept_type was DEPRECATED & REMOVED from the semantic-layer schema
        # (metrics are duck-typed via computation_template). Still-live databases
        # may physically retain the legacy column, and the frozen graph artifacts
        # source their concept_type label from it. Write it ONLY when the column is
        # physically present — mirroring export_graph_metadata's has_type guard — so
        # this seed neither re-adds the purged column nor crashes ("no column named
        # concept_type") on a fresh database built from the new schema.
        _concept_cols = {row[1] for row in cur.execute("PRAGMA table_info(schema_concepts)")}
        _has_concept_type = "concept_type" in _concept_cols
        for name, (ctype, domain, desc) in NEW_CONCEPTS.items():
            if _has_concept_type:
                cur.execute(
                    "INSERT OR IGNORE INTO schema_concepts "
                    "(concept_name, concept_type, description, domain) VALUES (?,?,?,?)",
                    (name, ctype, desc, domain),
                )
            else:
                cur.execute(
                    "INSERT OR IGNORE INTO schema_concepts "
                    "(concept_name, description, domain) VALUES (?,?,?)",
                    (name, desc, domain),
                )
            # Repair: an earlier revision of this manifest inserted description /
            # domain in swapped column order. Re-set them from the authoritative
            # manifest values so existing rows are corrected too (INSERT OR IGNORE
            # leaves a pre-existing — possibly swapped — row untouched). Idempotent:
            # it always writes the same authored values.
            cur.execute(
                "UPDATE schema_concepts SET description = ?, domain = ? "
                "WHERE concept_name = ?",
                (desc, domain, name),
            )

        # M3 additive guard: older databases predate schema_concepts.synonyms /
        # tags. Add them in place so this manifest stays self-contained.
        concept_cols = {row[1] for row in cur.execute("PRAGMA table_info(schema_concepts)")}
        if "synonyms" not in concept_cols:
            cur.execute("ALTER TABLE schema_concepts ADD COLUMN synonyms TEXT")
        if "tags" not in concept_cols:
            cur.execute("ALTER TABLE schema_concepts ADD COLUMN tags TEXT")
        # M4 additive guard: a metric concept node carries its dialect-agnostic
        # computation_template. Non-metric concepts leave it NULL.
        if "computation_template" not in concept_cols:
            cur.execute("ALTER TABLE schema_concepts ADD COLUMN computation_template TEXT")

        # M3 MRP vocabulary — concept nodes carry synonyms + tags (canonical JSON
        # arrays) on top of concept_type / domain / description.
        for name, (ctype, domain, desc, synonyms, tags) in MRP_CONCEPTS.items():
            if _has_concept_type:
                cur.execute(
                    "INSERT OR IGNORE INTO schema_concepts "
                    "(concept_name, concept_type, description, domain, synonyms, tags) "
                    "VALUES (?,?,?,?,?,?)",
                    (name, ctype, desc, domain, json.dumps(synonyms), json.dumps(tags)),
                )
            else:
                cur.execute(
                    "INSERT OR IGNORE INTO schema_concepts "
                    "(concept_name, description, domain, synonyms, tags) "
                    "VALUES (?,?,?,?,?)",
                    (name, desc, domain, json.dumps(synonyms), json.dumps(tags)),
                )
            # Set the full M3 payload from the authoritative manifest values. This
            # is unconditional (not just a NULL backfill) so it both upgrades a
            # pre-M3 row and repairs the historical description/domain swap; it is
            # idempotent because it always writes the same authored values.
            cur.execute(
                "UPDATE schema_concepts SET description = ?, domain = ?, "
                "synonyms = ?, tags = ? WHERE concept_name = ?",
                (desc, domain, json.dumps(synonyms), json.dumps(tags), name),
            )

        # Self-seed manifest-referenced perspectives missing from the canonical
        # schema seed (fresh-bootstrap databases). Idempotent by name.
        for pname, (pdesc, prole, pfocus) in NEW_PERSPECTIVES.items():
            cur.execute(
                "INSERT OR IGNORE INTO schema_perspectives "
                "(perspective_name, description, stakeholder_role, priority_focus) "
                "VALUES (?,?,?,?)",
                (pname, pdesc, prole, pfocus),
            )
            if cur.rowcount:
                print(f"  perspective: {pname} (seeded — missing from schema seed)")

        for persp, concept, table, column, weight, hint in ELEVATIONS:
            pid = _perspective_id(cur, persp)
            cid = _concept_id(cur, concept)
            cur.execute(
                "INSERT OR IGNORE INTO schema_perspective_concepts "
                "(perspective_id, concept_id, relationship_type, priority_weight) "
                "VALUES (?,?, 'USES_DEFINITION', ?)",
                (pid, cid, weight),
            )
            cur.execute(
                "INSERT OR IGNORE INTO schema_concept_fields "
                "(table_name, field_name, concept_id, is_primary_meaning, context_hint) "
                "VALUES (?,?,?,1,?)",
                (table, column, cid, hint),
            )
            print(f"  elevation: {persp:24} {table}.{column} -> {concept}")

        # M4: stamp each showcase metric's dialect-agnostic computation_template
        # onto its (already-shipped) concept node. Unconditional UPDATE keyed by
        # name, so it is idempotent and never recreates the concept.
        for concept, template in METRIC_TEMPLATES.items():
            _concept_id(cur, concept)  # fail loudly if a metric concept is missing
            cur.execute(
                "UPDATE schema_concepts SET computation_template = ? "
                "WHERE concept_name = ?",
                (template, concept),
            )

        # M4: metric resolves_to edges. Same shape as ELEVATIONS but each row also
        # carries the template variable_name the bound column fills.
        for persp, concept, table, column, variable_name, weight, hint in METRIC_ELEVATIONS:
            pid = _perspective_id(cur, persp)
            cid = _concept_id(cur, concept)
            cur.execute(
                "INSERT OR IGNORE INTO schema_perspective_concepts "
                "(perspective_id, concept_id, relationship_type, priority_weight) "
                "VALUES (?,?, 'USES_DEFINITION', ?)",
                (pid, cid, weight),
            )
            cur.execute(
                "INSERT OR IGNORE INTO schema_concept_fields "
                "(table_name, field_name, concept_id, is_primary_meaning, "
                "context_hint, variable_name) VALUES (?,?,?,1,?,?)",
                (table, column, cid, hint, variable_name),
            )
            # variable_name is set on insert; repair it on a pre-existing row too
            # (idempotent — always writes the same authored value).
            cur.execute(
                "UPDATE schema_concept_fields SET variable_name = ? "
                "WHERE table_name = ? AND field_name = ? AND concept_id = ?",
                (variable_name, table, column, cid),
            )
            print(f"  metric var: {persp:20} {table}.{column} -> {concept} {{{variable_name}}}")

        # Number each field's definitions deterministically: per (table, field)
        # the primary meaning is 1 and each further definition increments. This
        # is the authoritative ``component_index`` the exporter carries into the
        # graph as ``field_component`` — authored in SQLite, never inferred at
        # runtime. Ordered by concept_id so re-running is idempotent (same DB =
        # same numbering). Requires SQLite >= 3.25 for window functions.
        cur.execute(
            """
            WITH numbered AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY table_name, field_name
                           ORDER BY concept_id
                       ) AS rn
                FROM schema_concept_fields
            )
            UPDATE schema_concept_fields
               SET component_index = (SELECT rn FROM numbered
                                       WHERE numbered.id = schema_concept_fields.id)
            """
        )

        conn.commit()
    finally:
        conn.close()

    print(f"seeded {len(NEW_CONCEPTS) + len(MRP_CONCEPTS)} concept(s), "
          f"{len(ELEVATIONS)} elevation(s), "
          f"{len(METRIC_TEMPLATES)} metric template(s) + {len(METRIC_ELEVATIONS)} "
          f"metric var binding(s) (idempotent). "
          f"Re-run export_graph_metadata.py to emit resolves_to edges.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
