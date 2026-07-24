---
name: Intent IDs unstable across fresh bootstraps
description: schema_intents autoincrement IDs differ between the live DB and fresh bootstraps — migrations must resolve intents by name, never hardcode IDs.
---

**Rule:** Never hardcode `schema_intents.intent_id` in migrations or wiring
code. Insert intents by name (`intent_name` is UNIQUE), resolve the real ID at
runtime, and key idempotent repairs on stable identifiers (query names /
binding keys / exact explanation strings — NOT `query_category` when the
category is shared across intents, e.g. `supplier_performance` is shared by
supplier_scorecard and supplier_payables_exposure).

**Why:** The schema seed inserts intents in file order on an empty table, so
fresh bootstraps give the ledger intents IDs 18–19, while on the live DB those
IDs belong to supplier_payables_exposure (18) and order_revenue_recognition
(19). Hardcoded-ID `INSERT OR IGNORE` conflicts on the PK and silently no-ops:
the intent never exists on fresh clones and its palette rows attach to
whatever ledger intent holds that ID. Receivables was fixed (name-based +
UPDATE OR REPLACE repair, 2026-07-24); **the payables side still has this bug
on fresh bootstraps** (hardcoded 18 in add_supplier_payables_wiring and the
uninvoiced-receipts / partial-receipt-accrual / twm-coverage palette
migrations) — live DB is unaffected and fail-closes on its placeholder
binding key. User was informed; no task created per their directive.

**How to apply:** Any new intent/palette migration: insert by name, `SELECT
intent_id WHERE intent_name = ?`, fail closed if missing, use the resolved ID
for all child rows, and add a fail-closed verify (the receivables pair is the
reference pattern). If asked to fix payables, repair by query NAME list, not
category.
