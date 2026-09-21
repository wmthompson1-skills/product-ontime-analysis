-- SQLite Schema for Manufacturing Analytics
-- Generated from PostgreSQL schema
--
-- NOTE: The following 21 PoC tables were removed in the drop_poc_tables migration
-- (May 2026) because they were never populated with data:
--   corrective_actions, daily_deliveries, downtime_events, effectiveness_metrics,
--   equipment_metrics, equipment_reliability, failure_events, financial_impact,
--   industry_benchmarks, maintenance_targets, manufacturing_acronyms,
--   non_conformant_materials, product_defects, product_lines, production_lines,
--   production_quality, production_schedule, products, quality_costs,
--   quality_incidents, users
-- Run hf-space-inventory-sqlgen/migrations/drop_poc_tables.py to apply to
-- an existing database.

CREATE TABLE IF NOT EXISTS schema_edges (
    edge_id                INTEGER PRIMARY KEY,   -- unique FK edge id; INSERT OR IGNORE is idempotent
    from_table             TEXT,
    to_table               TEXT,
    relationship_type      TEXT,
    join_column            TEXT,
    weight                 INTEGER DEFAULT 1,
    created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
    join_column_description TEXT,
    natural_language_alias TEXT,
    few_shot_example       TEXT,
    context                TEXT
);

-- one row per logical relationship; makes INSERT OR IGNORE truly idempotent
CREATE UNIQUE INDEX IF NOT EXISTS ux_schema_edges_logical
    ON schema_edges(from_table, to_table, join_column);

