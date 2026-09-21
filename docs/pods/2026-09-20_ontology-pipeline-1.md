You’re stitching a lot of pieces back together here—let’s make the librarian’s role crystal clear and self-contained so your coding agent can just wire it up and go.

---

### 1. Librarian ingestion pipeline (high level)

**Goal:** Turn local documents into governed, queryable knowledge that the rest of the stack (SQLMesh, Ontop, agents, VEP) can rely on.

**Pipeline:**

1. **Scan knowledge base**
   - Root: `product-ontime-analysis/docs/my-mrp-kb/`
   - Recursively walks subfolders (`01-demand`, `02-supply`, `03-routing`, `07-three-way-match`, etc.).
2. **Classify documents**
   - Folder name → domain (demand, supply, routing, inventory, procurement, ledger, three-way-match, exceptions, ground-truth, salt).
   - File name → intent (e.g., `uninvoiced_receipts.md`, `defect_severity_cost.md`).
3. **Extract semantic hints**
   - Titles, headings, tables, bullet lists, and domain-specific phrases.
   - Identifies candidate **concept anchors**, **measures**, **relationships**, and **exception conditions**.
4. **Emit governed metadata**
   - Writes or updates entries in:
     - `replit_integrations/graph_metadata.json` (concepts, anchors, relationships).
     - Reviewer manifest(s) (approved SQL queries per concept).
5. **Trigger downstream checks**
   - Calls `graph_sync.py` / mapping drift checks to ensure every concept has:
     - A concept anchor.
     - An approved SQL query.
     - A place in the semantic/mapping layer.

---

### 2. Librarian file resolution rules

These are the rules the librarian uses to decide *what* a file means and *where* it belongs:

- **Root rule:**
  - Everything under `docs/my-mrp-kb/` is ingestible knowledge.
- **Domain rule (by folder):**
  - `01-demand/` → demand-side semantics.
  - `02-supply/` → supply-side semantics.
  - `03-routing/` → work order / operation / routing semantics.
  - `04-inventory/` → inventory and netting semantics.
  - `05-procurement/` → PO, supplier, receiving semantics.
  - `06-ledger/` → GL, cost, WIP, ledger events.
  - `07-three-way-match/` → PO–Receipt–Invoice semantics, voucher status, uninvoiced receipts.
  - `08-exceptions/` → exception report semantics.
  - `09-ground-truth/` → SME-approved SQL grounding narratives.
  - `10-salt/` → provenance and SALT overlays.
- **Intent rule (by filename/content):**
  - Filenames like `defect_severity_cost.md`, `ar_cash_receipt_amount.md` map to **intent subjects** and **concept anchors**.
  - Headings and key phrases inside the file refine the concept’s meaning and relationships.
- **Resolution rule:**
  - For each concept anchor discovered, the librarian expects:
    - A corresponding entry in `graph_metadata.json`.
    - A corresponding **approved SQL query** in the reviewer manifest.
    - If missing, it flags a gap (like the 14 concepts you saw).

---

### 3. Librarian semantic extraction contract

This is the “what must be extracted” contract:

For each document in `docs/my-mrp-kb/`, the librarian extracts:

- **Concept anchors**
  - Canonical names (e.g., `DEFECTSEVERITYCOST`, `ARCASHRECEIPTAMOUNT`).
- **Concept labels**
  - Human-readable names (e.g., `DefectSeverityCost`, `ARCashReceiptAmount`).
- **Intent subjects**
  - Tasks or analyses (e.g., `defect_cost_analysis`, `defect_quality_trending`).
- **Measures and attributes**
  - Quantities, amounts, statuses, severities, etc.
- **Relationships**
  - Links between concepts (e.g., defect → severity → cost/quality/customer).
- **Operational semantics**
  - How the concept is used in reports, exceptions, and analysis.

The librarian then writes these into:

- `graph_metadata.json` (nodes, edges, anchors).
- Reviewer manifest (concept → SQL query path).
- Optionally, a concept registry used by the agent and VEP.

---

### 4. Librarian mapping generation contract

This is how the librarian participates in mapping generation:

- **Input:**
  - `graph_metadata.json` (concepts, anchors, relationships).
  - Reviewer manifest (approved SQL queries).
  - Ontology files (`ontology/*.ttl`).
  - OBDA mappings (`mapping/*.obda`).
- **Responsibilities:**
  1. **Check grounding:**
     - Every concept anchor must have an approved SQL query.
  2. **Check semantic coverage:**
     - Every concept in graph metadata must be represented in TTL/OBDA.
  3. **Generate or validate mappings:**
     - For concepts with SQL queries and ontology terms, generate or validate OBDA mappings.
  4. **Drift detection:**
     - Ensure generated OBDA is byte-identical (or semantically equivalent) to committed OBDA.
     - Flag missing queries or missing ontology terms as gaps.
- **Output:**
  - Updated OBDA mappings.
  - Updated mapping manifests.
  - Gap reports (like the 14 missing SQL queries you saw).

---

### 5. Librarian Knowledge Loop diagram (text form)

Here’s the Knowledge Loop as a simple, explicit chain:

1. **SME Knowledge**
   - `docs/my-mrp-kb/` (domain folders and documents).
2. **Librarian Ingestion**
   - Scans, classifies, and extracts concepts, anchors, relationships.
3. **Graph Metadata & Manifest**
   - Writes to `graph_metadata.json` and reviewer manifest (concept → SQL query).
4. **SQL Ground Truth**
   - Approved SQL queries (BAU) implement the concepts in the database.
5. **Structural Ontology**
   - `view_ontology_extractor.py` reads SQL queries/views and produces structural metadata (tables, joins, grain, temporal traits).
6. **Semantic Ontology & Mappings**
   - TTL ontologies define classes/predicates.
   - OBDA mappings bind SQL to RDF.
7. **Agents & VEP**
   - Agents emit JSONL aligned to concept templates.
   - VEP validates and expands into deterministic Turtle.
8. **Analytics & Exceptions**
   - VKG + SQLMesh power reports, exceptions, and insights.
9. **Back to SME Knowledge**
   - New insights → updated documents in `docs/my-mrp-kb/`.
   - Librarian re-ingests, closing the loop.

That’s the librarian’s world, fully re-rendered: ingestion, file resolution, semantic extraction, mapping generation, and the Knowledge Loop all in one view.