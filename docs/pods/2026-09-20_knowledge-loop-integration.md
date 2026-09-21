William, yes — if the librarian is the driver of **local knowledge ingestion**, then the coding agent needs a **clean, authoritative listing** of *every file and folder the librarian consumes*.

And you're right to flag this now:
the *Knowledge Loop* is embodied in **docs/my-mrp-kb/**, not in **docs/plans/** — and the librarian's ingestion pipeline depends on this folder.

The point of this pod is the origin story: this is where it started. Ingesting the documents below — via the librarian's `list_mrp_documents()` / `read_document()` / `stage_terminology()` tools — is what eventually led to the ontology mosaic. The chain runs corpus → librarian extraction → staged terminology → ontology mosaic; nothing downstream exists without this folder.

Below is the **actual structure**, verified directly against the repo and against `scripts/librarian_server.py`'s `list_mrp_documents()` output (35 files, 2026-09-20). An earlier version of this pod described a different, speculative 10-folder scheme (`01-demand/` … `10-salt/`) that did not match reality — this revision replaces it with what's actually on disk. Nothing here needed restoring; the corpus is already populated with real SME content.

---

# 📁 **Authoritative Location of Librarian Knowledge Base**
```
product-ontime-analysis/docs/my-mrp-kb/
```

This folder is the **local knowledge corpus** the librarian ingests. It contains SME reference documents, exception report definitions, operational semantics, grounding narratives, and domain-specific knowledge slices — the *human-intent layer* of the Knowledge Loop.

---

# 🧩 **Folder Structure (actual, verified)**

```
docs/my-mrp-kb/
    01-core-framework/
    02-capacity-planning/
    03-customer-order-demand/
    04-shop-floor-routing/
    05-inventory-transactions/
    06-supplier-rating/
    07-three-way-match/
```

35 files total. Most documents exist as an SME-authored `.docx` original with a parsed/extracted `.md` counterpart; two folders (`05-inventory-transactions/`, `07-three-way-match/`) also carry a `document_index.json`.

---

# 📂 **01-core-framework/**
- Knowledge Loop Framework - Aerospace MRP (`.docx` + `.md`)
- Manufacturing and MRP Terminology in Semantic Models (`.docx` + `.md`)
- My MRP - Deterministic Semantic Architecture (`.docx` + `.md`)
- My MRP 501.001 Outline - Core - Parts (`.docx` + `.md`)

The foundational vocabulary and architecture documents — this is what `stage_terminology()` reads by default (`Manufacturing and MRP Terminology in Semantic Models.docx`).

---

# 📂 **02-capacity-planning/**
- Capacity Planning - Aerospace MRP.md

---

# 📂 **03-customer-order-demand/**
- Customer Order Demand - Aerospace MRP.md

---

# 📂 **04-shop-floor-routing/**
- Shop Floor Work and Routing - Aerospace MRP.md

Work order / operation semantics, routing topology, WO/OP join rules — grounding for the routing ontology.

---

# 📂 **05-inventory-transactions/**
- Adjusting_Materials.md
- Document_Hierarchy_Map.md
- Inventory_Transaction_Entry_Index.md
- Inventory_Transaction_Terminology_Guide.md
- My MRP 501.1 Outline.md
- README.md
- Receiving_Materials_Into_Inventory.md
- Receiving_by_Part.md
- Returning_Issued_Materials.md
- Returning_Received_Materials.md
- VMINVENTWhat.md
- VMINVENT_APLfrmInventoryEntry.md
- VMINVENT_APLfrmInventoryTransfer.md
- VMINVENT_APLfrmIssue.md
- VMINVENTfrmIssue.md
- document_index.json

The largest folder — inventory transaction semantics (receiving, adjustments, returns), keyed to the real Infor VISUAL screen names (`VMINVENT*`).

---

# 📂 **06-supplier-rating/**
- Supplier Rating Grounding - Receiving Flow and LEFT JOIN.md

---

# 📂 **07-three-way-match/**
- SQLMesh_Incremental_Three_Way_Match_Model (`.docx` + `.md`)
- TWM - Accrual Lifecycle and Reconciliation Guide.docx
- TWM - Purchase-Order Line Accrual Glossary.docx
- TWM - Purchase-Order Line Accrual Guide.docx
- Uninvoiced_Receivers_Report_-_Detailed.md
- document_index.json

Directly relevant to the current work: PO ↔ Receipt ↔ Invoice semantics, voucher status rules, exception logic — the semantic anchor for `three_way_match.ttl`/`three_way_match.obda` and the `complete_three_way_match.py` migration.

---

# 🧭 **How the Librarian Uses These Folders**

The librarian's tools (`scripts/librarian_server.py`, verified working 2026-09-20):

### **1. Directory scan**
`list_mrp_documents(directory=None)` — lists every file under `docs/my-mrp-kb/**` (35 files).

### **2. Document read**
`read_document(filepath)` — reads a `.docx` file's paragraphs + table cells into plain text. `.md` files are read directly (not through this tool).

### **3. Terminology staging**
`stage_terminology(filepath=None, commit=False)` — deterministic glossary extraction from a terminology `.docx`, anchored against existing SME-approved perspectives/categories. Dry run by default; writes a reviewable `proposed_terms.csv` + JSON artifact to `mrp_research_staging/<run_id>/` (gitignored). `commit=True` stages approved terms into the isolated `mrp_research` ArangoDB database.

### **4. Research graph commit**
`commit_to_arangodb(payload)` — upserts nodes/edges into the **isolated** `mrp_research` database (`ai_research_node`/`ai_research_edge` collections), never the certified `manufacturing_graph`. Gated off by default (`MRP_ENABLE_GRAPH_COMMIT=true` required) and hard-guarded: refuses if the target database name resolves to a certified name, or if the collection names aren't namespaced under `ai_research_*`. Verified via `scripts/tests/test_librarian_knowledge_loop.py`.

### **5. Border-extract lineage (optional, not currently populated)**
`parse_ddl_csv(csv_filepath)` — extracts view-dependency edges from a CSV of SQL Server view DDL. Reads from `certificate_for_receiving/border_extracts/`, which does not currently exist in this checkout — this tool cleanly raises `FileNotFoundError` rather than failing silently.

---

# 🎯 **Why this folder matters**

This is the semantic backbone the rest of the architecture was built on top of:

- The librarian cannot ingest knowledge without it.
- The Knowledge Loop cannot operate without it.
- The mapping layer cannot be unified without it.
- The ontology mosaic, VEP validation, and YAML template grounding all depend on this corpus.
- `07-three-way-match/` specifically grounds the three-way-match TTL/OBDA work and the `complete_three_way_match.py` migration.
- The research-graph isolation (`mrp_research` vs. `manufacturing_graph`) means the librarian can stage new terminology/relationships from these documents without ever risking the certified semantic layer.