CREATE TABLE IF NOT EXISTS schema_nodes (
    table_name TEXT NOT NULL UNIQUE,
    table_type TEXT,
    description text,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_nodes (table_name, table_type, description) VALUES
-- Core semantic layer tables (always present)
('EMPLOYEE',        'Table', 'Employee master records'),
-- Purchasing / WIP digital twin — master data
('part',            'Table', 'Part / item master — description, class, UOM, cost, revision, material spec, CAGE code'),
('suppliers',       'Table', 'Supplier master — name, category, certification level, payment terms, lead time'),
('shop_resource',   'Table', 'Shop work centers and outside-service buckets (machine, labor, service types)'),
('service',         'Table', 'Outside service definitions (anodize, heat treat, NDT, plating, painting)'),
-- Purchasing / WIP digital twin — transactional
('purchase_order',  'Table', 'Purchase order headers for material and outside-service buys'),
('po_line',         'Table', 'Purchase order line items (part, quantity, unit cost, line total)'),
('receiving',       'Table', 'Goods receipts against purchase orders — quantity ordered vs received, inspection status'),
('payables',  'Table', 'Accounts-payable headers (acts as the invoice header) linked to purchase orders — three-way match and payment status'),
('certification',   'Table', 'Supplier certification records (CoC, FAI, PPAP, 8130-3, Material Test Report)'),
('work_order',      'Table', 'Work order master — part, quantity, status, routing template, accumulated actual costs'),
('operation',       'Table', 'Work order routing steps — sequence, resource, estimated vs actual hours and costs'),
('material_issue',  'Table', 'Raw material issues from stock to WIP work orders (quantity, unit cost, total cost)'),
('labor_ticket',    'Table', 'Labor time postings against work order operations (clock-in/out, hours, cost)'),
('requirement',     'Table', 'Resource requirements (labor / material / burden) per routing operation; component_type PART = as-designed routing standard (engineering, per-unit) vs WORK_ORDER = as-built actuals (manufacturing, batch)');

-- ─────────────────────────────────────────────────────────────────────────────
-- FK EDGE REGISTRY  (used by graph_sync to build containment / join graph)
-- ─────────────────────────────────────────────────────────────────────────────
INSERT OR IGNORE INTO schema_edges
    (edge_id, from_table, to_table, relationship_type, join_column, weight, natural_language_alias) VALUES
-- Purchasing chain
(1,  'po_line',       'purchase_order', 'FOREIGN_KEY', 'po_id',        1, 'PO lines belong to a purchase order'),
(2,  'po_line',       'part',           'FOREIGN_KEY', 'part_id',       1, 'PO line references a part'),
(3,  'receiving',     'purchase_order', 'FOREIGN_KEY', 'po_id',         1, 'Receipt closes against a purchase order'),
(4,  'receiving',     'suppliers',      'FOREIGN_KEY', 'supplier_id',   1, 'Receipt records the delivering supplier'),
(5,  'receiving',     'part',           'FOREIGN_KEY', 'part_id',       1, 'Receipt records the received part'),
(6,  'payables','purchase_order', 'FOREIGN_KEY', 'po_id',         1, 'Invoice matches to a purchase order'),
(7,  'payables','suppliers',      'FOREIGN_KEY', 'supplier_id',   1, 'Invoice issued by a supplier'),
(8,  'purchase_order','suppliers',      'FOREIGN_KEY', 'supplier_id',   1, 'Purchase order placed with a supplier'),
(9,  'certification', 'receiving',      'FOREIGN_KEY', 'receipt_id',    1, 'Cert attached to a receiving line'),
(10, 'certification', 'part',           'FOREIGN_KEY', 'part_id',       1, 'Cert covers a specific part'),
-- WIP chain
(11, 'work_order',    'part',           'FOREIGN_KEY', 'part_id',       1, 'Work order manufactures a part'),
(12, 'operation',     'work_order',     'FOREIGN_KEY', 'wo_id',         1, 'Routing step belongs to a work order'),
(13, 'operation',     'shop_resource',  'FOREIGN_KEY', 'resource_id',   1, 'Routing step runs on a shop resource'),
(14, 'material_issue','work_order',     'FOREIGN_KEY', 'wo_id',         1, 'Material issued to a work order'),
(15, 'material_issue','part',           'FOREIGN_KEY', 'part_id',       1, 'Material issue consumes a part'),
(16, 'labor_ticket',  'work_order',     'FOREIGN_KEY', 'wo_id',         1, 'Labor ticket posted against a work order'),
-- Outside service cross-link
(17, 'purchase_order','work_order',     'FOREIGN_KEY', 'wo_id',         1, 'Outside-service PO tied to a work order'),
(18, 'operation',     'service',        'FOREIGN_KEY', 'service_id',    1, 'Outside-service op uses a service definition'),
(19, 'operation',     'suppliers',      'FOREIGN_KEY', 'vendor_id',     1, 'Outside-service op dispatched to a supplier'),
-- Requirement chain (engineering vs manufacturing differentiator on component_type)
(20, 'requirement',   'operation',      'FOREIGN_KEY', 'operation_rowid', 1, 'Requirement consumed at a routing operation (work-order level)'),
(21, 'requirement',   'part',           'FOREIGN_KEY', 'material_part_id', 1, 'Material requirement consumes a part'),
(22, 'requirement',   'part',           'FOREIGN_KEY', 'component_id',   1, 'Design-level requirement belongs to a part component (engineering)'),
(23, 'requirement',   'work_order',     'FOREIGN_KEY', 'component_id',   1, 'Work-order-level requirement belongs to a work order component (manufacturing)');

-- ─────────────────────────────────────────────────────────────────────────────
-- ERP TABLE DDL  (aerospace manufacturing — Purchasing / WIP digital twin)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS part (
    part_id          TEXT PRIMARY KEY,          -- e.g. P-12345
    part_description TEXT NOT NULL,
    part_class       TEXT NOT NULL CHECK(part_class IN ('RAW','HARDWARE','MAKE','BUY','OUTSIDE_SERVICE')),
    unit_of_measure  TEXT NOT NULL DEFAULT 'EA',  -- EA / LB / FT / IN
    unit_cost        REAL DEFAULT 0.0,
    lead_time_days   INTEGER DEFAULT 0,
    reorder_point    REAL DEFAULT 0.0,
    on_hand_qty      REAL DEFAULT 0.0,
    revision         TEXT DEFAULT 'A',           -- drawing revision level
    cage_code        TEXT,                        -- 5-char CAGE code (aerospace supplier ID)
    drawing_number   TEXT,
    material_spec    TEXT,                        -- e.g. AMS 4928 / 6061-T6 / 304 SS
    planner_code     TEXT DEFAULT 'ENGINEERING',  -- owning material planner (item-master native)
    buyer_code       TEXT,                        -- owning buyer (EMPLOYEE.buyer_code); NULL = in-house part
    safety_stock     REAL DEFAULT 1,              -- SME rule: exactly 1 for planning parts (never 0, never computed)
    active           INTEGER DEFAULT 1,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS EMPLOYEE (
    employee_id      TEXT PRIMARY KEY,            -- e.g. EMP-001
    employee_name    TEXT NOT NULL,
    job_title        TEXT NOT NULL,               -- CNC Machinist / Welder / Assembler / Inspector / Buyer
    department       TEXT NOT NULL,               -- Machining / Welding / Assembly / Quality / Purchasing
    hourly_rate      REAL NOT NULL DEFAULT 0.0,   -- plant workers $40.00-$45.00
    buyer_code       TEXT UNIQUE,                 -- BUYER-1..BUYER-10; NULL for non-buyers
    home_resource_id TEXT,                        -- shop_resource the worker mans (NULL for buyers)
    hire_date        DATE,
    active           INTEGER DEFAULT 1,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id        TEXT PRIMARY KEY,              -- e.g. S-001
    supplier_name      TEXT NOT NULL,
    contact_email      TEXT,
    phone              TEXT,
    address            TEXT,
    performance_rating REAL DEFAULT 0.0,
    certification_level TEXT,                    -- AS9100D / ISO9001 / NADCAP / etc.
    category           TEXT,                     -- material / outside_service / hardware
    payment_terms      TEXT DEFAULT 'Net30',
    lead_time_days     INTEGER DEFAULT 14,
    outside_service    INTEGER DEFAULT 0,        -- 1 = primarily an outside-service vendor
    active             INTEGER DEFAULT 1,
    created_date       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS purchase_order (
    po_id         TEXT PRIMARY KEY,              -- e.g. PO-240001
    supplier_id   TEXT NOT NULL,                 -- FK → suppliers
    po_type       TEXT NOT NULL CHECK(po_type IN ('material','outside_service')),
    po_date       DATE NOT NULL,
    required_date DATE,
    status        TEXT NOT NULL,                 -- Open / Partial / Closed / Cancelled
    total_cost    REAL DEFAULT 0.0,
    wo_id         TEXT,                          -- FK → work_order (outside_service POs)
    service_id    TEXT,                          -- FK → service (outside_service POs)
    buyer_id      TEXT DEFAULT 'BUYER-1',
    site_id       TEXT DEFAULT 'SITE-1',
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS po_line (
    line_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id            TEXT NOT NULL,              -- FK → purchase_order
    part_id          TEXT NOT NULL,              -- FK → part
    part_description TEXT NOT NULL,
    quantity         REAL NOT NULL,
    unit_cost        REAL NOT NULL,
    line_total       REAL NOT NULL,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS receiving (
    receipt_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id             TEXT NOT NULL,             -- FK → purchase_order
    supplier_id       TEXT NOT NULL,             -- FK → suppliers
    part_id           TEXT NOT NULL,             -- FK → part
    quantity_ordered  REAL NOT NULL,
    quantity_received REAL NOT NULL,
    receipt_date      DATE NOT NULL,             -- date the receipt transaction was posted
    received_date     DATE,                       -- date goods physically arrived (dock date);
                                                  -- distinct noun mirroring the real source
                                                  -- R.RECEIVED_DATE — the temporal filter column
    inspection_status TEXT NOT NULL DEFAULT 'Pending',  -- Pending / Passed / Failed / Waived
    cert_required     INTEGER DEFAULT 0,         -- 1 = CoC / FAI / 8130-3 required
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payables (
    invoice_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id                  TEXT NOT NULL,        -- FK → purchase_order
    supplier_id            TEXT NOT NULL,        -- FK → suppliers
    invoice_number         TEXT NOT NULL UNIQUE,
    invoice_date           DATE NOT NULL,
    due_date               DATE NOT NULL,
    amount_dollars         REAL NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'Open',  -- Open / Paid / Disputed / Void
    payment_date           DATE,
    three_way_match_status TEXT DEFAULT 'Pending',        -- Pending / Matched / Exception
    created_at             DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS certification (
    cert_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id  INTEGER,                         -- FK → receiving (NULL = stand-alone cert)
    part_id     TEXT NOT NULL,                   -- FK → part
    supplier_id TEXT NOT NULL,                   -- FK → suppliers
    cert_type   TEXT NOT NULL,                   -- CoC / FAI / PPAP / 8130-3 / Material_Test_Report
    issued_date DATE NOT NULL,
    expiry_date DATE,
    status      TEXT DEFAULT 'Active' CHECK(status IN ('Active','Expired','Revoked')),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shop_resource (
    resource_id     TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    resource_type   TEXT NOT NULL CHECK(resource_type IN ('M','L','S')),
    -- M=Machine  L=Labor  S=Outside Service bucket
    run_cost_per_hr REAL DEFAULT 0.0,
    bur_per_hr_run  REAL DEFAULT 0.0,
    active          INTEGER DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS service (
    service_id     TEXT PRIMARY KEY,
    description    TEXT NOT NULL,
    default_vendor TEXT,                         -- FK → suppliers
    base_charge    REAL DEFAULT 0.0,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS work_order (
    wo_id            TEXT PRIMARY KEY,           -- e.g. WO-240001
    workorder_type   TEXT NOT NULL CHECK(workorder_type IN ('M','W')),
    -- M=Manufacturing  W=Rework
    part_id          TEXT NOT NULL,              -- FK → part
    part_description TEXT NOT NULL,
    quantity         REAL NOT NULL,
    status           TEXT NOT NULL,              -- unreleased / firmed / released / closed (firmed = first lots tested or recently engineered parts; set by migrations/relabel_work_order_status.py)
    open_date        DATE,
    close_date       DATE,
    required_date    DATE,
    routing_template TEXT,                       -- AIRFRAME / FASTENER / MACHINED / WELDMENT
    act_lab_cost     REAL DEFAULT 0.0,
    act_bur_cost     REAL DEFAULT 0.0,
    act_ser_cost     REAL DEFAULT 0.0,
    act_mat_cost     REAL DEFAULT 0.0,
    desired_rls_date  DATETIME,                  -- planner release anchor; on/before the first operation start (mirrors dbo.WORK_ORDER.DESIRED_RLS_DATE), set by migrations/backfill_operation_schedule.py
    sched_start_date  DATETIME,                  -- scheduled start = earliest operation start (derived from routing), set by migrations/backfill_operation_schedule.py
    sched_finish_date DATETIME,                  -- scheduled finish = latest operation finish (derived from routing), set by migrations/backfill_operation_schedule.py
    outside_service  INTEGER DEFAULT 0,          -- 1 if any op routes outside
    service_date     DATE,                       -- outside-service due date (display-only), set by migrations/backfill_mrp_demand_supply.py
    vendor_id        TEXT,                       -- outside-service vendor → suppliers (display-only), set by migrations/backfill_mrp_demand_supply.py
    site_id          TEXT DEFAULT 'SITE-1',
    demand_order_line_id INTEGER REFERENCES customer_order_line (order_line_id),
                                                 -- demand-source linkage (Release Order → MO); NULL = unlinked MO
                                                 -- (ships from stock / forecast). Set by
                                                 -- migrations/add_demand_linkage_and_forecast.py.
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS operation (
    rowid_pk           INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id              TEXT NOT NULL,            -- FK → work_order
    workorder_type     TEXT NOT NULL,
    sequence_no        INTEGER NOT NULL,         -- routing step number; gapped (e.g. 20, 80, 220), set by migrations/regap_and_seed_requirements.py
    resource_id        TEXT NOT NULL,            -- FK → shop_resource
    service_id         TEXT,                     -- FK → service (outside ops only)
    vendor_id          TEXT,                     -- FK → suppliers (outside ops only)
    run_type           TEXT DEFAULT 'HR',        -- HR=per hour  PC=per piece
    setup_hrs          REAL DEFAULT 0.0,
    run_hrs            REAL DEFAULT 0.0,
    act_setup_hrs      REAL DEFAULT 0.0,
    act_run_hrs        REAL DEFAULT 0.0,
    est_atl_lab_cost   REAL DEFAULT 0.0,
    est_atl_bur_cost   REAL DEFAULT 0.0,
    est_atl_ser_cost   REAL DEFAULT 0.0,
    act_atl_lab_cost   REAL DEFAULT 0.0,
    act_atl_bur_cost   REAL DEFAULT 0.0,
    act_atl_ser_cost   REAL DEFAULT 0.0,
    status             TEXT DEFAULT 'Q',         -- Q=Queued  S=Started  C=Complete  (job progress is read from status + close_date, NOT sequence_no)
    sched_start_date   DATETIME,
    sched_finish_date  DATETIME,
    service_begin_date DATETIME,
    close_date         DATETIME,                  -- set when status='C'; ordered along the routing (see migrations/backfill_operation_progress.py)
    last_disp_date     DATETIME,                 -- last outside-service dispatch date
    last_recv_date     DATETIME,                 -- last outside-service receipt date
    operation_type_id  TEXT,                     -- FK → operation_type (the KIND of op: CNC, PAINT, NDT, …)
    UNIQUE(wo_id, sequence_no)
);

-- Operation-type reference lookup: the closed set of operation "kinds"
-- (CNC, Paint, NDT, Inspect, Assembly, …) an operation row can be classified
-- as. operation.operation_type_id is a foreign key into this table. resource_id
-- names a representative default work center; active=0 retires a type without
-- deleting it. Seeded below so a fresh database carries the full taxonomy; the
-- committed manufacturing.db has its existing operation rows stamped by
-- migrations/add_operation_type.py. Mirrors the private SQL Server
-- OPERATION_TYPE (ID, DESCRIPTION, RESOURCE_ID) model.
CREATE TABLE IF NOT EXISTS operation_type (
    operation_type_id TEXT    NOT NULL PRIMARY KEY,
    description       TEXT    NOT NULL,
    category          TEXT    NOT NULL DEFAULT 'Other',
    resource_id       TEXT,
    active            INTEGER NOT NULL DEFAULT 1
);

INSERT INTO operation_type (operation_type_id, description, category, resource_id, active) VALUES
    ('CNC',      'CNC Milling',                   'Machining',         'CNC-MILL-1',   1),
    ('TURN',     'CNC Turning / Lathe',           'Machining',         'LATHE-1',      1),
    ('WJET',     'Waterjet Cutting',              'Machining',         'MC-006',       1),
    ('DEBURR',   'Deburr / Finishing',            'Finishing',         'DRILL-PRESS',  1),
    ('WELD',     'Welding',                       'Fabrication',       'WELD-A',       1),
    ('ASSY',     'Assembly',                      'Assembly',          'ASSEM-LINE-1', 1),
    ('INSPECT',  'In-Process Inspection / CMM',   'Quality',           'INSPECT-CMM',  1),
    ('FINSP',    'Final Inspection',              'Quality',           'LB-004',       1),
    ('NDT',      'Non-Destructive Test',          'Quality',           'SV-003',       1),
    ('REVIEW',   'Engineering / Planning Review', 'Engineering',       NULL,           1),
    ('ANOD',     'Anodize',                       'Outside Finishing', 'SV-001',       1),
    ('CHEM',     'Chemical Film',                 'Outside Finishing', 'SV-004',       1),
    ('HTRT',     'Heat Treat',                    'Outside Process',   'SV-002',       1),
    ('PLATE',    'Plating',                       'Outside Finishing', 'OUTSIDE',      1),
    ('PAINT',    'Paint / Prime',                 'Outside Finishing', 'SV-005',       1),
    ('PARTMARK', 'Part Marking / Etch',           'Finishing',         'DRILL-PRESS',  1);

CREATE TABLE IF NOT EXISTS material_issue (
    issue_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id            TEXT NOT NULL,              -- FK → work_order
    part_id          TEXT NOT NULL,              -- FK → part
    part_description TEXT NOT NULL,
    quantity         REAL NOT NULL,
    unit_cost        REAL NOT NULL,
    total_cost       REAL NOT NULL,
    issue_date       DATE NOT NULL,
    issued_by        TEXT NOT NULL,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS labor_ticket (
    ticket_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id       TEXT NOT NULL,                   -- FK → work_order
    sequence_no INTEGER NOT NULL,                -- matches operation.sequence_no
    employee_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,                   -- FK → shop_resource
    clock_in    DATETIME NOT NULL,
    clock_out   DATETIME NOT NULL,
    total_hours REAL NOT NULL,
    labor_cost  REAL NOT NULL,                   -- total_hours × run_cost_per_hr
    burden_cost REAL NOT NULL,                   -- total_hours × bur_per_hr_run
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Requirement: resource demand (labor / material / burden) per routing operation.
-- component_type is the ENGINEERING vs MANUFACTURING differentiator:
--   PART       -> design level (as-designed routing standard; engineering, per-unit basis)
--   WORK_ORDER -> work-order level (as-built actuals; manufacturing, batch basis)
-- The committed manufacturing.db has work-order-level MATERIAL rows seeded by
-- migrations/regap_and_seed_requirements.py (each tied to a concrete operation via
-- operation_rowid), so operations can be queried for the material they require.
CREATE TABLE IF NOT EXISTS requirement (
    requirement_id    TEXT PRIMARY KEY,                  -- e.g. REQ-000001
    component_type    TEXT NOT NULL CHECK(component_type IN ('PART','WORK_ORDER')),
    component_id      TEXT NOT NULL,                     -- part_id (design) or wo_id (work order)
    requirement_level TEXT NOT NULL CHECK(requirement_level IN ('DESIGN','WORK_ORDER')),
    requirement_type  TEXT NOT NULL CHECK(requirement_type IN ('LABOR','MATERIAL','BURDEN')),
    operation_seq     INTEGER NOT NULL,                  -- routing sequence step (gapped, e.g. 20, 80, 220); design-level operation handle
    operation_rowid   INTEGER,                           -- FK -> operation (work-order-level concrete op; NULL at design level)
    material_part_id  TEXT,                              -- FK -> part when requirement_type='MATERIAL'
    std_qty           REAL DEFAULT 0.0,                  -- per-unit standard quantity / hours (as-designed)
    actual_qty        REAL DEFAULT 0.0,                  -- actual consumed (as-built, work-order)
    unit_cost         REAL DEFAULT 0.0,
    extended_cost     REAL DEFAULT 0.0,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY ("operation_rowid") REFERENCES "operation" ("rowid_pk"),
    FOREIGN KEY ("material_part_id") REFERENCES "part" ("part_id")
);

-- Ground truth query → table usage index (also created at runtime by solder_engine.py)
CREATE TABLE IF NOT EXISTS ground_truth_table_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query_file      TEXT    NOT NULL,
    category_id     TEXT    NOT NULL,
    query_name      TEXT    NOT NULL,
    table_name      TEXT    NOT NULL,
    reference_count INTEGER NOT NULL DEFAULT 1,
    select_count    INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_gt_usage
    ON ground_truth_table_usage (category_id, query_name, table_name);

-- =============================================================================
-- SEMANTIC LAYER: Perspective & Intent Graph Constructs
-- Based on treating perspective and intent as first-class graph constructs
-- =============================================================================

-- Schema Concepts: Multiple interpretations of ambiguous fields
CREATE TABLE IF NOT EXISTS schema_concepts (
    concept_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_name TEXT NOT NULL UNIQUE,
    -- concept_type was DEPRECATED & REMOVED from the semantic layer. A concept
    -- is a metric STRICTLY by DUCK TYPING: computation_template IS NOT NULL AND
    -- computation_template <> ''. (The graph layer's sql_graph_nodes keeps its
    -- own type label for structural rendering — that is separate and untouched.)
    description TEXT,
    domain TEXT,  -- 'quality', 'finance', 'operations', 'compliance', 'customer'
    synonyms TEXT,  -- M3: canonical JSON array of synonym strings (e.g. '["ROP","reorder level"]'); NULL/absent => []
    tags TEXT,      -- M3: canonical JSON array of curated filter tags (e.g. '["mrp","inventory"]'); NULL/absent => []
    parent_concept_id INTEGER,  -- for REFINES relationship
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    computation_template TEXT,  -- M4: dialect-agnostic metric template with {variable} placeholders; its presence is what makes a concept a metric (duck typing). Also added by seed_elevations.py via guarded ALTER.
    FOREIGN KEY (parent_concept_id) REFERENCES schema_concepts(concept_id)
);

-- Links fields to concepts (CAN_MEAN relationship)
CREATE TABLE IF NOT EXISTS schema_concept_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    field_name TEXT NOT NULL,
    concept_id INTEGER NOT NULL,
    is_primary_meaning INTEGER DEFAULT 0,  -- 1 = default interpretation
    context_hint TEXT,  -- when this meaning applies
    component_index INTEGER NOT NULL DEFAULT 1,  -- field-definition number (1 = primary; 2,3.. = further meanings)
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (concept_id) REFERENCES schema_concepts(concept_id),
    UNIQUE(table_name, field_name, concept_id)
);

-- Seed data: Manufacturing domain concepts
-- NOTE: concept_type was DEPRECATED & REMOVED. A concept becomes a metric only
-- by carrying a computation_template (duck typing), which seed_elevations.py
-- attaches to the showcase metrics — never seeded as a static type label here.
INSERT INTO schema_concepts (concept_name, description, domain) VALUES
-- Quality domain concepts
('DefectSeverityQuality', 'Defect severity from quality control perspective - focuses on product conformance', 'quality'),
('DefectSeverityCost', 'Defect severity from cost impact perspective - focuses on financial exposure', 'finance'),
('DefectSeverityCustomer', 'Defect severity from customer visibility perspective - focuses on brand risk', 'customer'),

-- Status concepts (multi-meaning)
('OrderLifecycleState', 'Order status representing lifecycle stage in fulfillment', 'operations'),
('OrderAccountingState', 'Order status from revenue recognition perspective', 'finance'),
('OrderCustomerState', 'Order status as visible to customer', 'customer'),

-- Delivery concepts
('DeliveryPerformanceOps', 'Delivery metrics for operational planning', 'operations'),
('DeliveryPerformanceSupplier', 'Delivery metrics for supplier scorecard', 'quality'),
('DeliveryPerformanceFinance', 'Delivery metrics for cost/penalty calculation', 'finance'),

-- Equipment concepts
('EquipmentStateProduction', 'Equipment status for production scheduling', 'operations'),
('EquipmentStateMaintenance', 'Equipment status for maintenance planning', 'operations'),
('EquipmentStateCompliance', 'Equipment status for regulatory compliance', 'compliance'),

-- Failure concepts
('FailureSeverityProduction', 'Failure severity based on production impact', 'operations'),
('FailureSeveritySafety', 'Failure severity based on safety implications', 'compliance'),
('FailureSeverityCost', 'Failure severity based on repair/replacement cost', 'finance'),

-- NCM concepts
('NCMDispositionQuality', 'NCM disposition from quality standpoint', 'quality'),
('NCMDispositionFinance', 'NCM disposition from cost recovery standpoint', 'finance'),

-- OEE concepts
('OEEOperational', 'OEE for shift/line performance tracking', 'operations'),
('OEEStrategic', 'OEE for capital investment decisions', 'finance'),

-- Receivables concepts
('ARCashReceiptAmount', 'Cash receipts applied against AR invoices, read from receivable_payment.amount', 'finance');

-- Seed data: Link ambiguous fields to concepts
-- NOTE: All concept_field rows referencing empty PoC tables were removed in
-- the drop_poc_tables migration. The tables they referenced (product_defects,
-- failure_events, equipment_metrics, daily_deliveries) no longer exist.
-- Add rows here when new populated tables with ambiguous fields are onboarded.

-- Schema Perspectives: Organizational viewpoints that constrain valid meanings
CREATE TABLE IF NOT EXISTS schema_perspectives (
    perspective_id INTEGER PRIMARY KEY AUTOINCREMENT,
    perspective_name TEXT NOT NULL UNIQUE,
    description TEXT,
    stakeholder_role TEXT,  -- typical role using this perspective
    priority_focus TEXT,    -- what this perspective prioritizes
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Junction table: Which concepts each perspective uses or suppresses
CREATE TABLE IF NOT EXISTS schema_perspective_concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    perspective_id INTEGER NOT NULL,
    concept_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL DEFAULT 'USES_DEFINITION',  -- 'USES_DEFINITION', 'SUPPRESSES', 'EMPHASIZES'
    priority_weight INTEGER DEFAULT 1,  -- higher = more important to this perspective
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (perspective_id) REFERENCES schema_perspectives(perspective_id),
    FOREIGN KEY (concept_id) REFERENCES schema_concepts(concept_id),
    UNIQUE(perspective_id, concept_id)
);

-- Entity categories: top-level domain groupings for schema tables / graph vertices.
-- These map to the 11 ArangoDB vertex category labels and drive the pill bar in the Define Relationship UI.
CREATE TABLE IF NOT EXISTS schema_entity_categories (
    category_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    category_name TEXT NOT NULL UNIQUE,   -- e.g. Customer_Order, Manufacturing
    display_order INTEGER DEFAULT 99,
    description   TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO schema_entity_categories (category_name, display_order, description) VALUES
('Customer_Order',         1,  'Customer orders, fulfilment, and delivery'),
('Demand_Forecast',        2,  'Demand planning and forecast accuracy'),
('Engineering',            3,  'Engineering change orders and BOM management'),
('General_Ledger',         4,  'GL accounts, cost centres, and financial postings'),
('Inventory_Transactions', 5,  'Stock movements, receipts, and adjustments'),
('Manufacturing',          6,  'Production orders, routing, and WIP'),
('Parts',                  7,  'Part master, revisions, and classifications'),
('Payables',               8,  'Supplier invoices and payments'),
('Receivables',            9,  'Customer invoices and collections'),
('Visual_Admin',           10, 'UI configuration and admin metadata'),
('Work_Orders',            11, 'Maintenance and shop-floor work orders');

-- Seed data: 7 Organizational Perspectives
-- IDs are explicit to prevent auto-increment from creating duplicates on re-seed.
-- Perspective taxonomy is aligned with ERP module boundaries:
--   Accounting sub-ledgers: Accounts_Payable, Accounts_Receivable, General_Ledger
--   ERP modules: Quality, Work_Orders, Manufacturing, Inventory
INSERT INTO schema_perspectives (perspective_id, perspective_name, description, stakeholder_role, priority_focus) VALUES
(1,  'Quality',               'Product conformance, defect prevention, and continuous improvement',               'Quality Engineer, QA Manager',        'Defect rates, NCM resolution, process capability'),
(2,  'Payables',             'Supplier invoices, purchase orders, vendor receipts, and payables aging.',           'AP Manager, Purchasing Manager',      'Invoice matching, payables aging, PO variance'),
(3,  'Work_Orders',          'Routing of resources in sequence on a work order (SEQUENCE_NO, RESOURCE_ID).',       'Production Planner, Shop Supervisor', 'Routing efficiency, operation sequence, outside-service cycle time'),
(4,  'General_Ledger',       'RM, WIP, FG, and COGS postings through the manufacturing cost flow.',               'Controller, Cost Accountant',         'Inventory valuation, COGS, variance analysis'),
(5,  'Receivables',          'Customer orders, sales billing, delivery commitments, and receivables exposure.',    'AR Manager, Sales Manager',           'Order fill rate, invoice aging, on-time delivery'),
(6,  'CRM',                  'Customer relationship management: contacts, accounts, opportunities, and activity.', 'Sales Rep, Account Manager',          'Pipeline, customer lifetime value, contact coverage'),
(7,  'Manufacturing',        'Production execution, schedule adherence, equipment effectiveness, and WIP.',        'Production Manager, Plant Supervisor','OEE, schedule variance, WIP turns, cycle time, downtime'),
(8,  'Inventory_Transactions','Material movements, stock receipts, material issues to WIP, and on-hand accuracy.','Materials Manager, Warehouse Supervisor','Stock accuracy, receipt qty vs ordered, material cost postings'),
(9,  'Customer_Order',       'Order fulfillment, delivery commitments, and order-lifecycle tracking.',             'Sales Manager, Customer Success',     'On-time delivery, fill rate, order-to-ship cycle time'),
(10, 'Demand_Forecast',      'Demand planning, forecast accuracy, and inventory replenishment signals.',           'Supply Chain Planner, Demand Manager','Forecast error (MAPE), bias, planning horizon coverage'),
(11, 'Engineering',          'Engineering change orders, BOM management, and part revisions.',                     'Design Engineer, Mfg Engineer',       'ECO cycle time, BOM accuracy, revision control'),
(12, 'Parts',                'Part master, revisions, classifications, and material specifications.',              'Materials Engineer, Config Manager',  'Part count, revision status, obsolescence rate'),
(13, 'Visual_Admin',         'UI configuration, admin metadata, and workspace layout settings.',                   'System Administrator',                'Config completeness, admin access, UI state');

-- Seed data: Perspective-Concept relationships (USES_DEFINITION)
-- Quality perspective uses quality-focused concepts
-- NOTE: Only perspective-concept pairs whose concept_id exists in the curated
-- schema_concepts set are seeded. Earlier seed rows referencing concept_ids
-- 4,5,6,10-19 pointed to concepts that were later removed/re-keyed during
-- curation, leaving orphaned (dead-FK) bridge rows that could never become
-- graph edges. Those 13 obsolete pairs were dropped so the SQLite source
-- matches the active concept definitions (and the ArangoDB projection).
INSERT INTO schema_perspective_concepts (perspective_id, concept_id, relationship_type, priority_weight) VALUES
(1, 1, 'USES_DEFINITION', 3),   -- Quality uses DefectSeverityQuality (primary)
(1, 8, 'USES_DEFINITION', 2),   -- Quality uses DeliveryPerformanceSupplier

-- Accounts_Payable perspective uses cost/payables concepts
(2, 2, 'USES_DEFINITION', 3),   -- AP uses DefectSeverityCost
(2, 9, 'USES_DEFINITION', 2),   -- AP uses DeliveryPerformanceFinance

-- Work_Orders perspective uses routing/sequencing concepts
(3, 7, 'USES_DEFINITION', 3),   -- Work_Orders uses DeliveryPerformanceOps

-- Accounts_Receivable perspective uses customer-facing concepts
(5, 3, 'USES_DEFINITION', 3),   -- AR uses DefectSeverityCustomer
(5, 8, 'USES_DEFINITION', 2);   -- AR uses DeliveryPerformanceSupplier (for visibility)

-- Schema Intents: Analytical goals that binary-switch concept weights
-- Intent elevates one field interpretation to 1.0 while de-elevating alternatives to 0.0
CREATE TABLE IF NOT EXISTS schema_intents (
    intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_name TEXT NOT NULL UNIQUE,
    intent_category TEXT NOT NULL,  -- maps to query categories (quality_control, supplier_performance, etc.)
    description TEXT,
    typical_question TEXT,  -- example natural language question for this intent
    primary_binding_key TEXT,  -- default binding key for SolderEngine query assembly
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Junction table: Intent-Concept weight mappings (the binary switch)
-- Weight semantics per treatise: Binary activation, NOT prioritization
--   1 = Explicitly elevated / active (use this interpretation)
--   0 = Neutral / not selected
--  -1 = Explicitly suppressed (never use this interpretation)
CREATE TABLE IF NOT EXISTS schema_intent_concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id INTEGER NOT NULL,
    concept_id INTEGER NOT NULL,
    intent_factor_weight INTEGER NOT NULL DEFAULT 0 CHECK (intent_factor_weight IN (-1, 0, 1)),
    explanation TEXT,  -- why this concept is elevated/suppressed for this intent
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (intent_id) REFERENCES schema_intents(intent_id),
    FOREIGN KEY (concept_id) REFERENCES schema_concepts(concept_id),
    UNIQUE(intent_id, concept_id)
);

-- Link intents to ground truth SQL queries
CREATE TABLE IF NOT EXISTS schema_intent_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id INTEGER NOT NULL,
    query_category TEXT NOT NULL,  -- maps to index.json category id
    query_file TEXT NOT NULL,      -- SQL file name
    query_index INTEGER,           -- which query in the file (0-based)
    query_name TEXT,               -- human-readable query name
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (intent_id) REFERENCES schema_intents(intent_id)
);

-- Self-heal: remove duplicate intent-query rows accumulated before the
-- unique index existed (this file re-runs on every boot; the seed INSERT
-- below used to lack OR IGNORE, duplicating rows each startup).
DELETE FROM schema_intent_queries
WHERE id NOT IN (
    SELECT MIN(id) FROM schema_intent_queries
    GROUP BY intent_id, query_file, query_index
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_intent_queries_unique
    ON schema_intent_queries (intent_id, query_file, query_index);

-- OPERATES_WITHIN: Intent → Perspective relationship
-- Intent operates within a perspective, constraining the graph traversal path
-- Weight semantics per treatise: Binary activation, NOT prioritization
--   1 = Active path (this perspective is used)
--   0 = Neutral / not selected
--  -1 = Explicitly suppressed path
CREATE TABLE IF NOT EXISTS schema_intent_perspectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id INTEGER NOT NULL,
    perspective_id INTEGER NOT NULL,
    intent_factor_weight INTEGER NOT NULL DEFAULT 1 CHECK (intent_factor_weight IN (-1, 0, 1)),
    explanation TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (intent_id) REFERENCES schema_intents(intent_id),
    FOREIGN KEY (perspective_id) REFERENCES schema_perspectives(perspective_id),
    UNIQUE(intent_id, perspective_id)
);

-- Seed data: Analytical Intents aligned with ground truth query categories
-- NOTE: Equipment Reliability intents (oee_operational, oee_capital_planning,
--   maintenance_scheduling) and Production Analytics intents (schedule_adherence,
--   line_efficiency, quality_cost_allocation) were removed — their query files
--   exclusively referenced empty PoC tables dropped in drop_poc_tables migration.
INSERT INTO schema_intents (intent_name, intent_category, description, typical_question) VALUES
-- Quality Control intents
('defect_cost_analysis', 'quality_control', 'Analyze defects from cost/financial impact perspective', 'What is the cost impact of defects by severity?'),
('defect_quality_trending', 'quality_control', 'Track defect rates and quality trends over time', 'What is the defect rate trend by product line?'),
('defect_customer_impact', 'quality_control', 'Assess defects from customer experience perspective', 'Which defects are most likely to reach customers?'),

-- Supplier Performance intents
('supplier_scorecard', 'supplier_performance', 'Evaluate supplier delivery and quality metrics', 'Which suppliers have the best on-time delivery?'),
('supplier_cost_penalties', 'supplier_performance', 'Analyze supplier performance for penalty/credit calculations', 'What penalties are owed for late deliveries?');

-- MRP / Inventory Management intents (M3 — added with MRP Query Palette wiring)
INSERT OR IGNORE INTO schema_intents (intent_name, intent_category, description, typical_question) VALUES
('inventory_planning', 'inventory_management', 'Identify parts needing replenishment based on reorder point and lead time', 'Which parts need to be reordered?'),
('inventory_stock_status', 'inventory_management', 'Summarise current on-hand stock levels and inventory value by part class', 'What are our current stock levels by part class?');

-- MRP intent-concept links (name-based subqueries — ID-independent)
INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: Reorder point is the primary replenishment trigger'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_planning' AND sc.concept_name = 'ReorderPoint';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: Lead time sets the planning horizon for replenishment'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_planning' AND sc.concept_name = 'LeadTime';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: On-hand quantity compared against reorder point'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_planning' AND sc.concept_name = 'OnHandQuantity';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: On-hand quantity is the primary stock-status measure'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_stock_status' AND sc.concept_name = 'OnHandQuantity';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 0, 'Neutral: Lead time informational in stock-status context'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_stock_status' AND sc.concept_name = 'LeadTime';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 0, 'Neutral: Reorder point informational in stock-status context'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_stock_status' AND sc.concept_name = 'ReorderPoint';

-- MRP intent-perspective links
INSERT OR IGNORE INTO schema_intent_perspectives (intent_id, perspective_id, intent_factor_weight, explanation)
SELECT si.intent_id, sp.perspective_id, 1, 'inventory_planning operates within Inventory_Transactions perspective'
FROM schema_intents si, schema_perspectives sp
WHERE si.intent_name = 'inventory_planning' AND sp.perspective_name = 'Inventory_Transactions';

INSERT OR IGNORE INTO schema_intent_perspectives (intent_id, perspective_id, intent_factor_weight, explanation)
SELECT si.intent_id, sp.perspective_id, 1, 'inventory_stock_status operates within Inventory_Transactions perspective'
FROM schema_intents si, schema_perspectives sp
WHERE si.intent_name = 'inventory_stock_status' AND sp.perspective_name = 'Inventory_Transactions';

-- ATP and AllocatedQuantity intents (M3 batch 7 — dataset-derived MRP concepts)
-- Both are derivations over customer_order_line, not single-column anchors.
-- primary_binding_key is set here so SolderEngine resolves the approved snippet
-- directly without needing a schema_concept_fields elevation path.
INSERT OR IGNORE INTO schema_intents (intent_name, intent_category, description, typical_question, primary_binding_key) VALUES
('inventory_atp', 'inventory_management',
 'Available-to-promise quantity per part: on-hand minus open customer order commitments',
 'How much stock is available to promise to new orders?',
 'inventory_atp_20260703_000004'),
('inventory_allocated_qty', 'inventory_management',
 'Quantity of on-hand stock committed to existing customer orders (allocated demand)',
 'How much inventory is already allocated to open orders?',
 'inventory_allocated_20260703_000005');

-- ATP intent-concept links
INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: AvailableToPromise is the primary derived measure for this intent'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_atp' AND sc.concept_name = 'AvailableToPromise';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 0, 'Neutral: AllocatedQuantity is the deduction term in the ATP calculation'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_atp' AND sc.concept_name = 'AllocatedQuantity';

-- AllocatedQuantity intent-concept link
INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: AllocatedQuantity is the open customer-order demand commitment'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_allocated_qty' AND sc.concept_name = 'AllocatedQuantity';

-- ATP and AllocatedQuantity perspective links
INSERT OR IGNORE INTO schema_intent_perspectives (intent_id, perspective_id, intent_factor_weight, explanation)
SELECT si.intent_id, sp.perspective_id, 1, 'inventory_atp operates within Inventory_Transactions perspective'
FROM schema_intents si, schema_perspectives sp
WHERE si.intent_name = 'inventory_atp' AND sp.perspective_name = 'Inventory_Transactions';

INSERT OR IGNORE INTO schema_intent_perspectives (intent_id, perspective_id, intent_factor_weight, explanation)
SELECT si.intent_id, sp.perspective_id, 1, 'inventory_allocated_qty operates within Inventory_Transactions perspective'
FROM schema_intents si, schema_perspectives sp
WHERE si.intent_name = 'inventory_allocated_qty' AND sp.perspective_name = 'Inventory_Transactions';

-- Batch 8: five remaining MRP glossary concepts (dataset-derived measures)
-- Each uses primary_binding_key for direct snippet resolution. Column anchors
-- for these concepts are registered in seed_elevations.py batch 8.
INSERT OR IGNORE INTO schema_intents (intent_name, intent_category, description, typical_question, primary_binding_key) VALUES
('inventory_safety_stock', 'inventory_management',
 'Safety stock proxy per part: reorder point minus estimated lead-time demand from open orders',
 'How much safety stock buffer does each part carry?',
 'inventory_safetystock_20260703_000006'),
('inventory_lead_time_demand', 'inventory_management',
 'Expected demand during the replenishment lead-time window (avg daily demand × lead_time_days)',
 'How much demand will occur during the lead time for each part?',
 'inventory_leadtimedemand_20260703_000007'),
('inventory_minimum_stock', 'inventory_management',
 'Minimum stock quantity per part: the reorder point as the min level in a min/max policy',
 'What is the minimum stock level for each part?',
 'inventory_minimumstock_20260703_000008'),
('inventory_maximum_stock', 'inventory_management',
 'Maximum stock quantity per part: reorder point plus average historical PO replenishment qty',
 'What is the maximum stock level each part should be replenished up to?',
 'inventory_maximumstock_20260703_000009'),
('inventory_eoq', 'inventory_management',
 'Economic order quantity proxy per part: average observed PO order quantity from purchase history',
 'What is the economic order quantity for each part?',
 'inventory_eoq_20260703_000010');

-- Batch 8 intent-concept links
INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: SafetyStock is the primary measure for this intent'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_safety_stock' AND sc.concept_name = 'SafetyStock';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: LeadTimeDemand is the primary measure for this intent'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_lead_time_demand' AND sc.concept_name = 'LeadTimeDemand';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: MinimumStockQuantity is the primary measure for this intent'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_minimum_stock' AND sc.concept_name = 'MinimumStockQuantity';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: MaximumStockQuantity is the primary measure for this intent'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_maximum_stock' AND sc.concept_name = 'MaximumStockQuantity';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: EconomicOrderQuantity is the primary measure for this intent'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'inventory_eoq' AND sc.concept_name = 'EconomicOrderQuantity';

-- Batch 8 perspective links
INSERT OR IGNORE INTO schema_intent_perspectives (intent_id, perspective_id, intent_factor_weight, explanation)
SELECT si.intent_id, sp.perspective_id, 1, 'operates within Inventory_Transactions perspective'
FROM schema_intents si, schema_perspectives sp
WHERE si.intent_name IN (
    'inventory_safety_stock','inventory_lead_time_demand',
    'inventory_minimum_stock','inventory_maximum_stock','inventory_eoq'
) AND sp.perspective_name = 'Inventory_Transactions';

-- Job Costing Ledger intents (NLQ layer over the gl_* tables).
-- Each intent resolves directly through primary_binding_key to an
-- SME-approved governed snippet — no schema_concept_fields elevation path.
INSERT OR IGNORE INTO schema_intents (intent_name, intent_category, description, typical_question, primary_binding_key) VALUES
('ledger_inventory_balance', 'job_costing_ledger',
 'Signed running balance of each perpetual inventory bucket (Raw Materials / WIP / Finished Goods) from the gl_* sub-ledgers',
 'How much value sits in raw materials, WIP, and finished goods?',
 'ledger_inventorybalance_20260719_000001'),
('ledger_job_cost_summary', 'job_costing_ledger',
 'Job cost roll-up per work order and cost element (LABOR / MATERIAL / BURDEN / SERVICE) from gl_job_cost_detail',
 'What has job WO-00004 cost so far, by cost element?',
 'ledger_jobcostsummary_20260719_000002'),
('ledger_event_trace', 'job_costing_ledger',
 'Chronological gl_events audit trail for a job with the originating source document per posting',
 'Show the event trace for job WO-00001',
 'ledger_eventtrace_20260719_000003'),
('ledger_material_issued', 'job_costing_ledger',
 'Material issued over a period: RM_ISSUE outflow from the Raw Materials bucket per part and job',
 'What material was issued in July?',
 'ledger_materialissued_20260719_000004'),
('ledger_fg_production', 'job_costing_ledger',
 'Finished goods produced over a period: FG_COMPLETION inflow into the Finished Goods bucket per part and job',
 'What finished goods were produced this year?',
 'ledger_fgproduced_20260719_000005');

-- Job Costing Ledger perspective links
INSERT OR IGNORE INTO schema_intent_perspectives (intent_id, perspective_id, intent_factor_weight, explanation)
SELECT si.intent_id, sp.perspective_id, 1, 'operates within General_Ledger perspective'
FROM schema_intents si, schema_perspectives sp
WHERE si.intent_name IN (
    'ledger_inventory_balance','ledger_job_cost_summary','ledger_event_trace',
    'ledger_material_issued','ledger_fg_production'
) AND sp.perspective_name = 'General_Ledger';

-- Job Costing Ledger intent-concept links (name-based subqueries — ID-independent).
-- Concepts are seeded by replit_integrations/seed_elevations.py (batch 9); these
-- inserts are silently zero-row until that seeder has run — the app re-applies
-- this seed on every boot, so they self-heal (same pattern as the MRP links).
INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: bucket balance is the measure this intent reports'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'ledger_inventory_balance' AND sc.concept_name = 'InventoryBucketBalance';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: cost element is the roll-up axis of the job cost summary'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'ledger_job_cost_summary' AND sc.concept_name = 'JobCostElement';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: posting event class drives the chronological audit trace'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'ledger_event_trace' AND sc.concept_name = 'LedgerPostingEventClass';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: RM_ISSUE outflow is the measure this intent reports'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'ledger_material_issued' AND sc.concept_name = 'MaterialIssueFlow';

INSERT OR IGNORE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation)
SELECT si.intent_id, sc.concept_id, 1, 'ELEVATED: FG_COMPLETION inflow is the measure this intent reports'
FROM schema_intents si, schema_concepts sc
WHERE si.intent_name = 'ledger_fg_production' AND sc.concept_name = 'FinishedGoodsProductionFlow';

-- Job Costing Ledger palette queries (new file — indexes 0..4, no displacement)
INSERT OR IGNORE INTO schema_intent_queries (intent_id, query_category, query_file, query_index, query_name)
SELECT si.intent_id, 'job_costing_ledger', 'job_costing_ledger.sql', q.qi, q.qn
FROM schema_intents si
JOIN (
    SELECT 'ledger_inventory_balance' AS iname, 0 AS qi, 'Inventory Balance per Bucket' AS qn
    UNION ALL SELECT 'ledger_job_cost_summary', 1, 'Job Cost Summary by Cost Element'
    UNION ALL SELECT 'ledger_event_trace', 2, 'Job Event Trace'
    UNION ALL SELECT 'ledger_material_issued', 3, 'Material Issued over a Period'
    UNION ALL SELECT 'ledger_fg_production', 4, 'Finished Goods Produced over a Period'
) q ON si.intent_name = q.iname;

-- Seed data: Intent-Concept weight mappings (binary elevation/suppression)
-- Weight semantics: 1 = elevated, 0 = neutral, -1 = suppressed
-- For defect analysis, each intent elevates ONE severity interpretation
INSERT INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation) VALUES
-- defect_cost_analysis (intent 1) elevates DefectSeverityCost, suppresses others
(1, 1, 0, 'Quality classification not relevant for cost analysis'),
(1, 2, 1, 'ELEVATED: Cost impact interpretation for financial analysis'),
(1, 3, 0, 'Customer impact not primary for cost analysis'),

-- defect_quality_trending (intent 2) elevates DefectSeverityQuality
(2, 1, 1, 'ELEVATED: Quality classification for trending'),
(2, 2, 0, 'Cost perspective not primary for quality trending'),
(2, 3, 0, 'Customer perspective not primary for quality trending'),

-- defect_customer_impact (intent 3) elevates DefectSeverityCustomer
(3, 1, 0, 'Internal quality classification not customer-facing'),
(3, 2, 0, 'Cost impact not customer-facing'),
(3, 3, 1, 'ELEVATED: Customer experience interpretation'),

-- supplier_scorecard (intent 4) elevates DeliveryPerformanceSupplier
(4, 7, 0, 'Operational view not for supplier scoring'),
(4, 8, 1, 'ELEVATED: Supplier scorecard metrics'),
(4, 9, 0, 'Financial penalties separate from scorecard'),

-- supplier_cost_penalties (intent 5) elevates DeliveryPerformanceFinance
(5, 7, 0, 'Operational planning not for penalty calc'),
(5, 8, 0, 'Scorecard metrics not for penalty calc'),
(5, 9, 1, 'ELEVATED: Financial penalty calculations');
-- NOTE: Intents 6-11 (equipment_reliability and production_analytics categories)
-- were removed — their query files exclusively referenced empty PoC tables.

-- Seed data: Link intents to ground truth queries
INSERT OR IGNORE INTO schema_intent_queries (intent_id, query_category, query_file, query_index, query_name) VALUES
-- Quality Control queries
(1, 'quality_control', 'quality_control.sql', 0, 'Defects by severity with cost rollup'),
(2, 'quality_control', 'quality_control.sql', 1, 'Weekly defect rate trend'),
(3, 'quality_control', 'quality_control.sql', 2, 'Customer escape risk analysis'),

-- Supplier Performance queries
(4, 'supplier_performance', 'supplier_performance.sql', 0, 'Supplier delivery scorecard'),
(5, 'supplier_performance', 'supplier_performance.sql', 1, 'Late delivery penalty calculation');
-- NOTE: Equipment Reliability and Production Analytics intent-query rows removed
-- (query files exclusively referenced empty PoC tables).

-- Seed data: OPERATES_WITHIN relationships (Intent → Perspective)
-- Maps each intent to its constraining perspective(s)
-- Weight semantics: 1 = active path, 0 = neutral, -1 = suppressed
-- Perspective IDs: 1=Quality, 2=Finance, 3=Operations, 4=Compliance, 5=Customer
INSERT INTO schema_intent_perspectives (intent_id, perspective_id, intent_factor_weight, explanation) VALUES
-- Quality Control intents
(1, 2, 1, 'defect_cost_analysis operates within Finance perspective'),
(2, 1, 1, 'defect_quality_trending operates within Quality perspective'),
(3, 5, 1, 'defect_customer_impact operates within Customer perspective'),

-- Supplier Performance intents
(4, 1, 1, 'supplier_scorecard operates within Quality perspective'),
(5, 2, 1, 'supplier_cost_penalties operates within Finance perspective');
-- NOTE: Equipment Reliability and Production Analytics intent perspectives removed
-- (intents dropped with their empty-table query files).

-- =============================================================================
-- APP METADATA TABLES (Plan-009)
-- These three tables are the SQLite source of truth for DAB field definitions
-- and graph topology annotations.  dab_config.json is a synced artifact, not
-- a source of truth.  Excluded from get_all_tables() via APP_METADATA_TABLES.
-- =============================================================================

-- API / DAB field descriptions: display name, description, and example value
-- keyed on the four-part column identity.
CREATE TABLE IF NOT EXISTS api_field_descriptions (
    source_database TEXT    NOT NULL,
    schema_name     TEXT    NOT NULL,
    table_name      TEXT    NOT NULL,
    column_name     TEXT    NOT NULL,
    display_name    TEXT,
    description     TEXT,
    example_value   TEXT,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_database, schema_name, table_name, column_name)
);

-- Structural containment topology annotations: records the intended edge type
-- between node pairs in the containment graph (database → schema → table → column).
CREATE TABLE IF NOT EXISTS schema_topology_metadata (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_type TEXT    NOT NULL CHECK(source_node_type IN ('database', 'schema', 'table', 'column')),
    target_node_type TEXT    NOT NULL CHECK(target_node_type IN ('database', 'schema', 'table', 'column')),
    source_key       TEXT    NOT NULL,
    target_key       TEXT    NOT NULL,
    edge_predicate   TEXT    NOT NULL DEFAULT 'CONTAINS',
    weight           INTEGER NOT NULL DEFAULT 1,
    notes            TEXT,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_node_type, target_node_type, source_key, target_key, edge_predicate)
);

-- DAB field definitions: certified, SME-approved field definition text keyed
-- on the same four-part column identity as api_field_descriptions.
CREATE TABLE IF NOT EXISTS dab_field_definitions (
    source_database  TEXT    NOT NULL,
    schema_name      TEXT    NOT NULL,
    table_name       TEXT    NOT NULL,
    column_name      TEXT    NOT NULL,
    field_definition TEXT,
    certified        INTEGER NOT NULL DEFAULT 0 CHECK(certified IN (0, 1)),
    updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_database, schema_name, table_name, column_name)
);

-- Column masking policies: per-column data-masking strategy + rationale keyed on
-- the same four-part column identity as api_field_descriptions. The local
-- stand-in for the company DAB's masking layer: an SME picks a strategy
-- (none/hash/partial/redact), certifies it, then publishes certified rows into
-- dab_config.json (each field's "masking" attribute). SQLite is the source of
-- truth; strategies are chosen deterministically (no LLM).
CREATE TABLE IF NOT EXISTS column_masking_policies (
    source_database  TEXT    NOT NULL,
    schema_name      TEXT    NOT NULL,
    table_name       TEXT    NOT NULL,
    column_name      TEXT    NOT NULL,
    masking_strategy TEXT    NOT NULL DEFAULT 'none'
        CHECK(masking_strategy IN ('none', 'hash', 'partial', 'redact')),
    rationale        TEXT,
    certified        INTEGER NOT NULL DEFAULT 0 CHECK(certified IN (0, 1)),
    updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_database, schema_name, table_name, column_name)
);

-- Masking matrix: the column-masking DAG, one row per masked column. Mirrors the
-- root CSV masking_matrix.csv (the human-editable copy)
-- and is kept in sync by masking_matrix.py. Distinct from column_masking_policies
-- (the SME strategy tab) — the two live side by side. dag_no is the DAG node id
-- and primary key; parent_table/parent_column carry lineage. status static/complete
-- means the row is certified/locked (data already pulled into SQLMesh), active means
-- still in progress. masking_rule notates the transform, e.g. hash_sha256(col,length)
-- = SHA-256(value+salt) hashed to the same width as the field in the schema; that
-- width is held in field_length (0 = unbounded -> full 64-char digest).
CREATE TABLE IF NOT EXISTS masking_matrix (
    dag_no           TEXT    NOT NULL PRIMARY KEY,
    table_name       TEXT    NOT NULL,
    column_name      TEXT    NOT NULL DEFAULT '',
    parent_table     TEXT    NOT NULL DEFAULT '',
    parent_column    TEXT    NOT NULL DEFAULT '',
    masking_rule     TEXT,
    masking_type     TEXT,
    field_length     INTEGER NOT NULL DEFAULT 0,
    masking_mode     INTEGER NOT NULL DEFAULT 1,
    pre_stage_server TEXT,
    status           TEXT    NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'static', 'complete'))
);

-- Masking-type reference lookup: the closed set of masking types and their
-- masking_mode numbers, used by masking_matrix. Mirrors the root CSV
-- masking_type.csv and is kept in sync by masking_type.py. status 'active' means
-- the type may be assigned; 'inactive' retires it without deleting it.
CREATE TABLE IF NOT EXISTS masking_type (
    masking_type TEXT    NOT NULL PRIMARY KEY,
    masking_mode INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'inactive'))
);

-- No seed rows here (intentionally). Like masking_matrix, this table is populated
-- at boot from the root masking_type.csv (and masking_type.write_default_csv
-- recreates that CSV if it goes missing). Seeding here would resurrect rows an SME
-- deleted via the Masking Matrix tab, because init_sqlite_db re-runs this script
-- with INSERT OR IGNORE on every boot.

-- SQL graph source tables: hold the full canonical graph (nodes + edges in the
-- fixed 6-slot composite-key form) that replit_integrations/export_graph_metadata.py
-- materializes and then serializes graph_metadata.json FROM. SQLite is the source
-- of truth; the JSON is provably a dump of these rows. One column per JSON field;
-- columns that apply to only one node/edge kind are NULL for the others. The
-- ordinal column preserves the exact JSON emission order on read-back.
CREATE TABLE IF NOT EXISTS sql_graph_nodes (
    ordinal       INTEGER NOT NULL,
    _key          TEXT    NOT NULL PRIMARY KEY,
    _id           TEXT    NOT NULL,
    node_type     TEXT    NOT NULL CHECK(node_type IN ('table', 'column')),
    node_family   TEXT    NOT NULL,
    perspective   TEXT    NOT NULL,
    table_name    TEXT    NOT NULL,
    column_name   TEXT,
    column_slot   TEXT,
    predicate     TEXT    NOT NULL,
    unique_id     TEXT    NOT NULL,
    description   TEXT,
    column_type   TEXT,
    "notnull"     INTEGER,
    default_value TEXT,
    primary_key   INTEGER,
    foreign_key   INTEGER
);

CREATE TABLE IF NOT EXISTS sql_graph_edges (
    ordinal           INTEGER NOT NULL,
    _key              TEXT    NOT NULL PRIMARY KEY,
    _id               TEXT    NOT NULL,
    _from             TEXT    NOT NULL,
    _to               TEXT    NOT NULL,
    edge_family       TEXT    NOT NULL,
    edge_type         TEXT    NOT NULL CHECK(edge_type IN ('has_column', 'references', 'resolves_to')),
    perspective       TEXT    NOT NULL,
    unique_id         TEXT    NOT NULL,
    references_table  TEXT,
    references_column TEXT,
    weight            INTEGER,
    priority_weight   INTEGER,
    field_component   INTEGER
);

-- SME-authored canonical edges (Define Relationship UI source of truth).
-- Durable input table: the exporter MERGES these into the materialized
-- sql_graph_edges every run, so authored relationships survive a re-export
-- (sql_graph_edges itself is delete+reinsert each export). Absent columns are
-- stored as '' (not NULL) so the UNIQUE constraint dedupes correctly.
CREATE TABLE IF NOT EXISTS sql_graph_authored_edges (
    authored_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_type     TEXT    NOT NULL CHECK(edge_type IN ('has_column', 'references', 'resolves_to')),
    from_table    TEXT    NOT NULL,
    from_column   TEXT    NOT NULL DEFAULT '',
    to_table      TEXT    NOT NULL,
    to_column     TEXT    NOT NULL DEFAULT '',
    perspective   TEXT    NOT NULL DEFAULT 'system',
    weight        INTEGER,
    concept       TEXT,
    created_by    TEXT    NOT NULL DEFAULT 'define_relationship_ui',
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(edge_type, from_table, from_column, to_table, to_column, perspective)
);

-- Column bindings: maps semantic intent slots to physical columns.
-- Created here as an additive guard; populated by the solder engine.
CREATE TABLE IF NOT EXISTS column_bindings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_name     TEXT    NOT NULL,
    slot_name       TEXT    NOT NULL,
    table_name      TEXT    NOT NULL,
    column_name     TEXT    NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(intent_name, slot_name)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- MINIMAL SYNTHETIC GL LEDGER TABLES (see migrations/add_gl_ledger_tables.py)
-- Deliberately minimal: no period-close, control-account, or validation
-- columns. job_id = work_order.wo_id; FKs are structural-only (enforcement
-- OFF — declared for graph derivation). event_date carries NO default: every
-- timestamp must be data-derived from a source document, never wall-clock.
-- Population (posting functions) is a later task; this is DDL only.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gl_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT,                   -- FK -> work_order.wo_id (structural)
    event_type  TEXT NOT NULL,          -- e.g. RM_ISSUE / LABOR / BURDEN / SERVICE / FG_COMPLETION
    amount      REAL NOT NULL DEFAULT 0.0,
    event_date  DATETIME NOT NULL,      -- data-derived from the source document, never wall-clock
    source_table TEXT,                  -- originating document table (material_issue, labor_ticket, ...)
    source_id   TEXT,                   -- originating document row key
    FOREIGN KEY (job_id) REFERENCES work_order (wo_id)
);

CREATE TABLE IF NOT EXISTS gl_raw_materials_inventory (
    line_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER,                -- FK -> gl_events (structural)
    job_id      TEXT,                   -- FK -> work_order.wo_id (structural)
    part_id     TEXT,                   -- FK -> part (structural)
    amount      REAL NOT NULL DEFAULT 0.0,   -- signed: + into RM, - out of RM
    event_type  TEXT NOT NULL,
    event_date  DATETIME NOT NULL,      -- data-derived, never wall-clock
    FOREIGN KEY (event_id) REFERENCES gl_events (event_id),
    FOREIGN KEY (job_id) REFERENCES work_order (wo_id),
    FOREIGN KEY (part_id) REFERENCES part (part_id)
);

CREATE TABLE IF NOT EXISTS gl_wip_inventory (
    line_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER,                -- FK -> gl_events (structural)
    job_id      TEXT,                   -- FK -> work_order.wo_id (structural)
    part_id     TEXT,                   -- FK -> part (structural)
    amount      REAL NOT NULL DEFAULT 0.0,   -- signed: + into WIP, - out of WIP
    event_type  TEXT NOT NULL,
    event_date  DATETIME NOT NULL,      -- data-derived, never wall-clock
    FOREIGN KEY (event_id) REFERENCES gl_events (event_id),
    FOREIGN KEY (job_id) REFERENCES work_order (wo_id),
    FOREIGN KEY (part_id) REFERENCES part (part_id)
);

CREATE TABLE IF NOT EXISTS gl_finished_goods_inventory (
    line_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER,                -- FK -> gl_events (structural)
    job_id      TEXT,                   -- FK -> work_order.wo_id (structural)
    part_id     TEXT,                   -- FK -> part (structural)
    amount      REAL NOT NULL DEFAULT 0.0,   -- signed: + into FG, - out of FG
    event_type  TEXT NOT NULL,
    event_date  DATETIME NOT NULL,      -- data-derived, never wall-clock
    FOREIGN KEY (event_id) REFERENCES gl_events (event_id),
    FOREIGN KEY (job_id) REFERENCES work_order (wo_id),
    FOREIGN KEY (part_id) REFERENCES part (part_id)
);

CREATE TABLE IF NOT EXISTS gl_job_cost_detail (
    line_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER,                -- FK -> gl_events (structural)
    job_id      TEXT NOT NULL,          -- FK -> work_order.wo_id (structural)
    amount      REAL NOT NULL DEFAULT 0.0,
    event_type  TEXT NOT NULL,          -- cost element: LABOR / MATERIAL / BURDEN / SERVICE
    event_date  DATETIME NOT NULL,      -- data-derived, never wall-clock
    FOREIGN KEY (event_id) REFERENCES gl_events (event_id),
    FOREIGN KEY (job_id) REFERENCES work_order (wo_id)
);
