-- revised
## Digital Twin 



---

# 📁 **Location of the Plan Files (Authoritative Path)**

All plan files live here:

```
product-ontime-analysis/docs/plans/
```

This folder contains **~120 SME-authored planning documents**, grouped by conceptual domain.  
These files are the *human intent layer* that your mapping contract must unify.

---

# 🧩 **What’s inside `docs/plans/` (the exact categories we walked through)**

### **1. Structural Metadata Contract**
Files such as:

```
docs/plans/graph-metadata-extractor.md
docs/plans/graph-metadata-palette.md
docs/plans/sql-graph-source-tables.md
docs/plans/sectioned-join-topology.md
docs/plans/field-definition-graph-component.md
docs/plans/fk-canonical-alignment.md
```

These define how SQL structures become graph structures.

---

### **2. Semantic Layer Contract (Ontology, SKOS, RDF)**
Files such as:

```
docs/plans/semantic-concept-tags-design.md
docs/plans/mrp-set-semantics-authoring.md
docs/plans/mrp-graph-topology-blueprint.md
docs/plans/ledger-03-skos-jsonld.md
docs/plans/ledger-04-rdf-event-classes.md
docs/plans/ledger-06-semantic-bindings.md
```

These define the semantic vocabulary and concept governance.

---

### **3. Mapping Layer Contract (OBDA, Ontop, Mapping Rules)**
Files such as:

```
docs/plans/ontop-autogenerate-obda-mapping.md
docs/plans/ontop-mapping-drift-guard.md
docs/plans/ontop-expand-graph-coverage.md
docs/plans/replit-integrations-graph-metadata.md
```

These define how SQL → RDF mappings are generated and validated.

---

### **4. Temporal Semantics Contract**
Files such as:

```
docs/plans/temporal-parameter-contract.md
docs/plans/demand-linkage-and-horizon-extension.md
docs/plans/mrp-demand-supply-grid.md
```

These define horizon, netting, and time‑phased semantics.

---

### **5. Governed Business Logic Contract**
Files such as:

```
docs/plans/customer-order-demand-kb.md
docs/plans/job-costing-ledger.md
docs/plans/gl-perspective-ground-truth.md
docs/plans/procurement-gl-posting.md
docs/plans/three-way-match-completion.md
docs/plans/twm-spine-consolidation-finish.md
docs/plans/wo-close-wip-relief.md
```

These encode the SME logic behind the SQL views.

---

# 🎯 **Why this matters for the coding agent**

Your coding agent needs this directory because:

- It is the **source of human intent** behind every mapping.  
- It is the **semantic grounding** for the synthetic repo.  
- It is the **reference set** for the ontology mosaic.  
- It is the **governance layer** for the new template registry.  
- It is the **context** for the Validation & Expansion Pipe (VEP).

When the coding agent restores the repo, this folder must be preserved exactly as‑is.

---

