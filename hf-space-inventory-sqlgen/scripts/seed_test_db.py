"""Seed the local manufacturing.db with minimal schema rows required by tests.
This script is idempotent and will add missing columns if tables already exist.
"""
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(__file__))
DB = os.path.join(ROOT, "app_schema", "manufacturing.db")

def ensure_column(conn, table, column, definition):
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Create tables if missing (minimal definitions)
cur.execute("CREATE TABLE IF NOT EXISTS schema_intents (intent_id INTEGER PRIMARY KEY AUTOINCREMENT, intent_name TEXT UNIQUE, description TEXT, typical_question TEXT, primary_binding_key TEXT)")
# schema_concepts has NO concept_type: metric-ness is duck typed on
# computation_template (kept here so metric queries work against a dev DB).
cur.execute("CREATE TABLE IF NOT EXISTS schema_concepts (concept_id INTEGER PRIMARY KEY AUTOINCREMENT, concept_name TEXT UNIQUE, description TEXT, computation_template TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS schema_intent_concepts (intent_concept_id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id INTEGER, concept_id INTEGER, intent_factor_weight REAL)")
cur.execute("CREATE TABLE IF NOT EXISTS schema_perspectives (perspective_id INTEGER PRIMARY KEY AUTOINCREMENT, perspective_name TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS schema_intent_perspectives (id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id INTEGER, perspective_id INTEGER, intent_factor_weight REAL)")
cur.execute("CREATE TABLE IF NOT EXISTS schema_concept_fields (id INTEGER PRIMARY KEY AUTOINCREMENT, concept_id INTEGER, table_name TEXT, field_name TEXT, context_hint TEXT)")

# Ensure expected columns exist (adds if missing)
ensure_column(conn, 'schema_intents', 'intent_category', 'TEXT')
ensure_column(conn, 'schema_intent_concepts', 'explanation', 'TEXT')
ensure_column(conn, 'schema_concepts', 'computation_template', 'TEXT')
# schema_sqlite.sql INSERTs columns that seed_test_db.py's CREATE TABLE
# statements omit.  If any of these are missing, executescript() aborts on
# the first offending INSERT, ensure_app_metadata_tables() never runs, and
# sql_graph_nodes is never created — causing 500s in the MCP endpoints.
ensure_column(conn, 'schema_perspectives', 'description', 'TEXT')
ensure_column(conn, 'schema_perspectives', 'stakeholder_role', 'TEXT')
ensure_column(conn, 'schema_perspectives', 'priority_focus', 'TEXT')
ensure_column(conn, 'schema_concepts', 'domain', 'TEXT')
ensure_column(conn, 'schema_intent_perspectives', 'explanation', 'TEXT')

# Insert intent and concepts
cur.execute("INSERT OR IGNORE INTO schema_intents (intent_name, description) VALUES (?,?)", ("defect_cost_analysis","Detect cost-related defects"))
cur.execute("INSERT OR IGNORE INTO schema_concepts (concept_name, description) VALUES (?,?)", ("DefectSeverityCost","NCM cost"))
cur.execute("INSERT OR IGNORE INTO schema_concepts (concept_name, description) VALUES (?,?)", ("DefectSeverityQuality","Quality measure"))
cur.execute("INSERT OR IGNORE INTO schema_concepts (concept_name, description) VALUES (?,?)", ("DefectSeverityCustomer","Customer impact"))

# Resolve ids
cur.execute("SELECT intent_id FROM schema_intents WHERE intent_name = ?", ("defect_cost_analysis",))
row = cur.fetchone()
if not row:
    conn.commit()
    cur.execute("SELECT intent_id FROM schema_intents WHERE intent_name = ?", ("defect_cost_analysis",))
    row = cur.fetchone()
intent_id = row[0]

def cid(name):
    cur.execute("SELECT concept_id FROM schema_concepts WHERE concept_name = ?", (name,))
    r = cur.fetchone()
    return r[0]

cost_cid = cid("DefectSeverityCost")
qual_cid = cid("DefectSeverityQuality")
cust_cid = cid("DefectSeverityCustomer")

# Insert intent-concept weights: cost=1.0, others=0.0
cur.execute("INSERT OR REPLACE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation) VALUES (?,?,?,?)", (intent_id, cost_cid, 1.0, "Elevate cost"))
cur.execute("INSERT OR REPLACE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation) VALUES (?,?,?,?)", (intent_id, qual_cid, 0.0, "Neutral quality"))
cur.execute("INSERT OR REPLACE INTO schema_intent_concepts (intent_id, concept_id, intent_factor_weight, explanation) VALUES (?,?,?,?)", (intent_id, cust_cid, 0.0, "Neutral customer"))

# Perspectives
cur.execute("INSERT OR IGNORE INTO schema_perspectives (perspective_name) VALUES (?)", ("Finance",))
cur.execute("INSERT OR IGNORE INTO schema_perspectives (perspective_name) VALUES (?)", ("Quality",))
cur.execute("INSERT OR IGNORE INTO schema_perspectives (perspective_name) VALUES (?)", ("Customer",))
cur.execute("SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?", ("Finance",))
fin_pid = cur.fetchone()[0]
cur.execute("INSERT OR REPLACE INTO schema_intent_perspectives (intent_id, perspective_id, intent_factor_weight) VALUES (?,?,?)", (intent_id, fin_pid, 1.0))

# Concept fields mapping for generation
cur.execute("INSERT OR REPLACE INTO schema_concept_fields (concept_id, table_name, field_name, context_hint) VALUES (?,?,?,?)", (cost_cid, 'stg_manufacturing_flat', 'ncm_cost', 'NCM cost field'))
cur.execute("INSERT OR REPLACE INTO schema_concept_fields (concept_id, table_name, field_name, context_hint) VALUES (?,?,?,?)", (qual_cid, 'stg_manufacturing_flat', 'quality_score', 'Quality score'))
cur.execute("INSERT OR REPLACE INTO schema_concept_fields (concept_id, table_name, field_name, context_hint) VALUES (?,?,?,?)", (cust_cid, 'stg_manufacturing_flat', 'customer_impact_flag', 'Customer impact flag'))

conn.commit()
conn.close()

print("Seeded/updated manufacturing.db with minimal solder schema and weights.")
