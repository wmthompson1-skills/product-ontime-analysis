"""
Manufacturing Inventory SQL Generator - Hugging Face Space
MCP-compliant FastAPI server for natural language to SQL conversion

This Space demonstrates:
1. MCP (Model Context Protocol) discovery pattern
2. Natural language to SQL generation for inventory management
3. Schema introspection and query validation
4. Manufacturing domain-specific SQL patterns
"""

import os
from pathlib import Path
from dotenv import load_dotenv

def _load_anchored_env():
    """Walk up from this file's directory looking for a .env file (up to 3 levels).
    Loads the first one found. Silently skips if none exists (e.g. Replit, HF Space)."""
    anchor = Path(__file__).resolve().parent
    for _ in range(3):
        candidate = anchor / '.env'
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return candidate
        anchor = anchor.parent
    return None

_load_anchored_env()

import json
import csv
import io
import re
import datetime
import tempfile
import warnings
import logging
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import gradio as gr
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import SQLAlchemyError
from solder_engine import SolderEngine, MetricAssemblyError
from production_dispatcher import ProductionDispatcher
from field_description_pipeline import draft_field_description, compute_field_coverage
import masking_matrix as mmx
import masking_type as mtx
from masking_policy_pipeline import (
    MASKING_STRATEGIES,
    suggest_masking_strategy,
    compute_masking_coverage,
)

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "app_schema")
QUERIES_DIR = os.path.join(SCHEMA_DIR, "queries")
QUERY_API_KEY = os.environ.get("QUERY_API_KEY", "")
SQLITE_DB_PATH = os.path.join(SCHEMA_DIR, "manufacturing.db")
db_engine = None

# Plan-009: four-part column key defaults used by the metadata write paths.
SQL_MCP_SOURCE_DATABASE = os.environ.get("SQL_MCP_SOURCE_DATABASE", "manufacturing")
SQL_MCP_DEFAULT_SCHEMA  = os.environ.get("SQL_MCP_DEFAULT_SCHEMA",  "dbo")

# Metadata tables excluded from the user-facing table list returned by
# get_all_tables().  They are internal infrastructure, not ERP source tables.
APP_METADATA_TABLES: set = {
    "api_field_descriptions",
    "schema_topology_metadata",
    "dab_field_definitions",
    "column_bindings",
    "column_masking_policies",
    "masking_matrix",
}


def _is_app_metadata_table(name: str) -> bool:
    """True for internal infrastructure tables hidden from the Schema Browser.

    Covers the explicit APP_METADATA_TABLES set plus every semantic-layer
    bridge table (schema_* prefix: schema_nodes, schema_edges, schema_intents,
    schema_concepts, …) — none of them are ERP source tables.
    """
    return name in APP_METADATA_TABLES or name.startswith("schema_")

from bridge_health import (
    BRIDGE_HEALTH_MAP,
    SCHEMA_NODES_HEALTH_MAP,
    run_bridge_health_check_impl as _bridge_health_check_impl,
    get_sweep1_coverage_gaps as _get_sweep1_coverage_gaps,
)


def get_db_engine():
    """Get or create SQLite database engine"""
    global db_engine
    if db_engine is None:
        db_engine = create_engine(f"sqlite:///{SQLITE_DB_PATH}")
        init_sqlite_db()
    return db_engine

def ensure_app_metadata_tables(conn) -> None:
    """Ensure Plan-009 metadata tables exist and handle legacy schema migration.

    Two concerns:
    (a) Legacy migration for api_field_descriptions — if the table exists but
        lacks source_database / schema_name columns (old two-column PK), rename
        it to _legacy, recreate with the four-column PK, migrate existing rows
        using SQL_MCP_SOURCE_DATABASE / SQL_MCP_DEFAULT_SCHEMA env defaults,
        then drop the legacy table.
    (b) Additive CREATE TABLE IF NOT EXISTS guards for all four metadata tables
        (api_field_descriptions, schema_topology_metadata, dab_field_definitions,
        column_bindings) so this function is safe to call on every startup.
    """
    import sqlite3

    cur = conn.cursor()

    # ── (a) legacy migration ──────────────────────────────────────────────────
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='api_field_descriptions'"
    )
    if cur.fetchone():
        cur.execute("PRAGMA table_info(api_field_descriptions)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "source_database" not in existing_cols or "schema_name" not in existing_cols:
            # Old schema detected — rename, recreate, migrate, drop.
            cur.execute(
                "ALTER TABLE api_field_descriptions RENAME TO api_field_descriptions_legacy"
            )
            cur.execute("""
                CREATE TABLE api_field_descriptions (
                    source_database TEXT    NOT NULL,
                    schema_name     TEXT    NOT NULL,
                    table_name      TEXT    NOT NULL,
                    column_name     TEXT    NOT NULL,
                    display_name    TEXT,
                    description     TEXT,
                    example_value   TEXT,
                    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source_database, schema_name, table_name, column_name)
                )
            """)
            # Migrate rows; fill missing key parts from env defaults.
            legacy_cols = existing_cols
            sel_cols = []
            for c in ("table_name", "column_name", "display_name", "description", "example_value"):
                sel_cols.append(c if c in legacy_cols else f"NULL AS {c}")
            cur.execute(f"""
                INSERT OR IGNORE INTO api_field_descriptions
                    (source_database, schema_name, table_name, column_name,
                     display_name, description, example_value)
                SELECT
                    '{SQL_MCP_SOURCE_DATABASE}',
                    '{SQL_MCP_DEFAULT_SCHEMA}',
                    {', '.join(sel_cols)}
                FROM api_field_descriptions_legacy
            """)
            cur.execute("DROP TABLE api_field_descriptions_legacy")
            conn.commit()
            print("api_field_descriptions: legacy schema migrated to four-column PK.")

    # ── (b) additive guards ───────────────────────────────────────────────────
    cur.executescript("""
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

        CREATE TABLE IF NOT EXISTS api_table_descriptions (
            source_database TEXT    NOT NULL,
            schema_name     TEXT    NOT NULL,
            table_name      TEXT    NOT NULL,
            display_name    TEXT,
            description     TEXT,
            ai_context      TEXT,
            updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (source_database, schema_name, table_name)
        );

        CREATE TABLE IF NOT EXISTS schema_topology_metadata (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_node_type TEXT    NOT NULL
                CHECK(source_node_type IN ('database', 'schema', 'table', 'column')),
            target_node_type TEXT    NOT NULL
                CHECK(target_node_type IN ('database', 'schema', 'table', 'column')),
            source_key       TEXT    NOT NULL,
            target_key       TEXT    NOT NULL,
            edge_predicate   TEXT    NOT NULL DEFAULT 'HAS_COLUMN',
            weight           INTEGER NOT NULL DEFAULT 1,
            notes            TEXT,
            created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_node_type, target_node_type, source_key, target_key, edge_predicate)
        );

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

        CREATE TABLE IF NOT EXISTS column_bindings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            intent_name TEXT    NOT NULL,
            slot_name   TEXT    NOT NULL,
            table_name  TEXT    NOT NULL,
            column_name TEXT    NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(intent_name, slot_name)
        );

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

        CREATE TABLE IF NOT EXISTS masking_type (
            masking_type TEXT    NOT NULL PRIMARY KEY,
            masking_mode INTEGER NOT NULL DEFAULT 0,
            status       TEXT    NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'inactive'))
        );

        CREATE TABLE IF NOT EXISTS sql_graph_nodes (
            ordinal       INTEGER NOT NULL,
            _key          TEXT    NOT NULL PRIMARY KEY,
            _id           TEXT    NOT NULL,
            node_type     TEXT    NOT NULL CHECK(node_type IN ('table', 'column', 'concept')),
            node_family   TEXT    NOT NULL,
            perspective   TEXT    NOT NULL,
            table_name    TEXT,
            column_name   TEXT,
            column_slot   TEXT,
            concept_name  TEXT,
            concept_type  TEXT,
            domain        TEXT,
            synonyms      TEXT,
            tags          TEXT,
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
    """)

    # ── (c) additive column guards for already-existing tables ─────────────────
    # CREATE TABLE IF NOT EXISTS never adds a column to a table that already
    # exists, so widen the source-of-truth tables in place for older databases.
    def _add_column_if_missing(table: str, column: str, decl: str) -> None:
        cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            return
        cur.execute(f"PRAGMA table_info({table})")
        if column not in {row[1] for row in cur.fetchall()}:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")

    def _drop_column_if_present(table: str, column: str) -> None:
        cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            return
        cur.execute(f"PRAGMA table_info({table})")
        if column in {row[1] for row in cur.fetchall()}:
            cur.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    def _migrate_edge_type_check(table: str) -> None:
        """Rebuild a graph edge table whose edge_type CHECK predates the v16 rename.

        SQLite fixes a CHECK at CREATE time, so a table created before the
        canonical column->concept predicate was renamed to ``resolves_to`` still
        admits only the legacy token — a ``resolves_to`` insert then fails its
        CHECK, and CREATE TABLE IF NOT EXISTS cannot repair it. Rebuild the table
        from its own (token-swapped) DDL, carrying rows over and translating any
        legacy edge_type value so SME-authored relationships are preserved.
        """
        cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
        row = cur.fetchone()
        if not row or not row[0]:
            return
        ddl = row[0]
        if "'resolves_to'" in ddl or "'elevates'" not in ddl:
            return
        new_ddl = ddl.replace("'elevates'", "'resolves_to'")
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
        col_list = ", ".join(f'"{c}"' for c in cols)
        select_list = ", ".join(
            ("CASE WHEN edge_type='elevates' THEN 'resolves_to' ELSE edge_type END"
             if c == "edge_type" else f'"{c}"')
            for c in cols
        )
        tmp = f"{table}__pre_v16"
        cur.execute(f"DROP TABLE IF EXISTS {tmp}")
        cur.execute(f"ALTER TABLE {table} RENAME TO {tmp}")
        cur.execute(new_ddl)
        cur.execute(f"INSERT INTO {table} ({col_list}) SELECT {select_list} FROM {tmp}")
        cur.execute(f"DROP TABLE {tmp}")

    # Field-definition number on each field's elevation (1 = primary; 2,3.. = further meanings).
    _add_column_if_missing(
        "schema_concept_fields", "component_index",
        "component_index INTEGER NOT NULL DEFAULT 1",
    )
    # field_component on resolves_to edges mirrors schema_concept_fields.component_index.
    _add_column_if_missing("sql_graph_edges", "field_component", "field_component INTEGER")
    # priority_weight on resolves_to edges — non-gating SME priority kept beside the binary weight (M2).
    _add_column_if_missing("sql_graph_edges", "priority_weight", "priority_weight INTEGER")
    # concept_name on concept nodes (node_type='concept'); NULL for table/column rows.
    _add_column_if_missing("sql_graph_nodes", "concept_name", "concept_name TEXT")
    # M3: richer concept-node payload — concept_type / domain / synonyms / tags.
    # NULL for table/column rows; synonyms/tags hold canonical JSON arrays.
    _add_column_if_missing("sql_graph_nodes", "concept_type", "concept_type TEXT")
    _add_column_if_missing("sql_graph_nodes", "domain", "domain TEXT")
    _add_column_if_missing("sql_graph_nodes", "synonyms", "synonyms TEXT")
    _add_column_if_missing("sql_graph_nodes", "tags", "tags TEXT")
    # M3: schema_concepts gains synonyms / tags (canonical JSON arrays) so the
    # exporter can surface the richer concept payload from older databases too.
    _add_column_if_missing("schema_concepts", "synonyms", "synonyms TEXT")
    _add_column_if_missing("schema_concepts", "tags", "tags TEXT")
    # M2: the resolves_to edge no longer stores a concept string — identity lives on
    # the _to concept node — so drop the legacy column from older edges tables. No
    # compatibility window, so the app's shape matches the exporter's rebuilt one.
    _drop_column_if_present("sql_graph_edges", "concept")

    # v16: rebuild edge tables whose edge_type CHECK predates the resolves_to
    # rename so authored/derived ``resolves_to`` rows pass the constraint.
    try:
        _migrate_edge_type_check("sql_graph_edges")
        _migrate_edge_type_check("sql_graph_authored_edges")
    except Exception as _mig_err:
        print(f"[ensure_app_metadata_tables] edge_type CHECK migration skipped: {_mig_err}")

    # View ontology table — stores the ontological structure (tables, joins,
    # predicates, grain, time-phasing) extracted from the 7 MRP ground-truth views.
    try:
        from view_ontology_extractor import create_view_ontology_table
        create_view_ontology_table(conn)
    except Exception as _vo_err:
        print(f"[ensure_app_metadata_tables] view_ontology table skipped: {_vo_err}")

    conn.commit()


def _widen_table_columns(conn, table: str, columns: dict) -> None:
    """Additively add any missing columns to an existing table.

    No-op when the table does not exist yet or the column is already present.
    SQLite cannot ALTER ADD a NOT NULL column without a default, so callers
    pass nullable column declarations; the idempotent seed then supplies values.
    """
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if not cur.fetchone():
        return
    existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, decl in columns.items():
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")


def init_sqlite_db():
    """Initialize SQLite database from schema file.
    
    Executes both CREATE TABLE and INSERT statements to ensure
    seed data (concepts, perspectives, etc.) is loaded on first run.
    Uses sqlite3 module directly for proper multi-statement handling.
    """
    import sqlite3
    
    schema_file = os.path.join(SCHEMA_DIR, "schema_sqlite.sql")
    if not os.path.exists(schema_file):
        return
    
    with open(schema_file, 'r') as f:
        schema_sql = f.read()
    
    # Replace INSERT with INSERT OR IGNORE for idempotent seeding
    schema_sql = schema_sql.replace('INSERT INTO', 'INSERT OR IGNORE INTO')
    
    # Use sqlite3 directly for executescript (handles multi-line statements)
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        # Self-heal older databases BEFORE seeding. CREATE TABLE IF NOT EXISTS
        # never widens a table that already exists, so a database created by an
        # older schema (e.g. schema_concepts missing domain or
        # computation_template) makes the seed INSERT or the metric duck-typing
        # queries fail. executescript aborts on that first error, so every table
        # defined after schema_concepts is never created and the resolve
        # endpoints then raise 500 ("no such table"). Widen the columns the seed
        # and the metric queries reference first so the idempotent seed runs to
        # completion. concept_type is intentionally NOT re-added: it was
        # DEPRECATED & REMOVED from the semantic layer (metric-ness is duck typed
        # on computation_template). No-op on a fresh DB: the table does not exist
        # yet and is created in full below.
        try:
            _widen_table_columns(
                conn,
                "schema_concepts",
                {
                    "description": "description TEXT",
                    "domain": "domain TEXT",
                    "computation_template": "computation_template TEXT",
                },
            )
            # work_order gained routing-derived scheduling columns (Task #205):
            # scheduled start/finish (the WO window derived from its operations)
            # and a planner release anchor. CREATE TABLE IF NOT EXISTS never widens
            # an existing table, so add them in place for older databases. Nullable
            # so the ALTER succeeds; migrations/backfill_operation_schedule.py
            # supplies the values.
            _widen_table_columns(
                conn,
                "work_order",
                {
                    "desired_rls_date": "desired_rls_date DATETIME",
                    "sched_start_date": "sched_start_date DATETIME",
                    "sched_finish_date": "sched_finish_date DATETIME",
                    # Outside-service header enrichment (MRP data foundation).
                    # Display-only; set by migrations/backfill_mrp_demand_supply.py.
                    "service_date": "service_date DATE",
                    "vendor_id": "vendor_id TEXT",
                    # Demand-source linkage (Task #244): nullable declared FK to
                    # customer_order_line.order_line_id; values set by
                    # migrations/add_demand_linkage_and_forecast.py.
                    "demand_order_line_id":
                        "demand_order_line_id INTEGER "
                        "REFERENCES customer_order_line (order_line_id)",
                },
            )
            # `part` gained a native item-master planner column (the owning
            # material planner). CREATE TABLE IF NOT EXISTS never widens an
            # existing table, so add it in place for older databases. The
            # DEFAULT matches schema_sqlite.sql so graph re-exports stay
            # reproducible (PRAGMA default_value is identical on both paths).
            _widen_table_columns(
                conn,
                "part",
                {
                    "planner_code": "planner_code TEXT DEFAULT 'ENGINEERING'",
                    # Owning buyer (EMPLOYEE.buyer_code). Nullable, no default:
                    # in-house MAKE parts are not bought by anyone. Values are
                    # backfilled by migrations/add_employees_and_buyers.py.
                    "buyer_code": "buyer_code TEXT",
                    # SME rule (Task #244): safety stock is exactly 1 for
                    # planning parts. ADD COLUMN with DEFAULT fills existing
                    # rows with 1 on older databases.
                    "safety_stock": "safety_stock REAL DEFAULT 1",
                },
            )
            conn.commit()
        except Exception as e:
            print(f"Database migration warning: {e}")

        conn.executescript(schema_sql)
        conn.commit()
        ensure_app_metadata_tables(conn)
    except Exception as e:
        # Log but don't fail - some statements may already exist
        print(f"Database init warning: {e}")
    finally:
        conn.close()

    # Keep the masking matrix CSV (masking_matrix.csv at the repo root) and the
    # SQLite masking_matrix table in sync on every startup: load the human-editable
    # CSV into SQLite (idempotent upsert). The CSV is the SME-facing copy used for
    # approval; the app can also write it back. Wrapped so a missing or hand-edited
    # CSV never blocks boot.
    try:
        from masking_matrix import load_matrix_from_csv, DEFAULT_CSV_PATH
        if os.path.exists(DEFAULT_CSV_PATH):
            _mm = load_matrix_from_csv(csv_path=DEFAULT_CSV_PATH, db_path=SQLITE_DB_PATH)
            if _mm.get("ok"):
                print(f"masking_matrix: synced {_mm.get('loaded', 0)} row(s) from CSV.")
            else:
                print(f"masking_matrix sync warning: {_mm.get('error')}")
    except Exception as e:
        print(f"masking_matrix sync warning: {e}")

    # Same for the masking-type reference lookup (masking_type.csv at the repo
    # root <-> SQLite masking_type table). Idempotent upsert; never blocks boot.
    try:
        from masking_type import load_types_from_csv, DEFAULT_CSV_PATH as _MT_CSV
        if os.path.exists(_MT_CSV):
            _mt = load_types_from_csv(csv_path=_MT_CSV, db_path=SQLITE_DB_PATH)
            if _mt.get("ok"):
                print(f"masking_type: synced {_mt.get('loaded', 0)} row(s) from CSV.")
            else:
                print(f"masking_type sync warning: {_mt.get('error')}")
    except Exception as e:
        print(f"masking_type sync warning: {e}")

    # Same pattern for the field descriptions (field_descriptions.csv at the repo
    # root <-> SQLite api_field_descriptions overlay). This committed, SME-editable
    # CSV covers every canonical-graph column node; loading it on boot keeps the
    # plain-language descriptions alive across a gitignored-DB rebuild. Idempotent
    # upsert; never blocks boot.
    try:
        from field_description_pipeline import (
            load_descriptions_from_csv, DEFAULT_CSV_PATH as _FD_CSV,
        )
        if os.path.exists(_FD_CSV):
            _fd = load_descriptions_from_csv(csv_path=_FD_CSV, db_path=SQLITE_DB_PATH)
            if _fd.get("ok"):
                print(f"field_descriptions: synced {_fd.get('loaded', 0)} row(s) from CSV.")
            else:
                print(f"field_descriptions sync warning: {_fd.get('error')}")
    except Exception as e:
        print(f"field_descriptions sync warning: {e}")

    # Same pattern for the table-level meta-context (table_descriptions.csv at the
    # repo root <-> SQLite api_table_descriptions overlay). This is the AI
    # meta-context about the showcase tables a metric draws from. OVERLAY ONLY:
    # it is never written onto the canonical-graph table/column nodes, so
    # graph_metadata.json stays byte-identical. Idempotent upsert; never blocks boot.
    try:
        from table_description_pipeline import (
            load_descriptions_from_csv as _load_tbl_csv,
            DEFAULT_CSV_PATH as _TD_CSV,
        )
        if os.path.exists(_TD_CSV):
            _td = _load_tbl_csv(csv_path=_TD_CSV, db_path=SQLITE_DB_PATH)
            if _td.get("ok"):
                print(f"table_descriptions: synced {_td.get('loaded', 0)} row(s) from CSV.")
            else:
                print(f"table_descriptions sync warning: {_td.get('error')}")
    except Exception as e:
        print(f"table_descriptions sync warning: {e}")

    # Load the SKOS job-costing ledger concepts (committed JSON-LD at
    # poc/ontop-ontology-poc/ontology/ledger_skos.jsonld) into the in-memory
    # read-only store the semantic layer queries. The loader FAILS CLOSED on
    # malformed input (SkosLoadError); a load failure is surfaced loudly here
    # but does not block boot — consumers calling the accessor later will get
    # the same fail-closed error rather than a partial scheme.
    try:
        from skos_ledger import get_ledger_concept_store
        _skos = get_ledger_concept_store(reload=True)
        print(
            f"skos_ledger: loaded {len(_skos.all_concepts())} concept(s) "
            f"({len(_skos.top_concepts())} top) from {_skos.scheme_label!r}."
        )
    except Exception as e:
        print(f"skos_ledger LOAD FAILED (fail-closed, concepts unavailable): {e}")

    # Load the corpus vocabulary SKOS scheme (committed JSON-LD at
    # poc/ontop-ontology-poc/ontology/corpus_vocab_skos.jsonld): entity /
    # practice terms, the work-order lifecycle progression, time-based derived
    # terms, and forbidden-synonym governance. Same fail-closed contract as
    # the ledger scheme: a load failure is loud but never blocks boot.
    try:
        from corpus_vocab import get_corpus_vocab_store
        _cv = get_corpus_vocab_store(reload=True)
        print(
            f"corpus_vocab: loaded {len(_cv.all_concepts())} concept(s), "
            f"{len(_cv.all_collections())} collection(s) from "
            f"{_cv.scheme_label!r}."
        )
    except Exception as e:
        print(f"corpus_vocab LOAD FAILED (fail-closed, vocabulary unavailable): {e}")

    # Load the governed ledger binding map (committed JSON at
    # poc/ontop-ontology-poc/ledger_binding_map.json) into its read-only
    # store: SKOS concept URI -> gl_* table, RDF event class ->
    # gl_events.event_type. FAILS CLOSED on any cross-layer inconsistency
    # (LedgerBindingError); surfaced loudly here but never blocks boot.
    try:
        from ledger_bindings import get_ledger_binding_store
        _lb = get_ledger_binding_store(reload=True)
        print(
            f"ledger_bindings: loaded {len(_lb.concept_bindings)} concept->table "
            f"and {len(_lb.event_bindings)} event-class->event_type binding(s)."
        )
    except Exception as e:
        print(f"ledger_bindings LOAD FAILED (fail-closed, bindings unavailable): {e}")

    # Extract the embedded ontological structure from the 7 MRP ground-truth SQL
    # views plus the 5 governed General_Ledger queries and seed
    # sql_view_ontology. INSERT OR REPLACE — idempotent on every boot, safe to
    # re-run. Never blocks boot.
    try:
        import sqlite3 as _vo_sqlite3
        from view_ontology_extractor import (
            extract_all_mrp_views,
            extract_all_ledger_views,
            seed_view_ontology_table,
        )
        _vo_base = os.path.dirname(os.path.abspath(__file__))
        _vo_manifest = os.path.join(_vo_base, "app_schema", "ground_truth", "reviewer_manifest.json")
        if os.path.exists(_vo_manifest):
            _vos = extract_all_mrp_views(_vo_manifest, _vo_base)
            _vos += extract_all_ledger_views(_vo_manifest, _vo_base)
            with _vo_sqlite3.connect(SQLITE_DB_PATH) as _vc:
                _vn = seed_view_ontology_table(_vc, _vos)
            print(f"view_ontology: seeded {_vn} view(s) into sql_view_ontology.")
    except Exception as e:
        print(f"view_ontology seed warning: {e}")

    # Index per-table usage for every APPROVED reviewer-manifest snippet into
    # ground_truth_table_usage (idempotent upsert). Keeps the Ground Truth
    # mosaic's table-usage log complete for snippets living outside
    # QUERIES_DIR (e.g. the governed ledger queries). Never blocks boot.
    try:
        _gt_engine = SolderEngine()
        _gt_summary = _gt_engine.index_snippet_table_usage(verbose=False)
        print(f"snippet_table_usage: indexed {len(_gt_summary)} approved snippet(s).")
    except Exception as e:
        print(f"snippet_table_usage index warning: {e}")

def get_table_create_sql(table_name: str) -> str:
    """Generate CREATE TABLE SQL for a given table (SQLite version)"""
    engine = get_db_engine()
    if not engine:
        return "-- Database not connected"
    
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT sql FROM sqlite_master WHERE type='table' AND name=:table_name"), {"table_name": table_name})
            row = result.fetchone()
            if row and row[0]:
                return row[0]
            return f"-- Table '{table_name}' not found"
    except Exception as e:
        return f"-- Error: {str(e)}"

def get_table_create_sql_legacy(table_name: str) -> str:
    """Generate CREATE TABLE SQL for a given table (PostgreSQL version - deprecated)"""
    engine = get_db_engine()
    if not engine:
        return "-- Database not connected"
    
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT column_name, data_type, character_maximum_length, 
                       is_nullable, column_default
                FROM information_schema.columns 
                WHERE table_schema = 'public' AND table_name = :table_name
                ORDER BY ordinal_position
            """), {"table_name": table_name})
            columns = result.fetchall()
            
            if not columns:
                return f"-- Table '{table_name}' not found"
            
            col_defs = []
            for col in columns:
                col_name, data_type, max_len, nullable, default = col
                type_str = data_type.upper()
                if max_len:
                    type_str = f"{type_str}({max_len})"
                null_str = "" if nullable == "YES" else " NOT NULL"
                default_str = f" DEFAULT {default}" if default else ""
                col_defs.append(f"    {col_name} {type_str}{null_str}{default_str}")
            
            pk_result = conn.execute(text("""
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu 
                    ON tc.constraint_name = kcu.constraint_name 
                    AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY' 
                    AND tc.table_schema = 'public'
                    AND tc.table_name = :table_name
                ORDER BY kcu.ordinal_position
            """), {"table_name": table_name})
            pk_cols = [row[0] for row in pk_result.fetchall()]
            
            if pk_cols:
                col_defs.append(f"    PRIMARY KEY ({', '.join(pk_cols)})")
            
            return f"CREATE TABLE {table_name} (\n" + ",\n".join(col_defs) + "\n);"
    except Exception as e:
        return f"-- Error: {str(e)}"

def get_all_tables() -> List[str]:
    """Get list of ERP/structural tables in the SQLite database.

    Internal infrastructure tables (APP_METADATA_TABLES plus the schema_*
    bridge tables) are excluded so they never appear in the Schema Browser
    or ground-truth table list.
    Source: SQLite PRAGMA table_info — same source used by structural
    containment graph sync.
    """
    engine = get_db_engine()
    if not engine:
        return []
    
    try:
        inspector = inspect(engine)
        inspector.clear_cache()
        return [t for t in inspector.get_table_names() if not _is_app_metadata_table(t)]
    except Exception:
        return []

def _get_structural_schema_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return the structural schema as a nested dict keyed by table → column.

    Built from SQLite PRAGMA table_info — this is the immutable structural
    base used by get_unified_schema().  Only ERP tables are included
    (internal infrastructure tables are excluded, matching get_all_tables()).

    Shape:
      {
        "TABLE_NAME": {
          "column_name": {
            "type": "TEXT",
            "notnull": 0,
            "pk": 0,
          },
          ...
        },
        ...
      }
    """
    import sqlite3
    snapshot: Dict[str, Dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cur.fetchall() if not _is_app_metadata_table(row[0])]
        for tbl in tables:
            cur.execute(f"PRAGMA table_info({tbl})")
            cols: Dict[str, Any] = {}
            for row in cur.fetchall():
                # row: (cid, name, type, notnull, dflt_value, pk)
                cols[row[1]] = {
                    "type":    row[2] or "TEXT",
                    "notnull": row[3],
                    "pk":      row[5],
                }
            if cols:
                snapshot[tbl] = cols
        conn.close()
    except Exception as exc:
        print(f"_get_structural_schema_snapshot warning: {exc}")
    return snapshot


def get_unified_schema() -> Dict[str, Dict[str, Any]]:
    """Return the structural schema enriched with Plan-009 metadata overlays.

    Layer order:
      1. STRUCTURAL BASE — PRAGMA table_info for all ERP tables.
      2. api_field_descriptions overlay — adds display_name, description,
         example_value per column (matched on table_name + column_name within
         the configured source_database / schema_name defaults).
      3. dab_field_definitions overlay — adds field_definition, certified.
      4. column_masking_policies overlay — adds masking_strategy,
         masking_rationale, masking_certified.

    Orphaned metadata rows (no matching structural column) are silently skipped
    so stale rows never corrupt the output.

    Return shape mirrors _get_structural_schema_snapshot() but with extra keys:
      {
        "TABLE_NAME": {
          "column_name": {
            "type": "TEXT", "notnull": 0, "pk": 0,
            "display_name": ..., "description": ..., "example_value": ...,
            "field_definition": ..., "certified": 0,
            "masking_strategy": ..., "masking_rationale": ...,
            "masking_certified": 0,
          }
        }
      }
    """
    import sqlite3
    schema = _get_structural_schema_snapshot()
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()

        # ── overlay 1: api_field_descriptions ─────────────────────────────────
        cur.execute("""
            SELECT table_name, column_name, display_name, description, example_value
            FROM   api_field_descriptions
            WHERE  source_database = ? AND schema_name = ?
        """, (SQL_MCP_SOURCE_DATABASE, SQL_MCP_DEFAULT_SCHEMA))
        for tbl, col, display_name, desc, example in cur.fetchall():
            if tbl in schema and col in schema[tbl]:
                schema[tbl][col]["display_name"]  = display_name
                schema[tbl][col]["description"]   = desc
                schema[tbl][col]["example_value"] = example

        # ── overlay 2: dab_field_definitions ──────────────────────────────────
        cur.execute("""
            SELECT table_name, column_name, field_definition, certified
            FROM   dab_field_definitions
            WHERE  source_database = ? AND schema_name = ?
        """, (SQL_MCP_SOURCE_DATABASE, SQL_MCP_DEFAULT_SCHEMA))
        for tbl, col, field_def, certified in cur.fetchall():
            if tbl in schema and col in schema[tbl]:
                schema[tbl][col]["field_definition"] = field_def
                schema[tbl][col]["certified"]        = certified

        # ── overlay 4: column_masking_policies ────────────────────────────────
        cur.execute("""
            SELECT table_name, column_name, masking_strategy, rationale, certified
            FROM   column_masking_policies
            WHERE  source_database = ? AND schema_name = ?
        """, (SQL_MCP_SOURCE_DATABASE, SQL_MCP_DEFAULT_SCHEMA))
        for tbl, col, strategy, rationale, mask_cert in cur.fetchall():
            if tbl in schema and col in schema[tbl]:
                schema[tbl][col]["masking_strategy"]  = strategy
                schema[tbl][col]["masking_rationale"] = rationale
                schema[tbl][col]["masking_certified"] = mask_cert

        conn.close()
    except Exception as exc:
        print(f"get_unified_schema warning: {exc}")
    return schema


# ── metadata read/write helpers ────────────────────────────────────────────────

def _validate_column_exists(table_name: str, column_name: str) -> bool:
    """Return True if (table_name, column_name) exists in the structural schema."""
    snap = _get_structural_schema_snapshot()
    return table_name in snap and column_name in snap[table_name]


def get_field_description_record(
    table_name: str,
    column_name: str,
    source_database: str = SQL_MCP_SOURCE_DATABASE,
    schema_name: str = SQL_MCP_DEFAULT_SCHEMA,
) -> Optional[Dict[str, Any]]:
    """Return the api_field_descriptions row for a column, or None if absent."""
    import sqlite3
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT display_name, description, example_value, updated_at
            FROM   api_field_descriptions
            WHERE  source_database = ? AND schema_name = ?
              AND  table_name = ? AND column_name = ?
        """, (source_database, schema_name, table_name, column_name))
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "source_database": source_database,
                "schema_name":     schema_name,
                "table_name":      table_name,
                "column_name":     column_name,
                "display_name":    row[0],
                "description":     row[1],
                "example_value":   row[2],
                "updated_at":      row[3],
            }
        return None
    except Exception as exc:
        print(f"get_field_description_record warning: {exc}")
        return None


def save_field_description(
    table_name: str,
    column_name: str,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    example_value: Optional[str] = None,
    source_database: str = SQL_MCP_SOURCE_DATABASE,
    schema_name: str = SQL_MCP_DEFAULT_SCHEMA,
) -> Dict[str, Any]:
    """Upsert an api_field_descriptions row.

    Validates that (table_name, column_name) exists in the structural schema
    before writing.  Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    import sqlite3
    if not _validate_column_exists(table_name, column_name):
        return {"ok": False, "error": f"Column '{table_name}.{column_name}' not found in structural schema."}
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.execute("""
            INSERT INTO api_field_descriptions
                (source_database, schema_name, table_name, column_name,
                 display_name, description, example_value, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_database, schema_name, table_name, column_name)
            DO UPDATE SET
                display_name  = excluded.display_name,
                description   = excluded.description,
                example_value = excluded.example_value,
                updated_at    = CURRENT_TIMESTAMP
        """, (source_database, schema_name, table_name, column_name,
              display_name, description, example_value))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_dab_field_definition_record(
    table_name: str,
    column_name: str,
    source_database: str = SQL_MCP_SOURCE_DATABASE,
    schema_name: str = SQL_MCP_DEFAULT_SCHEMA,
) -> Optional[Dict[str, Any]]:
    """Return the dab_field_definitions row for a column, or None if absent."""
    import sqlite3
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT field_definition, certified, updated_at
            FROM   dab_field_definitions
            WHERE  source_database = ? AND schema_name = ?
              AND  table_name = ? AND column_name = ?
        """, (source_database, schema_name, table_name, column_name))
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "source_database":  source_database,
                "schema_name":      schema_name,
                "table_name":       table_name,
                "column_name":      column_name,
                "field_definition": row[0],
                "certified":        bool(row[1]),
                "updated_at":       row[2],
            }
        return None
    except Exception as exc:
        print(f"get_dab_field_definition_record warning: {exc}")
        return None


def save_dab_field_definition(
    table_name: str,
    column_name: str,
    field_definition: Optional[str] = None,
    certified: bool = False,
    source_database: str = SQL_MCP_SOURCE_DATABASE,
    schema_name: str = SQL_MCP_DEFAULT_SCHEMA,
) -> Dict[str, Any]:
    """Upsert a dab_field_definitions row.

    Validates structural column existence before writing.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    import sqlite3
    if not _validate_column_exists(table_name, column_name):
        return {"ok": False, "error": f"Column '{table_name}.{column_name}' not found in structural schema."}
    certified_int = 1 if certified else 0
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.execute("""
            INSERT INTO dab_field_definitions
                (source_database, schema_name, table_name, column_name,
                 field_definition, certified, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_database, schema_name, table_name, column_name)
            DO UPDATE SET
                field_definition = excluded.field_definition,
                certified        = excluded.certified,
                updated_at       = CURRENT_TIMESTAMP
        """, (source_database, schema_name, table_name, column_name,
              field_definition, certified_int))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_column_masking_record(
    table_name: str,
    column_name: str,
    source_database: str = SQL_MCP_SOURCE_DATABASE,
    schema_name: str = SQL_MCP_DEFAULT_SCHEMA,
) -> Optional[Dict[str, Any]]:
    """Return the column_masking_policies row for a column, or None if absent."""
    import sqlite3
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT masking_strategy, rationale, certified, updated_at
            FROM   column_masking_policies
            WHERE  source_database = ? AND schema_name = ?
              AND  table_name = ? AND column_name = ?
        """, (source_database, schema_name, table_name, column_name))
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "source_database":  source_database,
                "schema_name":      schema_name,
                "table_name":       table_name,
                "column_name":      column_name,
                "masking_strategy": row[0],
                "rationale":        row[1],
                "certified":        bool(row[2]),
                "updated_at":       row[3],
            }
        return None
    except Exception as exc:
        print(f"get_column_masking_record warning: {exc}")
        return None


def save_column_masking_policy(
    table_name: str,
    column_name: str,
    masking_strategy: str,
    rationale: Optional[str] = None,
    source_database: str = SQL_MCP_SOURCE_DATABASE,
    schema_name: str = SQL_MCP_DEFAULT_SCHEMA,
) -> Dict[str, Any]:
    """Upsert a column_masking_policies row (the *save* step).

    Validates structural column existence and the strategy vocabulary before
    writing. Never changes the certified flag (so saving does not un-certify).
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    import sqlite3
    if not _validate_column_exists(table_name, column_name):
        return {"ok": False, "error": f"Column '{table_name}.{column_name}' not found in structural schema."}
    strategy = (masking_strategy or "none").lower()
    if strategy not in MASKING_STRATEGIES:
        return {"ok": False, "error": f"Unknown masking strategy: {masking_strategy!r}"}
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.execute("""
            INSERT INTO column_masking_policies
                (source_database, schema_name, table_name, column_name,
                 masking_strategy, rationale, certified, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(source_database, schema_name, table_name, column_name)
            DO UPDATE SET
                masking_strategy = excluded.masking_strategy,
                rationale        = excluded.rationale,
                updated_at       = CURRENT_TIMESTAMP
        """, (source_database, schema_name, table_name, column_name,
              strategy, rationale))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def certify_column_masking_policy(
    table_name: str,
    column_name: str,
    masking_strategy: str,
    rationale: Optional[str] = None,
    certified: bool = False,
    source_database: str = SQL_MCP_SOURCE_DATABASE,
    schema_name: str = SQL_MCP_DEFAULT_SCHEMA,
) -> Dict[str, Any]:
    """Upsert a column_masking_policies row including the certified flag.

    Validates structural column existence and the strategy vocabulary before
    writing. Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    import sqlite3
    if not _validate_column_exists(table_name, column_name):
        return {"ok": False, "error": f"Column '{table_name}.{column_name}' not found in structural schema."}
    strategy = (masking_strategy or "none").lower()
    if strategy not in MASKING_STRATEGIES:
        return {"ok": False, "error": f"Unknown masking strategy: {masking_strategy!r}"}
    certified_int = 1 if certified else 0
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.execute("""
            INSERT INTO column_masking_policies
                (source_database, schema_name, table_name, column_name,
                 masking_strategy, rationale, certified, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_database, schema_name, table_name, column_name)
            DO UPDATE SET
                masking_strategy = excluded.masking_strategy,
                rationale        = excluded.rationale,
                certified        = excluded.certified,
                updated_at       = CURRENT_TIMESTAMP
        """, (source_database, schema_name, table_name, column_name,
              strategy, rationale, certified_int))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_readonly_sql(sql: str) -> Dict[str, Any]:
    """Execute read-only SQL query (SELECT only)"""
    engine = get_db_engine()
    if not engine:
        return {"error": "Database not connected", "rows": [], "columns": []}
    
    sql_stripped = sql.strip().upper()
    if not sql_stripped.startswith("SELECT"):
        return {"error": "Only SELECT queries are allowed for safety", "rows": [], "columns": []}
    
    dangerous_keywords = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "CREATE"]
    for keyword in dangerous_keywords:
        if keyword in sql_stripped:
            return {"error": f"Query contains forbidden keyword: {keyword}", "rows": [], "columns": []}
    
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            rows = [list(row) for row in result.fetchmany(100)]
            return {"error": None, "columns": columns, "rows": rows}
    except SQLAlchemyError as e:
        return {"error": str(e), "rows": [], "columns": []}

def count_queries_in_file(sql_file_path: str) -> int:
    """Count the number of queries in a SQL file by counting '-- Query:' markers"""
    if not os.path.exists(sql_file_path):
        return 0
    try:
        with open(sql_file_path, 'r') as f:
            content = f.read()
        return content.count('-- Query:')
    except Exception:
        return 0

def get_query_categories() -> Dict[str, Any]:
    """Load query index from schema/queries/index.json with dynamic query counts"""
    index_path = os.path.join(QUERIES_DIR, "index.json")
    if not os.path.exists(index_path):
        return {"categories": [], "error": "Query index not found"}
    
    try:
        with open(index_path, 'r') as f:
            index = json.load(f)
        
        for category in index.get("categories", []):
            sql_file = os.path.join(QUERIES_DIR, category["file"])
            category["query_count"] = count_queries_in_file(sql_file)
        
        return index
    except Exception as e:
        return {"categories": [], "error": str(e)}

def get_saved_queries(category_id: str) -> List[Dict[str, str]]:
    """Parse SQL file and extract individual queries with their comments"""
    index = get_query_categories()
    
    category = next((c for c in index.get("categories", []) if c["id"] == category_id), None)
    if not category:
        return []
    
    sql_file = os.path.join(QUERIES_DIR, category["file"])
    if not os.path.exists(sql_file):
        return []
    
    try:
        with open(sql_file, 'r') as f:
            content = f.read()
        
        queries = []
        current_query = {"name": "", "description": "", "sql": "", "binding_key": ""}
        lines = content.split('\n')
        
        for line in lines:
            if line.startswith('-- Query:'):
                if current_query["name"] and current_query["sql"].strip():
                    queries.append(current_query)
                current_query = {"name": line.replace('-- Query:', '').strip(), "description": "", "sql": "", "binding_key": ""}
            elif line.startswith('-- Description:'):
                current_query["description"] = line.replace('-- Description:', '').strip()
            elif line.startswith('-- Binding:'):
                current_query["binding_key"] = line.replace('-- Binding:', '').strip()
            elif not line.startswith('-- ') and line.strip():
                current_query["sql"] += line + "\n"
        
        # Name-guarded like the in-loop append: header text before the first
        # '-- Query:' marker must never surface as a nameless palette entry.
        if current_query["name"] and current_query["sql"].strip():
            queries.append(current_query)
        
        return queries
    except Exception:
        return []

def save_query_to_file(category_id: str, query_name: str, description: str, sql: str) -> Dict[str, Any]:
    """Append a new query to the appropriate category file"""
    index = get_query_categories()
    
    category = next((c for c in index.get("categories", []) if c["id"] == category_id), None)
    if not category:
        return {"success": False, "error": f"Category '{category_id}' not found"}
    
    sql_file = os.path.join(QUERIES_DIR, category["file"])
    
    try:
        new_query = f"\n-- Query: {query_name}\n-- Description: {description}\n{sql.strip()}\n"
        
        with open(sql_file, 'a') as f:
            f.write(new_query)
        
        return {"success": True, "message": f"Query '{query_name}' saved to {category['name']}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

GROUND_TRUTH_DIR = os.path.join(SCHEMA_DIR, "ground_truth")
GROUND_TRUTH_SQL_DIR = os.path.join(GROUND_TRUTH_DIR, "sql_snippets")
MANIFEST_PATH = os.path.join(GROUND_TRUTH_DIR, "reviewer_manifest.json")


def get_ground_truth_tables() -> List[str]:
    """Extract unique table names referenced in APPROVED ground truth SQL files."""
    if not os.path.exists(MANIFEST_PATH):
        return []

    try:
        with open(MANIFEST_PATH, "r") as f:
            manifest = json.load(f)
    except Exception:
        return []

    approved = manifest.get("approved_snippets", {})
    all_tables = set()
    known_tables = set(get_all_tables())

    for entry in approved.values():
        if entry.get("validation_status") != "APPROVED":
            continue
        file_path = entry.get("file_path", "")
        if not os.path.isabs(file_path):
            file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), file_path)
        if not os.path.exists(file_path):
            alt = os.path.join(GROUND_TRUTH_DIR, os.path.basename(file_path))
            if os.path.exists(alt):
                file_path = alt
            else:
                continue
        try:
            with open(file_path, "r") as sf:
                sql_text = sf.read().upper()
            for m in re.finditer(r'\bFROM\s+(\w+)', sql_text):
                tbl = m.group(1).lower()
                if tbl in known_tables:
                    all_tables.add(tbl)
            for m in re.finditer(r'\bJOIN\s+(\w+)', sql_text):
                tbl = m.group(1).lower()
                if tbl in known_tables:
                    all_tables.add(tbl)
        except Exception:
            continue

    return sorted(all_tables)


def _sanitize_slug(value: str) -> str:
    slug = re.sub(r'[^a-z0-9_]', '_', value.lower().strip())
    slug = re.sub(r'_+', '_', slug).strip('_')
    return slug or "unknown"


def _auto_concept_from_sql(sql_text: str) -> str:
    """Auto-generate a concept name from SQL when user leaves Concept blank."""
    upper = sql_text.upper()
    from_match = re.search(r'\bFROM\s+(\w+)', upper)
    if from_match:
        table = from_match.group(1)
        select_cols = re.search(r'SELECT\s+(.*?)\s+FROM', upper, re.DOTALL)
        if select_cols:
            cols = select_cols.group(1).strip()
            if cols == "*":
                return f"{table}_OVERVIEW"
            col_parts = []
            for c in cols.split(','):
                part = c.strip().split('.')[-1].split(' ')[-1]
                part = re.sub(r'[^A-Z0-9_]', '', part)
                if part:
                    col_parts.append(part)
            if col_parts and len(col_parts) <= 3:
                return f"{table}_{'_'.join(col_parts)}"
            return f"{table}_QUERY"
        return f"{table}_QUERY"
    return "UNCLASSIFIED"


def save_sme_submission(sql_text: str, category: str, perspective: str, concept: str, justification: str) -> str:
    os.makedirs(GROUND_TRUTH_SQL_DIR, exist_ok=True)

    safe_perspective = _sanitize_slug(perspective)
    safe_concept = _sanitize_slug(concept)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    binding_key = f"{safe_perspective}_{safe_concept}_{timestamp}"
    filename = f"{binding_key}.sql"
    file_path = os.path.join(GROUND_TRUTH_SQL_DIR, filename)

    with open(file_path, "w") as f:
        f.write(sql_text)

    logic_type = "SPATIAL_ALIAS" if "User_Defined" in sql_text else "DIRECT"

    manifest_entry = {
        "concept_anchor": concept.upper(),
        "perspective": perspective,
        "category": category,
        "logic_type": logic_type,
        "file_path": file_path,
        "sme_justification": justification,
        "validation_status": "PENDING",
        "binding_key": binding_key,
        "created_at": datetime.datetime.now().isoformat()
    }

    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, Exception):
            manifest = {"approved_snippets": {}}
    else:
        manifest = {"approved_snippets": {}}

    if "approved_snippets" not in manifest:
        manifest["approved_snippets"] = {}

    manifest["approved_snippets"][binding_key] = manifest_entry
    manifest["last_updated"] = datetime.datetime.now().isoformat()

    tmp_fd, tmp_path = tempfile.mkstemp(dir=GROUND_TRUTH_DIR, suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(manifest, f, indent=4)
        os.replace(tmp_path, MANIFEST_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return f"Saved {filename} and updated Reviewer Manifest. Logic type: {logic_type}. Status: PENDING review."


def resolve_sql_bindings(sql_dir: str = None) -> List[Dict[str, Any]]:
    if sql_dir is None:
        sql_dir = GROUND_TRUTH_SQL_DIR

    bindings = []
    if not os.path.exists(sql_dir):
        return bindings

    manifest = {}
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r") as f:
                manifest = json.load(f)
        except Exception:
            pass

    approved_snippets = manifest.get("approved_snippets", {})

    for filename in sorted(os.listdir(sql_dir)):
        if not filename.endswith(".sql"):
            continue

        binding_key = filename.replace(".sql", "")
        file_path = os.path.join(sql_dir, filename)

        entry = approved_snippets.get(binding_key, {})

        bindings.append({
            "filename": filename,
            "binding_key": binding_key,
            "perspective": entry.get("perspective", ""),
            "concept": entry.get("concept_anchor", ""),
            "category": entry.get("category", ""),
            "justification": entry.get("sme_justification", ""),
            "validation_status": entry.get("validation_status", "UNKNOWN"),
            "created_at": entry.get("created_at", ""),
            "path": file_path
        })

    return bindings


def get_manifest_summary() -> Dict[str, Any]:
    if not os.path.exists(MANIFEST_PATH):
        return {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "bindings": []}

    try:
        with open(MANIFEST_PATH, "r") as f:
            manifest = json.load(f)
    except Exception:
        return {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "bindings": []}

    snippets = manifest.get("approved_snippets", {})
    bindings_list = list(snippets.values())
    return {
        "total": len(bindings_list),
        "pending": sum(1 for b in bindings_list if b.get("validation_status") == "PENDING"),
        "approved": sum(1 for b in bindings_list if b.get("validation_status") == "APPROVED"),
        "rejected": sum(1 for b in bindings_list if b.get("validation_status") == "REJECTED"),
        "bindings": bindings_list
    }


CATEGORY_MAP = {
    "Quality": "quality_control",
    "Inventory": "production_analytics",
    "Delivery": "supplier_performance",
    "Financial": "production_analytics",
    "Production": "production_analytics",
}

REVERSE_CATEGORY_MAP = {v: k for k, v in CATEGORY_MAP.items()}


def _ensure_manifest_entry(query_name: str, sql_text: str, description: str, category_id: str) -> str:
    """Reverse bridge: create a PENDING manifest entry for a Ground Truth query that has no binding key."""
    safe_name = _sanitize_slug(query_name)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    binding_key = f"gt_{safe_name}_{timestamp}"
    filename = f"{binding_key}.sql"
    file_path = os.path.join(GROUND_TRUTH_SQL_DIR, filename)

    os.makedirs(GROUND_TRUTH_SQL_DIR, exist_ok=True)
    with open(file_path, "w") as f:
        f.write(sql_text.strip())

    category_label = REVERSE_CATEGORY_MAP.get(category_id, category_id)
    logic_type = "SPATIAL_ALIAS" if "User_Defined" in sql_text else "DIRECT"

    manifest_entry = {
        "concept_anchor": safe_name.upper(),
        "perspective": "Pending",
        "category": category_label,
        "logic_type": logic_type,
        "file_path": file_path,
        "sme_justification": f"Auto-created from Ground Truth: {query_name} — {description}",
        "validation_status": "PENDING",
        "binding_key": binding_key,
        "created_at": datetime.datetime.now().isoformat()
    }

    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, Exception):
            manifest = {"approved_snippets": {}}
    else:
        manifest = {"approved_snippets": {}}

    if "approved_snippets" not in manifest:
        manifest["approved_snippets"] = {}

    manifest["approved_snippets"][binding_key] = manifest_entry
    manifest["last_updated"] = datetime.datetime.now().isoformat()

    tmp_fd, tmp_path = tempfile.mkstemp(dir=GROUND_TRUTH_DIR, suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(manifest, f, indent=4)
        os.replace(tmp_path, MANIFEST_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    _inject_binding_tag(query_name, binding_key, category_id)

    return binding_key


def _inject_binding_tag(query_name: str, binding_key: str, category_id: str):
    """Inject a -- Binding: tag into the category .sql file so subsequent loads find it directly."""
    index = get_query_categories()
    category = next((c for c in index.get("categories", []) if c["id"] == category_id), None)
    if not category:
        return
    sql_file = os.path.join(QUERIES_DIR, category["file"])
    if not os.path.exists(sql_file):
        return
    with open(sql_file, "r") as f:
        content = f.read()
    marker = f"-- Query: {query_name}"
    if marker not in content:
        return
    if f"-- Binding: {binding_key}" in content:
        return
    patched = content.replace(marker, f"{marker}\n-- Binding: {binding_key}")
    with open(sql_file, "w") as f:
        f.write(patched)


def _append_to_category_file(entry: Dict[str, Any], binding_key: str) -> str:
    category_name = entry.get("category", "")
    category_id = CATEGORY_MAP.get(category_name, "")
    if not category_id:
        category_id = CATEGORY_MAP.get(category_name.title(), "")
    if not category_id:
        return f"No category mapping for '{category_name}'"

    index = get_query_categories()
    category = next((c for c in index.get("categories", []) if c["id"] == category_id), None)
    if not category:
        return f"Category '{category_id}' not found in index"

    sql_file = os.path.join(QUERIES_DIR, category["file"])

    concept = entry.get("concept_anchor", binding_key)
    perspective = entry.get("perspective", "")
    query_name = f"{concept} ({perspective})"
    justification = entry.get("sme_justification", "SME-approved query")

    if os.path.exists(sql_file):
        with open(sql_file, "r") as f:
            existing = f.read()
        if binding_key in existing or f"-- Query: {query_name}" in existing:
            return f"Already in {category['name']} (skipped duplicate)"

    file_path = entry.get("file_path", "")
    candidates = [
        file_path,
        os.path.join(GROUND_TRUTH_SQL_DIR, os.path.basename(file_path)),
        os.path.join(GROUND_TRUTH_SQL_DIR, f"{binding_key}.sql"),
        os.path.join(GROUND_TRUTH_DIR, os.path.basename(file_path)),
        os.path.join(GROUND_TRUTH_DIR, f"{binding_key}.sql"),
    ]

    sql_text = ""
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            with open(candidate, "r") as f:
                sql_text = f.read().strip()
            break

    if not sql_text:
        return f"Could not find SQL file for {binding_key}"

    new_entry = f"\n\n-- Query: {query_name}\n-- Description: {justification}\n-- Binding: {binding_key}\n{sql_text}\n"

    with open(sql_file, "a") as f:
        f.write(new_entry)

    return f"Added to {category['name']} category"


def update_binding_status(binding_key: str, new_status: str) -> str:
    if not os.path.exists(MANIFEST_PATH):
        return "Manifest file not found."

    try:
        with open(MANIFEST_PATH, "r") as f:
            manifest = json.load(f)
    except Exception as e:
        return f"Error reading manifest: {e}"

    snippets = manifest.get("approved_snippets", {})

    if binding_key not in snippets:
        return f"Binding key '{binding_key}' not found in manifest."

    snippets[binding_key]["validation_status"] = new_status
    snippets[binding_key]["reviewed_at"] = datetime.datetime.now().isoformat()
    manifest["last_updated"] = datetime.datetime.now().isoformat()

    tmp_fd, tmp_path = tempfile.mkstemp(dir=GROUND_TRUTH_DIR, suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(manifest, f, indent=4)
        os.replace(tmp_path, MANIFEST_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    category_msg = ""
    if new_status == "APPROVED":
        category_msg = _append_to_category_file(snippets[binding_key], binding_key)
        if category_msg:
            category_msg = f" | {category_msg}"

    return f"Updated '{binding_key}' to {new_status}.{category_msg}"


app = FastAPI(
    title="Manufacturing Inventory SQL Generator",
    description="MCP-compliant natural language to SQL for inventory management",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SAMPLE_SCHEMA = {
    "tables": {
        "inventory": {
            "columns": {
                "part_id": {"type": "VARCHAR(50)", "primary_key": True, "description": "Unique part identifier"},
                "part_name": {"type": "VARCHAR(200)", "description": "Part name/description"},
                "category": {"type": "VARCHAR(100)", "description": "Part category (raw_material, component, finished_good)"},
                "quantity_on_hand": {"type": "INTEGER", "description": "Current stock quantity"},
                "reorder_point": {"type": "INTEGER", "description": "Minimum quantity before reorder"},
                "unit_cost": {"type": "DECIMAL(10,2)", "description": "Cost per unit in USD"},
                "supplier_id": {"type": "VARCHAR(50)", "description": "Primary supplier ID"},
                "warehouse_location": {"type": "VARCHAR(50)", "description": "Warehouse bin location"},
                "last_updated": {"type": "TIMESTAMP", "description": "Last inventory update timestamp"}
            }
        },
        "suppliers": {
            "columns": {
                "supplier_id": {"type": "VARCHAR(50)", "primary_key": True, "description": "Unique supplier identifier"},
                "supplier_name": {"type": "VARCHAR(200)", "description": "Supplier company name"},
                "lead_time_days": {"type": "INTEGER", "description": "Average lead time in days"},
                "quality_rating": {"type": "DECIMAL(3,2)", "description": "Quality score 0.00-5.00"},
                "on_time_delivery_rate": {"type": "DECIMAL(5,2)", "description": "On-time delivery percentage"}
            }
        },
        "transactions": {
            "columns": {
                "transaction_id": {"type": "SERIAL", "primary_key": True, "description": "Auto-increment transaction ID"},
                "part_id": {"type": "VARCHAR(50)", "foreign_key": "inventory.part_id", "description": "Part being transacted"},
                "transaction_type": {"type": "VARCHAR(20)", "description": "Type: receipt, issue, adjustment, transfer"},
                "quantity": {"type": "INTEGER", "description": "Quantity transacted (positive or negative)"},
                "transaction_date": {"type": "TIMESTAMP", "description": "When transaction occurred"},
                "reference_number": {"type": "VARCHAR(100)", "description": "PO number, work order, etc."}
            }
        }
    },
    "relationships": [
        {"from": "inventory.supplier_id", "to": "suppliers.supplier_id", "type": "many-to-one"},
        {"from": "transactions.part_id", "to": "inventory.part_id", "type": "many-to-one"}
    ]
}

SQL_TEMPLATES = {
    "low_stock": """SELECT part_id, part_name, quantity_on_hand, reorder_point,
       (reorder_point - quantity_on_hand) AS units_below_reorder
FROM inventory
WHERE quantity_on_hand < reorder_point
ORDER BY units_below_reorder DESC;""",
    
    "inventory_value": """SELECT category,
       COUNT(*) AS part_count,
       SUM(quantity_on_hand) AS total_units,
       SUM(quantity_on_hand * unit_cost) AS total_value
FROM inventory
GROUP BY category
ORDER BY total_value DESC;""",
    
    "supplier_performance": """SELECT s.supplier_name,
       s.quality_rating,
       s.on_time_delivery_rate,
       COUNT(DISTINCT i.part_id) AS parts_supplied
FROM suppliers s
LEFT JOIN inventory i ON s.supplier_id = i.supplier_id
GROUP BY s.supplier_id, s.supplier_name, s.quality_rating, s.on_time_delivery_rate
ORDER BY s.quality_rating DESC;""",
    
    "transaction_summary": """SELECT DATE(transaction_date) AS txn_date,
       transaction_type,
       COUNT(*) AS transaction_count,
       SUM(ABS(quantity)) AS total_units
FROM transactions
WHERE transaction_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(transaction_date), transaction_type
ORDER BY txn_date DESC, transaction_type;"""
}

class MCPToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: Dict[str, Any]

class MCPDiscoveryResponse(BaseModel):
    name: str
    version: str
    description: str
    tools: List[MCPToolDefinition]
    resources: List[Dict[str, Any]]
    prompts: List[Dict[str, Any]]

class SQLGenerationRequest(BaseModel):
    query: str = Field(..., description="Natural language query to convert to SQL")
    include_explanation: bool = Field(default=True, description="Include explanation of generated SQL")

class SQLGenerationResponse(BaseModel):
    sql: str
    explanation: Optional[str] = None
    tables_used: List[str]
    estimated_complexity: str

class SchemaUploadRequest(BaseModel):
    schema_definition: str = Field(..., description="JSON schema definition")

def analyze_query_intent(query: str) -> Dict[str, Any]:
    """Analyze natural language query to determine SQL intent"""
    query_lower = query.lower()
    
    intent = {
        "action": "select",
        "tables": [],
        "aggregation": False,
        "filtering": False,
        "sorting": False,
        "grouping": False,
        "keywords": []
    }
    
    if any(word in query_lower for word in ["count", "total", "sum", "average", "avg", "how many"]):
        intent["aggregation"] = True
        intent["keywords"].append("aggregation")
    
    if any(word in query_lower for word in ["low", "below", "under", "less than", "shortage", "reorder"]):
        intent["filtering"] = True
        intent["keywords"].append("threshold_filter")
    
    if any(word in query_lower for word in ["top", "highest", "lowest", "best", "worst", "rank"]):
        intent["sorting"] = True
        intent["keywords"].append("ranking")
    
    if any(word in query_lower for word in ["by category", "by supplier", "per warehouse", "group by", "breakdown"]):
        intent["grouping"] = True
        intent["keywords"].append("grouping")
    
    if any(word in query_lower for word in ["inventory", "stock", "part", "quantity", "warehouse"]):
        intent["tables"].append("inventory")
    if any(word in query_lower for word in ["supplier", "vendor", "lead time", "quality rating"]):
        intent["tables"].append("suppliers")
    if any(word in query_lower for word in ["transaction", "receipt", "issue", "transfer", "movement"]):
        intent["tables"].append("transactions")
    
    if not intent["tables"]:
        intent["tables"] = ["inventory"]
    
    return intent

def generate_sql_from_intent(query: str, intent: Dict[str, Any]) -> SQLGenerationResponse:
    """Generate SQL based on analyzed intent"""
    query_lower = query.lower()
    
    if "low stock" in query_lower or "below reorder" in query_lower or "need to reorder" in query_lower:
        return SQLGenerationResponse(
            sql=SQL_TEMPLATES["low_stock"],
            explanation="This query identifies parts where current stock is below the reorder point, "
                       "sorted by urgency (how far below reorder point).",
            tables_used=["inventory"],
            estimated_complexity="simple"
        )
    
    if "inventory value" in query_lower or "total value" in query_lower or "worth" in query_lower:
        return SQLGenerationResponse(
            sql=SQL_TEMPLATES["inventory_value"],
            explanation="Calculates total inventory value by category, showing part counts, "
                       "total units, and monetary value.",
            tables_used=["inventory"],
            estimated_complexity="moderate"
        )
    
    if "supplier" in query_lower and ("performance" in query_lower or "rating" in query_lower or "quality" in query_lower):
        return SQLGenerationResponse(
            sql=SQL_TEMPLATES["supplier_performance"],
            explanation="Shows supplier performance metrics including quality rating, "
                       "on-time delivery rate, and number of parts supplied.",
            tables_used=["suppliers", "inventory"],
            estimated_complexity="moderate"
        )
    
    if "transaction" in query_lower or "movement" in query_lower or "activity" in query_lower:
        return SQLGenerationResponse(
            sql=SQL_TEMPLATES["transaction_summary"],
            explanation="Summarizes inventory transactions over the last 30 days, "
                       "grouped by date and transaction type.",
            tables_used=["transactions"],
            estimated_complexity="moderate"
        )
    
    tables = intent["tables"]
    main_table = tables[0] if tables else "inventory"
    
    if intent["aggregation"] and intent["grouping"]:
        if main_table == "inventory":
            sql = """SELECT category,
       COUNT(*) AS part_count,
       SUM(quantity_on_hand) AS total_quantity,
       AVG(unit_cost) AS avg_unit_cost
FROM inventory
GROUP BY category
ORDER BY total_quantity DESC;"""
            explanation = "Aggregates inventory data by category with counts and totals."
        else:
            sql = f"SELECT * FROM {main_table} LIMIT 100;"
            explanation = f"Basic query on {main_table} table."
    
    elif intent["filtering"]:
        sql = """SELECT part_id, part_name, quantity_on_hand, reorder_point
FROM inventory
WHERE quantity_on_hand < reorder_point
ORDER BY quantity_on_hand ASC;"""
        explanation = "Filters inventory for items needing attention based on stock levels."
    
    elif intent["sorting"]:
        sql = """SELECT part_id, part_name, quantity_on_hand, unit_cost,
       (quantity_on_hand * unit_cost) AS total_value
FROM inventory
ORDER BY total_value DESC
LIMIT 20;"""
        explanation = "Returns top items sorted by total inventory value."
    
    else:
        sql = f"""SELECT *
FROM {main_table}
LIMIT 100;"""
        explanation = f"Basic select query on {main_table} table. Refine your question for more specific results."
    
    complexity = "simple"
    if len(tables) > 1 or intent["aggregation"]:
        complexity = "moderate"
    if len(tables) > 2 or (intent["aggregation"] and intent["grouping"]):
        complexity = "complex"
    
    return SQLGenerationResponse(
        sql=sql,
        explanation=explanation,
        tables_used=tables,
        estimated_complexity=complexity
    )


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main Gradio interface"""
    return """
    <html>
        <head>
            <title>Manufacturing Inventory SQL Generator</title>
            <meta http-equiv="refresh" content="0; url=/gradio/" />
        </head>
        <body>
            <p>Redirecting to <a href="/gradio/">Gradio Interface</a>...</p>
        </body>
    </html>
    """


@app.get("/mcp/discover", response_model=MCPDiscoveryResponse)
async def mcp_discover():
    """MCP Discovery endpoint - returns available tools and capabilities"""
    return MCPDiscoveryResponse(
        name="manufacturing-inventory-sqlgen",
        version="1.0.0",
        description="Natural language to SQL generator for manufacturing inventory management",
        tools=[
            MCPToolDefinition(
                name="generate_sql",
                description="Convert natural language query to SQL for inventory database",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language query about inventory"
                        },
                        "include_explanation": {
                            "type": "boolean",
                            "default": True,
                            "description": "Include explanation of generated SQL"
                        }
                    },
                    "required": ["query"]
                }
            ),
            MCPToolDefinition(
                name="get_schema",
                description="Get the database schema for inventory tables",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            MCPToolDefinition(
                name="get_sql_templates",
                description="Get pre-built SQL templates for common inventory queries",
                input_schema={
                    "type": "object",
                    "properties": {
                        "template_name": {
                            "type": "string",
                            "enum": ["low_stock", "inventory_value", "supplier_performance", "transaction_summary"],
                            "description": "Name of the SQL template"
                        }
                    },
                    "required": []
                }
            ),
            MCPToolDefinition(
                name="analyze_csv",
                description="Analyze uploaded CSV to suggest schema and queries",
                input_schema={
                    "type": "object",
                    "properties": {
                        "csv_content": {
                            "type": "string",
                            "description": "CSV file content as string"
                        }
                    },
                    "required": ["csv_content"]
                }
            ),
            MCPToolDefinition(
                name="get_db_tables",
                description="Get list of all tables from connected PostgreSQL database",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            MCPToolDefinition(
                name="get_table_ddl",
                description="Get CREATE TABLE SQL for a specific table",
                input_schema={
                    "type": "object",
                    "properties": {
                        "table_name": {
                            "type": "string",
                            "description": "Name of the table to get DDL for"
                        }
                    },
                    "required": ["table_name"]
                }
            ),
            MCPToolDefinition(
                name="get_all_ddl",
                description="Get CREATE TABLE SQL for all tables in the database",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            MCPToolDefinition(
                name="execute_sql",
                description="Execute read-only SQL query (SELECT only) against the database",
                input_schema={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "SQL SELECT query to execute"
                        }
                    },
                    "required": ["sql"]
                }
            )
        ],
        resources=[
            {
                "uri": "schema://inventory",
                "name": "Inventory Schema",
                "description": "Database schema for inventory management tables",
                "mimeType": "application/json"
            }
        ],
        prompts=[
            {
                "name": "inventory_query",
                "description": "Template for inventory-related queries",
                "arguments": [
                    {"name": "question", "description": "The inventory question to answer", "required": True}
                ]
            }
        ]
    )


def _get_erp_config() -> dict:
    """Single source of truth for ERP runtime config.

    Returns a dict with:
      - erp_instance_name: resolved ERP name (env var or default)
      - erp_instance_name_source: "env" if ERP_INSTANCE_NAME is set, "default" otherwise
    """
    raw = os.environ.get("ERP_INSTANCE_NAME")
    return {
        "erp_instance_name": raw if raw else "ERP_Instance_1",
        "erp_instance_name_source": "env" if raw else "default",
    }


@app.get("/mcp/config")
async def get_mcp_config():
    """Return active runtime configuration so operators can verify settings without reading logs.

    Response fields:
      - erp_instance_name: value of ERP_INSTANCE_NAME env var (default "ERP_Instance_1")
      - erp_instance_name_source: "env" if set explicitly, "default" if falling back
      - sqlite_db_path: resolved path to the SQLite database file
      - query_api_key_set: boolean – true when QUERY_API_KEY is non-empty
    """
    cfg = _get_erp_config()
    return {
        **cfg,
        "sqlite_db_path": SQLITE_DB_PATH,
        "query_api_key_set": bool(QUERY_API_KEY),
    }


@app.get("/mcp/tools/erp_info")
async def get_erp_info():
    """Return the current ERP instance name from the environment.

    Response fields:
      - erp_instance_name: value of ERP_INSTANCE_NAME env var (default "ERP_Instance_1")
    """
    raw = os.environ.get("ERP_INSTANCE_NAME")
    return {"erp_instance_name": raw if raw else "ERP_Instance_1"}


@app.post("/mcp/tools/generate_sql", response_model=SQLGenerationResponse)
async def generate_sql(request: SQLGenerationRequest):
    """Generate SQL from natural language query"""
    intent = analyze_query_intent(request.query)
    response = generate_sql_from_intent(request.query, intent)
    
    if not request.include_explanation:
        response.explanation = None
    
    return response


@app.get("/mcp/tools/get_schema")
async def get_schema():
    """Return the database schema"""
    return {"schema": SAMPLE_SCHEMA}


@app.get("/mcp/tools/get_sql_templates")
async def get_sql_templates(template_name: Optional[str] = None):
    """Return SQL templates for common queries"""
    if template_name:
        if template_name not in SQL_TEMPLATES:
            raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")
        return {"template_name": template_name, "sql": SQL_TEMPLATES[template_name]}
    return {"templates": SQL_TEMPLATES}


@app.post("/mcp/tools/analyze_csv")
async def analyze_csv(csv_content: str = Form(...)):
    """Analyze CSV content and suggest schema"""
    try:
        reader = csv.DictReader(io.StringIO(csv_content))
        rows = list(reader)
        
        if not rows:
            return {"error": "Empty CSV file"}
        
        columns = list(rows[0].keys())
        
        column_analysis = {}
        for col in columns:
            values = [row[col] for row in rows if row[col]]
            
            col_type = "VARCHAR(255)"
            if all(v.isdigit() for v in values[:10] if v):
                col_type = "INTEGER"
            elif all(v.replace(".", "").replace("-", "").isdigit() for v in values[:10] if v):
                col_type = "DECIMAL(10,2)"
            
            column_analysis[col] = {
                "suggested_type": col_type,
                "sample_values": values[:3],
                "non_null_count": len(values)
            }
        
        return {
            "row_count": len(rows),
            "columns": column_analysis,
            "suggested_queries": [
                f"SELECT * FROM uploaded_data LIMIT 10;",
                f"SELECT COUNT(*) FROM uploaded_data;",
                f"SELECT {columns[0]}, COUNT(*) FROM uploaded_data GROUP BY {columns[0]};" if columns else ""
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error parsing CSV: {str(e)}")


@app.get("/mcp/tools/get_db_tables")
async def get_db_tables():
    """Get list of all tables from connected PostgreSQL database"""
    tables = get_all_tables()
    if not tables:
        return {"error": "Database not connected or no tables found", "tables": []}
    return {"tables": tables, "count": len(tables)}


@app.get("/mcp/tools/list_schema_tables")
async def list_schema_tables():
    """Get graph nodes (ERP tables) from schema_nodes, grouped by source namespace.

    Returns a SearchResult-compatible payload:
      {
        "matches_found": int,
        "grouped_results": {
          "<ERP_INSTANCE_NAME>": [{"table_name": str, "qualified_name": str}, ...]
        }
      }
    Source of truth is schema_nodes (table_type = 'Table') — SQLite drives the
    graph; tables are nodes. Falls back to get_all_tables() if schema_nodes is empty.
    """
    erp_instance_name = os.environ.get("ERP_INSTANCE_NAME", "ERP_Instance_1")
    erp_records: list = []
    engine = get_db_engine()
    if engine:
        try:
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT table_name FROM schema_nodes "
                        "WHERE table_type = 'Table' ORDER BY table_name"
                    )
                )
                rows = result.fetchall()
                if rows:
                    erp_records = [
                        {"table_name": r[0], "qualified_name": f"dbo.{r[0].upper()}"}
                        for r in rows
                    ]
        except Exception:
            pass
    if not erp_records:
        erp_records = [
            {"table_name": t, "qualified_name": f"dbo.{t.upper()}"}
            for t in sorted(get_all_tables())
        ]
    grouped: dict = {}
    if erp_records:
        grouped[erp_instance_name] = erp_records
    return {"matches_found": len(erp_records), "grouped_results": grouped}


@app.get("/mcp/tools/get_table_ddl")
async def get_table_ddl(table_name: str):
    """Get CREATE TABLE SQL for a specific table"""
    sql = get_table_create_sql(table_name)
    return {"table_name": table_name, "ddl": sql}


@app.get("/mcp/tools/get_all_ddl")
async def get_all_ddl():
    """Get CREATE TABLE SQL for all tables in the database"""
    tables = get_all_tables()
    if not tables:
        return {"error": "Database not connected or no tables found", "ddl": {}}
    
    ddl_statements = {}
    for table in tables:
        ddl_statements[table] = get_table_create_sql(table)
    
    return {"tables": tables, "ddl": ddl_statements}


@app.post("/mcp/tools/execute_sql")
async def execute_sql_endpoint(sql: str = Form(...)):
    """Execute read-only SQL query against the database"""
    result = execute_readonly_sql(sql)
    return result


@app.get("/mcp/tools/get_entity_categories")
async def get_entity_categories():
    """Return perspective names from schema_perspectives (SQLite) as the category pill list.

    'Category' is the user-facing label for 'Perspective' — they are the same concept.
    The selected value is stamped as both edge.category and edge.perspective on every
    committed ArangoDB edge document. Source of truth is schema_perspectives, not the
    legacy schema_entity_categories table."""
    try:
        engine = get_db_engine()
        if not engine:
            return {"categories": [], "source": "unavailable"}
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT perspective_name FROM schema_perspectives ORDER BY perspective_id")
            ).fetchall()
        return {"categories": [r[0] for r in rows], "source": "sqlite"}
    except Exception as exc:
        return {"categories": [], "error": str(exc)}

@app.get("/mcp/tools/get_saved_categories")
async def get_saved_categories():
    """Get list of saved query categories"""
    return get_query_categories()


@app.get("/mcp/tools/get_saved_queries")
async def get_saved_queries_endpoint(category_id: str):
    """Get saved queries for a specific category"""
    queries = get_saved_queries(category_id)
    return {"category_id": category_id, "queries": queries, "count": len(queries)}


class SaveQueryRequest(BaseModel):
    category_id: str = Field(..., description="Category to save the query to")
    query_name: str = Field(..., description="Name of the query")
    description: str = Field(..., description="Description of what the query does")
    sql: str = Field(..., description="The SQL query")


@app.post("/mcp/tools/save_query")
async def save_query_endpoint(
    request: SaveQueryRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Save a validated SQL query from the Flask app (ground truth).
    
    Requires X-API-Key header for authentication. Set QUERY_API_KEY env var.
    """
    if not QUERY_API_KEY:
        raise HTTPException(
            status_code=503, 
            detail="Query save endpoint not configured. Set QUERY_API_KEY environment variable."
        )
    
    if not x_api_key or x_api_key != QUERY_API_KEY:
        raise HTTPException(
            status_code=401, 
            detail="Invalid or missing API key. Include X-API-Key header."
        )
    
    result = save_query_to_file(
        request.category_id,
        request.query_name,
        request.description,
        request.sql
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# =============================================================================
# SEMANTIC LAYER: Concept, Perspective, and Intent Endpoints
# =============================================================================

@app.get("/mcp/tools/get_concepts")
async def get_concepts(domain: Optional[str] = None, concept_type: Optional[str] = None):
    """Get all schema concepts, optionally filtered by domain or type.
    
    Concepts represent multiple possible interpretations of ambiguous fields.
    Domains: quality, finance, operations, compliance, customer
    Types: state, metric, classification, outcome
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            query = (
                "SELECT concept_id, concept_name, "
                "CASE WHEN computation_template IS NOT NULL AND computation_template <> '' "
                "THEN 'metric' ELSE NULL END AS concept_type, "
                "description, domain FROM schema_concepts WHERE 1=1"
            )
            params = {}
            if domain:
                query += " AND domain = :domain"
                params["domain"] = domain
            if concept_type:
                # concept_type is DEPRECATED: metric-ness is duck typed on
                # computation_template. 'metric' filters to templated concepts;
                # any other value filters to the non-metric remainder.
                if concept_type == "metric":
                    query += " AND computation_template IS NOT NULL AND computation_template <> ''"
                else:
                    query += " AND (computation_template IS NULL OR computation_template = '')"
            query += " ORDER BY domain, concept_name"
            
            result = conn.execute(text(query), params)
            concepts = [
                {"concept_id": r[0], "concept_name": r[1], "concept_type": r[2], 
                 "description": r[3], "domain": r[4]}
                for r in result.fetchall()
            ]
            return {"concepts": concepts, "count": len(concepts)}
    except Exception as e:
        return {"error": str(e), "concepts": [], "count": 0}


@app.get("/mcp/tools/get_field_concepts")
async def get_field_concepts(table_name: Optional[str] = None, field_name: Optional[str] = None):
    """Get concept mappings for ambiguous fields (CAN_MEAN relationships).
    
    Shows how the same field can have multiple interpretations based on context.
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            query = """
                SELECT cf.table_name, cf.field_name, cf.is_primary_meaning, cf.context_hint,
                       c.concept_id, c.concept_name, CASE WHEN c.computation_template IS NOT NULL AND c.computation_template <> '' THEN 'metric' ELSE NULL END AS concept_type, c.description, c.domain
                FROM schema_concept_fields cf
                JOIN schema_concepts c ON cf.concept_id = c.concept_id
                WHERE 1=1
            """
            params = {}
            if table_name:
                query += " AND cf.table_name = :table_name"
                params["table_name"] = table_name
            if field_name:
                query += " AND cf.field_name = :field_name"
                params["field_name"] = field_name
            query += " ORDER BY cf.table_name, cf.field_name, cf.is_primary_meaning DESC"
            
            result = conn.execute(text(query), params)
            mappings = [
                {
                    "table_name": r[0], "field_name": r[1], 
                    "is_primary": bool(r[2]), "context_hint": r[3],
                    "concept": {
                        "concept_id": r[4], "concept_name": r[5],
                        "concept_type": r[6], "description": r[7], "domain": r[8]
                    }
                }
                for r in result.fetchall()
            ]
            return {"field_concepts": mappings, "count": len(mappings)}
    except Exception as e:
        return {"error": str(e), "field_concepts": [], "count": 0}


@app.get("/mcp/tools/get_resolves_to")
async def get_resolves_to(concept_name: Optional[str] = None):
    """Get M4 metric/template variable bindings (resolves_to) for metric concepts.

    Reads the binding rows from the SQLite source of truth
    (`schema_concept_fields` where `variable_name IS NOT NULL`, joined to
    `schema_concepts`) and enriches each with `field_key` taken from the live
    ArangoDB `resolves_to` edges (the canonical column-node key on the edge's
    `_from`). When ArangoDB is unreachable, `field_key` falls back to the
    deterministic canonical column-node key, so the endpoint never hard-fails.

    No new table is introduced — this is a read adapter over the existing M4
    model. Each item carries the cross-repo payload structure expected by the
    public fleet: `concept`, `variable_name`, `table_name`, `field_name`,
    `field_key`, `context_hint`.

    Args:
        concept_name: Optional metric concept name filter (e.g. ``OEEOperational``).
    """
    engine = get_db_engine()

    arango_field_keys: dict = {}
    arango_available = False
    try:
        import importlib
        gs = importlib.import_module("graph_sync")
        client = gs.get_arango_client()
        db = gs.get_arango_db(client)
        cursor = db.aql.execute(
            "FOR e IN manufacturing_graph_edge "
            "FILTER e.edge_type == @et AND e.variable_name != null "
            "RETURN {f: e._from, t: e._to, v: e.variable_name}",
            bind_vars={"et": "resolves_to"},
        )
        for row in cursor:
            to_key = str(row.get("t", "")).split("/")[-1]
            from_key = str(row.get("f", "")).split("/")[-1]
            edge_concept = to_key.split(":")[0] if to_key else ""
            var = row.get("v")
            if edge_concept and var:
                arango_field_keys[(edge_concept, var)] = from_key
        arango_available = True
    except Exception:
        arango_available = False

    try:
        with engine.connect() as conn:
            query = """
                SELECT c.concept_name, cf.variable_name, cf.table_name,
                       cf.field_name, cf.context_hint
                FROM schema_concept_fields cf
                JOIN schema_concepts c ON cf.concept_id = c.concept_id
                WHERE cf.variable_name IS NOT NULL
            """
            params = {}
            if concept_name:
                query += " AND c.concept_name = :concept_name"
                params["concept_name"] = concept_name
            query += " ORDER BY c.concept_name, cf.variable_name"

            result = conn.execute(text(query), params)
            bindings = []
            for r in result.fetchall():
                concept, variable_name, table_name, field_name, context_hint = r
                canonical_field_key = f"{table_name}:{field_name}:structural:system:none:none"
                arango_field_key = arango_field_keys.get((concept, variable_name))
                field_key = arango_field_key if arango_field_key == canonical_field_key else canonical_field_key
                bindings.append({
                    "concept": concept,
                    "variable_name": variable_name,
                    "table_name": table_name,
                    "field_name": field_name,
                    "field_key": field_key,
                    "context_hint": context_hint,
                })
            return {
                "resolves_to": bindings,
                "count": len(bindings),
                "arango_available": arango_available,
                "field_key_source": "arango" if arango_available else "derived",
            }
    except Exception as e:
        return {"error": str(e), "resolves_to": [], "count": 0}


@app.get("/mcp/tools/get_ledger_bindings")
async def get_ledger_bindings():
    """Get the governed job-costing ledger binding map (read-only).

    Serves the validated in-memory view of the committed binding map
    (`poc/ontop-ontology-poc/ledger_binding_map.json`):

      * `concept_table_bindings` — SKOS concept URI -> physical `gl_*` table
        (with the concept's prefLabel for display), and
      * `event_class_bindings` — RDF (OWL) posting-event class ->
        `gl_events.event_type` (with the closeMatch SKOS concept URI).

    The loader FAILS CLOSED (ledger_bindings.py) — a map that disagrees with
    the SKOS scheme, the event ontology, or the governed SQL graph is never
    served; this endpoint then reports the load error instead.
    """
    try:
        from ledger_bindings import get_ledger_binding_store
        store = get_ledger_binding_store()
        payload = store.as_records()
        payload["concept_binding_count"] = len(payload["concept_table_bindings"])
        payload["event_binding_count"] = len(payload["event_class_bindings"])
        return payload
    except Exception as e:
        return {
            "error": str(e),
            "concept_table_bindings": [],
            "event_class_bindings": [],
            "concept_binding_count": 0,
            "event_binding_count": 0,
        }


@app.get("/mcp/tools/get_ambiguous_fields")
async def get_ambiguous_fields():
    """Get list of fields that have multiple concept interpretations.
    
    These are the fields where perspective/intent matters for correct interpretation.
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT cf.table_name, cf.field_name, COUNT(*) as concept_count,
                       GROUP_CONCAT(c.concept_name, ', ') as concepts,
                       GROUP_CONCAT(c.domain, ', ') as domains
                FROM schema_concept_fields cf
                JOIN schema_concepts c ON cf.concept_id = c.concept_id
                GROUP BY cf.table_name, cf.field_name
                HAVING COUNT(*) > 1
                ORDER BY COUNT(*) DESC
            """))
            fields = [
                {
                    "table_name": r[0], "field_name": r[1], 
                    "concept_count": r[2], "concepts": r[3], "domains": r[4]
                }
                for r in result.fetchall()
            ]
            return {"ambiguous_fields": fields, "count": len(fields)}
    except Exception as e:
        return {"error": str(e), "ambiguous_fields": [], "count": 0}


@app.get("/mcp/tools/get_perspectives")
async def get_perspectives():
    """Get all organizational perspectives.
    
    Perspectives are viewpoints that constrain which concept interpretations are valid.
    Each perspective represents a stakeholder group with specific priorities.
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT perspective_id, perspective_name, description, 
                       stakeholder_role, priority_focus
                FROM schema_perspectives
                ORDER BY perspective_name
            """))
            perspectives = [
                {
                    "perspective_id": r[0], "perspective_name": r[1],
                    "description": r[2], "stakeholder_role": r[3],
                    "priority_focus": r[4]
                }
                for r in result.fetchall()
            ]
            return {"perspectives": perspectives, "count": len(perspectives)}
    except Exception as e:
        return {"error": str(e), "perspectives": [], "count": 0}


@app.get("/mcp/tools/get_perspective_concepts")
async def get_perspective_concepts(perspective_name: Optional[str] = None):
    """Get concepts used by each perspective (Perspective_Concepts bridge rows).

    Reads the `schema_perspective_concepts` bridge table — the SQLite
    source-of-truth that feeds the ArangoDB `Perspective_Concepts` document
    collection. (The retired three-hop `USES_DEFINITION` edge collection is
    no longer used; perspective lives as a property on each bridge row.)

    Shows which concept interpretations are valid for each organizational perspective.
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            query = """
                SELECT p.perspective_name, p.description, p.stakeholder_role,
                       pc.relationship_type, pc.priority_weight,
                       c.concept_id, c.concept_name, CASE WHEN c.computation_template IS NOT NULL AND c.computation_template <> '' THEN 'metric' ELSE NULL END AS concept_type, c.description as concept_desc, c.domain
                FROM schema_perspectives p
                JOIN schema_perspective_concepts pc ON p.perspective_id = pc.perspective_id
                JOIN schema_concepts c ON pc.concept_id = c.concept_id
                WHERE 1=1
            """
            params = {}
            if perspective_name:
                query += " AND p.perspective_name = :perspective_name"
                params["perspective_name"] = perspective_name
            query += " ORDER BY p.perspective_name, pc.priority_weight DESC"
            
            result = conn.execute(text(query), params)
            mappings = [
                {
                    "perspective": r[0], "perspective_desc": r[1], 
                    "stakeholder_role": r[2], "relationship_type": r[3],
                    "priority_weight": r[4],
                    "concept": {
                        "concept_id": r[5], "concept_name": r[6],
                        "concept_type": r[7], "description": r[8], "domain": r[9]
                    }
                }
                for r in result.fetchall()
            ]
            return {"perspective_concepts": mappings, "count": len(mappings)}
    except Exception as e:
        return {"error": str(e), "perspective_concepts": [], "count": 0}


@app.get("/mcp/tools/resolve_field_for_perspective")
async def resolve_field_for_perspective(table_name: str, field_name: str, perspective_name: str):
    """Resolve which concept interpretation applies for a field given a perspective.
    
    This is the semantic disambiguation endpoint - given a perspective, it returns
    the correct interpretation of an ambiguous field.
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT c.concept_id, c.concept_name, CASE WHEN c.computation_template IS NOT NULL AND c.computation_template <> '' THEN 'metric' ELSE NULL END AS concept_type, c.description, c.domain,
                       cf.context_hint, pc.priority_weight
                FROM schema_concept_fields cf
                JOIN schema_concepts c ON cf.concept_id = c.concept_id
                JOIN schema_perspective_concepts pc ON c.concept_id = pc.concept_id
                JOIN schema_perspectives p ON pc.perspective_id = p.perspective_id
                WHERE cf.table_name = :table_name 
                  AND cf.field_name = :field_name
                  AND p.perspective_name = :perspective_name
                ORDER BY pc.priority_weight DESC
                LIMIT 1
            """), {"table_name": table_name, "field_name": field_name, "perspective_name": perspective_name})
            
            row = result.fetchone()
            if row:
                return {
                    "resolved": True,
                    "table_name": table_name,
                    "field_name": field_name,
                    "perspective": perspective_name,
                    "concept": {
                        "concept_id": row[0], "concept_name": row[1],
                        "concept_type": row[2], "description": row[3], 
                        "domain": row[4], "context_hint": row[5],
                        "priority_weight": row[6]
                    }
                }
            else:
                return {
                    "resolved": False,
                    "table_name": table_name,
                    "field_name": field_name,
                    "perspective": perspective_name,
                    "message": "No concept mapping found for this field/perspective combination"
                }
    except Exception as e:
        return {"error": str(e), "resolved": False}


@app.get("/mcp/tools/get_intents")
async def get_intents(category: Optional[str] = None):
    """Get all analytical intents, optionally filtered by category.
    
    Intents are analytical goals that binary-switch concept weights.
    Each intent elevates one field interpretation to 1.0 while suppressing alternatives to 0.0.
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            query = """
                SELECT intent_id, intent_name, intent_category, description, typical_question
                FROM schema_intents
                WHERE 1=1
            """
            params = {}
            if category:
                query += " AND intent_category = :category"
                params["category"] = category
            query += " ORDER BY intent_category, intent_name"
            
            result = conn.execute(text(query), params)
            intents = [
                {
                    "intent_id": r[0], "intent_name": r[1],
                    "intent_category": r[2], "description": r[3],
                    "typical_question": r[4]
                }
                for r in result.fetchall()
            ]
            return {"intents": intents, "count": len(intents)}
    except Exception as e:
        return {"error": str(e), "intents": [], "count": 0}


@app.get("/mcp/tools/get_intent_weights")
async def get_intent_weights(intent_name: str):
    """Get concept weights for a specific intent (the binary elevation/suppression).
    
    Returns which concepts are elevated (1.0) vs suppressed (0.0) for this intent.
    This is the core disambiguation mechanism - intent determines which interpretation wins.
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT i.intent_name, i.description, i.typical_question,
                       ic.intent_factor_weight, ic.explanation,
                       c.concept_id, c.concept_name, CASE WHEN c.computation_template IS NOT NULL AND c.computation_template <> '' THEN 'metric' ELSE NULL END AS concept_type, c.description as concept_desc, c.domain
                FROM schema_intents i
                JOIN schema_intent_concepts ic ON i.intent_id = ic.intent_id
                JOIN schema_concepts c ON ic.concept_id = c.concept_id
                WHERE i.intent_name = :intent_name
                ORDER BY ic.intent_factor_weight DESC, c.concept_name
            """), {"intent_name": intent_name})
            
            rows = result.fetchall()
            if not rows:
                return {"error": f"Intent '{intent_name}' not found", "weights": []}
            
            weights = [
                {
                    "intent_factor_weight": r[3],
                    "status": "ELEVATED" if r[3] == 1.0 else "SUPPRESSED",
                    "explanation": r[4],
                    "concept": {
                        "concept_id": r[5], "concept_name": r[6],
                        "concept_type": r[7], "description": r[8], "domain": r[9]
                    }
                }
                for r in rows
            ]
            
            return {
                "intent_name": rows[0][0],
                "description": rows[0][1],
                "typical_question": rows[0][2],
                "weights": weights,
                "elevated_count": sum(1 for w in weights if w["intent_factor_weight"] == 1.0),
                "suppressed_count": sum(1 for w in weights if w["intent_factor_weight"] == 0.0)
            }
    except Exception as e:
        return {"error": str(e), "weights": []}


@app.get("/mcp/tools/get_intent_queries")
async def get_intent_queries(intent_name: Optional[str] = None):
    """Get ground truth SQL queries linked to intents.
    
    Maps intents to validated SQL examples that demonstrate the correct interpretation.
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            query = """
                SELECT i.intent_name, i.intent_category, i.description,
                       iq.query_category, iq.query_file, iq.query_index, iq.query_name
                FROM schema_intents i
                JOIN schema_intent_queries iq ON i.intent_id = iq.intent_id
                WHERE 1=1
            """
            params = {}
            if intent_name:
                query += " AND i.intent_name = :intent_name"
                params["intent_name"] = intent_name
            query += " ORDER BY i.intent_category, i.intent_name"
            
            result = conn.execute(text(query), params)
            queries = [
                {
                    "intent_name": r[0], "intent_category": r[1], "intent_description": r[2],
                    "query_category": r[3], "query_file": r[4], 
                    "query_index": r[5], "query_name": r[6]
                }
                for r in result.fetchall()
            ]
            return {"intent_queries": queries, "count": len(queries)}
    except Exception as e:
        return {"error": str(e), "intent_queries": [], "count": 0}


@app.get("/mcp/tools/resolve_field_for_intent")
async def resolve_field_for_intent(table_name: str, field_name: str, intent_name: str):
    """Resolve which concept interpretation applies for a field given an intent.
    
    This is the intent-based semantic disambiguation endpoint.
    Unlike perspective (which uses priority weights), intent uses binary elevation:
    - 1.0 = this interpretation is THE correct one for this intent
    - 0.0 = this interpretation should be ignored for this intent
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT c.concept_id, c.concept_name, CASE WHEN c.computation_template IS NOT NULL AND c.computation_template <> '' THEN 'metric' ELSE NULL END AS concept_type, c.description, c.domain,
                       cf.context_hint, ic.intent_factor_weight, ic.explanation,
                       i.typical_question
                FROM schema_concept_fields cf
                JOIN schema_concepts c ON cf.concept_id = c.concept_id
                JOIN schema_intent_concepts ic ON c.concept_id = ic.concept_id
                JOIN schema_intents i ON ic.intent_id = i.intent_id
                WHERE cf.table_name = :table_name 
                  AND cf.field_name = :field_name
                  AND i.intent_name = :intent_name
                  AND ic.intent_factor_weight = 1.0
                LIMIT 1
            """), {"table_name": table_name, "field_name": field_name, "intent_name": intent_name})
            
            row = result.fetchone()
            if row:
                return {
                    "resolved": True,
                    "table_name": table_name,
                    "field_name": field_name,
                    "intent": intent_name,
                    "typical_question": row[8],
                    "concept": {
                        "concept_id": row[0], "concept_name": row[1],
                        "concept_type": row[2], "description": row[3], 
                        "domain": row[4], "context_hint": row[5],
                        "intent_factor_weight": row[6], 
                        "explanation": row[7]
                    }
                }
            else:
                return {
                    "resolved": False,
                    "table_name": table_name,
                    "field_name": field_name,
                    "intent": intent_name,
                    "message": "No elevated concept found for this field/intent combination"
                }
    except Exception as e:
        return {"error": str(e), "resolved": False}


@app.get("/mcp/tools/get_intent_perspectives")
async def get_intent_perspectives(intent_name: Optional[str] = None):
    """Get Intent → Perspective bridge rows (Perspective_Intents).

    Reads the `schema_intent_perspectives` bridge table — the SQLite
    source-of-truth that feeds the ArangoDB `Perspective_Intents` document
    collection (keyed by `perspective__intent`).

    The retired three-hop traversal
    `Intent -[OPERATES_WITHIN]-> Perspective -[USES_DEFINITION]-> Concept`
    has been deprecated. Perspective is now a property carried on each
    bridge row, and `(Intent, Field) -> Concept` is resolved directly via
    the bridge tables `schema_intent_perspectives` and
    `schema_perspective_concepts` (no Perspective vertex involved).

    Each returned row includes a `relationship` field set to
    `"PERSPECTIVE_INTENT_ROW"` (the bridge-row shape).
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            query = """
                SELECT i.intent_name, i.intent_category, i.description as intent_desc,
                       ip.intent_factor_weight, ip.explanation,
                       p.perspective_id, p.perspective_name, p.description as perspective_desc,
                       p.stakeholder_role
                FROM schema_intents i
                JOIN schema_intent_perspectives ip ON i.intent_id = ip.intent_id
                JOIN schema_perspectives p ON ip.perspective_id = p.perspective_id
                WHERE ip.intent_factor_weight = 1.0
            """
            params = {}
            if intent_name:
                query += " AND i.intent_name = :intent_name"
                params["intent_name"] = intent_name
            query += " ORDER BY i.intent_category, i.intent_name"
            
            result = conn.execute(text(query), params)
            mappings = [
                {
                    "intent_name": r[0], "intent_category": r[1], 
                    "intent_description": r[2],
                    "relationship": "PERSPECTIVE_INTENT_ROW",
                    "intent_factor_weight": r[3], "explanation": r[4],
                    "perspective": {
                        "perspective_id": r[5], "perspective_name": r[6],
                        "description": r[7], "stakeholder_role": r[8]
                    }
                }
                for r in result.fetchall()
            ]
            return {
                "perspective_intent_rows": mappings,
                "count": len(mappings),
            }
    except Exception as e:
        return {"error": str(e), "count": 0}


class CommitEdgeRequest(BaseModel):
    predicate: str
    source_id: str
    target_id: str
    intent: Optional[str] = None
    perspective: Optional[str] = None
    category: Optional[str] = None
    explanation: Optional[str] = None
    binding_key: Optional[str] = None
    concept_anchor: Optional[str] = None
    from_column: Optional[str] = None
    to_column: Optional[str] = None


def _parse_entity_name(label: str) -> str:
    """Strip the ' (source_label)' suffix from a UI display label.

    'production_orders (ERP_Instance_1)' → 'production_orders'
    'defect_quality_trending'            → 'defect_quality_trending'
    """
    return label.split(" (")[0].strip()


def _resolve_arango_handle(label: str, db, graph_name: str) -> str:
    """Translate a UI display label to an ArangoDB document handle.

    If label already contains '/' it is returned unchanged (already a handle).
    Otherwise the entity name is extracted and the known vertex collections are
    searched in priority order:
      1. intents
      2. concepts
      3. bindings
      4. {graph_name}_node  (atomic column / table nodes)

    Raises ValueError when the entity cannot be resolved.
    """
    if "/" in label:
        return label

    entity_name = _parse_entity_name(label)

    priority_collections = [
        "intents",
        "concepts",
        "bindings",
        f"{graph_name}_node",
    ]

    for coll_name in priority_collections:
        try:
            coll = db.collection(coll_name)
            if coll.has(entity_name):
                return f"{coll_name}/{entity_name}"
            # Also try table_ prefixed key for atomic node collection
            if coll_name == f"{graph_name}_node":
                table_key = f"table_{entity_name}"
                if coll.has(table_key):
                    return f"{coll_name}/{table_key}"
        except Exception:
            continue

    raise ValueError(
        f"Entity {entity_name!r} not found in any vertex collection "
        f"(intents, concepts, bindings, {graph_name}_node). "
        "Pass a fully-qualified ArangoDB handle (collection/key) to skip resolution."
    )


# ── Canonical SQLite-first authoring (HAS_COLUMN / FOREIGN_KEY / RESOLVES_TO) ─
# These predicates have a canonical edge type in the SQLite source of truth, so
# the Define Relationship UI writes them to sql_graph_authored_edges first; the
# exporter merges them into sql_graph_edges and the existing sync pipeline
# carries them to the live ArangoDB graph. ArangoDB is updated best-effort only.
SQL_GRAPH_NODES_TABLE = "sql_graph_nodes"
AUTHORED_EDGES_TABLE = "sql_graph_authored_edges"
CANONICAL_SQLITE_PREDICATES = frozenset({"HAS_COLUMN", "FOREIGN_KEY", "RESOLVES_TO"})


def _resolve_sql_graph_endpoint(conn, label: str, expect: str) -> dict:
    """Resolve a UI display label to a canonical node in ``sql_graph_nodes``.

    ``expect`` is 'table' or 'column'. The label may carry a ' (source)' suffix
    and/or a schema prefix (e.g. 'dbo.PART' for a table, 'part.part_id' for a
    column); matching is case-insensitive against the verified SQLite source so
    an endpoint that is not a real canonical node is rejected (no dangling edge).

    Returns {table, column, node_id}; raises ValueError when no node matches.
    """
    raw = label.split(" (")[0].strip()
    segs = [s for s in raw.split(".") if s]
    if not segs:
        raise ValueError(f"Empty endpoint label: {label!r}")

    if expect == "column":
        if len(segs) < 2:
            raise ValueError(
                f"{label!r} is not a column reference (expected table.column)")
        table_part, col_part = segs[-2], segs[-1]
        row = conn.execute(
            f"SELECT table_name, column_name, _id FROM {SQL_GRAPH_NODES_TABLE} "
            "WHERE node_type='column' AND LOWER(table_name)=LOWER(?) "
            "AND LOWER(column_name)=LOWER(?) LIMIT 1",
            (table_part, col_part),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"No canonical column node in sql_graph_nodes for {label!r} "
                f"({table_part}.{col_part})")
        return {"table": row["table_name"], "column": row["column_name"], "node_id": row["_id"]}

    table_part = segs[-1]
    row = conn.execute(
        f"SELECT table_name, _id FROM {SQL_GRAPH_NODES_TABLE} "
        "WHERE node_type='table' AND LOWER(table_name)=LOWER(?) LIMIT 1",
        (table_part,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"No canonical table node in sql_graph_nodes for {label!r} ({table_part})")
    return {"table": row["table_name"], "column": None, "node_id": row["_id"]}


def _best_effort_arango_canonical_sync(predicate: str, req) -> str:
    """Mirror a canonical authored edge into the live ArangoDB graph (best-effort).

    Never raises: ArangoDB is downstream of the SQLite source here, so any
    failure (offline, unresolved handle, write error) is swallowed and reported
    as a note appended to the response message. Uses the same AQL UPSERT and the
    same live-graph handle resolution the predicate used before this became
    SQLite-first, so live behaviour is unchanged — only demoted to secondary.
    """
    import importlib
    try:
        gs = importlib.import_module("graph_sync")
        client = gs.get_arango_client()
        db = gs.get_arango_db(client)
        graph_name = gs.GRAPH_NAME
        source_handle = _resolve_arango_handle(req.source_id, db, graph_name)
        target_handle = _resolve_arango_handle(req.target_id, db, graph_name)
    except Exception:
        return "; ArangoDB sync skipped (offline or endpoint unresolved)"

    try:
        if predicate == "RESOLVES_TO":
            weight = 1
            # The live legacy named-graph edge collection is literally named
            # ``elevates`` (defined in graph_sync.EDGE_COLLECTIONS). The v16 rename
            # changed the canonical edge_type TOKEN (``resolves_to``, stored in SQLite
            # and the flat ``manufacturing_graph_edge`` collection) — it intentionally
            # does NOT rename this legacy live collection. This best-effort mirror keeps
            # writing into ``elevates`` so live behaviour is unchanged. See this
            # function's docstring and load_canonical_to_arango.py (which is the real
            # canonical live load, targeting manufacturing_graph_edge).
            aql = """
            UPSERT { _from: @source, _to: @target }
            INSERT { _from: @source, _to: @target, weight: @weight,
                     intent_name: @intent, explanation: @explanation,
                     category: @category, perspective: @perspective,
                     created_by: 'define_relationship_ui' }
            UPDATE { weight: @weight, intent_name: @intent,
                     explanation: @explanation, category: @category,
                     perspective: @perspective }
            IN elevates RETURN NEW
            """
            db.aql.execute(aql, bind_vars={
                "source": source_handle, "target": target_handle, "weight": weight,
                "intent": req.intent or "", "explanation": req.explanation or f"{predicate} via UI",
                "category": req.category or "", "perspective": req.perspective or "",
            })
        elif predicate == "HAS_COLUMN":
            aql = """
            UPSERT { _from: @source, _to: @target }
            INSERT { _from: @source, _to: @target, category: @category,
                     perspective: @perspective, created_by: 'define_relationship_ui' }
            UPDATE { category: @category, perspective: @perspective }
            IN HAS_COLUMN RETURN NEW
            """
            db.aql.execute(aql, bind_vars={
                "source": source_handle, "target": target_handle,
                "category": req.category or "", "perspective": req.perspective or "",
            })
        elif predicate == "FOREIGN_KEY":
            aql = """
            UPSERT { _from: @source, _to: @target }
            INSERT { _from: @source, _to: @target, from_column: @from_column,
                     to_column: @to_column, category: @category,
                     perspective: @perspective, created_by: 'define_relationship_ui' }
            UPDATE { from_column: @from_column, to_column: @to_column,
                     category: @category, perspective: @perspective }
            IN FOREIGN_KEY RETURN NEW
            """
            db.aql.execute(aql, bind_vars={
                "source": source_handle, "target": target_handle,
                "from_column": req.from_column or "", "to_column": req.to_column or "",
                "category": req.category or "", "perspective": req.perspective or "",
            })
        return "; synced to live ArangoDB"
    except Exception:
        return "; ArangoDB sync skipped (write failed)"


def _commit_canonical_edge_sqlite_first(predicate: str, req) -> dict:
    """Write a canonical edge to ``sql_graph_authored_edges`` (source of truth).

    Resolves endpoints against ``sql_graph_nodes``, records the authored edge
    with duplicate protection (the UNIQUE tuple), then best-effort syncs to the
    live ArangoDB graph. Returns the standard {ok, created, edge_id, message}.
    """
    import sqlite3 as _sqlite3
    from fastapi import HTTPException

    conn = _sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = _sqlite3.Row
    try:
        if predicate == "HAS_COLUMN":
            try:
                src = _resolve_sql_graph_endpoint(conn, req.source_id, "table")
                tgt = _resolve_sql_graph_endpoint(conn, req.target_id, "column")
            except ValueError as ve:
                raise HTTPException(status_code=422, detail=str(ve))
            edge_type = "has_column"
            fields = dict(from_table=src["table"], from_column="",
                          to_table=tgt["table"], to_column=tgt["column"],
                          perspective="system", weight=None, concept=None)
        elif predicate == "FOREIGN_KEY":
            try:
                src = _resolve_sql_graph_endpoint(conn, req.source_id, "table")
                tgt = _resolve_sql_graph_endpoint(conn, req.target_id, "table")
            except ValueError as ve:
                raise HTTPException(status_code=422, detail=str(ve))
            edge_type = "references"
            fields = dict(from_table=src["table"], from_column=(req.from_column or ""),
                          to_table=tgt["table"], to_column=(req.to_column or ""),
                          perspective="system", weight=None, concept=None)
        elif predicate == "RESOLVES_TO":
            persp = (req.perspective or "").strip()
            if not persp or persp.lower() == "system":
                raise HTTPException(status_code=422,
                    detail="RESOLVES_TO requires a business perspective "
                           "(choose a category other than ALL)")
            try:
                tgt = _resolve_sql_graph_endpoint(conn, req.target_id, "column")
            except ValueError as ve:
                raise HTTPException(status_code=422,
                    detail=f"RESOLVES_TO target must be a canonical column node: {ve}")
            edge_type = "resolves_to"
            fields = dict(from_table=tgt["table"], from_column=tgt["column"],
                          to_table=tgt["table"], to_column=tgt["column"],
                          perspective=persp,
                          weight=1,
                          concept=(req.concept_anchor or ""))
        else:
            raise HTTPException(status_code=400,
                detail=f"Not a canonical SQLite predicate: {predicate!r}")

        existing = conn.execute(
            f"""SELECT authored_id FROM {AUTHORED_EDGES_TABLE}
                WHERE edge_type=? AND from_table=? AND from_column=?
                  AND to_table=? AND to_column=? AND perspective=?""",
            (edge_type, fields["from_table"], fields["from_column"],
             fields["to_table"], fields["to_column"], fields["perspective"]),
        ).fetchone()
        if existing is None:
            cur = conn.execute(
                f"""INSERT INTO {AUTHORED_EDGES_TABLE}
                    (edge_type, from_table, from_column, to_table, to_column,
                     perspective, weight, concept, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'define_relationship_ui')""",
                (edge_type, fields["from_table"], fields["from_column"],
                 fields["to_table"], fields["to_column"], fields["perspective"],
                 fields["weight"], fields["concept"]),
            )
            authored_id = cur.lastrowid
            created = True
        else:
            authored_id = existing["authored_id"]
            conn.execute(
                f"UPDATE {AUTHORED_EDGES_TABLE} SET weight=?, concept=? WHERE authored_id=?",
                (fields["weight"], fields["concept"], authored_id),
            )
            created = False
        conn.commit()
        edge_id = f"sqlite:{AUTHORED_EDGES_TABLE}/{authored_id}"
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SQLite authoring write failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    arango_note = _best_effort_arango_canonical_sync(predicate, req)
    verb = "authored in SQLite" if created else "already authored in SQLite — updated"
    return {"ok": True, "created": created, "edge_id": edge_id,
            "message": f"{predicate} edge {verb}{arango_note}"}


@app.post("/mcp/tools/commit_edge")
async def commit_edge(req: CommitEdgeRequest):
    """Write a new edge (or bridge document) using predicate-based routing.

    Predicate routing:
      RESOLVES_TO            → ArangoDB legacy ``elevates`` edge collection (weight 1;
                               collection name unchanged by v16 — only the canonical
                               edge_type token became ``resolves_to``)
      BOUND_TO               → ArangoDB bound_to edge collection
      HAS_COLUMN             → ArangoDB HAS_COLUMN edge collection
      FOREIGN_KEY            → ArangoDB FOREIGN_KEY edge collection
      MAPS_TO_CONCEPT / CAN_MEAN → ArangoDB CAN_MEAN edge collection
      OPERATES_WITHIN        → SQLite schema_intent_perspectives (source of truth)
                               then ArangoDB Perspective_Intents (best-effort sync)
      USES_DEFINITION        → SQLite schema_perspective_concepts (source of truth)
                               then ArangoDB Perspective_Concepts (best-effort sync)

    Returns {ok, created, edge_id, message} on success; raises HTTP 400/422 on bad input,
    HTTP 503 if ArangoDB is unreachable for Arango-routed predicates.
    `created` is True when a new edge was inserted, False when an existing edge was updated.
    """
    import sqlite3 as _sqlite3
    import importlib
    from fastapi import HTTPException

    predicate = req.predicate.upper().strip()

    # ── Bridge predicates: SQLite is source of truth ─────────────────────────
    if predicate == "OPERATES_WITHIN":
        if not (req.intent and req.perspective):
            raise HTTPException(status_code=422,
                detail="OPERATES_WITHIN requires intent and perspective")
        try:
            conn = _sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = _sqlite3.Row
            intent_row = conn.execute(
                "SELECT intent_id FROM schema_intents WHERE intent_name = ?",
                (req.intent,),
            ).fetchone()
            if intent_row is None:
                raise HTTPException(status_code=422,
                    detail=f"Intent not found in SQLite: {req.intent!r}")
            perspective_row = conn.execute(
                "SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?",
                (req.perspective,),
            ).fetchone()
            if perspective_row is None:
                raise HTTPException(status_code=422,
                    detail=f"Perspective not found in SQLite: {req.perspective!r}")
            existing = conn.execute(
                "SELECT 1 FROM schema_intent_perspectives WHERE intent_id = ? AND perspective_id = ?",
                (intent_row["intent_id"], perspective_row["perspective_id"]),
            ).fetchone()
            row_created = existing is None
            conn.execute(
                """INSERT INTO schema_intent_perspectives
                       (intent_id, perspective_id, intent_factor_weight, explanation)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(intent_id, perspective_id) DO UPDATE SET
                       intent_factor_weight = excluded.intent_factor_weight,
                       explanation = excluded.explanation""",
                (intent_row["intent_id"], perspective_row["perspective_id"],
                 req.explanation or "Added via Define Relationship UI"),
            )
            conn.commit()
            edge_id = f"sqlite:schema_intent_perspectives/{req.intent}__{req.perspective}"
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SQLite write failed: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # Best-effort ArangoDB sync — never blocks the response
        arango_note = ""
        try:
            gs = importlib.import_module("graph_sync")
            client = gs.get_arango_client()
            db = gs.get_arango_db(client)
            seg3 = lambda s: "".join(c for c in s if c.isalpha())[:3].upper()
            key = f"{seg3(req.intent)}_001_{req.perspective}"
            db.collection("Perspective_Intents").insert(
                {"_key": key, "perspective": req.perspective,
                 "intent": req.intent, "created_by": "define_relationship_ui"},
                overwrite=True,
            )
            arango_note = "; synced to ArangoDB Perspective_Intents"
        except Exception:
            arango_note = "; ArangoDB sync skipped (offline)"

        ow_msg = ("OPERATES_WITHIN bridge row saved to SQLite" if row_created
                  else "OPERATES_WITHIN bridge row already exists — updated in SQLite")
        return {"ok": True, "created": row_created, "edge_id": edge_id,
                "message": f"{ow_msg}{arango_note}"}

    if predicate == "USES_DEFINITION":
        if not (req.perspective and req.concept_anchor):
            raise HTTPException(status_code=422,
                detail="USES_DEFINITION requires perspective and concept_anchor (concept name)")
        try:
            conn = _sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = _sqlite3.Row
            perspective_row = conn.execute(
                "SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?",
                (req.perspective,),
            ).fetchone()
            if perspective_row is None:
                raise HTTPException(status_code=422,
                    detail=f"Perspective not found in SQLite: {req.perspective!r}")
            concept_row = conn.execute(
                "SELECT concept_id FROM schema_concepts WHERE concept_name = ?",
                (req.concept_anchor,),
            ).fetchone()
            if concept_row is None:
                raise HTTPException(status_code=422,
                    detail=f"Concept not found in SQLite: {req.concept_anchor!r}")
            existing = conn.execute(
                "SELECT 1 FROM schema_perspective_concepts WHERE perspective_id = ? AND concept_id = ?",
                (perspective_row["perspective_id"], concept_row["concept_id"]),
            ).fetchone()
            row_created = existing is None
            conn.execute(
                """INSERT INTO schema_perspective_concepts
                       (perspective_id, concept_id, relationship_type, priority_weight)
                   VALUES (?, ?, 'USES_DEFINITION', 1)
                   ON CONFLICT(perspective_id, concept_id) DO UPDATE SET
                       relationship_type = excluded.relationship_type,
                       priority_weight   = excluded.priority_weight""",
                (perspective_row["perspective_id"], concept_row["concept_id"]),
            )
            conn.commit()
            edge_id = f"sqlite:schema_perspective_concepts/{req.perspective}__{req.concept_anchor}"
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SQLite write failed: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # Best-effort ArangoDB sync
        arango_note = ""
        try:
            gs = importlib.import_module("graph_sync")
            client = gs.get_arango_client()
            db = gs.get_arango_db(client)
            seg3 = lambda s: "".join(c for c in s if c.isalpha())[:3].upper()
            key = f"{seg3(req.perspective)}_001_{req.concept_anchor}"
            db.collection("Perspective_Concepts").insert(
                {"_key": key, "perspective": req.perspective,
                 "concept": req.concept_anchor, "created_by": "define_relationship_ui"},
                overwrite=True,
            )
            arango_note = "; synced to ArangoDB Perspective_Concepts"
        except Exception:
            arango_note = "; ArangoDB sync skipped (offline)"

        ud_msg = ("USES_DEFINITION bridge row saved to SQLite" if row_created
                  else "USES_DEFINITION bridge row already exists — updated in SQLite")
        return {"ok": True, "created": row_created, "edge_id": edge_id,
                "message": f"{ud_msg}{arango_note}"}

    # ── Canonical predicates: SQLite source of truth, ArangoDB best-effort ────
    if predicate in CANONICAL_SQLITE_PREDICATES:
        return _commit_canonical_edge_sqlite_first(predicate, req)

    # ── ArangoDB-routed predicates (BOUND_TO, MAPS_TO_CONCEPT/CAN_MEAN) ───────
    try:
        gs = importlib.import_module("graph_sync")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"graph_sync unavailable: {e}")

    try:
        client = gs.get_arango_client()
        db = gs.get_arango_db(client)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"ArangoDB connection failed: {e}")

    graph_name = gs.GRAPH_NAME

    # Resolve display labels to real ArangoDB document handles.
    # If source_id / target_id are already handles (contain '/') they pass through.
    try:
        source_handle = _resolve_arango_handle(req.source_id, db, graph_name)
        target_handle = _resolve_arango_handle(req.target_id, db, graph_name)
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))

    try:
        if predicate == "BOUND_TO":
            aql = """
            UPSERT { _from: @source, _to: @target }
            INSERT {
                _from:          @source,
                _to:            @target,
                binding_key:    @binding_key,
                concept_anchor: @concept_anchor,
                category:       @category,
                perspective:    @perspective,
                created_by:     'define_relationship_ui'
            }
            UPDATE {
                binding_key:    @binding_key,
                concept_anchor: @concept_anchor,
                category:       @category,
                perspective:    @perspective
            }
            IN bound_to
            RETURN { doc: NEW, created: OLD == null }
            """
            result = list(db.aql.execute(aql, bind_vars={
                "source": source_handle,
                "target": target_handle,
                "binding_key": req.binding_key or "",
                "concept_anchor": req.concept_anchor or "",
                "category": req.category or "",
                "perspective": req.perspective or "",
            }))
            row = result[0]
            doc, created = row["doc"], row["created"]
            msg = ("BOUND_TO edge created" if created
                   else "BOUND_TO edge already exists — updated")
            return {"ok": True, "created": created, "edge_id": doc["_id"], "message": msg}

        elif predicate in ("MAPS_TO_CONCEPT", "CAN_MEAN"):
            aql = """
            UPSERT { _from: @source, _to: @target }
            INSERT {
                _from:       @source,
                _to:         @target,
                category:    @category,
                perspective: @perspective,
                created_by:  'define_relationship_ui'
            }
            UPDATE { category: @category, perspective: @perspective }
            IN CAN_MEAN
            RETURN { doc: NEW, created: OLD == null }
            """
            result = list(db.aql.execute(aql, bind_vars={
                "source": source_handle, "target": target_handle,
                "category": req.category or "",
                "perspective": req.perspective or "",
            }))
            row = result[0]
            doc, created = row["doc"], row["created"]
            msg = ("MAPS_TO_CONCEPT (CAN_MEAN) edge created" if created
                   else "MAPS_TO_CONCEPT (CAN_MEAN) edge already exists — updated")
            return {"ok": True, "created": created, "edge_id": doc["_id"], "message": msg}

        else:
            raise HTTPException(status_code=400,
                detail=f"Unknown predicate: {req.predicate!r}. "
                "Supported: RESOLVES_TO, HAS_COLUMN, FOREIGN_KEY "
                "(SQLite-first), BOUND_TO, MAPS_TO_CONCEPT, OPERATES_WITHIN, "
                "USES_DEFINITION")

    except Exception as e:
        if "HTTPException" in type(e).__name__:
            raise
        raise HTTPException(status_code=500, detail=f"ArangoDB write failed: {e}")


@app.delete("/mcp/tools/commit_edge")
async def delete_commit_edge(edge_id: str):
    """Remove an edge or bridge document previously created by POST /mcp/tools/commit_edge.

    Accepts the edge_id returned by the POST handler and routes deletion based on prefix:
      sqlite:schema_intent_perspectives/<perspective>__<intent>  → DELETE from SQLite
      sqlite:schema_perspective_concepts/<perspective>__<concept> → DELETE from SQLite
      <collection>/<key>  (no prefix)                            → ArangoDB document remove

    Returns {ok, message} on success; raises HTTP 404/422/503 on failure.
    """
    import sqlite3 as _sqlite3
    import importlib
    from fastapi import HTTPException

    if not edge_id:
        raise HTTPException(status_code=422, detail="edge_id is required")

    # ── SQLite-backed bridge rows ─────────────────────────────────────────────
    if edge_id.startswith("sqlite:"):
        remainder = edge_id[len("sqlite:"):]
        # remainder is table_name/composite_key
        if "/" not in remainder:
            raise HTTPException(status_code=422, detail=f"Malformed sqlite edge_id: {edge_id!r}")
        table_name, composite_key = remainder.split("/", 1)

        try:
            conn = _sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = _sqlite3.Row

            if table_name == "schema_intent_perspectives":
                # key format: intent__perspective  (matches POST edge_id: sqlite:schema_intent_perspectives/{intent}__{perspective})
                if "__" not in composite_key:
                    raise HTTPException(status_code=422,
                        detail=f"Cannot parse intent__perspective from {composite_key!r}")
                intent, perspective = composite_key.split("__", 1)
                intent_row = conn.execute(
                    "SELECT intent_id FROM schema_intents WHERE intent_name = ?",
                    (intent,),
                ).fetchone()
                perspective_row = conn.execute(
                    "SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?",
                    (perspective,),
                ).fetchone()
                if intent_row is None or perspective_row is None:
                    raise HTTPException(status_code=404,
                        detail=f"Bridge row not found: intent={intent!r}, perspective={perspective!r}")
                cur = conn.execute(
                    "DELETE FROM schema_intent_perspectives WHERE intent_id = ? AND perspective_id = ?",
                    (intent_row["intent_id"], perspective_row["perspective_id"]),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404,
                        detail=(
                            f"Bridge row already deleted: "
                            f"intent={intent!r}, perspective={perspective!r}"
                        ))
                conn.commit()

            elif table_name == "sql_graph_authored_edges":
                # key format: authored_id (integer PK from the authoring table)
                try:
                    authored_id = int(composite_key)
                except ValueError:
                    raise HTTPException(status_code=422,
                        detail=f"Malformed authored edge id: {composite_key!r}")
                cur = conn.execute(
                    "DELETE FROM sql_graph_authored_edges WHERE authored_id = ?",
                    (authored_id,),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404,
                        detail=f"Authored edge already deleted: id={authored_id}")
                conn.commit()

            elif table_name == "schema_perspective_concepts":
                # key format: perspective__concept
                if "__" not in composite_key:
                    raise HTTPException(status_code=422,
                        detail=f"Cannot parse perspective__concept from {composite_key!r}")
                perspective, concept = composite_key.split("__", 1)
                perspective_row = conn.execute(
                    "SELECT perspective_id FROM schema_perspectives WHERE perspective_name = ?",
                    (perspective,),
                ).fetchone()
                concept_row = conn.execute(
                    "SELECT concept_id FROM schema_concepts WHERE concept_name = ?",
                    (concept,),
                ).fetchone()
                if perspective_row is None or concept_row is None:
                    raise HTTPException(status_code=404,
                        detail=f"Bridge row not found: perspective={perspective!r}, concept={concept!r}")
                cur = conn.execute(
                    "DELETE FROM schema_perspective_concepts WHERE perspective_id = ? AND concept_id = ?",
                    (perspective_row["perspective_id"], concept_row["concept_id"]),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404,
                        detail=(
                            f"Bridge row already deleted: "
                            f"perspective={perspective!r}, concept={concept!r}"
                        ))
                conn.commit()

            else:
                raise HTTPException(status_code=422,
                    detail=f"Unknown SQLite table in edge_id: {table_name!r}")

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SQLite delete failed: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return {"ok": True, "message": f"Bridge row deleted from {table_name}"}

    # ── ArangoDB-backed edges ─────────────────────────────────────────────────
    # edge_id is a standard ArangoDB document handle: collection/key
    if "/" not in edge_id:
        raise HTTPException(status_code=422,
            detail=f"Malformed edge_id (expected collection/key or sqlite:…): {edge_id!r}")

    collection_name, doc_key = edge_id.split("/", 1)

    # Restrict deletion to collections that commit_edge is allowed to write.
    # This prevents callers from removing arbitrary ArangoDB documents.
    _ALLOWED_EDGE_COLLECTIONS = frozenset({
        "elevates", "bound_to", "HAS_COLUMN", "FOREIGN_KEY", "CAN_MEAN",
    })
    if collection_name not in _ALLOWED_EDGE_COLLECTIONS:
        raise HTTPException(status_code=422,
            detail=(
                f"Collection {collection_name!r} is not a valid undo target. "
                f"Allowed: {sorted(_ALLOWED_EDGE_COLLECTIONS)}"
            ))

    try:
        gs = importlib.import_module("graph_sync")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"graph_sync unavailable: {e}")

    try:
        client = gs.get_arango_client()
        db = gs.get_arango_db(client)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"ArangoDB connection failed: {e}")

    try:
        col = db.collection(collection_name)
        if not col.has(doc_key):
            raise HTTPException(status_code=404,
                detail=f"Edge not found in ArangoDB: {edge_id!r}")
        doc = col.get(doc_key)
        if doc.get("created_by") != "define_relationship_ui":
            raise HTTPException(status_code=403,
                detail="This edge was not created through the UI and cannot be undone here.")
        col.delete(doc_key)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ArangoDB delete failed: {e}")

    return {"ok": True, "message": f"Edge {edge_id!r} removed from ArangoDB"}


@app.get("/mcp/tools/list_table_columns")
async def list_table_columns(table: str):
    """Return column metadata for a single ERP table.

    Source: SQLite PRAGMA table_info — same source used by structural
    containment sync to populate the 'columns' ArangoDB collection.

    Returns:
        {
          "table_name": str,
          "columns": [
            {
              "column_name": str,
              "data_type": str,
              "not_null": bool,
              "primary_key": bool,
              "foreign_key": bool,    # declared FK child column (PRAGMA foreign_key_list)
              "qualified_name": str   # "table.column"
            },
            ...
          ]
        }
    Raises HTTP 404 when the table does not exist in the SQLite database.
    Raises HTTP 422 when no table name is supplied.
    """
    import sqlite3 as _sqlite3

    if not table:
        raise HTTPException(status_code=422, detail="table parameter is required")

    try:
        conn = _sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = _sqlite3.Row

        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()

        if not rows:
            # Case-insensitive fallback: find the real physical table name.
            match = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND LOWER(name)=LOWER(?)",
                (table,),
            ).fetchone()
            if match:
                physical = match["name"]
                rows = conn.execute(f"PRAGMA table_info({physical})").fetchall()
                table = physical

        conn.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SQLite error: {exc}")

    if not rows:
        return {"table_name": table, "columns": []}

    # Declared foreign-key child columns, from the same PRAGMA the graph
    # exporter uses to build references edges — so this endpoint and the graph
    # agree on which columns are FKs.
    try:
        fk_conn = _sqlite3.connect(SQLITE_DB_PATH)
        fk_conn.row_factory = _sqlite3.Row
        fk_child_cols = {
            r["from"]
            for r in fk_conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }
        fk_conn.close()
    except Exception:
        fk_child_cols = set()

    columns = [
        {
            "column_name": row["name"],
            "data_type": row["type"] or "TEXT",
            "not_null": bool(row["notnull"]),
            "primary_key": bool(row["pk"]),
            "foreign_key": row["name"] in fk_child_cols,
            "qualified_name": f"{table}.{row['name']}",
        }
        for row in rows
    ]
    return {"table_name": table, "columns": columns}


@app.get("/mcp/tools/get_entity_config")
async def get_entity_config(entity: str = ""):
    """Return DAB-style entity + field descriptions from dab_config.json.

    Source of truth is the flat JSON file — SQLite import is planned later.
    No entity param → returns list of all entity names.
    ?entity=Customer → returns that entity's full config block.
    """
    import json as _json

    config_path = os.path.join(os.path.dirname(__file__), "dab_config.json")
    try:
        with open(config_path, "r") as f:
            config = _json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="dab_config.json not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Config read error: {exc}")

    entities = config.get("entities", {})

    if not entity:
        return {
            "schema_version": config.get("schema_version"),
            "data_source": config.get("data_source"),
            "entity_names": sorted(entities.keys()),
            "entity_count": len(entities),
        }

    if entity not in entities:
        # Case-insensitive fallback
        match = next((k for k in entities if k.lower() == entity.lower()), None)
        if not match:
            raise HTTPException(status_code=404, detail=f"Entity '{entity}' not found in dab_config.json")
        entity = match

    return {"entity": entity, **entities[entity]}


@app.get("/mcp/tools/graph_stats")
async def get_graph_stats():
    """Return total edge counts per collection from ArangoDB plus SQLite bridge row counts.

    Used by the DefineRelationship UI to show a live edge-count badge so users can
    confirm that an 'Add to Graph' commit actually grew the graph.

    Returns:
        {
          "total_edges": int,          # sum across all edge collections
          "collections": {             # per-collection counts
            "elevates": int,
            "bound_to": int,
            "HAS_COLUMN": int,
            "FOREIGN_KEY": int,
            "CAN_MEAN": int,
            "Perspective_Intents": int,
            "Perspective_Concepts": int,
          },
          "arango_available": bool,
          "sqlite_bridge_rows": int,   # schema_intent_perspectives + schema_perspective_concepts
          "sql_graph_authored_rows": int,  # SME-authored canonical edges (SQLite source of truth)
        }

    Note: sql_graph_authored_rows is reported separately and is NOT added to
    total_edges — once the exporter/sync pipeline carries an authored edge to
    the live ArangoDB graph it is already counted in that collection, so folding
    it into total_edges would double-count it.
    """
    import importlib
    import sqlite3 as _sqlite3

    ARANGO_EDGE_COLLECTIONS = [
        "elevates", "bound_to", "HAS_COLUMN", "FOREIGN_KEY", "CAN_MEAN",
        "Perspective_Intents", "Perspective_Concepts",
    ]

    counts: dict = {}
    arango_available = False

    try:
        gs = importlib.import_module("graph_sync")
        client = gs.get_arango_client()
        db = gs.get_arango_db(client)
        for col_name in ARANGO_EDGE_COLLECTIONS:
            try:
                counts[col_name] = db.collection(col_name).count()
            except Exception:
                counts[col_name] = 0
        arango_available = True
    except Exception:
        for col_name in ARANGO_EDGE_COLLECTIONS:
            counts[col_name] = 0

    # SQLite bridge rows + authored canonical edges (always available locally)
    sqlite_bridge_rows = 0
    sql_graph_authored_rows = 0
    try:
        conn = _sqlite3.connect(SQLITE_DB_PATH)
        r1 = conn.execute("SELECT COUNT(*) FROM schema_intent_perspectives").fetchone()
        r2 = conn.execute("SELECT COUNT(*) FROM schema_perspective_concepts").fetchone()
        sqlite_bridge_rows = (r1[0] if r1 else 0) + (r2[0] if r2 else 0)
        try:
            r3 = conn.execute("SELECT COUNT(*) FROM sql_graph_authored_edges").fetchone()
            sql_graph_authored_rows = r3[0] if r3 else 0
        except Exception:
            sql_graph_authored_rows = 0
        conn.close()
    except Exception:
        pass

    total_edges = sum(counts.values()) + sqlite_bridge_rows

    return {
        "total_edges": total_edges,
        "collections": counts,
        "arango_available": arango_available,
        "sqlite_bridge_rows": sqlite_bridge_rows,
        "sql_graph_authored_rows": sql_graph_authored_rows,
    }


@app.get("/mcp/tools/resolve_semantic_path")
async def resolve_semantic_path(table_name: str, field_name: str, intent_name: str):
    """Resolve (Intent, Field) → Concept via the perspective bridge rows.

    This is the complete semantic disambiguation endpoint. It follows the
    current bridge-row model (the legacy three-hop traversal
    `Intent -[OPERATES_WITHIN]-> Perspective -[USES_DEFINITION]-> Concept`
    has been retired — Perspective is no longer a vertex):

    1. Start from Intent.
    2. Look up the perspective(s) carried on the matching
       `schema_intent_perspectives` bridge rows (the SQLite feed for the
       ArangoDB `Perspective_Intents` document collection — formerly
       OPERATES_WITHIN edges).
    3. Look up the concept(s) carried on the matching
       `schema_perspective_concepts` bridge rows for that perspective
       (the SQLite feed for `Perspective_Concepts` — formerly
       USES_DEFINITION edges).
    4. Match against the Field's CAN_MEAN concepts.
    5. Apply `intent_factor_weight` to select the elevated concept.

    Returns the deterministically resolved concept for the field given
    the intent, using bridge-oriented keys (`perspective_intent_row`,
    `perspective_concept_row`).
    """
    engine = get_db_engine()
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    -- Intent info
                    i.intent_name, i.intent_category, i.typical_question,
                    -- Perspective info (via OPERATES_WITHIN)
                    p.perspective_name, p.stakeholder_role,
                    -- Concept info (via USES_DEFINITION and CAN_MEAN)
                    c.concept_id, c.concept_name, CASE WHEN c.computation_template IS NOT NULL AND c.computation_template <> '' THEN 'metric' ELSE NULL END AS concept_type, c.description, c.domain,
                    -- Edge weights
                    ip.intent_factor_weight as operates_within_weight,
                    pc.priority_weight as uses_definition_weight,
                    cf.context_hint,
                    ic.intent_factor_weight as concept_elevation_weight,
                    ic.explanation
                FROM schema_concept_fields cf
                JOIN schema_concepts c ON cf.concept_id = c.concept_id
                JOIN schema_intent_concepts ic ON c.concept_id = ic.concept_id
                JOIN schema_intents i ON ic.intent_id = i.intent_id
                JOIN schema_intent_perspectives ip ON i.intent_id = ip.intent_id
                JOIN schema_perspectives p ON ip.perspective_id = p.perspective_id
                JOIN schema_perspective_concepts pc ON p.perspective_id = pc.perspective_id 
                    AND c.concept_id = pc.concept_id
                WHERE cf.table_name = :table_name 
                  AND cf.field_name = :field_name
                  AND i.intent_name = :intent_name
                  AND ip.intent_factor_weight = 1.0  -- Active OPERATES_WITHIN path
                  AND ic.intent_factor_weight = 1.0  -- Elevated concept
                LIMIT 1
            """), {"table_name": table_name, "field_name": field_name, "intent_name": intent_name})
            
            row = result.fetchone()
            if row:
                return {
                    "resolved": True,
                    "field": {"table_name": table_name, "field_name": field_name},
                    "traversal_path": {
                        "intent": {
                            "name": row[0], "category": row[1],
                            "typical_question": row[2]
                        },
                        "perspective_intent_row": {
                            "perspective": row[3], "stakeholder_role": row[4],
                            "weight": row[10]
                        },
                        "perspective_concept_row": {
                            "priority_weight": row[11]
                        },
                        "can_mean": {
                            "context_hint": row[12]
                        }
                    },
                    "resolved_concept": {
                        "concept_id": row[5], "concept_name": row[6],
                        "concept_type": row[7], "description": row[8], 
                        "domain": row[9],
                        "elevation_weight": row[13], "explanation": row[14]
                    }
                }
            else:
                return {
                    "resolved": False,
                    "field": {"table_name": table_name, "field_name": field_name},
                    "intent": intent_name,
                    "message": "No valid path found. Check that Intent operates within a Perspective that uses a Concept the Field can mean."
                }
    except Exception as e:
        return {"error": str(e), "resolved": False}


from semantic_reasoning import (
    compare_query_plans, resolve_intent_probabilistic, 
    infer_intent_from_sql, get_graph_syntax_examples,
    resolve_field_meaning, validate_semantic_model, ResolutionResult
)

@app.get("/mcp/tools/compare_query_plans")
async def api_compare_query_plans(table_name: str, field_name: str):
    """Feature 1: Show how different intents produce different query plans for the same field"""
    engine = get_db_engine()
    plans = compare_query_plans(engine, table_name, field_name)
    return {"field": f"{table_name}.{field_name}", "query_plans": plans, "count": len(plans)}


class SQLInput(BaseModel):
    sql: str

@app.post("/mcp/tools/infer_intent")
async def api_infer_intent(body: SQLInput):
    """Feature 3: Automatically infer intent from SQL shape"""
    engine = get_db_engine()
    scores = infer_intent_from_sql(engine, body.sql)
    return {
        "sql_analyzed": body.sql[:200] + "..." if len(body.sql) > 200 else body.sql,
        "inferred_intents": [
            {
                "intent": s.intent_name,
                "confidence": s.confidence,
                "matched_fields": s.matched_fields,
                "matched_concepts": s.matched_concepts,
                "explanation": s.explanation
            } for s in scores[:5]
        ]
    }


@app.get("/mcp/tools/graph_syntax")
async def api_graph_syntax(intent_name: str, table_name: str, field_name: str):
    """Feature 4: Get Cypher and AQL syntax for semantic path traversal"""
    engine = get_db_engine()
    return get_graph_syntax_examples(engine, intent_name, table_name, field_name)


@app.get("/mcp/tools/resolve_field")
async def api_resolve_field(intent_name: str, table_name: str, field_name: str):
    """
    FORMAL RESOLUTION ALGORITHM
    
    For a given (Intent I, Field F), resolve to exactly one Concept C.
    
    Algorithm per treatise:
    1. Find perspectives where Intent operates (weight ≠ -1)
    2. Find concepts that perspectives use/emphasize
    3. Filter to concepts the field CAN_MEAN
    4. Apply intent elevation/suppression
    5. Assert exactly one result
    
    Returns resolution status: 'resolved', 'ambiguous', or 'no_path'
    """
    engine = get_db_engine()
    result = resolve_field_meaning(engine, intent_name, table_name, field_name)
    return {
        "intent": result.intent,
        "field": result.field_name,
        "status": result.status,
        "is_valid": result.is_valid,
        "resolved_concept": result.resolved_concept,
        "perspective": result.perspective,
        "candidate_concepts": result.candidate_concepts,
        "explanation": result.explanation
    }


@app.get("/mcp/tools/validate_model")
async def api_validate_model():
    """
    Validate entire semantic model for resolution completeness.
    
    Checks all (Intent, Field) combinations and reports:
    - Resolved: Valid single-concept resolution
    - Ambiguous: Multiple concepts (modeling error)
    - No Path: Missing edges (incomplete model)
    
    Use this to detect modeling errors before deploying.
    """
    engine = get_db_engine()
    validation = validate_semantic_model(engine)
    return {
        "summary": validation['summary'],
        "ambiguous_combinations": validation['ambiguous'][:10],
        "no_path_combinations": validation['no_path'][:10],
        "total_resolved": validation['summary']['resolved_count'],
        "total_errors": validation['summary']['ambiguous_count'] + validation['summary']['no_path_count']
    }


@app.api_route("/api/arango-sync", methods=["GET", "POST"])
async def api_arango_sync(
    dry_run: bool = False,
    api_key: Optional[str] = None,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """
    Trigger an ArangoDB semantic graph sync.

    Synchronizes intents, perspectives, concepts, and bindings from the
    SQLite semantic layer into ArangoDB as a named graph.

    Query params:
      dry_run=true  — validate and count without writing to ArangoDB

    Auth:
      X-API-Key header required (matches QUERY_API_KEY env var).
    """
    if not QUERY_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Sync endpoint not configured. Set QUERY_API_KEY environment variable."
        )
    resolved_key = x_api_key or api_key
    if not resolved_key or resolved_key != QUERY_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Include X-API-Key header or api_key query param."
        )
    try:
        from graph_sync import sync_graph
        report = sync_graph(dry_run=dry_run)
        total_vertices = sum(report.vertices_synced.values()) if isinstance(report.vertices_synced, dict) else 0
        total_edges = sum(report.edges_synced.values()) if isinstance(report.edges_synced, dict) else 0
        return {
            "status": "ok" if not report.errors else "error",
            "timestamp": report.timestamp,
            "dry_run": dry_run,
            "vertices": {
                "synced": report.vertices_synced,
                "new": report.vertices_new,
                "updated": report.vertices_updated,
                "total": total_vertices,
            },
            "edges": {
                "synced": report.edges_synced,
                "new": report.edges_new,
                "updated": report.edges_updated,
                "total": total_edges,
            },
            "errors": report.errors,
            "warnings": report.warnings,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Bridge health helper — module-level so tests can import it directly (#120)
# ---------------------------------------------------------------------------
_SYNC_LAST_STATUS: list[str] = ["Ready — click Dry Run or Sync"]

_BRIDGE_HEALTH_MAP = {
    "Perspective_Intents": "schema_intent_perspectives",
    "Perspective_Concepts": "schema_perspective_concepts",
}


def quick_bridge_health() -> str:
    """Return a one-line bridge health status string.

    Used by the Graph Sync tab (.then() chain) and importable by tests.
    Compares ArangoDB bridge-collection counts against SQLite source tables.

    Returns one of:
      "✅ IN SYNC @ HH:MM:SS"
      "❌ MISMATCH @ HH:MM:SS: <details>"
      "⚠️ ERROR — <msg>"
      "SKIP — <reason>"
    """
    import sqlite3 as _sq
    import datetime as _dt

    if not os.path.exists(SQLITE_DB_PATH):
        return "SKIP — SQLite DB not found"
    if not os.environ.get("ARANGO_HOST"):
        try:
            conn = _sq.connect(SQLITE_DB_PATH)
            parts = []
            for _tbl in _BRIDGE_HEALTH_MAP.values():
                n = conn.execute(f"SELECT COUNT(*) FROM {_tbl}").fetchone()[0]
                parts.append(f"{_tbl}: {n}")
            conn.close()
            ts = _dt.datetime.now().strftime("%H:%M:%S")
            return f"ArangoDB not configured — SQLite counts @ {ts}: " + ", ".join(parts)
        except Exception as exc:
            return f"SKIP — {exc}"
    try:
        from graph_sync import get_arango_client, get_arango_db
        cl = get_arango_client()
        db = get_arango_db(cl)
        conn = _sq.connect(SQLITE_DB_PATH)
        mismatches = []
        for ac, st in _BRIDGE_HEALTH_MAP.items():
            an = db.collection(ac).count() if db.has_collection(ac) else -1
            sn = conn.execute(f"SELECT COUNT(*) FROM {st}").fetchone()[0]
            if an != sn:
                mismatches.append(f"{ac}: Arango={an} SQLite={sn}")
        conn.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        if mismatches:
            return "❌ MISMATCH @ " + ts + ": " + "; ".join(mismatches)
        return "✅ IN SYNC @ " + ts
    except Exception as exc:
        return f"⚠️ ERROR — {exc}"


def create_gradio_interface():
    """Create the Gradio interface for the Space"""
    
    def process_query(query: str, include_explanation: bool) -> tuple:
        intent = analyze_query_intent(query)
        response = generate_sql_from_intent(query, intent)
        
        explanation = response.explanation if include_explanation else "Explanation disabled"
        tables = ", ".join(response.tables_used)
        
        return response.sql, explanation, tables, response.estimated_complexity
    
    def get_template(template_name: str) -> str:
        return SQL_TEMPLATES.get(template_name, "Template not found")
    
    def show_schema() -> str:
        return json.dumps(SAMPLE_SCHEMA, indent=2)
    
    def get_live_tables() -> List[str]:
        """Get list of tables from live database"""
        return get_all_tables()
    
    def get_perspectives_list() -> List[str]:
        """Get list of perspectives for dropdown"""
        engine = get_db_engine()
        try:
            with engine.connect() as conn:
                result = conn.execute(text("SELECT perspective_name FROM schema_perspectives ORDER BY perspective_name"))
                return [r[0] for r in result.fetchall()]
        except:
            return []
    
    def get_intents_list() -> List[tuple]:
        """Get list of intents for dropdown, marking which have ground truth SQL"""
        engine = get_db_engine()
        try:
            with engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT intent_name, intent_category, primary_binding_key 
                    FROM schema_intents ORDER BY intent_category, intent_name
                """))
                choices = []
                for r in result.fetchall():
                    has_binding = r[2] is not None
                    label = f"{r[0]} ({r[1]})" if has_binding else f"{r[0]} ({r[1]}) [no SQL]"
                    choices.append((label, r[0]))
                return choices
        except:
            return []
    
    def get_ambiguous_fields_list() -> List[tuple]:
        """Get list of ambiguous fields for dropdown"""
        engine = get_db_engine()
        try:
            with engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT DISTINCT cf.table_name, cf.field_name
                    FROM schema_concept_fields cf
                    GROUP BY cf.table_name, cf.field_name
                    HAVING COUNT(*) > 1
                    ORDER BY cf.table_name, cf.field_name
                """))
                return [(f"{r[0]}.{r[1]}", f"{r[0]}|{r[1]}") for r in result.fetchall()]
        except:
            return []
    
    def resolve_field_gradio(field_choice: str, intent_choice: str) -> tuple:
        """Resolve a field using the full graph traversal"""
        if not field_choice or not intent_choice:
            return "Select both a field and an intent to resolve.", ""
        
        table_name, field_name = field_choice.split("|")
        engine = get_db_engine()
        try:
            # Call the formal resolution algorithm to get ResolutionResult.explanation
            resolution = resolve_field_meaning(engine, intent_choice, table_name, field_name)

            # Build the explanation panel text based on resolution status
            if resolution.status == "ambiguous":
                candidates = ", ".join(f"`{c}`" for c in resolution.candidate_concepts)
                explanation_md = (
                    f'<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;'
                    f'padding:12px 16px;margin-top:8px;">'
                    f'<strong>⚠️ Ambiguous Resolution — Modeling Fix Required</strong><br><br>'
                    f'{resolution.explanation}<br><br>'
                    f'<em>Candidate concepts: {candidates}</em>'
                    f'</div>'
                )
            elif resolution.status == "no_path":
                explanation_md = (
                    f'<div style="background:#f8d7da;border:1px solid #f5c6cb;border-radius:6px;'
                    f'padding:12px 16px;margin-top:8px;">'
                    f'<strong>❌ No Resolution Path</strong><br><br>'
                    f'{resolution.explanation}'
                    f'</div>'
                )
            else:
                explanation_md = (
                    f'<div style="background:#d4edda;border:1px solid #c3e6cb;border-radius:6px;'
                    f'padding:12px 16px;margin-top:8px;">'
                    f'<strong>✅ Resolution Explanation</strong><br><br>'
                    f'{resolution.explanation}'
                    f'</div>'
                )

            with engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT 
                        i.intent_name, i.intent_category, i.typical_question,
                        p.perspective_name, p.stakeholder_role,
                        c.concept_id, c.concept_name, CASE WHEN c.computation_template IS NOT NULL AND c.computation_template <> '' THEN 'metric' ELSE NULL END AS concept_type, c.description, c.domain,
                        ip.intent_factor_weight, pc.priority_weight, cf.context_hint,
                        ic.intent_factor_weight, ic.explanation
                    FROM schema_concept_fields cf
                    JOIN schema_concepts c ON cf.concept_id = c.concept_id
                    JOIN schema_intent_concepts ic ON c.concept_id = ic.concept_id
                    JOIN schema_intents i ON ic.intent_id = i.intent_id
                    JOIN schema_intent_perspectives ip ON i.intent_id = ip.intent_id
                    JOIN schema_perspectives p ON ip.perspective_id = p.perspective_id
                    JOIN schema_perspective_concepts pc ON p.perspective_id = pc.perspective_id 
                        AND c.concept_id = pc.concept_id
                    WHERE cf.table_name = :table_name 
                      AND cf.field_name = :field_name
                      AND i.intent_name = :intent_name
                      AND ip.intent_factor_weight = 1.0
                      AND ic.intent_factor_weight = 1.0
                    LIMIT 1
                """), {"table_name": table_name, "field_name": field_name, "intent_name": intent_choice})
                
                row = result.fetchone()
                if row:
                    main_md = f"""## Graph Traversal Result

### Field
`{table_name}.{field_name}`

### Intent
**{row[0]}** ({row[1]})
*"{row[2]}"*

### Perspective_Intents bridge row → Perspective
**{row[3]}**
Stakeholder: {row[4]}
*(legacy alias: OPERATES_WITHIN)*

### Perspective_Concepts bridge row → Concept
**{row[6]}** (type: {row[7]})
Domain: {row[9]}

### Resolution
> {row[8]}

**Explanation:** {row[14]}
"""
                    return main_md, explanation_md
                else:
                    valid_intents_result = conn.execute(text("""
                        SELECT DISTINCT i.intent_name, c.concept_name
                        FROM schema_intents i
                        JOIN schema_intent_perspectives ip ON i.intent_id = ip.intent_id AND ip.intent_factor_weight = 1.0
                        JOIN schema_perspective_concepts pc ON ip.perspective_id = pc.perspective_id
                        JOIN schema_concepts c ON pc.concept_id = c.concept_id
                        JOIN schema_concept_fields cf ON c.concept_id = cf.concept_id
                        JOIN schema_intent_concepts ic ON i.intent_id = ic.intent_id AND c.concept_id = ic.concept_id AND ic.intent_factor_weight = 1.0
                        WHERE cf.table_name = :table_name AND cf.field_name = :field_name
                    """), {"table_name": table_name, "field_name": field_name})
                    valid_rows = valid_intents_result.fetchall()
                    
                    if valid_rows:
                        suggestions = "\n".join([f"- **{r[0]}** → resolves to `{r[1]}`" for r in valid_rows])
                        main_md = f"""## No Valid Path Found

Field: `{table_name}.{field_name}`
Intent: `{intent_choice}`

The selected intent does not have a valid semantic path to this field.

### Try these intents instead:
{suggestions}
"""
                    else:
                        main_md = f"""## No Valid Path Found

Field: `{table_name}.{field_name}`
Intent: `{intent_choice}`

No intents currently have complete semantic paths to this field.
Check that perspective-concept and intent-concept relationships are seeded.
"""
                    return main_md, explanation_md
        except Exception as e:
            return f"Error: {str(e)}", ""
    
    def get_graph_visualization() -> str:
        """Generate text-based graph visualization"""
        engine = get_db_engine()
        try:
            with engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT i.intent_name, p.perspective_name
                    FROM schema_intent_perspectives ip
                    JOIN schema_intents i ON ip.intent_id = i.intent_id
                    JOIN schema_perspectives p ON ip.perspective_id = p.perspective_id
                    WHERE ip.intent_factor_weight = 1.0
                    ORDER BY p.perspective_name, i.intent_name
                """))
                
                graph = "## Intent → Perspective Graph\n\n"
                current_perspective = None
                for row in result.fetchall():
                    if row[1] != current_perspective:
                        current_perspective = row[1]
                        graph += f"\n### {row[1]}\n"
                    graph += f"  - {row[0]} → **{row[1]}**\n"
                
                return graph
        except Exception as e:
            return f"Error loading graph: {str(e)}"
    
    def get_table_ddl_gradio(table_name: str) -> str:
        """Get CREATE TABLE SQL for selected table"""
        if not table_name:
            return "-- Select a table to view its schema"
        return get_table_create_sql(table_name)
    
    def get_all_ddl_gradio() -> str:
        """Get all CREATE TABLE statements"""
        tables = get_all_tables()
        if not tables:
            return "-- Database not connected or no tables found"
        
        all_ddl = []
        for table in sorted(tables):
            all_ddl.append(f"-- Table: {table}")
            all_ddl.append(get_table_create_sql(table))
            all_ddl.append("")
        
        return "\n".join(all_ddl)
    
    def execute_sql_gradio(sql: str) -> tuple:
        """Execute SQL and return results as formatted output"""
        if not sql.strip():
            return "-- Enter a SQL query", ""
        
        result = execute_readonly_sql(sql)
        
        if result["error"]:
            return f"-- Error: {result['error']}", ""
        
        if not result["rows"]:
            return "-- Query executed successfully. No rows returned.", ""
        
        header = " | ".join(str(c) for c in result["columns"])
        separator = "-" * len(header)
        rows_str = "\n".join(" | ".join(str(v) for v in row) for row in result["rows"])
        
        table_output = f"{header}\n{separator}\n{rows_str}"
        stats = f"Returned {len(result['rows'])} rows, {len(result['columns'])} columns"
        
        return table_output, stats
    
    _GT_SLOT_CSS = """
    <style>
    .gt-slot-select input, .gt-slot-select ul li, .gt-slot-select span {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace !important;
        white-space: pre !important;
        font-size: 13px !important;
    }
    </style>
    """

    with gr.Blocks() as demo:
        gr.HTML(_GT_SLOT_CSS, visible=True)
        gr.Markdown("""
        # 🏭 Manufacturing SQL Semantic Layer
        
        **MCP Context Builder** for GitHub Copilot integration.
        Select resources below and click **Copy to Copilot** to bundle context.
        
        | MCP Component | Purpose |
        |---------------|---------|
        | **Prompts** | Natural language question templates |
        | **Resources** | Schema DDL, ground truth SQL queries |
        | **Tools** | API endpoints for validation |
        """)
        
        def build_copilot_context(question: str, include_schema: bool, include_queries: bool, 
                                   selected_category: str, include_semantic: bool, selected_intent: str) -> str:
            """Build MCP context package for Copilot"""
            context_parts = []
            
            context_parts.append("# MCP Context for Manufacturing SQL Generation\n")
            
            if question.strip():
                context_parts.append("## Prompt")
                context_parts.append(f"User Question: {question}\n")
            
            if include_semantic and selected_intent:
                context_parts.append("## Semantic Context")
                context_parts.append(f"**Intent:** {selected_intent}")
                engine = get_db_engine()
                try:
                    with engine.connect() as conn:
                        result = conn.execute(text("""
                            SELECT i.intent_name, i.description, i.typical_question,
                                   p.perspective_name, p.stakeholder_role
                            FROM schema_intents i
                            JOIN schema_intent_perspectives ip ON i.intent_id = ip.intent_id
                            JOIN schema_perspectives p ON ip.perspective_id = p.perspective_id
                            WHERE i.intent_name = :intent_name AND ip.intent_factor_weight = 1.0
                        """), {"intent_name": selected_intent})
                        row = result.fetchone()
                        if row:
                            context_parts.append(f"*{row[1]}*")
                            context_parts.append(f"\n**Perspective:** {row[3]} ({row[4]})")
                            context_parts.append(f"**Typical Question:** {row[2]}\n")
                        
                        result2 = conn.execute(text("""
                            SELECT c.concept_name, c.description, ic.explanation
                            FROM schema_intent_concepts ic
                            JOIN schema_concepts c ON ic.concept_id = c.concept_id
                            WHERE ic.intent_id = (SELECT intent_id FROM schema_intents WHERE intent_name = :intent_name)
                              AND ic.intent_factor_weight = 1.0
                        """), {"intent_name": selected_intent})
                        elevated = result2.fetchall()
                        if elevated:
                            context_parts.append("**Elevated Concepts:**")
                            for c in elevated:
                                context_parts.append(f"- {c[0]}: {c[1]}")
                            context_parts.append("")
                except:
                    pass
            
            if include_schema:
                gt_tables = get_ground_truth_tables()
                context_parts.append(f"## Resources: Database Schema ({len(gt_tables)} tables from Ground Truth)")
                if gt_tables:
                    for table in gt_tables:
                        ddl = get_table_create_sql(table)
                        context_parts.append(f"```sql\n-- {table}\n{ddl}\n```")
                context_parts.append("")
            
            if include_queries and selected_category:
                context_parts.append("## Resources: Ground Truth SQL Examples")
                queries = get_saved_queries(selected_category)
                for q in queries[:5]:
                    context_parts.append(f"### {q['name']}")
                    context_parts.append(f"*{q['description']}*")
                    context_parts.append(f"```sql\n{q['sql']}```\n")
            
            context_parts.append("## Tools Available")
            context_parts.append("- `generate_sql`: Convert natural language to SQL")
            context_parts.append("- `get_table_ddl`: Get schema for specific table")
            context_parts.append("- `validate_sql`: Check SQL syntax against schema")
            context_parts.append("- `resolve_semantic_path`: Disambiguate field meanings via graph traversal")
            
            return "\n".join(context_parts)
        
        def get_category_choices():
            index = get_query_categories()
            return [(f"{c['name']} ({c['query_count']} queries)", c['id']) 
                    for c in index.get("categories", [])]
        
        # ---------- Selector tab helpers (shared selection pane) ----------
        _SEL_SEP = "||"

        def _sel_db():
            import sqlite3
            return sqlite3.connect(SQLITE_DB_PATH)

        def _sel_categories():
            """Stakeholder perspectives — same vocabulary as the
            Define Relationship category chips."""
            conn = _sel_db()
            try:
                return [r[0] for r in conn.execute(
                    "SELECT perspective_name FROM schema_perspectives "
                    "ORDER BY perspective_id")]
            finally:
                conn.close()

        def _sel_tables(tags):
            """Physical tables; if tags picked, only tables whose columns
            resolve to a concept used by those perspectives."""
            conn = _sel_db()
            try:
                if not tags:
                    return [r[0] for r in conn.execute(
                        "SELECT DISTINCT table_name FROM sql_graph_nodes "
                        "WHERE node_type='table' ORDER BY 1")]
                ph = ",".join("?" * len(tags))
                return [r[0] for r in conn.execute(
                    f"""
                    SELECT DISTINCT n.table_name
                    FROM sql_graph_edges e
                    JOIN sql_graph_nodes n  ON n._id = e._from
                    JOIN sql_graph_nodes cn ON cn._id = e._to
                    JOIN schema_concepts sc ON sc.concept_name = cn.concept_name
                    JOIN schema_perspective_concepts pc
                         ON pc.concept_id = sc.concept_id
                    JOIN schema_perspectives p
                         ON p.perspective_id = pc.perspective_id
                    WHERE e.edge_type = 'resolves_to'
                      AND p.perspective_name IN ({ph})
                    ORDER BY 1
                    """, list(tags))]
            finally:
                conn.close()

        def _sel_columns(table):
            """All columns of the table; mapped columns get a ✦ marker."""
            conn = _sel_db()
            try:
                mapped = {r[0] for r in conn.execute(
                    """
                    SELECT DISTINCT n.column_name
                    FROM sql_graph_edges e
                    JOIN sql_graph_nodes n ON n._id = e._from
                    WHERE e.edge_type = 'resolves_to' AND n.table_name = ?
                    """, (table,))}
                cols = [r[0] for r in conn.execute(
                    "SELECT column_name FROM sql_graph_nodes "
                    "WHERE node_type='column' AND table_name=? ORDER BY 1",
                    (table,))]
                return [(f"{c} ✦" if c in mapped else c, c) for c in cols]
            finally:
                conn.close()

        def _sel_concepts(table, column, tags):
            """Concepts the column (or any column of the table) resolves to,
            optionally narrowed to the tag categories."""
            conn = _sel_db()
            try:
                sql = """
                    SELECT DISTINCT cn.concept_name
                    FROM sql_graph_edges e
                    JOIN sql_graph_nodes n  ON n._id = e._from
                    JOIN sql_graph_nodes cn ON cn._id = e._to
                    WHERE e.edge_type = 'resolves_to' AND n.table_name = ?
                """
                params = [table]
                if column:
                    sql += " AND n.column_name = ?"
                    params.append(column)
                if tags:
                    ph = ",".join("?" * len(tags))
                    sql += f"""
                      AND cn.concept_name IN (
                        SELECT sc.concept_name FROM schema_concepts sc
                        JOIN schema_perspective_concepts pc
                             ON pc.concept_id = sc.concept_id
                        JOIN schema_perspectives p
                             ON p.perspective_id = pc.perspective_id
                        WHERE p.perspective_name IN ({ph}))
                    """
                    params.extend(tags)
                sql += " ORDER BY 1"
                return [r[0] for r in conn.execute(sql, params)]
            finally:
                conn.close()

        def _sel_intents(concept, tags):
            conn = _sel_db()
            try:
                sql = """
                    SELECT DISTINCT i.intent_name
                    FROM schema_intent_concepts ic
                    JOIN schema_concepts c ON c.concept_id = ic.concept_id
                    JOIN schema_intents i  ON i.intent_id = ic.intent_id
                    WHERE c.concept_name = ? AND ic.intent_factor_weight = 1
                """
                # NOTE: tags intentionally do NOT filter intents. The concept
                # is already perspective-filtered upstream; the sparse
                # intent<->perspective links would cut off valid chains
                # (e.g. Finance concepts elevate to query-bearing intents
                # that lack a Finance link).
                sql += " ORDER BY 1"
                return [r[0] for r in conn.execute(sql, [concept])]
            finally:
                conn.close()

        def _sel_query_count(tags):
            """How many ground-truth queries are reachable under these tags
            (via perspective concepts -> weight-1 intents -> queries)."""
            conn = _sel_db()
            try:
                if not tags:
                    return conn.execute(
                        "SELECT COUNT(*) FROM schema_intent_queries"
                    ).fetchone()[0]
                ph = ",".join("?" * len(tags))
                return conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT q.id)
                    FROM schema_perspectives sp
                    JOIN schema_perspective_concepts pc
                         ON pc.perspective_id = sp.perspective_id
                    JOIN schema_intent_concepts ic
                         ON ic.concept_id = pc.concept_id
                        AND ic.intent_factor_weight = 1
                    JOIN schema_intent_queries q
                         ON q.intent_id = ic.intent_id
                    WHERE sp.perspective_name IN ({ph})
                    """, list(tags)).fetchone()[0]
            finally:
                conn.close()

        def _sel_queries(intent):
            conn = _sel_db()
            try:
                return [r[0] for r in conn.execute(
                    """
                    SELECT q.query_name
                    FROM schema_intent_queries q
                    JOIN schema_intents i ON i.intent_id = q.intent_id
                    WHERE i.intent_name = ?
                    ORDER BY q.query_index
                    """, (intent,))]
            finally:
                conn.close()

        def _sel_reachable_queries(tags, table=None, column=None,
                                   concept=None):
            """Distinct ground-truth query names reachable under the current
            partial selection (tags / table / column / concept). Lets the
            query dropdown act as a live filter field instead of only
            filling after a full drill-down."""
            conn = _sel_db()
            try:
                sql = """
                    SELECT DISTINCT q.query_name
                    FROM schema_intent_queries q
                    JOIN schema_intent_concepts ic
                         ON ic.intent_id = q.intent_id
                        AND ic.intent_factor_weight = 1
                    JOIN schema_concepts c ON c.concept_id = ic.concept_id
                    WHERE 1=1
                """
                params = []
                if concept:
                    sql += " AND c.concept_name = ?"
                    params.append(concept)
                if tags:
                    ph = ",".join("?" * len(tags))
                    sql += f"""
                      AND c.concept_id IN (
                        SELECT pc.concept_id
                        FROM schema_perspective_concepts pc
                        JOIN schema_perspectives p
                             ON p.perspective_id = pc.perspective_id
                        WHERE p.perspective_name IN ({ph}))
                    """
                    params.extend(tags)
                if table:
                    sub = """
                      AND c.concept_name IN (
                        SELECT cn.concept_name
                        FROM sql_graph_edges e
                        JOIN sql_graph_nodes n  ON n._id = e._from
                        JOIN sql_graph_nodes cn ON cn._id = e._to
                        WHERE e.edge_type = 'resolves_to'
                          AND n.table_name = ?
                    """
                    params.append(table)
                    if column:
                        sub += " AND n.column_name = ?"
                        params.append(column)
                    sql += sub + ")"
                sql += " ORDER BY 1"
                return [r[0] for r in conn.execute(sql, params)]
            finally:
                conn.close()

        def _sel_query_choices(tags, table="", column="", concept=""):
            """Packed (label, value) pairs for the query dropdown at any
            stage of the drill-down."""
            csv = ",".join(tags)
            prefix = _SEL_SEP.join([csv, table, column, concept, ""])
            return [(q, prefix + _SEL_SEP + q)
                    for q in _sel_reachable_queries(
                        tags, table or None, column or None, concept or None)]

        def _sel_summary(tags, table=None, column=None, concept=None,
                         intent=None, query=None):
            lines = ["### Selection context"]
            lines.append(f"- **Tags:** {', '.join(tags) if tags else '(all)'}")
            if table:
                col_txt = f" · **Column:** `{column}`" if column else ""
                lines.append(f"- **Table:** `{table}`{col_txt}")
            if concept:
                lines.append(f"- **Concept:** {concept}")
            if intent:
                lines.append(f"- **Intent:** {intent}")
            if query:
                lines.append(f"- **Ground-truth query:** {query}")
            if not table:
                lines.append("\n*Pick a table to start the concrete chain.*")
            return "\n".join(lines)

        def _sel_parse(value, n):
            """Split a packed choice value into exactly n parts.

            Uses maxsplit so the last part (e.g. a free-text query name)
            keeps any literal separator characters intact.
            """
            parts = (value or "").split(_SEL_SEP, n - 1)
            parts += [""] * (n - len(parts))
            return parts[:n]

        with gr.Tab("🎛️ Selector"):
            gr.Markdown(
                "### One selection pane: abstract tags + concrete chain\n"
                "Tags are a lightweight filter. The chain below is fully "
                "concrete: physical table → column (✦ = semantically mapped) "
                "→ concept → analytical intent → ground-truth query."
            )
            with gr.Row():
                sel_tags = gr.Dropdown(
                    choices=_sel_categories(),
                    label="Perspective filter (type to search, multi-select)",
                    value=[], multiselect=True, filterable=True, scale=1)
                gr.Markdown("", scale=2)  # spacer keeps the filter narrow
            with gr.Row():
                # allow_custom_value on every chained dropdown: choices are
                # re-populated per session via gr.update, and after an app
                # restart the server-side session state is gone — without
                # this, any pick in a pre-restart browser page fails
                # preprocess validation ("not in the list of choices: []").
                sel_table = gr.Dropdown(
                    choices=[(t, "" + _SEL_SEP + t) for t in _sel_tables([])],
                    label="Table", value=None, scale=1,
                    allow_custom_value=True)
                sel_column = gr.Dropdown(choices=[], label="Column",
                                         value=None, scale=1,
                                         allow_custom_value=True)
                sel_concept = gr.Dropdown(choices=[], label="Concept",
                                          value=None, scale=1,
                                          allow_custom_value=True)
                sel_intent = gr.Dropdown(choices=[], label="Intent",
                                         value=None, scale=1,
                                         allow_custom_value=True)
                sel_query = gr.Dropdown(choices=_sel_query_choices([]),
                                        label="Ground-truth query",
                                        value=None, scale=1,
                                        allow_custom_value=True)
            sel_summary = gr.Markdown(_sel_summary([]))

            _SEL_CLEAR = gr.update(choices=[], value=None)

            def _sel_on_tags(tags):
                tags = tags or []
                csv = ",".join(tags)
                tables = _sel_tables(tags)
                summary = _sel_summary(tags)
                n_q = _sel_query_count(tags)
                if tags:
                    summary += (
                        f"\n\n**Ground-truth query coverage:** {n_q} "
                        "quer" + ("y" if n_q == 1 else "ies")
                        + " reachable under these tags."
                    )
                    if n_q == 0:
                        covered = [p for p in _sel_categories()
                                   if _sel_query_count([p]) > 0]
                        summary += (
                            "\n\n> ⚠️ No ground-truth queries are wired to "
                            "these perspectives yet — you can still browse "
                            "tables → columns → concepts → intents, but the "
                            "chain will end before a query. Perspectives with "
                            "coverage today: " + ", ".join(covered) + "."
                        )
                if tags and not tables:
                    summary += (
                        "\n\n> ⚠️ No tables have semantically mapped columns "
                        "for these tags yet — clear a tag to widen the list."
                    )
                return (
                    gr.update(choices=[(t, csv + _SEL_SEP + t) for t in tables],
                              value=None),
                    _SEL_CLEAR, _SEL_CLEAR, _SEL_CLEAR,
                    gr.update(choices=_sel_query_choices(tags), value=None),
                    summary,
                )

            def _sel_on_table(val):
                if not val:
                    return (gr.update(),) * 5
                csv, table = _sel_parse(val, 2)
                tags = [t for t in csv.split(",") if t]
                prefix = csv + _SEL_SEP + table + _SEL_SEP
                choices = [("(all columns)", prefix)]
                choices += [(label, prefix + c)
                            for label, c in _sel_columns(table)]
                # Default to "(all columns)": its .change fires and fills
                # the concept dropdown for the whole table immediately.
                return (
                    gr.update(choices=choices, value=prefix),
                    _SEL_CLEAR, _SEL_CLEAR,
                    gr.update(choices=_sel_query_choices(tags, table),
                              value=None),
                    _sel_summary(tags, table),
                )

            def _sel_on_column(val):
                if not val:
                    return (gr.update(),) * 4
                csv, table, column = _sel_parse(val, 3)
                tags = [t for t in csv.split(",") if t]
                concepts = _sel_concepts(table, column or None, tags)
                prefix = val + _SEL_SEP
                summary = _sel_summary(tags, table, column or None)
                if not concepts:
                    target = f"`{table}.{column}`" if column else f"`{table}`"
                    summary += (
                        f"\n\n> ⚠️ {target} has no semantic concept mapping "
                        "yet — pick a ✦-marked column to continue the chain."
                    )
                return (
                    gr.update(choices=[(c, prefix + c) for c in concepts],
                              value=None),
                    _SEL_CLEAR,
                    gr.update(choices=_sel_query_choices(tags, table, column),
                              value=None),
                    summary,
                )

            def _sel_on_concept(val):
                if not val:
                    return (gr.update(),) * 3
                csv, table, column, concept = _sel_parse(val, 4)
                tags = [t for t in csv.split(",") if t]
                intents = _sel_intents(concept, tags)
                prefix = val + _SEL_SEP
                summary = _sel_summary(tags, table, column or None, concept)
                if not intents:
                    summary += (
                        "\n\n> ⚠️ No analytical intent elevates this concept"
                        + (" within the selected tags — clear a tag to widen "
                           "the list." if tags else " yet — the chain ends "
                           "here.")
                    )
                intent_choices = [(i, prefix + i) for i in intents]
                if intents:
                    # "(any intent)" wildcard first and pre-selected: its
                    # .change fires with an empty intent segment and lists
                    # every query reachable from this concept.
                    intent_choices = [("(any intent)", prefix)] + intent_choices
                return (
                    gr.update(choices=intent_choices,
                              value=(prefix if intents else None)),
                    gr.update(choices=_sel_query_choices(tags, table, column,
                                                         concept),
                              value=None),
                    summary,
                )

            def _sel_on_intent(val):
                if not val:
                    return (gr.update(),) * 2
                csv, table, column, concept, intent = _sel_parse(val, 5)
                tags = [t for t in csv.split(",") if t]
                if not intent:
                    # "(any intent)" wildcard — every query reachable from
                    # the concept, without forcing an intent pick.
                    return (
                        gr.update(choices=_sel_query_choices(
                            tags, table, column, concept), value=None),
                        _sel_summary(tags, table, column or None,
                                     concept or None),
                    )
                queries = _sel_queries(intent)
                prefix = val + _SEL_SEP
                summary = _sel_summary(tags, table, column or None, concept,
                                       intent)
                if not queries:
                    summary += (
                        "\n\n> ⚠️ No ground-truth queries are mapped to this "
                        "intent yet — the chain ends here."
                    )
                return (
                    gr.update(choices=[(q, prefix + q) for q in queries],
                              value=None),
                    summary,
                )

            def _sel_on_query(val):
                if not val:
                    return gr.update()
                csv, table, column, concept, intent, query = _sel_parse(val, 6)
                tags = [t for t in csv.split(",") if t]
                return _sel_summary(tags, table or None, column or None,
                                    concept or None, intent or None, query)

            sel_tags.change(
                _sel_on_tags, inputs=[sel_tags],
                outputs=[sel_table, sel_column, sel_concept, sel_intent,
                         sel_query, sel_summary])
            sel_table.change(
                _sel_on_table, inputs=[sel_table],
                outputs=[sel_column, sel_concept, sel_intent, sel_query,
                         sel_summary])
            sel_column.change(
                _sel_on_column, inputs=[sel_column],
                outputs=[sel_concept, sel_intent, sel_query, sel_summary])
            sel_concept.change(
                _sel_on_concept, inputs=[sel_concept],
                outputs=[sel_intent, sel_query, sel_summary])
            sel_intent.change(
                _sel_on_intent, inputs=[sel_intent],
                outputs=[sel_query, sel_summary])
            sel_query.change(
                _sel_on_query, inputs=[sel_query],
                outputs=[sel_summary])

        with gr.Tab("🚀 Copilot Context"):
            gr.Markdown("### Build MCP Context Package")
            _cfg_cc = _get_erp_config()
            _cc_source_note = (
                "\n\n> ⚠️ Using the default ERP name. Set `ERP_INSTANCE_NAME` to configure your system."
                if _cfg_cc["erp_instance_name_source"] == "default" else ""
            )
            copilot_erp_md = gr.Markdown(
                f"**Active ERP:** `{_cfg_cc['erp_instance_name']}` "
                f"*(source: {_cfg_cc['erp_instance_name_source']})*{_cc_source_note}"
            )
            
            with gr.Row():
                with gr.Column():
                    question_input = gr.Textbox(
                        label="Your Question (Prompt)",
                        placeholder="e.g., Show me supplier on-time delivery rates for Q4",
                        lines=2
                    )
                    
                    gr.Markdown("#### Select Resources")
                    include_schema = gr.Checkbox(label="Include Database Schema (tables in Ground Truth)", value=True)
                    include_queries = gr.Checkbox(label="Include Ground Truth SQL Examples", value=True)
                    query_category = gr.Dropdown(
                        choices=get_category_choices(),
                        label="Query Category",
                        interactive=True
                    )
                    
                    gr.Markdown("#### Semantic Context")
                    include_semantic = gr.Checkbox(label="Include Semantic Layer Context", value=True)
                    semantic_intent = gr.Dropdown(
                        choices=get_intents_list(),
                        label="Analytical Intent",
                        info="Intent constrains field interpretations via graph traversal",
                        interactive=True
                    )
                    
                    copy_btn = gr.Button("📋 Copy to Copilot", variant="primary", size="lg")
                
                with gr.Column():
                    context_output = gr.Textbox(
                        label="MCP Context (copy this to Copilot Chat)",
                        lines=20,
                        max_lines=40
                    )
            
            copy_btn.click(
                fn=build_copilot_context,
                inputs=[question_input, include_schema, include_queries, query_category, include_semantic, semantic_intent],
                outputs=context_output
            )
        
        with gr.Tab("🔗 Define Relationship"):
            gr.Markdown("### Define Data Relationships\n\nSearch for entities, select a predicate, and add the relationship to the graph. Use the undo history to reverse recent additions.")
            gr.HTML(
                value="""<iframe
                    src="/define-relationship/"
                    style="width:100%;height:820px;border:none;border-radius:8px;background:#0f172a;"
                    title="Define Relationship Panel"
                ></iframe>"""
            )

        with gr.Tab("📊 Schema"):
            schema_header_md = gr.Markdown("### Database Schema Resources\n\n**Active ERP:** loading…")

            def _load_erp_header():
                cfg = _get_erp_config()
                erp_name = cfg["erp_instance_name"]
                source = cfg["erp_instance_name_source"]
                header = f"### Database Schema Resources\n\n**Active ERP:** `{erp_name}` *(source: {source})*"
                if source == "default":
                    header += "\n\n> ⚠️ Using the default ERP name. Set the `ERP_INSTANCE_NAME` environment variable to configure your system."
                return header

            initial_table_list = get_all_tables()
            gr.Markdown(f"**{len(initial_table_list)} tables available**")
            
            with gr.Row():
                with gr.Column():
                    refresh_tables_btn = gr.Button("Refresh Table List", variant="secondary")
                    table_dropdown = gr.Dropdown(
                        choices=initial_table_list,
                        value=initial_table_list[0] if initial_table_list else None,
                        label="Select Table",
                        interactive=True
                    )
                    get_ddl_btn = gr.Button("View DDL", variant="primary")
                    get_all_ddl_btn = gr.Button("View All Tables", variant="secondary")
                
                with gr.Column():
                    ddl_output = gr.Code(label="CREATE TABLE SQL", language="sql", lines=20)
            
            def refresh_table_list():
                tables = get_all_tables()
                return gr.update(choices=tables, value=tables[0] if tables else None)
            
            refresh_tables_btn.click(fn=refresh_table_list, outputs=table_dropdown)
            refresh_tables_btn.click(fn=_load_erp_header, outputs=schema_header_md)
            get_ddl_btn.click(fn=get_table_ddl_gradio, inputs=table_dropdown, outputs=ddl_output)
            get_all_ddl_btn.click(fn=get_all_ddl_gradio, outputs=ddl_output)
        
        with gr.Tab("📁 Ground Truth SQL Queries"):
            # Query-dropdown VALUES carry "category<US>name" so the chained
            # .change handler needs exactly ONE input (see the Gradio
            # chained-event rule used by the Ontology Mosaic cascade).
            _GT_SEP = "\x1f"

            def _gt_pack(cat, name):
                return f"{cat or ''}{_GT_SEP}{name or ''}"

            def _gt_unpack(token):
                if token and _GT_SEP in token:
                    cat, name = token.split(_GT_SEP, 1)
                    return (cat or None, name or None)
                return (None, token or None)

            def load_queries_for_category(category_id: str):
                if not category_id:
                    return gr.update(choices=[], value=None), "", "", ""
                queries = get_saved_queries(category_id)
                choices = [
                    (q['name'], _gt_pack(category_id, q['name']))
                    for q in queries
                    if q['name'].strip()
                ]
                return gr.update(choices=choices, value=None), "", "", ""
            
            def _find_binding_key_for_sql(sql_text: str) -> str:
                """Look up binding key from manifest by matching SQL content"""
                if not sql_text or not sql_text.strip():
                    return ""
                manifest_path = os.path.join(GROUND_TRUTH_DIR, "reviewer_manifest.json")
                try:
                    with open(manifest_path, 'r') as f:
                        manifest = json.load(f)

                    def normalize_sql(s: str) -> str:
                        if not s:
                            return ""
                        # Remove block comments /* ... */
                        s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
                        # Remove line comments -- ...\n
                        s = re.sub(r"--.*?\n", "\n", s)
                        # Remove any remaining SQL comments starting at line end
                        s = re.sub(r"--.*$", "", s, flags=re.M)
                        # Strip trailing semicolons and whitespace
                        s = s.replace(';', ' ')
                        # Collapse whitespace
                        s = " ".join(s.split())
                        return s.lower().strip()

                    sql_normalized = normalize_sql(sql_text)
                    for binding_key, entry in manifest.get("approved_snippets", {}).items():
                        file_path = entry.get("file_path", "")
                        candidates = []
                        # Raw path as provided
                        if file_path:
                            candidates.append(file_path)
                        # If manifest entry used a repo-relative path (e.g., app_schema/...), resolve relative to this package
                        candidates.append(os.path.join(os.path.dirname(__file__), file_path))
                        # Try resolving as basename inside ground truth directory
                        candidates.append(os.path.join(GROUND_TRUTH_DIR, os.path.basename(file_path)))
                        # Try resolving inside sql_snippets
                        candidates.append(os.path.join(GROUND_TRUTH_SQL_DIR, os.path.basename(file_path)))
                        # Try canonical binding_key filename inside sql_snippets
                        candidates.append(os.path.join(GROUND_TRUTH_SQL_DIR, f"{binding_key}.sql"))

                        resolved = None
                        for candidate in candidates:
                            try:
                                if candidate and os.path.exists(candidate):
                                    resolved = candidate
                                    break
                            except Exception:
                                continue

                        if not resolved:
                            print(f"[DEBUG] No snippet file found for binding {binding_key}; tried candidates: {candidates}")
                            continue

                        try:
                            with open(resolved, 'r') as sf:
                                snippet_sql = sf.read()
                        except Exception as e:
                            print(f"[DEBUG] error reading snippet file {resolved}: {e}")
                            continue

                        if normalize_sql(snippet_sql) == sql_normalized:
                            print(f"[DEBUG] Found binding for query -> {binding_key} (file: {resolved})")
                            return binding_key
                except Exception as e:
                    print(f"[DEBUG] Binding key lookup error: {e}")
                return ""

            def _find_binding_key_by_name(query_name: str) -> str:
                """Look up binding key from manifest by matching query name against concept/perspective"""
                try:
                    with open(MANIFEST_PATH, 'r') as f:
                        manifest = json.load(f)
                    query_lower = query_name.lower().replace(' ', '').replace('_', '').replace('-', '')
                    for key, entry in manifest.get("approved_snippets", {}).items():
                        concept = entry.get("concept_anchor", "").lower().replace('_', '')
                        perspective = entry.get("perspective", "")
                        sme_just = entry.get("sme_justification", "").lower()
                        entry_name = f"{concept} ({perspective.lower()})".replace(' ', '').replace('_', '')
                        if query_lower == entry_name:
                            return key
                        if query_name.lower() in sme_just:
                            return key
                except Exception:
                    pass
                return ""

            def load_query_sql(query_token: str):
                category_id, query_name = _gt_unpack(query_token)
                if category_id is None or query_name is None:
                    return "", "", ""
                queries = get_saved_queries(category_id)
                for q in queries:
                    if q['name'] == query_name:
                        binding_key = q.get('binding_key', '')
                        if not binding_key:
                            raw = _find_binding_key_for_sql(q['sql'])
                            if raw:
                                binding_key = raw
                        if not binding_key:
                            binding_key = _find_binding_key_by_name(query_name)
                        if not binding_key:
                            binding_key = _ensure_manifest_entry(
                                query_name, q['sql'], q.get('description', ''), category_id
                            )
                        return q['sql'], q['description'], binding_key
                return "", "", ""
            
            with gr.Row():
                saved_category = gr.Dropdown(
                    choices=get_category_choices(),
                    label="Query Category",
                    interactive=True,
                    scale=1,
                )
                saved_query_dropdown = gr.Dropdown(
                    choices=[],
                    label="Query",
                    interactive=True,
                    scale=2,
                )

            saved_sql_output = gr.Code(
                label="SQL Query",
                language="sql",
                lines=24,
                show_label=True,
                interactive=True,
            )

            with gr.Accordion("Details & Save Changes", open=False):
                saved_description = gr.Textbox(label="Description", interactive=True)
                saved_binding_key = gr.Textbox(
                    label="Binding Key (empty = not yet in manifest)",
                    interactive=False,
                )
                with gr.Row():
                    save_query_btn = gr.Button(
                        "Save Changes", variant="primary", elem_id="gt_save_btn"
                    )
                    save_query_status = gr.Textbox(label="Save Status", interactive=False)

            with gr.Row():
                run_query_btn = gr.Button("▶ Run Query", variant="secondary", scale=1)
                run_query_info = gr.Textbox(
                    label="",
                    value="Optional :start_date parameter is treated as NULL (no date filter).",
                    interactive=False,
                    scale=3,
                )
            gt_run_status = gr.Textbox(label="Run Status", interactive=False, visible=False)
            gt_results_df = gr.Dataframe(
                label="Query Results",
                visible=False,
                wrap=True,
            )

            def run_gt_query(sql_text):
                if not sql_text or not sql_text.strip():
                    return (
                        gr.update(value="No SQL loaded — select a query first.", visible=True),
                        gr.update(visible=False),
                    )
                # Replace any :param_name placeholders with NULL so the
                # SQL runs without requiring user-supplied bind values.
                # All current ground-truth queries use the guard idiom
                # `(:param IS NULL OR col op :param)` which short-circuits
                # cleanly when the param is NULL.
                sql_clean = re.sub(r":\w+", "NULL", sql_text.strip())
                result = run_sql_query(sql_clean)
                if result.get("error"):
                    return (
                        gr.update(value=f"Error: {result['error']}", visible=True),
                        gr.update(visible=False),
                    )
                columns = result.get("columns", [])
                rows = result.get("rows", [])
                if not rows:
                    status_msg = f"Query executed — 0 rows returned (all invoices collected)."
                else:
                    status_msg = f"Query executed — {len(rows)} row(s) returned."
                import pandas as pd
                df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
                return (
                    gr.update(value=status_msg, visible=True),
                    gr.update(value=df, visible=True),
                )

            def save_query_edits(category_id, query_name, new_sql, new_description):
                if not category_id or not query_name:
                    return "Select a category and query first."
                index = get_query_categories()
                category = next((c for c in index.get("categories", []) if c["id"] == category_id), None)
                if not category:
                    return f"Category '{category_id}' not found."
                sql_file = os.path.join(QUERIES_DIR, category["file"])
                if not os.path.exists(sql_file):
                    return f"File not found: {category['file']}"
                with open(sql_file, "r") as f:
                    content = f.read()
                queries = get_saved_queries(category_id)
                match = next((q for q in queries if q["name"] == query_name), None)
                if not match:
                    return f"Query '{query_name}' not found in {category['file']}."
                old_sql_block = match["sql"].strip()
                old_desc_line = f"-- Description: {match['description']}"
                new_desc_line = f"-- Description: {new_description.strip()}"
                new_sql_clean = new_sql.strip() if new_sql else old_sql_block
                updated = content.replace(old_desc_line, new_desc_line)
                updated = updated.replace(old_sql_block, new_sql_clean)
                with open(sql_file, "w") as f:
                    f.write(updated)
                return f"Saved changes to '{query_name}' in {category['name']}."

            run_query_btn.click(
                fn=run_gt_query,
                inputs=[saved_sql_output],
                outputs=[gt_run_status, gt_results_df],
            )

            saved_category.change(
                fn=load_queries_for_category,
                inputs=[saved_category],
                outputs=[saved_query_dropdown, saved_sql_output, saved_description, saved_binding_key]
            )

            saved_query_dropdown.change(
                fn=load_query_sql,
                inputs=[saved_query_dropdown],
                outputs=[saved_sql_output, saved_description, saved_binding_key]
            )

            def save_query_edits_from_token(query_token, new_sql, new_description):
                category_id, query_name = _gt_unpack(query_token)
                return save_query_edits(category_id, query_name, new_sql, new_description)

            save_query_btn.click(
                fn=save_query_edits_from_token,
                inputs=[saved_query_dropdown, saved_sql_output, saved_description],
                outputs=[save_query_status],
                api_name="save_ground_truth_edits"
            )
        
        with gr.Tab("🔗 Semantic Graph"):
            gr.Markdown("""
            ### Semantic Disambiguation via Perspective Bridge Rows

            Resolve ambiguous field meanings using the current bridge-row model.
            Perspective is no longer a vertex — it is a property carried on
            two bridge collections:

            ```
            (:Intent) --[Perspective_Intents row {perspective}]--> (Perspective_Concepts row {perspective}) --> (:Concept) <-[:CAN_MEAN]- (:Field)
            ```

            *(Legacy three-hop aliases:
            `OPERATES_WITHIN` → `Perspective_Intents`,
            `USES_DEFINITION` → `Perspective_Concepts`.)*

            Select an **Intent** (analytical goal) and an **Ambiguous Field** to see how the bridge rows resolve the field's meaning.
            """)
            
            with gr.Row():
                with gr.Column():
                    gr.Markdown("#### 1. Select Intent")
                    intent_dropdown = gr.Dropdown(
                        choices=get_intents_list(),
                        label="Analytical Intent",
                        info="Intent determines which perspective and concept to use",
                        interactive=True
                    )
                    
                    gr.Markdown("#### 2. Select Ambiguous Field")
                    field_dropdown = gr.Dropdown(
                        choices=get_ambiguous_fields_list(),
                        label="Field (table.column)",
                        info="Fields with multiple possible interpretations",
                        interactive=True
                    )
                    
                    resolve_btn = gr.Button("🔍 Resolve Field Meaning", variant="primary", size="lg")
                
                with gr.Column():
                    resolution_output = gr.Markdown(
                        value="Select an intent and field, then click **Resolve Field Meaning**.",
                        label="Graph Traversal Result"
                    )
                    explanation_output = gr.HTML(
                        value="",
                        label="Resolution Explanation"
                    )
            
            resolve_btn.click(
                fn=resolve_field_gradio,
                inputs=[field_dropdown, intent_dropdown],
                outputs=[resolution_output, explanation_output],
                api_name="resolve_semantic_field"
            )
            
            gr.Markdown("---")
            
            with gr.Accordion("View Intent → Perspective Graph", open=False):
                graph_output = gr.Markdown(value=get_graph_visualization())
            
            gr.Markdown("""
            #### Semantic MCP Endpoints
            
            | Endpoint | Purpose |
            |----------|---------|
            | `GET /mcp/tools/get_perspectives` | List organizational perspectives |
            | `GET /mcp/tools/get_intents` | List analytical intents |
            | `GET /mcp/tools/get_intent_perspectives` | View Perspective_Intents bridge rows |
            | `GET /mcp/tools/get_perspective_concepts` | View Perspective_Concepts bridge rows |
            | `GET /mcp/tools/resolve_semantic_path` | Resolve (Intent, Field) → Concept via the bridge rows |
            """)

            gr.Markdown("---")

            with gr.Accordion("Define Relationship", open=False):
                gr.Markdown("""
                Add or update a semantic relationship edge. Submit the same relationship twice to
                confirm it already exists — the status indicator will change from **green** (new)
                to **amber** (already exists / updated) so you never have to read the raw message.
                """)

                with gr.Row():
                    with gr.Column():
                        dr_predicate = gr.Dropdown(
                            choices=[
                                "RESOLVES_TO",
                                "OPERATES_WITHIN",
                                "USES_DEFINITION",
                                "BOUND_TO",
                                "HAS_COLUMN",
                                "FOREIGN_KEY",
                                "MAPS_TO_CONCEPT",
                            ],
                            label="Predicate",
                            info="Type of relationship to create",
                            interactive=True,
                        )
                        dr_source = gr.Textbox(
                            label="Source ID",
                            placeholder="e.g. intents/COST_ANALYSIS  or  COST_ANALYSIS",
                            info="The 'from' node. Use collection/key or a plain entity name.",
                        )
                        dr_target = gr.Textbox(
                            label="Target ID",
                            placeholder="e.g. concepts/NCM_COST  or  NCM_COST",
                            info="The 'to' node. Use collection/key or a plain entity name.",
                        )

                    with gr.Column():
                        dr_intent = gr.Textbox(
                            label="Intent (OPERATES_WITHIN only)",
                            placeholder="e.g. COST_ANALYSIS",
                        )
                        dr_perspective = gr.Textbox(
                            label="Perspective (OPERATES_WITHIN / USES_DEFINITION)",
                            placeholder="e.g. QUALITY_ENGINEER",
                        )
                        dr_concept = gr.Textbox(
                            label="Concept Anchor (USES_DEFINITION only)",
                            placeholder="e.g. NCM_COST",
                        )
                        dr_explanation = gr.Textbox(
                            label="Explanation (optional)",
                            placeholder="Why does this relationship exist?",
                            lines=2,
                        )

                dr_submit_btn = gr.Button("Add / Update Relationship", variant="primary")
                dr_status = gr.HTML(value="")

                def _define_relationship_handler(
                    predicate, source_id, target_id,
                    intent, perspective, concept_anchor, explanation
                ):
                    import requests as _requests

                    if not predicate:
                        return (
                            '<div style="background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;'
                            'border-radius:6px;padding:10px 14px;font-weight:bold;">'
                            "❌ Please select a predicate."
                            "</div>"
                        )
                    if not source_id or not target_id:
                        return (
                            '<div style="background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;'
                            'border-radius:6px;padding:10px 14px;font-weight:bold;">'
                            "❌ Source ID and Target ID are required."
                            "</div>"
                        )

                    port = int(os.environ.get("PORT", 8080))
                    url = f"http://localhost:{port}/mcp/tools/commit_edge"
                    payload = {
                        "predicate": predicate,
                        "source_id": source_id.strip(),
                        "target_id": target_id.strip(),
                    }
                    if intent and intent.strip():
                        payload["intent"] = intent.strip()
                    if perspective and perspective.strip():
                        payload["perspective"] = perspective.strip()
                    if concept_anchor and concept_anchor.strip():
                        payload["concept_anchor"] = concept_anchor.strip()
                    if explanation and explanation.strip():
                        payload["explanation"] = explanation.strip()

                    try:
                        resp = _requests.post(url, json=payload, timeout=15)
                        data = resp.json()
                    except Exception as exc:
                        return (
                            '<div style="background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;'
                            'border-radius:6px;padding:10px 14px;font-weight:bold;">'
                            f"❌ Request failed: {exc}"
                            "</div>"
                        )

                    if not resp.ok:
                        detail = data.get("detail", resp.text)
                        return (
                            '<div style="background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;'
                            'border-radius:6px;padding:10px 14px;font-weight:bold;">'
                            f"❌ Error {resp.status_code}: {detail}"
                            "</div>"
                        )

                    created = data.get("created", True)
                    message = data.get("message", "")
                    edge_id = data.get("edge_id", "")

                    if created:
                        return (
                            '<div style="background:#d4edda;color:#155724;border:1px solid #c3e6cb;'
                            'border-radius:6px;padding:10px 14px;font-weight:bold;">'
                            f"✅ Relationship created — {message}"
                            f'<br><span style="font-weight:normal;font-size:0.85em;">Edge ID: {edge_id}</span>'
                            "</div>"
                        )
                    else:
                        return (
                            '<div style="background:#fff3cd;color:#856404;border:1px solid #ffc107;'
                            'border-radius:6px;padding:10px 14px;font-weight:bold;">'
                            f"⚠️ Already exists — {message}"
                            f'<br><span style="font-weight:normal;font-size:0.85em;">Edge ID: {edge_id}</span>'
                            "</div>"
                        )

                dr_submit_btn.click(
                    fn=_define_relationship_handler,
                    inputs=[
                        dr_predicate, dr_source, dr_target,
                        dr_intent, dr_perspective, dr_concept, dr_explanation,
                    ],
                    outputs=dr_status,
                )

        with gr.Tab("🧠 Advanced Reasoning"):
            gr.Markdown("""
            ### Advanced Semantic Reasoning
            
            Demonstrates 4 advanced patterns for semantic graph traversal:
            1. **Query Plan Comparison** - How different intents interpret the same field
            2. **Intent Resolution** - Rank intents by confidence score
            3. **SQL Intent Inference** - Automatically detect intent from SQL shape
            4. **Graph Syntax Mapping** - Cypher and AQL traversal examples
            """)
            
            with gr.Accordion("1. Intent Factor Weight → Query Plan Changes", open=True):
                gr.Markdown("See how the same field resolves differently under different intents:")
                
                with gr.Row():
                    qp_field = gr.Dropdown(
                        choices=get_ambiguous_fields_list(),
                        label="Select Ambiguous Field",
                        interactive=True
                    )
                    qp_btn = gr.Button("Compare Query Plans", variant="primary")
                
                qp_output = gr.Markdown()
                
                def compare_plans_gradio(field_choice: str) -> str:
                    if not field_choice:
                        return "Select a field to compare query plans."
                    table_name, field_name = field_choice.split("|")
                    engine = get_db_engine()
                    plans = compare_query_plans(engine, table_name, field_name)
                    if not plans:
                        return f"No query plans found for `{table_name}.{field_name}`"
                    
                    output = f"## Query Plans for `{table_name}.{field_name}`\n\n"
                    for p in plans:
                        output += f"### Intent: {p['intent']}\n"
                        output += f"- **Perspective**: {p['perspective']}\n"
                        output += f"- **Resolves to**: `{p['resolves_to']}`\n"
                        output += f"- **Elevated concepts**: {', '.join(p['elevated']) or 'None'}\n"
                        output += f"- **Suggested joins**: {', '.join(p['suggested_joins']) or 'None'}\n\n"
                    return output
                
                qp_btn.click(fn=compare_plans_gradio, inputs=[qp_field], outputs=qp_output)
            
            with gr.Accordion("2. Intent Resolution", open=False):
                gr.Markdown("Given multiple fields, compute confidence scores for each intent:")
                
                fields_input = gr.Textbox(
                    label="Fields (comma-separated: table.field, table.field)",
                    placeholder="daily_deliveries.ontime_rate, product_defects.severity",
                    lines=1
                )
                prob_btn = gr.Button("Resolve Intents", variant="secondary")
                prob_output = gr.Markdown()
                
                def probabilistic_resolve_gradio(fields_str: str) -> str:
                    if not fields_str.strip():
                        return "Enter fields to analyze."
                    
                    fields = []
                    for f in fields_str.split(","):
                        parts = f.strip().split(".")
                        if len(parts) == 2:
                            fields.append((parts[0], parts[1]))
                    
                    if not fields:
                        return "Invalid field format. Use: table.field, table.field"
                    
                    engine = get_db_engine()
                    scores = resolve_intent_probabilistic(engine, fields)
                    
                    if not scores:
                        return "No intents found for the given fields."
                    
                    output = "## Intent Confidence Scores\n\n"
                    output += "| Intent | Confidence | Matched Fields | Matched Concepts |\n"
                    output += "|--------|------------|----------------|------------------|\n"
                    for s in scores[:5]:
                        output += f"| {s.intent_name} | {s.confidence:.1%} | {len(s.matched_fields)} | {', '.join(s.matched_concepts)} |\n"

                    top = scores[0]
                    output += f"\n**Resolution explanation** (top intent):\n\n> {top.explanation}"
                    
                    return output
                
                prob_btn.click(fn=probabilistic_resolve_gradio, inputs=[fields_input], outputs=prob_output)
            
            with gr.Accordion("3. Automatic Intent Inference from SQL Shape", open=False):
                gr.Markdown("Parse SQL to detect likely intent based on tables, columns, and patterns:")
                
                sql_input = gr.Textbox(
                    label="SQL Query",
                    placeholder="SELECT supplier_id, AVG(ontime_rate) FROM daily_deliveries GROUP BY supplier_id",
                    lines=3
                )
                infer_btn = gr.Button("Infer Intent", variant="secondary")
                infer_output = gr.Markdown()
                
                def infer_intent_gradio(sql: str) -> str:
                    if not sql.strip():
                        return "Enter a SQL query to analyze."
                    
                    engine = get_db_engine()
                    scores = infer_intent_from_sql(engine, sql)
                    
                    if not scores:
                        return "Could not infer intent from SQL. Check that tables/columns exist in schema."
                    
                    output = "## Inferred Intents\n\n"
                    for i, s in enumerate(scores[:3]):
                        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else ""
                        output += f"### {medal} {s.intent_name} ({s.confidence:.1%})\n"
                        output += f"- **Matched fields**: {', '.join(s.matched_fields)}\n"
                        output += f"- **Concepts**: {', '.join(s.matched_concepts)}\n"
                        output += f"- *{s.explanation}*\n\n"
                    
                    return output
                
                infer_btn.click(fn=infer_intent_gradio, inputs=[sql_input], outputs=infer_output)
            
            with gr.Accordion("4. ArangoDB / Neo4j Traversal Syntax", open=False):
                gr.Markdown("Generate explicit graph database syntax for the semantic path:")
                
                with gr.Row():
                    syntax_intent = gr.Dropdown(
                        choices=get_intents_list(),
                        label="Intent",
                        interactive=True
                    )
                    syntax_field = gr.Dropdown(
                        choices=get_ambiguous_fields_list(),
                        label="Field",
                        interactive=True
                    )
                
                syntax_btn = gr.Button("Generate Graph Syntax", variant="secondary")
                
                with gr.Tabs():
                    with gr.Tab("Cypher (Neo4j)"):
                        gr.Markdown(
                            "> ⚠️ **Reference only** — This Cypher query is illustrative and **cannot be run directly**. "
                            "The Perspective vertex collection is retired; query patterns now use bridge-row docs. "
                            "See `Perspective_Intents` / `Perspective_Concepts` collections for live traversal."
                        )
                        cypher_output = gr.Code(language="sql", label="Cypher Query")
                    with gr.Tab("AQL (ArangoDB)"):
                        gr.Markdown(
                            "> ⚠️ **Reference only** — This AQL query is illustrative and **cannot be run directly**. "
                            "The Perspective vertex collection is retired; query patterns now use bridge-row docs. "
                            "See `Perspective_Intents` / `Perspective_Concepts` collections for live traversal."
                        )
                        aql_output = gr.Code(language="sql", label="AQL Query")
                    with gr.Tab("SQL Equivalent"):
                        sql_equiv_output = gr.Code(language="sql", label="SQL Query")
                
                def generate_syntax_gradio(intent: str, field_choice: str):
                    if not intent or not field_choice:
                        return "-- Select intent and field", "-- Select intent and field", "-- Select intent and field"
                    
                    table_name, field_name = field_choice.split("|")
                    engine = get_db_engine()

                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter("always", DeprecationWarning)
                        syntax = get_graph_syntax_examples(engine, intent, table_name, field_name)

                    for w in caught:
                        msg = str(w.message)
                        logging.warning("Graph syntax DeprecationWarning: %s", msg)
                        gr.Warning(f"⚠️ Reference only — {msg}")

                    return syntax["cypher"], syntax["aql"], syntax["sql_equivalent"]
                
                syntax_btn.click(
                    fn=generate_syntax_gradio,
                    inputs=[syntax_intent, syntax_field],
                    outputs=[cypher_output, aql_output, sql_equiv_output]
                )
        
        with gr.Tab("📝 SME SQL Entry"):
            gr.Markdown("""
            ### SME Semantic SQL Submission

            Pick the semantic context, write the SQL, submit for review.
            Each submission gets a **deterministic filename** and a
            **Reviewer Manifest** entry
            (SME Submit → Binding Key → Manifest → Approver Review → Solder).
            """)

            # Same cascade source as the Ontology Mosaic: real manifest
            # categories and approved concept anchors drive the selector.
            try:
                from ground_truth_selector import (
                    SelectorCascade as _SmeCascade,
                    load_selector_entries as _sme_load_entries,
                )
                _sme_cascade = _SmeCascade(
                    _sme_load_entries(MANIFEST_PATH, SQLITE_DB_PATH)
                )
                _sme_cat_choices = _sme_cascade.filter_choices("category")
            except Exception:
                _sme_cascade = None
                _sme_cat_choices = []
            if not _sme_cat_choices:
                _sme_cat_choices = [
                    ("Inventory Management", "inventory_management"),
                    ("Quality Control", "quality_control"),
                    ("Delivery Performance", "delivery_performance"),
                    ("Production Analytics", "production_analytics"),
                    ("Supplier Performance", "supplier_performance"),
                ]
            _sme_cat0 = _sme_cat_choices[0][1]

            def _sme_concept_choices(cat):
                if _sme_cascade is None:
                    return []
                return _sme_cascade.anchor_choices({"category": cat})

            gr.Markdown("#### 1. Semantic Context")
            with gr.Row():
                sme_category = gr.Dropdown(
                    choices=_sme_cat_choices,
                    label="Category",
                    value=_sme_cat0,
                    interactive=True,
                    scale=1,
                )
                sme_perspective = gr.Dropdown(
                    choices=get_perspectives_list(),
                    label="Perspective",
                    info="Organizational lens for this SQL",
                    interactive=True,
                    scale=1,
                )
                sme_concept = gr.Dropdown(
                    choices=_sme_concept_choices(_sme_cat0),
                    label="Concept",
                    info="Pick an existing anchor or type a new one",
                    value=None,
                    allow_custom_value=True,
                    interactive=True,
                    scale=2,
                )

            def _sme_on_category(cat):
                return gr.update(
                    choices=_sme_concept_choices(cat), value=None
                )

            sme_category.change(
                fn=_sme_on_category,
                inputs=[sme_category],
                outputs=[sme_concept],
            )

            gr.Markdown("#### 2. SQL Statement")
            sme_sql = gr.Code(
                label="SQL Statement",
                language="sql",
                lines=16,
                value="-- Enter your SQL here\nSELECT ",
            )

            with gr.Row():
                sme_justification = gr.Textbox(
                    label="SME Justification / Notes",
                    placeholder="Why does this SQL represent the concept from this perspective?",
                    lines=2,
                    scale=4,
                )
                with gr.Column(scale=1, min_width=180):
                    sme_submit_btn = gr.Button(
                        "Submit for Review", variant="primary", size="lg"
                    )
            sme_status = gr.Textbox(
                label="Submission Status", interactive=False, value=""
            )

            with gr.Accordion("Reviewer Console — decision table & approvals", open=False):
                    def _friendly_name(binding_key: str, concept: str, perspective: str) -> str:
                        """Convert binding key to human-readable name for reviewer."""
                        if concept and concept != "UNKNOWN":
                            label = concept.replace("_", " ").title()
                            if perspective:
                                return f"{label} ({perspective})"
                            return label
                        parts = binding_key.replace("gt_", "").rsplit("_", 2)
                        if len(parts) >= 2:
                            return parts[0].replace("_", " ").title()
                        return binding_key

                    def load_reviewer_table() -> str:
                        bindings = resolve_sql_bindings()
                        if not bindings:
                            return "No submissions yet. Use the form above to submit SQL."
                        
                        output = "| Name | Perspective | Concept | Status |\n"
                        output += "|------|-------------|---------|--------|\n"
                        
                        status_icons = {
                            "PENDING": "⏳",
                            "APPROVED": "✅",
                            "REJECTED": "❌",
                            "UNKNOWN": "❓"
                        }
                        
                        for b in bindings:
                            icon = status_icons.get(b["validation_status"], "❓")
                            name = _friendly_name(b["binding_key"], b.get("concept", ""), b.get("perspective", ""))
                            output += f"| {name} | {b['perspective']} | {b['concept']} | {icon} {b['validation_status']} |\n"
                        
                        summary = get_manifest_summary()
                        output += f"\n**Total: {summary['total']}** | "
                        output += f"Pending: {summary['pending']} | "
                        output += f"Approved: {summary['approved']} | "
                        output += f"Rejected: {summary['rejected']}"
                        
                        return output
                    
                    reviewer_table = gr.Markdown(value=load_reviewer_table())
                    refresh_reviewer_btn = gr.Button("Refresh Table", variant="secondary")
                    
                    gr.Markdown("#### Review Actions")
                    
                    def get_pending_binding_keys():
                        bindings = resolve_sql_bindings()
                        result = []
                        for b in bindings:
                            friendly = _friendly_name(b["binding_key"], b.get("concept", ""), b.get("perspective", ""))
                            status = b.get("validation_status", "UNKNOWN")
                            label = f"{friendly} [{status}]"
                            result.append((label, b["binding_key"]))
                        return result
                    
                    with gr.Row():
                        review_binding_key = gr.Dropdown(
                            choices=get_pending_binding_keys(),
                            label="Binding Key",
                            interactive=True,
                            allow_custom_value=False
                        )
                        review_action = gr.Dropdown(
                            choices=["APPROVED", "REJECTED", "PENDING"],
                            label="Decision",
                            interactive=True
                        )
                    review_btn = gr.Button("Update Status", variant="secondary")
                    review_status = gr.Textbox(label="Review Status", interactive=False)
            
            def submit_sme(sql, category, perspective, concept, justification):
                if not sql or not sql.strip():
                    return "Please enter a SQL statement.", load_reviewer_table(), gr.update()
                cleaned_sql = sql.strip()
                if cleaned_sql in ("-- Enter your SQL here\nSELECT", "-- Enter your SQL here", "SELECT"):
                    return "Please enter a SQL statement.", load_reviewer_table(), gr.update()
                if not perspective:
                    return "Please select a perspective.", load_reviewer_table(), gr.update()
                if not concept or not concept.strip():
                    concept = _auto_concept_from_sql(cleaned_sql)
                
                result = save_sme_submission(cleaned_sql, category or _sme_cat0, perspective, concept.strip(), justification or "")
                keys = get_pending_binding_keys()
                return result, load_reviewer_table(), gr.update(choices=keys, value=keys[0] if keys else None)
            
            sme_submit_btn.click(
                fn=submit_sme,
                inputs=[sme_sql, sme_category, sme_perspective, sme_concept, sme_justification],
                outputs=[sme_status, reviewer_table, review_binding_key]
            )
            
            def refresh_reviewer_and_keys():
                keys = get_pending_binding_keys()
                return load_reviewer_table(), gr.update(choices=keys, value=keys[0] if keys else None)
            
            refresh_reviewer_btn.click(fn=refresh_reviewer_and_keys, outputs=[reviewer_table, review_binding_key])
            
            def do_review(binding_key, action):
                if not binding_key:
                    return "Select a binding key.", load_reviewer_table(), gr.update()
                if not action:
                    return "Select a decision.", load_reviewer_table(), gr.update()
                result = update_binding_status(binding_key.strip(), action)
                keys = get_pending_binding_keys()
                return result, load_reviewer_table(), gr.update(choices=keys, value=keys[0] if keys else None)
            
            review_btn.click(
                fn=do_review,
                inputs=[review_binding_key, review_action],
                outputs=[review_status, reviewer_table, review_binding_key]
            )
        
        solder = SolderEngine()
        
        with gr.Tab("💬 Ask a Question"):
            gr.Markdown("""
            ### Production Dispatcher — Hybrid RAG
            
            Ask a **natural language question** about your manufacturing data. The system:
            1. Routes your question to the correct **Intent** and **Concepts** (closed vocabulary)
            2. Passes those to the **SolderEngine** to assemble governed SQL
            3. Returns perspective-aware SQL built entirely from **SME-approved queries**
            
            The LLM acts as a **Semantic Router**, not a SQL generator. All SQL is governed.
            """)
            _cfg_aaq = _get_erp_config()
            _aaq_source_note = (
                "\n\n> ⚠️ Using the default ERP name. Set `ERP_INSTANCE_NAME` to configure your system."
                if _cfg_aaq["erp_instance_name_source"] == "default" else ""
            )
            aaq_erp_md = gr.Markdown(
                f"**Active ERP:** `{_cfg_aaq['erp_instance_name']}` "
                f"*(source: {_cfg_aaq['erp_instance_name_source']})*{_aaq_source_note}"
            )
            
            dispatcher = ProductionDispatcher(solder_engine=solder, use_live_api=True)
            
            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("#### Your Question")
                    nl_input = gr.Textbox(
                        label="Natural Language Query",
                        placeholder="e.g., What are our top defect costs this quarter?",
                        lines=3,
                        info="Ask about defects, costs, suppliers, OEE, scheduling, maintenance..."
                    )
                    
                    with gr.Row():
                        perspective_override = gr.Dropdown(
                            choices=[("Auto-detect", "")] + [(p, p) for p in dispatcher.perspectives],
                            value="",
                            label="Perspective Override",
                            info="Leave as Auto-detect to let the router choose",
                            interactive=True
                        )
                        dispatch_dialect = gr.Dropdown(
                            choices=[
                                ("SQLite", "sqlite"),
                                ("T-SQL (SQL Server)", "tsql"),
                                ("PostgreSQL", "postgres"),
                                ("MySQL", "mysql"),
                                ("BigQuery", "bigquery"),
                            ],
                            value="sqlite",
                            label="Target Dialect",
                            interactive=True
                        )
                    
                    with gr.Row():
                        dispatch_mode = gr.Radio(
                            choices=[("Live API (HuggingFace)", "live"), ("Demo Mode (Mock)", "mock")],
                            value="mock",
                            label="Routing Mode",
                            info="Demo mode uses keyword matching; Live uses HF Inference API"
                        )
                    
                    dispatch_btn = gr.Button("Ask", variant="primary", size="lg")
                    
                    gr.Markdown("#### Example Questions")
                    gr.Markdown("""
                    - *"Show me the cost of defects on the East Wall"*
                    - *"What is our supplier delivery performance?"*
                    - *"How is OEE trending for the assembly line?"*
                    - *"What are the top NCM costs this quarter?"*
                    - *"Which defects have customer impact?"*
                    - *"What's the maintenance schedule for critical equipment?"*
                    - *"Which parts need reordering?"*
                    - *"What are our current stock levels by part class?"*
                    """)
                
                with gr.Column(scale=1):
                    gr.Markdown("#### Governed SQL Output")
                    dispatch_sql = gr.Code(label="Assembled SQL", language="sql", lines=14)
                    
                    dispatch_metadata = gr.Markdown(label="Routing Metadata")
                    
                    dispatch_warnings = gr.Markdown(label="Warnings")
            
            def run_dispatch(question, perspective, dialect, mode):
                if not question or not question.strip():
                    return (
                        "-- Enter a question above",
                        "Enter a natural language question to begin.",
                        ""
                    )
                
                force_mock = (mode == "mock")
                persp = perspective if perspective else None
                
                result = dispatcher.dispatch(
                    user_query=question.strip(),
                    perspective_override=persp,
                    force_mock=force_mock,
                    target_dialect=dialect or "sqlite"
                )
                
                meta = f"## Routing Result\n\n"
                meta += f"**Mode:** {result.routing_mode}\n\n"
                meta += f"**Confidence:** {result.routing_confidence}\n\n"
                meta += f"**Intent:** `{result.intent}`\n\n"
                meta += f"**Binding Key:** `{result.binding_key}`\n\n" if result.binding_key else ""
                meta += f"**Concepts:** {', '.join(f'`{c}`' for c in result.concepts) if result.concepts else 'None'}\n\n"
                meta += f"**Perspective:** {result.perspective or 'N/A'}\n\n"
                
                if result.assembly_report:
                    meta += "### Assembly Report\n"
                    for line in result.assembly_report:
                        meta += f"{line}\n"
                    meta += "\n"
                
                warn_md = ""
                if result.warnings:
                    warn_md = "### Warnings\n"
                    for w in result.warnings:
                        warn_md += f"- {w}\n"
                
                if result.out_of_scope:
                    warn_md += "\n**This question is outside the manufacturing domain.** Try asking about defects, costs, suppliers, OEE, scheduling, or maintenance.\n"
                
                return result.assembled_sql, meta, warn_md
            
            dispatch_btn.click(
                fn=run_dispatch,
                inputs=[nl_input, perspective_override, dispatch_dialect, dispatch_mode],
                outputs=[dispatch_sql, dispatch_metadata, dispatch_warnings]
            )
        
        with gr.Tab("🔧 Solder Engine"):
            gr.Markdown("""
            ### Semantic Transpilation Engine
            
            The **Solder Pattern** assembles final executable SQL by combining:
            - **Elevation Weights** from the semantic graph (which concepts are relevant)
            - **Approved SQL Queries** from SME submissions (governed ground truth)
            - **SQLGlot AST Manipulation** for alias renaming, table qualification, and dialect transpilation
            
            ```
            Intent → ELEVATES → Concept → Approved Query → SQLGlot AST → Final SQL
            ```
            """)
            
            _SOLDER_CONCEPT_BLANK = ("(primary elevated concept)", "")

            def _solder_intents_for_category(category=None):
                """Intent names, optionally filtered to one intent_category."""
                return [
                    i["intent_name"]
                    for i in solder.get_available_intents()
                    if not category or i["intent_category"] == category
                ]

            def _solder_elevated_concepts(intent_name):
                """Concept names ELEVATED (weight = 1) by this intent."""
                import sqlite3
                if not intent_name:
                    return []
                conn = sqlite3.connect(SQLITE_DB_PATH)
                try:
                    rows = conn.execute(
                        """
                        SELECT c.concept_name
                        FROM schema_intent_concepts ic
                        JOIN schema_intents i ON i.intent_id = ic.intent_id
                        JOIN schema_concepts c ON c.concept_id = ic.concept_id
                        WHERE i.intent_name = ? AND ic.intent_factor_weight = 1
                        ORDER BY c.concept_name
                        """,
                        (intent_name,),
                    ).fetchall()
                finally:
                    conn.close()
                return [r[0] for r in rows]

            with gr.Row():
                with gr.Column():
                    gr.Markdown("#### 1. Select Intent")
                    available_intents = solder.get_available_intents()
                    solder_categories = sorted({i["intent_category"] for i in available_intents})

                    with gr.Row():
                        solder_category = gr.Dropdown(
                            choices=solder_categories,
                            label="Perspective",
                            info="Business area — narrows the intent list",
                            interactive=True,
                        )
                        solder_intent = gr.Dropdown(
                            choices=_solder_intents_for_category(),
                            label="Analytical Intent",
                            info="Determines which concepts are ELEVATED vs SUPPRESSED",
                            interactive=True,
                        )

                    gr.Markdown("#### 2. Target Concept (optional)")
                    solder_concept = gr.Dropdown(
                        choices=[_SOLDER_CONCEPT_BLANK],
                        value="",
                        label="Target Concept",
                        info="Only concepts ELEVATED by the selected intent",
                        interactive=True,
                    )

                    def _on_solder_category_change(category):
                        # Single-input handler: a programmatic gr.update-triggered
                        # .change only delivers the trigger's own value.
                        return (
                            gr.update(choices=_solder_intents_for_category(category), value=None),
                            gr.update(choices=[_SOLDER_CONCEPT_BLANK], value=""),
                        )

                    def _on_solder_intent_change(intent_name):
                        opts = [_SOLDER_CONCEPT_BLANK] + [
                            (c, c) for c in _solder_elevated_concepts(intent_name)
                        ]
                        return gr.update(choices=opts, value="")

                    solder_category.change(
                        fn=_on_solder_category_change,
                        inputs=[solder_category],
                        outputs=[solder_intent, solder_concept],
                    )
                    solder_intent.change(
                        fn=_on_solder_intent_change,
                        inputs=[solder_intent],
                        outputs=[solder_concept],
                    )
                    
                    gr.Markdown("#### 3. Output Dialect")
                    solder_dialect = gr.Dropdown(
                        choices=[
                            ("SQLite", "sqlite"),
                            ("T-SQL (SQL Server)", "tsql"),
                            ("PostgreSQL", "postgres"),
                            ("MySQL", "mysql"),
                            ("BigQuery", "bigquery"),
                        ],
                        value="sqlite",
                        label="Target SQL Dialect",
                        interactive=True
                    )
                    
                    with gr.Accordion("Alias Overrides (Advanced)", open=False):
                        gr.Markdown("Rename table aliases in the soldered SQL:")
                        alias_old = gr.Textbox(label="Old Alias", placeholder="e.g., t1")
                        alias_new = gr.Textbox(label="New Alias", placeholder="e.g., defects")
                    
                    solder_btn = gr.Button("Solder Query", variant="primary", size="lg")
                    report_btn = gr.Button("View Elevation Report", variant="secondary")
                
                with gr.Column():
                    gr.Markdown("#### Soldered Output")
                    soldered_sql_output = gr.Code(label="Final SQL", language="sql", lines=10)
                    
                    solder_details = gr.Markdown(label="Solder Details")
                    
                    solder_report_output = gr.Markdown(label="Elevation Report")
            
            def run_solder(intent, concept, dialect, old_alias, new_alias):
                if not intent:
                    return "-- Select an intent to solder", "Select an intent above."
                
                overrides = {}
                if old_alias and old_alias.strip() and new_alias and new_alias.strip():
                    overrides[old_alias.strip()] = new_alias.strip()
                
                result = solder.solder(
                    intent_name=intent,
                    target_concept=concept.strip() if concept and concept.strip() else None,
                    target_dialect=dialect or "sqlite",
                    context_overrides=overrides if overrides else None
                )
                
                details = f"## Solder Result\n\n"
                details += f"**Binding Key:** `{result.binding_key}`\n\n"
                details += f"**Perspective:** {result.perspective}\n\n"
                details += f"**Concept:** {result.concept}\n\n"
                details += f"**Logic Type:** {result.logic_type}\n\n"
                details += f"**Elevation Weight:** {result.elevation_weight}\n\n"
                details += f"**Dialect:** {result.dialect}\n\n"
                
                if result.ast_operations:
                    details += "### AST Operations\n"
                    for op in result.ast_operations:
                        details += f"- {op}\n"
                    details += "\n"
                
                if result.warnings:
                    details += "### Warnings\n"
                    for w in result.warnings:
                        details += f"- {w}\n"
                
                return result.soldered_sql, details
            
            def run_report(intent):
                if not intent:
                    return "Select an intent to view the elevation report."
                return solder.get_solder_report(intent)
            
            solder_btn.click(
                fn=run_solder,
                inputs=[solder_intent, solder_concept, solder_dialect, alias_old, alias_new],
                outputs=[soldered_sql_output, solder_details]
            )
            
            report_btn.click(
                fn=run_report,
                inputs=[solder_intent],
                outputs=solder_report_output
            )
            
            gr.Markdown("---")
            gr.Markdown("""
            ### Preview Solder (Multi-Concept Assembly)
            
            Combine **multiple concepts** into a single cohesive query. The engine resolves 
            each concept's approved query, applies elevation weights, and assembles them 
            as projections from a base table. Suppressed concepts become `NULL`.
            """)
            
            with gr.Row():
                with gr.Column():
                    assemble_intent = gr.Dropdown(
                        choices=[
                            (f"{i['intent_name']} ({i['intent_category']})", i['intent_name'])
                            for i in available_intents
                        ],
                        label="Intent",
                        info="Controls which concepts are elevated vs suppressed",
                        interactive=True
                    )
                    
                    perspective_choices = ["Finance", "Quality", "Operations", "Customer", "Compliance"]
                    assemble_perspective = gr.Dropdown(
                        choices=perspective_choices,
                        label="Perspective",
                        info="SME perspective for query resolution",
                        interactive=True
                    )
                    
                    assemble_concepts = gr.Textbox(
                        label="Concepts (comma-separated)",
                        placeholder="e.g., DefectSeverityCost, DefectSeverityQuality, DefectSeverityCustomer",
                        info="List the concepts to include as projections"
                    )
                    
                    assemble_base_table = gr.Textbox(
                        label="Base Table",
                        value="stg_manufacturing_flat",
                        info="FROM clause table name"
                    )
                    
                    assemble_dialect = gr.Dropdown(
                        choices=[
                            ("SQLite", "sqlite"),
                            ("T-SQL (SQL Server)", "tsql"),
                            ("PostgreSQL", "postgres"),
                            ("MySQL", "mysql"),
                            ("BigQuery", "bigquery"),
                        ],
                        value="sqlite",
                        label="Target Dialect",
                        interactive=True
                    )
                    
                    assemble_btn = gr.Button("Assemble Query", variant="primary", size="lg")
                
                with gr.Column():
                    gr.Markdown("#### Assembled Output")
                    assembled_sql_output = gr.Code(label="Assembled SQL", language="sql", lines=12)
                    assembled_report = gr.Markdown(label="Assembly Report")
            
            def run_assemble(intent, perspective, concepts_str, base_table, dialect):
                if not intent:
                    return "-- Select an intent", "Select an intent above."
                if not concepts_str or not concepts_str.strip():
                    return "-- Enter concepts", "Enter comma-separated concept names."
                
                concepts = [c.strip() for c in concepts_str.split(",") if c.strip()]
                result = solder.assemble_query(
                    intent=intent,
                    perspective=perspective or "",
                    concepts=concepts,
                    base_table=base_table or "stg_manufacturing_flat",
                    target_dialect=dialect or "sqlite"
                )
                
                report_md = f"## Assembly Report\n\n"
                report_md += f"**Intent:** {result.get('intent', '')}\n\n"
                report_md += f"**Perspective:** {result.get('perspective', '')}\n\n"
                report_md += f"**Base Table:** `{result.get('base_table', '')}`\n\n"
                report_md += f"**Concepts Resolved:** {result.get('concept_count', 0)} / {len(concepts)}\n\n"
                report_md += f"**Dialect:** {result.get('dialect', 'sqlite')}\n\n"
                
                if result.get("fail_closed"):
                    fc = ", ".join(result.get("fail_closed_concepts", []))
                    fc_condition = result.get("fail_closed_condition") or ""
                    reason = {
                        "no_perspective_compatible_snippet": (
                            f"no perspective-compatible query under perspective "
                            f"**{perspective or '(none)'}**. Cross-perspective SQL is never served."
                        ),
                        "missing_snippet_file": "the approved binding has no ground-truth SQL to serve.",
                        "fingerprint_validation_failed": (
                            "the query failed structural-fingerprint validation "
                            "(unparseable or drifted from its SME-approved base tables)."
                        ),
                        "join_validation_failed": (
                            "the query failed join-edge validation (a join drifted "
                            "from its SME-approved / graph-recognized set)."
                        ),
                        "temporal_contract_validation_failed": (
                            "a baked-in tokenized (:named) parameter is not part of "
                            "the snippet's Temporal Parameter Contract. See the "
                            "Concept Resolution / Warnings below for the exact "
                            "parameter and reason."
                        ),
                        "multiple": "multiple fail-closed conditions were triggered.",
                    }.get(fc_condition, "one or more concepts could not be served.")
                    report_md += (
                        f"> ⛔ **FAIL-CLOSED ({fc_condition or 'unknown'})** for `{fc}` — "
                        f"{reason} No SQL is served.\n\n"
                    )
                
                if result.get("report"):
                    report_md += "### Concept Resolution\n"
                    for line in result["report"]:
                        report_md += f"{line}\n"
                    report_md += "\n"
                
                if result.get("warnings"):
                    report_md += "### Warnings\n"
                    for w in result["warnings"]:
                        report_md += f"- {w}\n"
                
                return result.get("sql", ""), report_md
            
            assemble_btn.click(
                fn=run_assemble,
                inputs=[assemble_intent, assemble_perspective, assemble_concepts, assemble_base_table, assemble_dialect],
                outputs=[assembled_sql_output, assembled_report]
            )
        
        with gr.Tab("📐 Metrics"):
            gr.Markdown("""
            ### Metrics — Define-Once, Generate-Anywhere

            A **metric** is a semantic *concept* node identified by **duck typing**
            — it carries a non-empty `computation_template` (there is no `concept_type`
            flag) — and stores a **dialect-agnostic computation template** with named `{variable}`
            placeholders — **never** static SQL. Each variable binds to a real physical
            column through a `resolves_to` edge. The **SolderEngine** substitutes the
            variables with table-qualified columns and transpiles, so the metric is
            *defined once* and generates identical SQL everywhere.

            Pick a metric below to see its template, its variable→column lineage, the
            AI meta-context for the tables it draws from, and the generated SQL in any
            dialect.
            """)

            try:
                _metric_list = solder.list_metrics()
            except Exception as _e:
                _metric_list = []
            _metric_choices = [(m["concept_name"], m["concept_name"]) for m in _metric_list]

            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("#### 1. Select Metric")
                    metric_dropdown = gr.Dropdown(
                        choices=_metric_choices,
                        label="Metric (concept node)",
                        info="Concepts with a computation_template",
                        interactive=True,
                        value=_metric_choices[0][1] if _metric_choices else None,
                    )

                    gr.Markdown("#### 2. Output Dialect")
                    metric_dialect = gr.Dropdown(
                        choices=[
                            ("SQLite", "sqlite"),
                            ("T-SQL (SQL Server)", "tsql"),
                            ("PostgreSQL", "postgres"),
                            ("MySQL", "mysql"),
                            ("BigQuery", "bigquery"),
                        ],
                        value="sqlite",
                        label="Target SQL Dialect",
                        interactive=True,
                    )

                    metric_btn = gr.Button("Generate Metric SQL", variant="primary", size="lg")

                with gr.Column(scale=2):
                    gr.Markdown("#### Generated SQL")
                    metric_sql_output = gr.Code(label="Metric SQL", language="sql", lines=8)
                    metric_info = gr.Markdown(label="Metric Details")

            def render_metric(metric, dialect):
                if not metric:
                    return "-- Select a metric above", "Select a metric to view its template and lineage."

                lineage = solder.get_metric_lineage(metric)
                if not lineage:
                    return f"-- '{metric}' is not a defined metric", f"**{metric}** has no computation template."

                # Build the human-facing detail panel.
                meta = lineage.get("metric", metric)
                info = f"## {meta}\n\n"

                # description (from the concept row)
                desc = ""
                for m in _metric_list:
                    if m["concept_name"] == metric:
                        desc = m.get("description") or ""
                        break
                if desc:
                    info += f"{desc}\n\n"

                info += "### Computation Template (dialect-agnostic)\n"
                info += f"```\n{lineage.get('template', '')}\n```\n\n"

                info += "### Lineage — variable → physical column\n"
                info += "| Variable | Physical column | SME meaning |\n"
                info += "|---|---|---|\n"
                for v in lineage.get("variables", []):
                    meaning = v.get("meaning") or v.get("display_name") or ""
                    info += f"| `{{{v['variable']}}}` | `{v['table']}.{v['column']}` | {meaning} |\n"
                info += "\n"

                tables = lineage.get("tables", [])
                perspectives = lineage.get("perspectives", [])
                info += f"**Tables:** {', '.join(f'`{t}`' for t in tables) or '—'}\n\n"
                info += f"**Perspectives served:** {', '.join(perspectives) or '—'}\n\n"

                # Table meta-context (AI overlay — never on physical column nodes).
                try:
                    from table_description_pipeline import get_table_description
                    ctx_blocks = []
                    for t in tables:
                        td = get_table_description(t)
                        if td:
                            label = td.get("display_name") or t
                            block = f"**`{t}`** — {label}\n\n"
                            if td.get("description"):
                                block += f"{td['description']}\n\n"
                            if td.get("ai_context"):
                                block += f"*AI context:* {td['ai_context']}\n"
                            ctx_blocks.append(block)
                    if ctx_blocks:
                        info += "### Table meta-context (AI overlay)\n"
                        info += "\n".join(ctx_blocks)
                        info += "\n"
                except Exception:
                    pass

                # Assemble the SQL (fail closed).
                try:
                    result = solder.assemble_metric_sql(metric, target_dialect=dialect or "sqlite")
                except MetricAssemblyError as exc:
                    return f"-- Assembly failed (fail closed):\n-- {exc}", info
                except Exception as exc:
                    return f"-- Unexpected error: {exc}", info

                if result.join_path:
                    info += "### Join path (FK-derived)\n"
                    for jp in result.join_path:
                        info += f"- {jp}\n"
                    info += "\n"
                if result.warnings:
                    info += "### Warnings\n"
                    for w in result.warnings:
                        info += f"- {w}\n"

                return result.sql, info

            metric_btn.click(
                fn=render_metric,
                inputs=[metric_dropdown, metric_dialect],
                outputs=[metric_sql_output, metric_info],
            )
            metric_dropdown.change(
                fn=render_metric,
                inputs=[metric_dropdown, metric_dialect],
                outputs=[metric_sql_output, metric_info],
            )
            metric_dialect.change(
                fn=render_metric,
                inputs=[metric_dropdown, metric_dialect],
                outputs=[metric_sql_output, metric_info],
            )

        with gr.Tab("📆 MRP Schedule"):
            import mrp_engine as mrp

            gr.Markdown("""
            ### MRP Demand-Supply Schedule Grid

            A classic **time-phased MRP grid** for a selected part: open customer-order
            demand **plus forecast demand** (customer orders consume the forecast per
            bucket, so nothing is double-counted) is netted against on-hand inventory and
            existing work-order / purchase-order scheduled receipts across a rolling
            **9-month horizon**, and lot-for-lot **planned orders** fill the gaps while
            keeping the projected balance at or above **safety stock (1)**. Planned order
            *releases* are the planned *receipts* pulled earlier by the part's lead time.

            Everything is **deterministic** and computed **read-only** against SQLite. The
            horizon is anchored to a **data-derived as-of date** (the latest work-order
            close date), never the wall clock. A part with missing planning inputs (no
            lead time, missing columns) **fails with a clear message** rather than
            silently planning against zero.
            """)

            try:
                _mc = mrp.connect()
                _mrp_parts = mrp.list_planning_parts(_mc)
                _mrp_as_of = mrp.compute_as_of(_mc)
                _mrp_buckets = mrp.month_buckets(_mrp_as_of)
                _mc.close()
            except Exception:
                _mrp_parts, _mrp_as_of, _mrp_buckets = [], None, []

            _mrp_choices = [
                (f"{p['part_id']} — {p['part_class']} · demand {p['demand_qty']}", p["part_id"])
                for p in _mrp_parts
            ]
            if _mrp_as_of and _mrp_buckets:
                _mrp_hdr = (
                    f"**As-of (data-derived):** {_mrp_as_of.strftime('%Y-%m-%d')}  ·  "
                    f"**Horizon:** {_mrp_buckets[0][0]} … {_mrp_buckets[-1][0]}  ·  "
                    f"**{len(_mrp_choices)}** planning parts"
                )
            else:
                _mrp_hdr = (
                    "_MRP data foundation not found — run "
                    "`python scripts/bootstrap_db.py` (one-command rebuild), "
                    "then restart the app._"
                )
            gr.Markdown(_mrp_hdr)

            with gr.Row():
                mrp_part = gr.Dropdown(
                    choices=_mrp_choices,
                    label="Planning part (open demand in horizon)",
                    value=_mrp_choices[0][1] if _mrp_choices else None,
                    interactive=True,
                    scale=3,
                )
                mrp_btn = gr.Button("Show MRP Grid", variant="primary", scale=1)

            mrp_detail = gr.Markdown()
            mrp_grid = gr.Dataframe(
                headers=["MRP Line"]
                + (["Past Due"] + [b[0] for b in _mrp_buckets] if _mrp_buckets else []),
                interactive=False,
                wrap=True,
                label="Time-phased MRP grid",
            )

            def render_mrp(part_id):
                import pandas as pd
                import mrp_engine as mrp

                if not part_id:
                    return None, "Select a planning part to view its MRP schedule."
                conn = mrp.connect()
                try:
                    grid = mrp.compute_mrp_grid(conn, part_id)
                except ValueError as exc:
                    return None, f"⚠️ {exc}"
                except Exception as exc:  # pragma: no cover - defensive
                    return None, f"⚠️ Unexpected error: {exc}"
                finally:
                    conn.close()

                cols = ["MRP Line"] + grid["columns"]
                data = [[label] + list(vals) for label, vals in grid["rows"]]
                df = pd.DataFrame(data, columns=cols)
                detail = (
                    f"**Part:** `{grid['part_id']}`  ·  class **{grid['part_class']}**  ·  "
                    f"lead time **{grid['lead_time_days']} days**  ·  "
                    f"on-hand **{grid['on_hand_qty']}**  ·  "
                    f"safety stock **{grid['safety_stock']:g}**  ·  "
                    f"in-horizon demand: CO **{grid['co_demand_qty']:g}** / "
                    f"forecast **{grid['forecast_qty']:g}**\n\n"
                    f"**As-of (data-derived):** {grid['as_of'].strftime('%Y-%m-%d')}  ·  "
                    f"**Horizon:** {grid['columns'][1]} … {grid['columns'][-1]}\n\n"
                    "*Planned Order Releases = Planned Order Receipts pulled earlier by the "
                    "part's lead time; anything already behind the plan start folds into "
                    "Past Due.*"
                )
                return df, detail

            mrp_btn.click(fn=render_mrp, inputs=[mrp_part], outputs=[mrp_grid, mrp_detail])
            mrp_part.change(fn=render_mrp, inputs=[mrp_part], outputs=[mrp_grid, mrp_detail])

        with gr.Tab("🧩 Ontology Mosaic"):
            gr.Markdown("<small>🎛️ <b>Selector v 1.0</b></small>")

            try:
                from ground_truth_selector import load_selector_entries
                _svo_entries = load_selector_entries(MANIFEST_PATH, SQLITE_DB_PATH)
            except Exception:
                _svo_entries = []

            _svo_by_binding = {e["binding_key"]: e for e in _svo_entries}

            # Selector v 1.0 chain — the same filters as the 🎛️ Selector tab
            # (Perspective filter → Table → Column → Concept → Intent →
            # Ground-truth query), reusing the shared _sel_* helpers and
            # single-input packed-token handlers. The final query pick drives
            # the three lens tabs below.
            with gr.Row():
                mo_tags = gr.Dropdown(
                    choices=_sel_categories(),
                    label="Perspective filter (type to search, multi-select)",
                    value=[], multiselect=True, filterable=True, scale=1)
                gr.Markdown("", scale=2)  # spacer keeps the filter narrow
            with gr.Row():
                # allow_custom_value: survives app restarts under a stale
                # browser session (see the 🎛️ Selector tab note).
                mo_table = gr.Dropdown(
                    choices=[(t, "" + _SEL_SEP + t) for t in _sel_tables([])],
                    label="Table", value=None, scale=1,
                    allow_custom_value=True)
                mo_column = gr.Dropdown(choices=[], label="Column",
                                        value=None, scale=1,
                                        allow_custom_value=True)
                mo_concept = gr.Dropdown(choices=[], label="Concept",
                                         value=None, scale=1,
                                         allow_custom_value=True)
                mo_intent = gr.Dropdown(choices=[], label="Intent",
                                        value=None, scale=1,
                                        allow_custom_value=True)
                mo_query = gr.Dropdown(choices=_sel_query_choices([]),
                                       label="Ground-truth query",
                                       value=None, scale=1,
                                       allow_custom_value=True)
            mo_summary = gr.Markdown(_sel_summary([]))

            gr.Markdown("""
            ### Ontology Mosaic — four lenses, one selector

            The ground-truth SQL **IS the view** — it tells the complete story.
            This mosaic shows that story through **four lenses**, all driven by
            the **Selector v 1.0 chain above** (Perspective filter → Table →
            Column → Concept → Intent → Ground-truth query):

            - **🔗 Join Topology** — the graph topology **extracted by SQLGlot**
              from the approved SQL: physical tables touched, join relationships,
              set-gating predicates, grain, and time-phasing — governed metadata
              in `sql_view_ontology`.
            - **🧠 Semantic Ontology** — the concept-layer story for the same
              anchor: the concept node, its `resolves_to` variable→column→table
              lineage, and its computation template (when it is a metric), read
              from the governed `sql_graph_nodes` / `sql_graph_edges` tables.
            - **📜 SQL** — the raw approved ground-truth SQL text.
            - **🧾 SQL Semantics** — metadata as the deliverable: a call to
              action that prints the **Structural Relational Ontology
              Contract — SQL** (the solder's lossless fingerprint, canonical
              join edges, predicates, grain, and output shape) instead of
              running or rendering the SQL.

            Nothing is executed against a database — pure AST analysis and
            governed metadata, read-only.
            """)

            _NO_JOINS_PLACEHOLDER = [[
                "— (single-table / set query)", "none", "—", "", "",
            ]]

            with gr.Tabs():
                with gr.Tab("🔗 Join Topology"):
                    svo_summary = gr.Markdown()
                    svo_joins = gr.Dataframe(
                        headers=["Left table", "Join type", "Right table", "Left key", "Right key"],
                        interactive=False,
                        label="Join relationships — outer query",
                        wrap=False,
                    )
                    svo_scopes = gr.Markdown()
                    svo_detail = gr.Markdown()
                with gr.Tab("🧠 Semantic Ontology"):
                    svo_semantic = gr.Markdown()
                with gr.Tab("📜 SQL"):
                    svo_sql = gr.Code(
                        language="sql", interactive=False,
                        label="Approved ground-truth SQL",
                    )
                with gr.Tab("🧾 SQL Semantics"):
                    gr.Markdown(
                        "**Metadata as the deliverable.** This lens never "
                        "executes SQL and never renders the raw file — the "
                        "call to action interrogates the governed SQL's AST "
                        "and prints a structured contract mapping syntax to "
                        "meaning: the solder's lossless fingerprint, canonical "
                        "join edges, set-membership predicates, grain, and "
                        "output shape."
                    )
                    sem_btn = gr.Button(
                        "Generate Semantics — SQL", variant="primary")
                    sem_contract = gr.Markdown(
                        "Pick a ground-truth query in the selector above — "
                        "the contract renders automatically (or press "
                        "**Generate Semantics — SQL**)."
                    )
                    sem_sql = gr.Code(
                        language="sql", interactive=False,
                        label="Approved ground-truth SQL (copy/paste)",
                    )
            # Stash the Selector v 1.0 query pick the moment it happens —
            # the button reads this State instead of the dropdown, because
            # dynamically re-choiced dropdowns don't reliably deliver their
            # packed value at click time.
            sem_sel = gr.State("")

            def _read_gt_sql(entry) -> str:
                """Load the raw ground-truth SQL text for an entry, '' if missing."""
                if not entry or not entry.get("file_path"):
                    return ""
                sql_path = os.path.normpath(
                    os.path.join(SCHEMA_DIR, "..", entry["file_path"])
                )
                if not os.path.exists(sql_path):
                    return ""
                try:
                    with open(sql_path, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    return ""

            def _render_semantic_pane(entry) -> str:
                """Semantic Ontology lens — graceful degradation on any gap."""
                if not entry:
                    return "Select a ground-truth query above."
                anchor = entry.get("concept_anchor") or ""
                try:
                    from semantic_ontology import (
                        get_semantic_ontology, render_semantic_ontology_markdown,
                    )
                    onto = get_semantic_ontology(SQLITE_DB_PATH, anchor)
                    return render_semantic_ontology_markdown(onto, anchor)
                except Exception as exc:
                    return f"Semantic layer unavailable: {exc}"

            def _temporal_validation_md(sql_text, binding_key, has_params):
                """Show SMEs exactly why a snippet would be blocked (or is safe)
                by the passive temporal-parameter contract check. Purely
                surfaces the existing fail-closed reason — nothing is executed
                and no value is bound."""
                if not (sql_text or "").strip():
                    return ""
                try:
                    ok, reason, _ = solder.validate_temporal_contract(
                        sql_text, binding_key or ""
                    )
                except Exception as exc:  # noqa: BLE001
                    ok, reason = False, (
                        f"the contract could not be derived to validate this "
                        f"snippet ({type(exc).__name__}: {exc})"
                    )
                if not ok:
                    return (
                        "#### ⛔ Temporal-parameter contract: BLOCKED "
                        "(fail-closed)\n"
                        f"> This snippet would **not** be served — {reason}.\n\n"
                        "> **Fix:** bind each named parameter to a physical "
                        "column inside a `(:param IS NULL OR <column> <op> "
                        ":param)` guarded predicate, then re-register the "
                        "snippet."
                    )
                if has_params:
                    return (
                        "#### ✅ Temporal-parameter contract: OK\n"
                        "> Every named parameter is a column-bound, classifiable "
                        "guarded predicate."
                    )
                return ""

            def render_view_ontology(binding_key):
                import sqlite3 as _r_sqlite
                from dataclasses import asdict as _r_asdict
                from view_ontology_extractor import (
                    extract_view_ontology, get_view_ontology_by_binding_key,
                    render_join_scope_sections_md, split_joins_by_scope,
                )

                if not binding_key:
                    _msg = "Select a ground-truth query above."
                    return _msg, [], "", _msg, _msg, ""

                entry = _svo_by_binding.get(binding_key)
                sql_text = _read_gt_sql(entry)
                semantic_md = _render_semantic_pane(entry)

                conn = _r_sqlite.connect(SQLITE_DB_PATH)
                try:
                    rec = get_view_ontology_by_binding_key(conn, binding_key)
                finally:
                    conn.close()

                extracted_live = False
                if rec is None and entry and sql_text:
                    try:
                        vo = extract_view_ontology(
                            sql_text, binding_key,
                            entry["concept_anchor"], entry["file_path"],
                        )
                        d = _r_asdict(vo)
                        rec = {
                            "concept_anchor": d["concept_anchor"],
                            "binding_key": d["binding_key"],
                            "physical_tables_json": d["physical_tables"],
                            "cte_names_json": d["cte_names"],
                            "joins_json": d["joins"],
                            "state_predicates_json": d["state_predicates"],
                            "grain_columns_json": d["grain_columns"],
                            "time_phased": d["time_phased"],
                            "temporal_keys_json": d["temporal_keys"],
                            "selected_columns_json": d["selected_columns"],
                            "semantics_version": d["semantics_version"],
                            "extracted_at": d["extracted_at"],
                            "temporal_parameters_json": d["temporal_parameters"],
                            "temporal_trait_json": d["temporal_trait"],
                        }
                        extracted_live = True
                    except Exception as exc:
                        return (
                            f"Extraction failed for `{binding_key}`: {exc}",
                            [], "", "", semantic_md, sql_text,
                        )

                if rec is None:
                    return (
                        f"No ontology found for `{binding_key}`.",
                        [], "", "", semantic_md, sql_text,
                    )

                meta = ""
                if entry:
                    meta = (
                        f"**Perspective:** `{entry['perspective']}` · "
                        f"**Logic:** `{entry['logic_type']}`\n\n"
                        f"_{entry['sme_justification']}_\n\n"
                    )

                tp = "⏱ **Time-phased**" if rec["time_phased"] else "· Single-point-in-time"
                tk = (
                    f"  Temporal keys: `{'`, `'.join(rec['temporal_keys_json'])}`"
                    if rec["temporal_keys_json"] else ""
                )
                _trait = rec.get("temporal_trait_json") or []
                trait_bits = (
                    "\n\n**Temporal trait:** "
                    + " · ".join(f"`{t}`" for t in _trait)
                    if _trait else ""
                )

                summary = (
                    f"**{rec['concept_anchor']}**  —  `{rec['binding_key']}`\n\n"
                    f"{meta}"
                    f"{tp}{tk}{trait_bits}\n\n"
                    f"**Physical tables** ({len(rec['physical_tables_json'])}):"
                    f"  `{'` · `'.join(rec['physical_tables_json'])}`\n\n"
                    f"**CTE scaffolding** ({len(rec['cte_names_json'])}):"
                    + (f"  `{'` · `'.join(rec['cte_names_json'])}`" if rec['cte_names_json'] else "  _(none)_")
                )

                # Sectioned join topology: outer-query joins in the dataframe,
                # one labeled markdown section per derived-subquery/CTE scope.
                # Legacy rows without scope info fall entirely into the outer
                # bucket — flat rendering, exactly as before.
                _outer_joins, _scoped_joins = split_joins_by_scope(rec["joins_json"])
                joins_table = [
                    [
                        j["left_table"], j["join_type"], j["right_table"],
                        j["left_key"] or "", j["right_key"] or "",
                    ]
                    for j in _outer_joins
                ] or _NO_JOINS_PLACEHOLDER
                scopes_md = render_join_scope_sections_md(
                    _scoped_joins, rec["cte_names_json"]
                )

                preds = rec["state_predicates_json"]
                grain = rec["grain_columns_json"]
                outcols = rec["selected_columns_json"]

                detail_lines = []
                if preds:
                    detail_lines.append("#### Set-membership predicates (WHERE)")
                    for p in preds:
                        detail_lines.append(f"```sql\n{p}\n```")
                if grain:
                    detail_lines.append(f"#### Grain  (GROUP BY / ORDER BY keys)\n`{'` · `'.join(grain)}`")
                if outcols:
                    detail_lines.append(
                        f"#### Output columns ({len(outcols)})\n"
                        + "  ".join(f"`{c}`" for c in outcols)
                    )
                _tparams = rec.get("temporal_parameters_json") or []
                if _tparams:
                    _ml = {"horizon": "Horizon window",
                           "netting": "Netting snapshot",
                           "filter": "Entity filter"}
                    _tc = ["#### Temporal Contract (named, NULL-guarded parameters)",
                           "| Parameter | Guards physical column | Op | Mode |",
                           "|---|---|---|---|"]
                    for _pm in _tparams:
                        _tc.append(
                            f"| `{_pm['token']}` | `{_pm['column']}` "
                            f"| `{_pm['operator']}` "
                            f"| {_ml.get(_pm['classification'], _pm['classification'])} |")
                    if _trait:
                        _tc.append("")
                        _tc.append(
                            "**Temporal trait:** "
                            + " · ".join(f"`{t}`" for t in _trait))
                    detail_lines.append("\n".join(_tc))
                _tv_md = _temporal_validation_md(
                    sql_text, binding_key, bool(_tparams)
                )
                if _tv_md:
                    detail_lines.append(_tv_md)
                _src = (
                    "extracted live from the SQL text (pure AST, not yet seeded)"
                    if extracted_live else "seeded in `sql_view_ontology`"
                )
                detail_lines.append(
                    f"\n---\n_Extracted by SQLGlot · semantics version `{rec['semantics_version']}`"
                    f" · as of {rec['extracted_at'][:10]} · {_src}_"
                )
                detail = "\n\n".join(detail_lines)

                return summary, joins_table, scopes_md, detail, semantic_md, sql_text

            _mosaic_outputs = [svo_summary, svo_joins, svo_scopes, svo_detail, svo_semantic, svo_sql]

            def _find_gt_query(name):
                """Locate a governed query by its '-- Query:' marker name."""
                idx = get_query_categories()
                for _cat in idx.get("categories", []):
                    for _q in get_saved_queries(_cat["id"]):
                        if _q["name"] == name:
                            return _q
                return None

            def _mosaic_render_chain(val):
                """Render the three lenses from the Selector v 1.0 query pick.

                If the governed query carries a '-- Binding:' marker that maps
                to a seeded ontology entry, reuse the seeded render path;
                otherwise extract the ontology live from the SQL text
                (pure AST, fail-visible).
                """
                _msg = "Pick a ground-truth query in the selector above."
                _csv, _t, _c, concept, _i, query = _sel_parse(val or "", 6)
                if not query:
                    return _msg, [], "", _msg, _msg, ""
                q = _find_gt_query(query)
                if q is None:
                    _err = f"Ground-truth query `{query}` not found in the governed files."
                    return _err, [], "", _err, _err, ""
                bk = (q.get("binding_key") or "").strip()
                if bk and bk in _svo_by_binding:
                    return render_view_ontology(bk)

                sql_text = q["sql"].strip()
                semantic_md = (
                    _render_semantic_pane({"concept_anchor": concept})
                    if concept else
                    "Pick a concept in the selector above to see its semantic-layer story."
                )
                try:
                    from dataclasses import asdict as _mrc_asdict
                    from view_ontology_extractor import (
                        extract_view_ontology,
                        render_join_scope_sections_md as _mrc_scope_md,
                        split_joins_by_scope as _mrc_split,
                    )
                    d = _mrc_asdict(extract_view_ontology(
                        sql_text, bk or f"chain::{query}",
                        concept or query, "",
                    ))
                except Exception as exc:
                    return (
                        f"Extraction failed for `{query}`: {exc}",
                        [], "", "", semantic_md, sql_text,
                    )

                tp = "⏱ **Time-phased**" if d["time_phased"] else "· Single-point-in-time"
                tk = (
                    f"  Temporal keys: `{'`, `'.join(d['temporal_keys'])}`"
                    if d["temporal_keys"] else ""
                )
                trait_bits = (
                    "\n\n**Temporal trait:** "
                    + " · ".join(f"`{t}`" for t in d["temporal_trait"])
                    if d.get("temporal_trait") else ""
                )
                desc = q.get("description") or ""
                summary = (
                    f"**{query}**\n\n"
                    + (f"_{desc}_\n\n" if desc else "")
                    + f"{tp}{tk}{trait_bits}\n\n"
                    f"**Physical tables** ({len(d['physical_tables'])}):"
                    f"  `{'` · `'.join(d['physical_tables'])}`\n\n"
                    f"**CTE scaffolding** ({len(d['cte_names'])}):"
                    + (f"  `{'` · `'.join(d['cte_names'])}`" if d["cte_names"] else "  _(none)_")
                )
                _outer_joins, _scoped_joins = _mrc_split(d["joins"])
                joins_table = [
                    [
                        j["left_table"], j["join_type"], j["right_table"],
                        j["left_key"] or "", j["right_key"] or "",
                    ]
                    for j in _outer_joins
                ] or _NO_JOINS_PLACEHOLDER
                scopes_md = _mrc_scope_md(_scoped_joins, d["cte_names"])
                detail_lines = []
                if d["state_predicates"]:
                    detail_lines.append("#### Set-membership predicates (WHERE)")
                    for p in d["state_predicates"]:
                        detail_lines.append(f"```sql\n{p}\n```")
                if d["grain_columns"]:
                    detail_lines.append(
                        f"#### Grain  (GROUP BY / ORDER BY keys)\n`{'` · `'.join(d['grain_columns'])}`"
                    )
                if d["selected_columns"]:
                    detail_lines.append(
                        f"#### Output columns ({len(d['selected_columns'])})\n"
                        + "  ".join(f"`{c}`" for c in d["selected_columns"])
                    )
                if d.get("temporal_parameters"):
                    _ml = {"horizon": "Horizon window",
                           "netting": "Netting snapshot",
                           "filter": "Entity filter"}
                    _tc = ["#### Temporal Contract (named, NULL-guarded parameters)",
                           "| Parameter | Guards physical column | Op | Mode |",
                           "|---|---|---|---|"]
                    for _pm in d["temporal_parameters"]:
                        _tc.append(
                            f"| `{_pm['token']}` | `{_pm['column']}` "
                            f"| `{_pm['operator']}` "
                            f"| {_ml.get(_pm['classification'], _pm['classification'])} |")
                    if d.get("temporal_trait"):
                        _tc.append("")
                        _tc.append(
                            "**Temporal trait:** "
                            + " · ".join(f"`{t}`" for t in d["temporal_trait"]))
                    detail_lines.append("\n".join(_tc))
                _tv_md = _temporal_validation_md(
                    sql_text, bk or f"chain::{query}",
                    bool(d.get("temporal_parameters")),
                )
                if _tv_md:
                    detail_lines.append(_tv_md)
                detail_lines.append(
                    f"\n---\n_Extracted live by SQLGlot (pure AST, not yet seeded)"
                    f" · semantics version `{d['semantics_version']}`"
                    f" · as of {d['extracted_at'][:10]}_"
                )
                detail = "\n\n".join(detail_lines)

                return summary, joins_table, scopes_md, detail, semantic_md, sql_text

            # Chain wiring — identical to the 🎛️ Selector tab: every handler
            # takes ONLY its trigger's packed value (Gradio's programmatic
            # .change events deliver just the trigger component's own value).
            mo_tags.change(
                fn=_sel_on_tags, inputs=[mo_tags],
                outputs=[mo_table, mo_column, mo_concept, mo_intent,
                         mo_query, mo_summary],
            )
            mo_table.change(
                fn=_sel_on_table, inputs=[mo_table],
                outputs=[mo_column, mo_concept, mo_intent, mo_query,
                         mo_summary],
            )
            mo_column.change(
                fn=_sel_on_column, inputs=[mo_column],
                outputs=[mo_concept, mo_intent, mo_query, mo_summary],
            )
            mo_concept.change(
                fn=_sel_on_concept, inputs=[mo_concept],
                outputs=[mo_intent, mo_query, mo_summary],
            )
            mo_intent.change(
                fn=_sel_on_intent, inputs=[mo_intent],
                outputs=[mo_query, mo_summary],
            )
            mo_query.change(
                fn=_sel_on_query, inputs=[mo_query],
                outputs=[mo_summary],
            )
            mo_query.change(
                fn=_mosaic_render_chain, inputs=[mo_query],
                outputs=_mosaic_outputs,
            )
            mo_query.change(
                fn=lambda v: v or "", inputs=[mo_query],
                outputs=[sem_sel],
            )

            def _sql_semantics_contract(val, live_val=""):
                """🧾 SQL Semantics call to action — metadata as the deliverable.

                Interrogates the governed SQL's AST (never executes it, never
                renders the raw file) and prints a Structural Relational
                Ontology Contract: the solder's lossless fingerprint, canonical
                join edges, set-membership predicates, grain, and output shape.
                Fail-visible at every hop — parse failure yields an error, not
                a guess.
                """
                # Prefer the stashed pick; fall back to the live dropdown
                # value so a click still works when the stash never fired.
                tags_csv, table, column, concept, intent, query = _sel_parse(
                    val or live_val or "", 6)
                if not query:
                    return ("Pick a ground-truth query in the selector above, "
                            "then press **Generate Semantics — SQL**."), ""
                q = _find_gt_query(query)
                if q is None:
                    return (f"Ground-truth query `{query}` not found in the "
                            f"governed files."), ""
                sql_text = (q.get("sql") or "").strip()
                if not sql_text:
                    return (f"`{query}` carries no SQL text — nothing to "
                            f"interrogate."), ""
                bk = (q.get("binding_key") or "").strip()

                import structural_fingerprint as _sfp
                try:
                    fingerprint = _sfp.base_table_set(sql_text, strict=True)
                except _sfp.FingerprintParseError as exc:
                    return (f"**Fail-closed:** SQLGlot could not parse "
                            f"`{query}` — no contract can be established.\n\n"
                            f"```\n{exc}\n```"), sql_text
                edges, unresolved = _sfp.join_edges_from_sql(sql_text)

                try:
                    from dataclasses import asdict as _sc_asdict
                    from view_ontology_extractor import extract_view_ontology
                    d = _sc_asdict(extract_view_ontology(
                        sql_text, bk or f"chain::{query}",
                        concept or query, "",
                    ))
                except Exception as exc:
                    return (f"AST facet extraction failed for `{query}`: "
                            f"{exc}"), sql_text

                _persp = tags_csv.replace(",", " · ") if tags_csv else "(all)"
                _sel_bits = [f"**Perspective:** {_persp}"]
                if table:
                    _sel_bits.append(f"**Table:** `{table}`")
                if column:
                    _sel_bits.append(f"**Column:** `{column}`")
                if concept:
                    _sel_bits.append(f"**Concept:** `{concept}`")
                if intent:
                    _sel_bits.append(f"**Intent:** {intent}")
                lines = [
                    "#### 🎛️ Selector v 1.0 pane",
                    "",
                    " ✦ ".join(_sel_bits),
                    "",
                    "---",
                    "",
                    "## OUTPUT: Structural Relational Ontology Contract — SQL",
                    "",
                    f"**Ground-truth query:** `{query}`"
                    + (f" · **Binding key:** `{bk}`" if bk else
                       " · _(no binding marker — contract derived live)_"),
                    "",
                    f"**Lossless fingerprint** ({len(fingerprint)} base tables"
                    f" · `{_sfp.EXTRACTOR_ID_V2}`):",
                    "",
                    "`[" + ", ".join(fingerprint) + "]`",
                    "",
                ]

                if edges:
                    lines.append(f"**Canonical join edges** ({len(edges)}):")
                    lines.append("")
                    lines.append("| Table A | Column A | Table B | Column B | Join |")
                    lines.append("|---|---|---|---|---|")
                    for e in edges:
                        ed = _sfp.edge_to_dict(e)
                        lines.append(
                            f"| `{ed['table_a']}` | `{ed['column_a']}` "
                            f"| `{ed['table_b']}` | `{ed['column_b']}` "
                            f"| {ed['join_type']} |")
                    lines.append("")
                if unresolved:
                    lines.append(
                        f"**Unresolved joins** ({len(unresolved)} — warn, never block):")
                    for u in unresolved:
                        lines.append(f"- `{u['reason']}` — `{u['detail']}`")
                    lines.append("")

                if d["state_predicates"]:
                    lines.append(
                        f"**Set-membership predicates** ({len(d['state_predicates'])}):")
                    for p in d["state_predicates"]:
                        lines.append(f"```sql\n{p}\n```")
                    lines.append("")
                if d["grain_columns"]:
                    lines.append(
                        "**Grain (GROUP BY / ORDER BY keys):**  "
                        + " · ".join(f"`{g}`" for g in d["grain_columns"]))
                    lines.append("")
                if d["cte_names"]:
                    lines.append(
                        f"**CTE scaffolding** ({len(d['cte_names'])}):  "
                        + " · ".join(f"`{c}`" for c in d["cte_names"]))
                    lines.append("")
                _tp = ("⏱ time-phased"
                       + (" (keys: " + ", ".join(f"`{k}`" for k in d["temporal_keys"]) + ")"
                          if d["temporal_keys"] else "")
                       if d["time_phased"] else "single-point-in-time")
                lines.append(f"**Temporal shape:** {_tp}")
                lines.append("")
                if d.get("temporal_parameters"):
                    _mode_label = {
                        "horizon": "Horizon window",
                        "netting": "Netting snapshot",
                        "filter": "Entity filter",
                    }
                    lines.append(
                        f"**Temporal Contract** ({len(d['temporal_parameters'])} "
                        "named, NULL-guarded parameters — baked into the "
                        "SME-approved snippet, served verbatim; nothing is "
                        "generated):")
                    lines.append("")
                    lines.append("| Parameter | Guards physical column | Op | Mode |")
                    lines.append("|---|---|---|---|")
                    for _pm in d["temporal_parameters"]:
                        lines.append(
                            f"| `{_pm['token']}` | `{_pm['column']}` "
                            f"| `{_pm['operator']}` "
                            f"| {_mode_label.get(_pm['classification'], _pm['classification'])} |")
                    if d.get("temporal_trait"):
                        lines.append("")
                        lines.append(
                            "**Temporal trait:** "
                            + " · ".join(f"`{t}`" for t in d["temporal_trait"])
                            + "  _(declares this binding key supports temporal"
                            " constraints)_")
                    lines.append("")
                if d["selected_columns"]:
                    lines.append(
                        f"**Output columns** ({len(d['selected_columns'])}):  "
                        + " ".join(f"`{c}`" for c in d["selected_columns"]))
                    lines.append("")
                lines.append(
                    "---\n_Nothing was executed — pure AST interrogation "
                    f"(SQLGlot, `{_sfp.FINGERPRINT_DIALECT}` dialect) · "
                    f"semantics version `{d['semantics_version']}` · "
                    f"as of {d['extracted_at'][:10]}_")
                return "\n".join(lines), sql_text

            sem_btn.click(
                fn=_sql_semantics_contract, inputs=[sem_sel, mo_query],
                outputs=[sem_contract, sem_sql],
            )
            # Auto-render the contract the moment a query is picked, so the
            # pane is never empty; the button remains as an explicit re-run.
            mo_query.change(
                fn=_sql_semantics_contract, inputs=[mo_query],
                outputs=[sem_contract, sem_sql],
            )

        with gr.Tab("🦉 Ontology Mapping"):
            gr.Markdown("""
            ### Ontop Ontology Mapping (SPARQL/OWL) — What the Solder Sees

            The Ontop POC republishes the governed SQL layer as a **virtual
            OWL/SPARQL graph**. Each OBDA mapping pairs a **target** (the RDF
            triples it mints) with a **source** — a governed SQL query. That
            source SQL is exactly *what the solder sees*, viewed through
            standards-based vocabulary.

            Each tab has one concern: the **Ontology Mosaic** holds the AST and
            semantic views, **Ground Truth SQL Queries** holds the SQL itself, and this tab holds the
            **ontology mapping** — the SPARQL-facing OWL vocabulary and the
            OBDA target/source pairs that bind it to the governed SQL. No
            database is touched, and nothing here is wired into the running app.
            """)

            try:
                from ontop_ontology_selector import (
                    load_ontop_entries,
                    load_binding_bridge as _oo_bridge_fn,
                    load_query_bindings as _oo_qbind_fn,
                    entries_for_binding as _oo_bound_fn,
                    selector_choices as _oo_choices_fn,
                    selector_choices_for_module as _oo_mod_choices_fn,
                    module_choices as _oo_cat_fn,
                    slot_legend as _oo_legend_fn,
                )
                _oo_entries = load_ontop_entries()
                _oo_bridge = _oo_bridge_fn()
                _oo_query_bindings = _oo_qbind_fn()
                _oo_choices = _oo_choices_fn(_oo_entries)
                _oo_cat_choices = _oo_cat_fn(_oo_entries)
                _oo_legend = _oo_legend_fn()
            except Exception as _oo_err:
                _oo_entries, _oo_choices, _oo_cat_choices = [], [], []
                _oo_bridge = {}
                _oo_query_bindings = {}
                _oo_mod_choices_fn = None
                _oo_bound_fn = None
                _oo_legend = f"Selector unavailable: {_oo_err}"

            _oo_by_key = {e["entry_key"]: e for e in _oo_entries}
            _oo_modules = sorted({e["module"] for e in _oo_entries})

            _oo_cat0 = _oo_cat_choices[0][1] if _oo_cat_choices else None
            _oo_map0_choices = (
                _oo_mod_choices_fn(_oo_entries, _oo_cat0)
                if _oo_mod_choices_fn else _oo_choices
            )

            # ── Selector v 1.0 chain on top (mirrors the Ontology Mosaic
            # tab): the final ground-truth query pick resolves through the
            # governed binding-key → mapping bridge to the OBDA mappings
            # that republish its story. Explicit bridge only — an unbound
            # query renders a visible "no ontology mapping bound" message.
            gr.Markdown("<small>🎛️ <b>Selector v 1.0</b></small>")

            with gr.Row():
                om_tags = gr.Dropdown(
                    choices=_sel_categories(),
                    label="Perspective filter (type to search, multi-select)",
                    value=[], multiselect=True, filterable=True, scale=1)
                gr.Markdown("", scale=2)  # spacer keeps the filter narrow
            with gr.Row():
                # allow_custom_value: survives app restarts under a stale
                # browser session (see the 🎛️ Selector tab note).
                om_table = gr.Dropdown(
                    choices=[(t, "" + _SEL_SEP + t) for t in _sel_tables([])],
                    label="Table", value=None, scale=1,
                    allow_custom_value=True)
                om_column = gr.Dropdown(choices=[], label="Column",
                                        value=None, scale=1,
                                        allow_custom_value=True)
                om_concept = gr.Dropdown(choices=[], label="Concept",
                                         value=None, scale=1,
                                         allow_custom_value=True)
                om_intent = gr.Dropdown(choices=[], label="Intent",
                                        value=None, scale=1,
                                        allow_custom_value=True)
                om_query = gr.Dropdown(choices=_sel_query_choices([]),
                                       label="Ground-truth query",
                                       value=None, scale=1,
                                       allow_custom_value=True)
            om_summary = gr.Markdown(_sel_summary([]))

            def _om_coverage_counts():
                """(bound, total) over the governed palette queries.

                A query counts as bound when its '-- Binding:' marker (or,
                failing that, the committed query_bindings hop) reaches a
                binding key the bridge maps to ≥1 showcase module.
                """
                total, bound = 0, 0
                try:
                    for _cat in get_query_categories().get("categories", []):
                        for _q in get_saved_queries(_cat["id"]):
                            total += 1
                            _bk = (_q.get("binding_key") or "").strip()
                            _bks = ([_bk] if _bk
                                    else _oo_query_bindings.get(_q["name"], []))
                            if any(_oo_bridge.get(_k) for _k in _bks):
                                bound += 1
                except Exception:
                    pass
                return bound, total

            _om_bound_count, _om_total_count = _om_coverage_counts()
            gr.Markdown(
                f"**{_om_bound_count} of {_om_total_count} governed queries** "
                f"currently have a showcase ontology bound "
                f"({len(_oo_entries)} OBDA mappings across "
                f"{len(_oo_modules)} showcase ontologies). The bridge is "
                f"explicit and committed — a query without a bound mapping "
                f"says so, it never guesses."
            )

            oo_summary = gr.Markdown()
            oo_detail = gr.Markdown()
            with gr.Accordion("Target triples + source SQL (what the solder sees)", open=False):
                oo_target = gr.Code(language=None, interactive=False, label="Target (RDF triple template)")
                oo_sql = gr.Code(language="sql", interactive=False, label="Source (governed SQL)")

            def _oo_render_one(entry):
                """Summary + minted-vocabulary markdown for ONE OBDA mapping.

                Returns (summary_md, detail_md, pretty_target, source_sql) —
                the shared building block for the single-mapping browse path
                and the multi-mapping bridge path. The footer line is NOT
                included here (callers append it once).
                """
                target = entry["target"]
                source_sql = entry["source_sql"]

                # Ontology header + minted-class annotation
                terms = entry.get("terms") or {}
                cls = entry["subject_class"].strip("()")
                cls_info = terms.get(cls) or {}
                cls_line = ""
                if entry["subject_class"] and not entry["subject_class"].startswith("("):
                    cls_line = (
                        f"**Mints class** `:{cls}`"
                        + (f" — *{cls_info.get('label', '')}*" if cls_info.get("label") else "")
                        + (f"\n\n> {cls_info['comment']}" if cls_info.get("comment") else "")
                        + "\n\n"
                    )
                elif entry["subject_class"]:
                    cls_line = (
                        f"**Fact-only mapping** — attaches properties to existing "
                        f"`:{cls}/…` individuals (no class mint).\n\n"
                    )

                summary = (
                    f"**{entry['mapping_id']}**  —  `{entry['module']}`\n\n"
                    f"**Showcase:** {entry['ontology_label'] or entry['module_title']}\n\n"
                    + (f"_{entry['ontology_comment']}_\n\n" if entry["ontology_comment"] else "")
                    + cls_line
                )

                detail_lines = []
                minted = entry["datatype_terms"] + entry["object_terms"]
                if minted:
                    detail_lines.append("#### Minted vocabulary (from the ontology)")
                    rows = []
                    for t in minted:
                        info = terms.get(t) or {}
                        kind = "🔗 link" if t in entry["object_terms"] else "· fact"
                        label = info.get("label", "")
                        comment = info.get("comment", "")
                        rows.append(
                            f"- {kind} `:{t}`"
                            + (f" — *{label}*" if label else "")
                            + (f"<br/>&nbsp;&nbsp;{comment}" if comment else "")
                        )
                    detail_lines.append("\n".join(rows))
                detail = "\n\n".join(detail_lines)

                pretty_target = target.replace(" ; ", " ;\n    ").replace(" .", " .")
                return summary, detail, pretty_target, source_sql

            _OO_FOOTER = (
                "\n---\n_Read-only POC · parity proven against the governed SQL "
                "on a WAL snapshot · for the AST view of governed SQL, see the "
                "Ontology Mosaic tab_"
            )

            def render_ontop_ontology(entry_key):
                if not entry_key:
                    return "Select an OBDA mapping above.", "", "", ""

                entry = _oo_by_key.get(entry_key)
                if entry is None:
                    return f"No mapping found for `{entry_key}`.", "", "", ""

                summary, detail, pretty_target, source_sql = _oo_render_one(entry)
                return summary, detail + _OO_FOOTER, pretty_target, source_sql

            def _om_render_chain(val):
                """Selector v 1.0 query pick → bridged OBDA mapping(s).

                Resolution is deterministic and fail-visible at every hop:
                query name → governed file → binding key → committed bridge
                → mapping entries. No hop ever falls back to a guess.
                """
                _msg = "Pick a ground-truth query in the selector above."
                _csv, _t, _c, _concept, _i, query = _sel_parse(val or "", 6)
                if not query:
                    return _msg, "", "", ""
                q = _find_gt_query(query)
                if q is None:
                    _err = f"Ground-truth query `{query}` not found in the governed files."
                    return _err, "", "", ""
                bk = (q.get("binding_key") or "").strip()
                bks = [bk] if bk else list(_oo_query_bindings.get(query, []))
                if not bks:
                    return (
                        f"**No ontology mapping bound.** `{query}` carries no "
                        f"binding marker and the committed bridge records no "
                        f"binding key for it, so it cannot resolve.",
                        "", "", "",
                    )
                bound = []
                if _oo_bound_fn:
                    _seen = set()
                    for _bk in bks:
                        for e in _oo_bound_fn(_oo_entries, _oo_bridge, _bk):
                            if e["entry_key"] not in _seen:
                                _seen.add(e["entry_key"])
                                bound.append(e)
                if not bound:
                    return (
                        f"**No ontology mapping bound to this query.**\n\n"
                        f"`{query}` is governed SQL, but it is not part of any "
                        f"published showcase ontology yet — the committed "
                        f"bridge (`poc/ontop-ontology-poc/binding_bridge.json`) "
                        f"has no entry for it. All mappings remain browsable "
                        f"in the accordion below.",
                        "", "", "",
                    )

                _mods = []
                for e in bound:
                    if e["module"] not in _mods:
                        _mods.append(e["module"])
                header = (
                    f"**{query}**  →  {len(bound)} bound mapping"
                    f"{'s' if len(bound) != 1 else ''} in "
                    + " · ".join(f"`{m}`" for m in _mods)
                )

                summaries, details, targets, sqls = [header], [], [], []
                for e in bound:
                    s, d, t, sql = _oo_render_one(e)
                    summaries.append(s)
                    if d:
                        details.append(f"### `{e['mapping_id']}`\n\n{d}")
                    targets.append(f"## {e['mapping_id']}\n{t}")
                    sqls.append(f"-- mappingId: {e['mapping_id']}\n{sql}")

                return (
                    "\n\n---\n\n".join(summaries),
                    "\n\n".join(details) + _OO_FOOTER,
                    "\n\n".join(targets),
                    "\n\n".join(sqls),
                )

            # ── Browse-all fallback (demoted): the flat module → mapping
            # picker keeps every mapping reachable, bridged or not.
            with gr.Accordion("Browse all OBDA mappings (flat picker)", open=False):
                gr.Markdown(
                    f"Same fixed-width 6-slot scheme as the Ontology Mosaic "
                    f"tab:\n\n{_oo_legend}"
                )
                with gr.Row():
                    oo_cat = gr.Dropdown(
                        choices=_oo_cat_choices,
                        value=_oo_cat0,
                        label="1 · Category (showcase ontology)",
                        interactive=True,
                        scale=1,
                    )
                    oo_picker = gr.Dropdown(
                        choices=_oo_map0_choices,
                        label="2 · OBDA mapping (6-slot summary)",
                        value=_oo_map0_choices[0][1] if _oo_map0_choices else None,
                        interactive=True,
                        scale=2,
                        elem_classes=["gt-slot-select"],
                    )
                    oo_btn = gr.Button("Show Mapping", variant="primary", scale=0)

            _oo_outputs = [oo_summary, oo_detail, oo_target, oo_sql]

            # Chain wiring — identical to the 🎛️ Selector tab: every handler
            # takes ONLY its trigger's packed value (Gradio's programmatic
            # .change events deliver just the trigger component's own value).
            om_tags.change(
                fn=_sel_on_tags, inputs=[om_tags],
                outputs=[om_table, om_column, om_concept, om_intent,
                         om_query, om_summary],
            )
            om_table.change(
                fn=_sel_on_table, inputs=[om_table],
                outputs=[om_column, om_concept, om_intent, om_query,
                         om_summary],
            )
            om_column.change(
                fn=_sel_on_column, inputs=[om_column],
                outputs=[om_concept, om_intent, om_query, om_summary],
            )
            om_concept.change(
                fn=_sel_on_concept, inputs=[om_concept],
                outputs=[om_intent, om_query, om_summary],
            )
            om_intent.change(
                fn=_sel_on_intent, inputs=[om_intent],
                outputs=[om_query, om_summary],
            )
            om_query.change(
                fn=_sel_on_query, inputs=[om_query],
                outputs=[om_summary],
            )
            om_query.change(
                fn=_om_render_chain, inputs=[om_query],
                outputs=_oo_outputs,
            )

            oo_btn.click(
                fn=render_ontop_ontology,
                inputs=[oo_picker],
                outputs=_oo_outputs,
            )
            oo_picker.change(
                fn=render_ontop_ontology,
                inputs=[oo_picker],
                outputs=_oo_outputs,
            )

            if _oo_mod_choices_fn is not None:
                # Category cascade: picking a showcase ontology narrows the
                # mapping dropdown and selects its first entry; the picker's
                # own .change then re-renders the panes below.
                def _oo_on_category(module):
                    choices = _oo_mod_choices_fn(_oo_entries, module)
                    value = choices[0][1] if choices else None
                    return gr.update(choices=choices, value=value)

                oo_cat.change(
                    fn=_oo_on_category,
                    inputs=[oo_cat],
                    outputs=[oo_picker],
                )

        with gr.Tab("🗳️ Term Review"):
            import sys as _tr_sys, pathlib as _tr_pl
            _tr_repo_scripts = str(_tr_pl.Path(__file__).parent.parent / "scripts")
            if _tr_repo_scripts not in _tr_sys.path:
                _tr_sys.path.insert(0, _tr_repo_scripts)
            try:
                import mrp_approval_committer as _tr_mac
                _tr_staging_root = _tr_mac.DEFAULT_STAGING_ROOT
                _tr_run_ids = sorted(
                    (d.name for d in _tr_staging_root.iterdir() if d.is_dir()),
                    reverse=True,
                ) if _tr_staging_root.is_dir() else []
            except Exception:
                _tr_mac = None
                _tr_staging_root = None
                _tr_run_ids = []

            _tr_commit_enabled = os.getenv("MRP_ENABLE_GRAPH_COMMIT", "").lower() == "true"
            _TR_DISPLAY_COLS = ["term", "definition", "anchored", "reviewer_decision"]

            gr.Markdown(
                "### 🗳️ MRP Research Term Review\n\n"
                "SMEs can approve or reject each proposed term here — no text editor or CLI needed. "
                "Select a staging run, edit the **reviewer_decision** column (`approved` / `rejected` / `proposed`), "
                "then **Save decisions** to persist your choices. When you're ready, **Commit approved** "
                "pushes the approved terms into the `mrp_research` graph.\n\n"
                + (
                    "⚠️ **Commit gate is OFF** — set `MRP_ENABLE_GRAPH_COMMIT=true` to enable live commits. "
                    "Save + dry-run preview are always available."
                    if not _tr_commit_enabled
                    else "✅ **Commit gate is ON** — the Commit button will write to ArangoDB."
                )
            )

            with gr.Row():
                tr_run_dd = gr.Dropdown(
                    choices=_tr_run_ids,
                    value=_tr_run_ids[0] if _tr_run_ids else None,
                    label="Staging run (most-recent first)",
                    interactive=True,
                    scale=3,
                )
                tr_load_btn = gr.Button("↺ Load terms", size="sm", scale=1)

            tr_status_md = gr.Markdown(
                "_Select a staging run and click **Load terms**._"
                if not _tr_run_ids else "_Click **Load terms** to begin._"
            )

            tr_grid = gr.Dataframe(
                headers=_TR_DISPLAY_COLS,
                datatype=["str", "str", "str", "str"],
                column_count=(4, "fixed"),
                row_count=(1, "dynamic"),
                label="Proposed terms — edit reviewer_decision then Save",
                interactive=True,
                wrap=True,
            )

            tr_summary_md = gr.Markdown()

            with gr.Row():
                tr_save_btn = gr.Button(
                    "💾 Save decisions", variant="primary", size="sm"
                )
                tr_commit_btn = gr.Button(
                    "🚀 Commit approved" if _tr_commit_enabled
                    else "🚀 Commit approved (gate off — dry-run only)",
                    variant="primary" if _tr_commit_enabled else "secondary",
                    size="sm",
                )

            tr_run_state = gr.State(value=None)

            def _tr_list_run_ids():
                """Return sorted run IDs (newest-first) from the staging root."""
                import sys as _s, pathlib as _p
                _rs = str(_p.Path(__file__).parent.parent / "scripts")
                if _rs not in _s.path:
                    _s.path.insert(0, _rs)
                try:
                    import mrp_approval_committer as _m
                    root = _m.DEFAULT_STAGING_ROOT
                    if not root.is_dir():
                        return []
                    return sorted((d.name for d in root.iterdir() if d.is_dir()), reverse=True)
                except Exception:
                    return []

            def _tr_load(run_id):
                """Load proposed_terms.csv for a run and return the display grid."""
                import sys as _s, pathlib as _p, csv as _csv, pandas as _pd
                _rs = str(_p.Path(__file__).parent.parent / "scripts")
                if _rs not in _s.path:
                    _s.path.insert(0, _rs)
                try:
                    import mrp_approval_committer as _m
                except ImportError as exc:
                    return None, f"⚠️ Could not import committer: {exc}", None

                root = _m.DEFAULT_STAGING_ROOT
                try:
                    run_dir = _m._resolve_run_dir(run_id or None, root)
                except FileNotFoundError as exc:
                    return None, f"⚠️ {exc}", None

                csv_path = run_dir / "proposed_terms.csv"
                if not csv_path.exists():
                    return None, f"⚠️ `proposed_terms.csv` not found in `{run_dir.name}`.", None

                rows = []
                with csv_path.open(encoding="utf-8", newline="") as fh:
                    for row in _csv.DictReader(fh):
                        rows.append(dict(row))

                if not rows:
                    return (
                        _pd.DataFrame(columns=["term", "definition", "anchored", "reviewer_decision"]),
                        f"_Run `{run_dir.name}` has no terms._",
                        str(run_dir),
                    )

                display = []
                for r in rows:
                    defn = (r.get("definition") or "").strip()
                    if len(defn) > 120:
                        defn = defn[:117] + "…"
                    display.append({
                        "term": r.get("term", ""),
                        "definition": defn,
                        "anchored": r.get("anchored", ""),
                        "reviewer_decision": (r.get("reviewer_decision") or "proposed").strip() or "proposed",
                    })

                pending = sum(1 for r in rows if (r.get("reviewer_decision") or "proposed").strip().lower() in ("", "proposed"))
                approved = sum(1 for r in rows if (r.get("reviewer_decision") or "").strip().lower() == "approved")
                rejected = sum(1 for r in rows if (r.get("reviewer_decision") or "").strip().lower() == "rejected")

                status = (
                    f"**Run:** `{run_dir.name}` · "
                    f"**{len(rows)}** terms · "
                    f"✅ {approved} approved · "
                    f"❌ {rejected} rejected · "
                    f"⏳ {pending} pending\n\n"
                    "_Edit the **reviewer_decision** column (approved / rejected / proposed), then click **Save decisions**._"
                )
                df = _pd.DataFrame(display, columns=["term", "definition", "anchored", "reviewer_decision"])
                return df, status, str(run_dir)

            def _tr_save(grid, run_dir_path):
                """Persist reviewer_decision values from the grid back to proposed_terms.csv."""
                import csv as _csv, pathlib as _p, io as _io, pandas as _pd
                if not run_dir_path:
                    return "⚠️ No run loaded — click **Load terms** first.", None
                run_dir = _p.Path(run_dir_path)
                csv_path = run_dir / "proposed_terms.csv"
                if not csv_path.exists():
                    return f"⚠️ `proposed_terms.csv` not found in `{run_dir.name}`.", None

                if grid is None:
                    return "⚠️ Grid is empty — nothing to save.", None

                if isinstance(grid, _pd.DataFrame):
                    grid_rows = grid.to_dict(orient="records")
                elif isinstance(grid, list):
                    cols = ["term", "definition", "anchored", "reviewer_decision"]
                    grid_rows = [
                        {c: (row[i] if isinstance(row, (list, tuple)) and i < len(row) else
                             row.get(c, "") if isinstance(row, dict) else "")
                         for i, c in enumerate(cols)}
                        for row in grid
                    ]
                else:
                    return "⚠️ Unexpected grid type — save aborted.", None

                decision_by_term = {
                    str(r.get("term", "")).strip(): str(r.get("reviewer_decision", "proposed")).strip()
                    for r in grid_rows
                    if r.get("term")
                }

                original_rows = []
                with csv_path.open(encoding="utf-8", newline="") as fh:
                    reader = _csv.DictReader(fh)
                    fieldnames = list(reader.fieldnames or [])
                    for row in reader:
                        original_rows.append(dict(row))

                if "reviewer_decision" not in fieldnames:
                    fieldnames.append("reviewer_decision")

                updated = 0
                for row in original_rows:
                    term = str(row.get("term", "")).strip()
                    if term in decision_by_term:
                        new_dec = decision_by_term[term].lower()
                        if new_dec not in ("approved", "rejected", "proposed"):
                            new_dec = "proposed"
                        if row.get("reviewer_decision", "").strip().lower() != new_dec:
                            row["reviewer_decision"] = new_dec
                            updated += 1
                        elif "reviewer_decision" not in row or not row["reviewer_decision"]:
                            row["reviewer_decision"] = new_dec

                buf = _io.StringIO()
                writer = _csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(original_rows)
                csv_path.write_text(buf.getvalue(), encoding="utf-8")

                approved = sum(1 for r in original_rows if (r.get("reviewer_decision") or "").strip().lower() == "approved")
                rejected = sum(1 for r in original_rows if (r.get("reviewer_decision") or "").strip().lower() == "rejected")
                pending = sum(1 for r in original_rows if (r.get("reviewer_decision") or "proposed").strip().lower() in ("", "proposed"))
                return (
                    f"💾 Saved `{csv_path.name}` ({len(original_rows)} rows, {updated} updated). "
                    f"✅ {approved} approved · ❌ {rejected} rejected · ⏳ {pending} pending"
                ), None

            def _tr_commit(run_dir_path):
                """Call the committer for the loaded run. Dry-run if gate is off."""
                import sys as _s, pathlib as _p
                _rs = str(_p.Path(__file__).parent.parent / "scripts")
                if _rs not in _s.path:
                    _s.path.insert(0, _rs)
                try:
                    import mrp_approval_committer as _m
                except ImportError as exc:
                    return f"⚠️ Could not import committer: {exc}"

                if not run_dir_path:
                    return "⚠️ No run loaded — click **Load terms** first, then save your decisions."

                commit_enabled = os.getenv("MRP_ENABLE_GRAPH_COMMIT", "").lower() == "true"
                run_dir = _p.Path(run_dir_path)
                try:
                    summary = _m.run(run_dir=run_dir, commit=commit_enabled)
                except (FileNotFoundError, ValueError) as exc:
                    return f"⚠️ {exc}"
                except Exception as exc:
                    return f"⚠️ Unexpected error: {type(exc).__name__}: {exc}"

                lines = [
                    f"**Run:** `{summary['run_id']}`",
                    f"✅ Approved: {summary['approved']} · ❌ Rejected: {summary['rejected']} · ⏳ Pending: {summary['pending_review']}",
                    f"Edges included: {summary['edges_included']} · Anchor nodes: {summary['anchor_nodes_included']}",
                ]
                if summary.get("dry_run"):
                    lines.append(
                        "ℹ️ **Dry run** — no data written. "
                        "Set `MRP_ENABLE_GRAPH_COMMIT=true` and re-click Commit to write to ArangoDB."
                    )
                elif summary.get("committed"):
                    commit_result = summary.get("commit_result", {})
                    lines.append(f"🚀 **Committed** — result: `{commit_result}`")
                else:
                    lines.append("ℹ️ No approved terms to commit.")

                return "\n\n".join(lines)

            tr_load_btn.click(
                fn=_tr_load,
                inputs=[tr_run_dd],
                outputs=[tr_grid, tr_status_md, tr_run_state],
            )
            tr_run_dd.change(
                fn=_tr_load,
                inputs=[tr_run_dd],
                outputs=[tr_grid, tr_status_md, tr_run_state],
            )
            tr_save_btn.click(
                fn=_tr_save,
                inputs=[tr_grid, tr_run_state],
                outputs=[tr_status_md, tr_summary_md],
            )
            tr_commit_btn.click(
                fn=_tr_commit,
                inputs=[tr_run_state],
                outputs=[tr_summary_md],
            )

            if _tr_run_ids:
                demo.load(
                    fn=lambda: _tr_load(_tr_run_ids[0]),
                    outputs=[tr_grid, tr_status_md, tr_run_state],
                )

        with gr.Tab("🔄 Graph Sync"):
            gr.Markdown("""
            ### ArangoDB Graph Sync
            
            Push the semantic layer from SQLite into ArangoDB as a named graph (`manufacturing_graph`).
            This keeps your cloud graph database in sync with local changes to intents, perspectives,
            concepts, elevation weights, and SME-approved bindings.
            
            **Graph structure (current bridge-row model):**

            Perspective is no longer a vertex collection. The retired
            `OPERATES_WITHIN` and `USES_DEFINITION` edge collections have
            been replaced by two composite-key bridge document collections
            that carry `perspective` as a property on each row.

            | Collection | Kind | From / Key | To | Purpose |
            |------------|------|------------|----|---------|
            | `Perspective_Intents` | bridge doc | `perspective__intent` | — | Replaces `OPERATES_WITHIN`; which lens an intent uses |
            | `Perspective_Concepts` | bridge doc | `perspective__concept` | — | Replaces `USES_DEFINITION`; which concepts a perspective defines |
            | `ELEVATES` | edge | Intent | Concept | Elevation weight (1.0 = primary, 0.0 = neutral) |
            | `BOUND_TO` | edge | Intent | Binding | Links intent to its approved SQL snippet |
            
            **How to use:**
            1. Click **Dry Run** to preview what will be synced (reads SQLite only, no ArangoDB connection)
            2. Click **Sync to ArangoDB** to push changes (creates new documents or updates existing ones)
            """)
            
            with gr.Row():
                with gr.Column():
                    sync_dry_run_btn = gr.Button("Dry Run (Preview)", variant="secondary")
                    sync_live_btn = gr.Button("Sync to ArangoDB", variant="primary")
                    sync_purge_stale = gr.Checkbox(
                        label="Purge stale tables (remove vertices/edges for tables no longer in SQLite)",
                        value=False,
                    )
                with gr.Column():
                    sync_status = gr.Textbox(label="Status", interactive=False, value="Ready — click Dry Run or Sync")

            sync_stale_panel = gr.Markdown(value="**Stale tables:** 0 stale tables removed")

            sync_report_output = gr.Code(label="Sync Report", language=None, lines=22)

            def _stale_panel_md(report, dry_run: bool) -> str:
                """Render the pruned-vertex/edge counts and stale table names (#149)."""
                if not report.stale_tables:
                    return "**Stale tables:** 0 stale tables removed"
                verb = "would be removed" if dry_run else "removed"
                total_pv = report.total_pruned_vertices
                total_pe = report.total_pruned_edges
                lines = [
                    f"**Stale tables — {len(report.stale_tables)} {verb}**",
                    "",
                    f"- Vertices pruned: **{total_pv}**",
                    f"- Edges pruned: **{total_pe}**",
                    "",
                    "Stale table names:",
                ]
                for name in report.stale_tables:
                    lines.append(f"- `{name}`")
                return "\n".join(lines)

            def run_graph_sync(dry_run: bool, purge_stale: bool):
                try:
                    from graph_sync import sync_graph
                    report = sync_graph(dry_run=dry_run, purge_stale=purge_stale)
                    if dry_run:
                        status = f"DRY RUN — {report.total_vertices} vertices, {report.total_edges} edges ready to sync"
                    elif report.success:
                        status = f"SUCCESS — {report.total_vertices} vertices, {report.total_edges} edges synced to ArangoDB"
                    else:
                        status = f"FAILED — see errors in report below"
                    _SYNC_LAST_STATUS[0] = status
                    return status, report.summary(), _stale_panel_md(report, dry_run)
                except Exception as e:
                    err = f"ERROR: {e}"
                    _SYNC_LAST_STATUS[0] = err
                    return err, str(e), "**Stale tables:** 0 stale tables removed"

            def _health_inline() -> str:
                """Append bridge health to the last sync status for inline display (#121)."""
                health = quick_bridge_health()
                return f"{_SYNC_LAST_STATUS[0]}  ·  {health}"

            sync_dry_run_btn.click(
                fn=lambda purge: run_graph_sync(dry_run=True, purge_stale=purge),
                inputs=[sync_purge_stale],
                outputs=[sync_status, sync_report_output, sync_stale_panel]
            )
            sync_live_btn.click(
                fn=lambda purge: run_graph_sync(dry_run=False, purge_stale=purge),
                inputs=[sync_purge_stale],
                outputs=[sync_status, sync_report_output, sync_stale_panel]
            ).then(
                fn=_health_inline,
                outputs=sync_status,
            )

        with gr.Tab("🩺 Bridge Health"):
            gr.Markdown("""
            ### Bridge Collection Health Check

            Compares ArangoDB bridge document counts against the SQLite source-of-truth tables.
            A mismatch means `graph_sync.py` left ArangoDB partially synced — run **Sync to ArangoDB**
            on the Graph Sync tab to fix it.

            | ArangoDB Collection | SQLite Table |
            |---------------------|--------------|
            | `Perspective_Intents` | `schema_intent_perspectives` |
            | `Perspective_Concepts` | `schema_perspective_concepts` |
            | `tables` | `schema_nodes` |
            """)

            health_check_btn = gr.Button("Check Now", variant="primary")

            with gr.Row():
                health_status = gr.Textbox(
                    label="Overall Status",
                    interactive=False,
                    value="Not checked yet — click Check Now",
                )
                health_timestamp = gr.Textbox(
                    label="Last Checked",
                    interactive=False,
                    value="—",
                )

            health_detail = gr.Code(label="Count Comparison", language=None, lines=14)

            gr.Markdown("---")
            gr.Markdown("### Semantic Triple Coverage (Sweep 1)")
            gr.Markdown(
                "Lists concepts that appear in ELEVATES edges but have no matching APPROVED SQL binding "
                "in the reviewer manifest — the same gaps reported by `verify_metadata_meaning.py` Sweep 1."
            )

            with gr.Row():
                coverage_badge = gr.Textbox(
                    label="Coverage Status",
                    interactive=False,
                    value="Not checked yet — click Check Now",
                )

            coverage_detail = gr.Code(
                label="Concepts Without an Approved SQL Query",
                language=None,
                lines=12,
            )

            def _format_coverage(result: dict) -> str:
                """Render the coverage gap dict as a readable plain-text report."""
                status = result.get("status", "error")
                msg = result.get("message", "")
                gap_concepts = result.get("gap_concepts", [])
                pass_count = result.get("pass_count", 0)
                skip_count = result.get("skip_count", 0)

                if status in ("skip", "error"):
                    return msg

                lines = [msg, ""]
                if not gap_concepts:
                    lines.append("No coverage gaps — every ELEVATES concept has an APPROVED SQL binding.")
                    return "\n".join(lines)

                col_w = [30, 30, 36]
                header = (
                    f"{'Intent (Subject)':<{col_w[0]}} "
                    f"{'Concept (Object)':<{col_w[1]}} "
                    f"{'Concept Anchor':<{col_w[2]}}"
                )
                lines.append(header)
                lines.append("-" * (sum(col_w) + 2))
                for gap in gap_concepts:
                    lines.append(
                        f"{gap['intent_name'][:col_w[0]]:<{col_w[0]}} "
                        f"{gap['concept_name'][:col_w[1]]:<{col_w[1]}} "
                        f"{gap['concept_anchor'][:col_w[2]]:<{col_w[2]}}"
                    )
                lines.append("-" * (sum(col_w) + 2))
                lines.append(
                    f"Triples with gaps: {len(gap_concepts)}  |  "
                    f"Triples OK: {pass_count}"
                    + (f"  |  Skipped (dangling): {skip_count}" if skip_count else "")
                )
                lines.append("")
                lines.append(
                    "Add an APPROVED SQL query for each concept above to the reviewer manifest, "
                    "then re-run graph_sync.py."
                )
                return "\n".join(lines)

            def run_bridge_health_check():
                bridge_result = _bridge_health_check_impl(
                    SQLITE_DB_PATH,
                    {**BRIDGE_HEALTH_MAP, **SCHEMA_NODES_HEALTH_MAP},
                )
                coverage_result = _get_sweep1_coverage_gaps(MANIFEST_PATH)

                status = coverage_result.get("status", "error")
                gap_concepts = coverage_result.get("gap_concepts", [])
                if status == "ok":
                    badge = f"✅  All triples covered — {coverage_result.get('pass_count', 0)} approved binding(s)"
                elif status == "gaps":
                    badge = f"⚠️  {len(gap_concepts)} concept(s) without an approved SQL query"
                elif status == "skip":
                    badge = "—  Skipped (ArangoDB not configured)"
                else:
                    badge = f"❌  Error — {coverage_result.get('message', '')}"

                coverage_text = _format_coverage(coverage_result)
                return (*bridge_result, badge, coverage_text)

            health_check_btn.click(
                fn=run_bridge_health_check,
                outputs=[health_status, health_timestamp, health_detail, coverage_badge, coverage_detail],
            )

        with gr.Tab("🎨 Query Palette"):
            gr.Markdown("### SQLMesh Query Palette\nRun SQL against the SQLMesh virtual layer. Queries resolve through masked/hashed physical tables automatically.")

            SQLMESH_PROJECT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Utilities', 'SQLMesh'))

            def _sqlmesh_ctx():
                from sqlmesh import Context as SMContext
                orig_cwd = os.getcwd()
                try:
                    os.chdir(SQLMESH_PROJECT)
                    return SMContext(paths=SQLMESH_PROJECT)
                finally:
                    os.chdir(orig_cwd)

            def get_sqlmesh_models():
                try:
                    ctx = _sqlmesh_ctx()
                    clean = []
                    for m in ctx.models:
                        parts = m.replace('"', '').split('.')
                        if len(parts) == 3:
                            clean.append(f"{parts[1]}.{parts[2]}")
                        else:
                            clean.append('.'.join(parts))
                    return sorted(set(clean))
                except Exception:
                    return []

            palette_models = get_sqlmesh_models()
            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("**Virtual Layer Explorer**")
                    model_dropdown = gr.Dropdown(
                        choices=palette_models,
                        label="Quick Select Model",
                        interactive=True,
                    )
                    generate_btn = gr.Button("Generate SELECT *")
                with gr.Column(scale=3):
                    palette_query = gr.Textbox(
                        label="SQL Query",
                        value="SELECT * FROM staging.stg_suppliers LIMIT 10",
                        lines=6,
                        interactive=True,
                    )
                    run_palette_btn = gr.Button("Run Query", variant="primary")

            palette_status = gr.Markdown("")
            palette_results = gr.Dataframe(label="Query Results", wrap=True)
            palette_dim_check = gr.Markdown("")

            def gen_select(model_name):
                if not model_name:
                    return "SELECT * FROM staging.stg_suppliers LIMIT 10"
                return f"SELECT * FROM {model_name} LIMIT 100"

            def run_palette_query(query):
                if not query or not query.strip():
                    return "Enter a query above.", None, ""
                orig_cwd = os.getcwd()
                try:
                    os.chdir(SQLMESH_PROJECT)
                    ctx = _sqlmesh_ctx()
                    df = ctx.fetchdf(query)
                    dim_msg = ""
                    if 'vendor_id' in df.columns:
                        unique_vendors = df['vendor_id'].unique()
                        dim_msg = f"**Dimensionality Check:** Detected `vendor_id` — {len(unique_vendors)} unique masked vendors: {', '.join(map(str, unique_vendors[:20]))}"
                    return f"Returned **{len(df)}** rows, **{len(df.columns)}** columns.", df, dim_msg
                except Exception as e:
                    return f"**Error:** {e}", None, ""
                finally:
                    os.chdir(orig_cwd)

            generate_btn.click(fn=gen_select, inputs=[model_dropdown], outputs=[palette_query])
            run_palette_btn.click(fn=run_palette_query, inputs=[palette_query], outputs=[palette_status, palette_results, palette_dim_check])

        with gr.Tab("🔌 MCP Endpoints"):
            gr.Markdown("""
            ### Model Context Protocol API
            
            These endpoints enable AI agent integration:
            
            | Endpoint | MCP Component | Description |
            |----------|---------------|-------------|
            | `GET /mcp/discover` | Discovery | Lists all available tools |
            | `GET /mcp/tools/get_schema` | Resource | Sample schema definition |
            | `GET /mcp/tools/get_all_ddl` | Resource | Full database DDL |
            | `GET /mcp/tools/get_saved_categories` | Resource | Query category index |
            | `GET /mcp/tools/get_saved_queries?category_id=X` | Resource | Ground truth SQL |
            | `POST /mcp/tools/generate_sql` | Tool | NL to SQL conversion |
            | `GET /mcp/tools/get_table_ddl?table_name=X` | Tool | Single table DDL |
            
            ### Usage with VS Code + Copilot
            
            1. Go to **Copilot Context** tab
            2. Click the **Copy to Copilot** button to build context
            3. Paste context into Copilot Chat
            4. Ask follow-up questions about your manufacturing data
            """)

        with gr.Tab("📋 Field Descriptions"):
            gr.Markdown(
                "### Entity Field Descriptions — Data Dictionary Authoring\n"
                "Source: **SQLite** (`api_field_descriptions` + `dab_field_definitions`) "
                "overlaid on the structural schema. This tab is the local stand-in for the "
                "company DAB: author a plain-language meaning per column, certify it, then "
                "**Publish to DAB** to flow certified definitions into `dab_config.json`.\n\n"
                "Drafting is **on-demand, one column at a time**: _Generate Draft_ is "
                "deterministic (no API cost); _Generate AI Draft_ uses OpenAI only when you "
                "click it."
            )

            fd_overall_md = gr.Markdown(value="_Loading overall coverage…_")

            with gr.Row():
                fd_entity_dd = gr.Dropdown(
                    label="Entity (table)",
                    choices=[],
                    value=None,
                    interactive=True,
                    scale=2,
                )
                fd_refresh_btn = gr.Button("↺ Refresh", scale=1, size="sm")

            fd_entity_info = gr.Markdown(value="_Select an entity above._")

            fd_fields_table = gr.Dataframe(
                headers=["Column", "Type", "PK", "Description", "Display Name", "Certified"],
                datatype=["str", "str", "str", "str", "str", "str"],
                label="Field Descriptions (unified schema)",
                interactive=False,
                wrap=True,
            )

            gr.Markdown("---\n#### ✍️ Author / Certify a Field")

            with gr.Row():
                fd_col_dd = gr.Dropdown(
                    label="Column", choices=[], value=None, interactive=True, scale=2,
                )
                fd_load_field_btn = gr.Button("Load", scale=1, size="sm")

            with gr.Row():
                fd_display_tb = gr.Textbox(label="Display Name", scale=1)
                fd_example_tb = gr.Textbox(label="Example Value", scale=1)
            fd_desc_tb = gr.Textbox(
                label="Description (SME meaning — overrides the abstract concept name)",
                lines=3,
            )

            with gr.Row():
                fd_gen_btn = gr.Button("✨ Generate Draft", size="sm")
                fd_gen_ai_btn = gr.Button("🤖 Generate AI Draft", size="sm")
                fd_save_btn = gr.Button("💾 Save Description", variant="primary", size="sm")

            gr.Markdown("**DAB certification** — the SME-approved definition that publishes downstream.")
            fd_dab_def_tb = gr.Textbox(label="DAB Definition", lines=2)
            fd_certified_chk = gr.Checkbox(label="Certified", value=False)
            with gr.Row():
                fd_certify_btn = gr.Button("✅ Certify to DAB", variant="primary", size="sm")
                fd_publish_btn = gr.Button("📤 Publish certified → dab_config.json", size="sm")

            fd_status_md = gr.Markdown(value="")

            def _fd_load_entities():
                try:
                    schema = get_unified_schema()
                    names = sorted(schema.keys())
                    first = names[0] if names else None
                    return gr.Dropdown(choices=names, value=first)
                except Exception:
                    return gr.Dropdown(choices=[], value=None)

            def _fd_overall():
                """Global described/certified coverage across every table."""
                try:
                    cov = compute_field_coverage(get_unified_schema())
                    denom = cov["columns"] or 1
                    pct_d = round(100 * cov["described"] / denom)
                    pct_c = round(100 * cov["certified"] / denom)
                    return (
                        f"**Overall coverage** — {cov['tables']} tables, "
                        f"{cov['columns']} columns · "
                        f"{cov['described']} described ({pct_d}%) · "
                        f"{cov['certified']} certified ({pct_c}%)"
                    )
                except Exception as exc:
                    return f"_Overall coverage unavailable: {exc}_"

            def _fd_show_entity(entity_name):
                if not entity_name:
                    return "_Select an entity above._", [], gr.Dropdown(choices=[], value=None), _fd_overall()
                try:
                    schema = get_unified_schema()
                    cols = schema.get(entity_name)
                    if not cols:
                        return (
                            f"_Table `{entity_name}` not found in structural schema._",
                            [], gr.Dropdown(choices=[], value=None), _fd_overall(),
                        )
                    col_count = len(cols)
                    desc_count = sum(1 for c in cols.values() if c.get("description"))
                    certified_count = sum(1 for c in cols.values() if c.get("certified"))
                    info_md = (
                        f"**Table:** `{entity_name}` — "
                        f"{col_count} column(s), "
                        f"{desc_count} described, "
                        f"{certified_count} certified"
                    )
                    rows = []
                    for col_name, meta in cols.items():
                        rows.append([
                            col_name,
                            meta.get("type", "—"),
                            "✓" if meta.get("pk") else "",
                            meta.get("description") or "—",
                            meta.get("display_name") or "—",
                            "✓" if meta.get("certified") else "",
                        ])
                    col_names = list(cols.keys())
                    first_col = col_names[0] if col_names else None
                    return info_md, rows, gr.Dropdown(choices=col_names, value=first_col), _fd_overall()
                except Exception as exc:
                    return f"_Error loading schema: {exc}_", [], gr.Dropdown(choices=[], value=None), _fd_overall()

            def _fd_load_field(entity_name, column_name):
                """Populate the editor from any saved description / DAB definition."""
                if not entity_name or not column_name:
                    return "", "", "", "", False, "_Pick an entity and column, then Load._"
                fdr = get_field_description_record(entity_name, column_name) or {}
                dab = get_dab_field_definition_record(entity_name, column_name) or {}
                status = (
                    f"Loaded `{entity_name}.{column_name}`."
                    if (fdr or dab) else
                    f"`{entity_name}.{column_name}` has no saved description yet — "
                    f"generate a draft below."
                )
                return (
                    fdr.get("display_name") or "",
                    fdr.get("example_value") or "",
                    fdr.get("description") or "",
                    dab.get("field_definition") or "",
                    bool(dab.get("certified")),
                    status,
                )

            def _fd_generate(entity_name, column_name, use_ai):
                if not entity_name or not column_name:
                    return "", "", "", "_Pick an entity and column first._"
                try:
                    d = draft_field_description(entity_name, column_name, use_ai=use_ai)
                    src = d.get("_source", "deterministic")
                    note = d.get("_note", "")
                    label = "🤖 AI draft" if src == "ai" else "✨ Deterministic draft"
                    status = f"{label} generated for `{entity_name}.{column_name}`. _Review and Save._"
                    if note:
                        status += f"\n\n_{note}_"
                    return (
                        d.get("display_name") or "",
                        d.get("example_value") or "",
                        d.get("description") or "",
                        status,
                    )
                except Exception as exc:
                    return "", "", "", f"_Draft failed: {exc}_"

            def _fd_save(entity_name, column_name, display, example, desc):
                if not entity_name or not column_name:
                    return "_Pick an entity and column first._", "_Select an entity above._", [], _fd_overall()
                res = save_field_description(
                    entity_name, column_name,
                    display_name=display or None,
                    description=desc or None,
                    example_value=example or None,
                )
                info, rows, _, overall = _fd_show_entity(entity_name)
                if not res.get("ok"):
                    return f"_Save failed: {res.get('error')}_", info, rows, overall
                return f"💾 Saved description for `{entity_name}.{column_name}`.", info, rows, overall

            def _fd_certify(entity_name, column_name, dab_def, certified):
                if not entity_name or not column_name:
                    return "_Pick an entity and column first._", "_Select an entity above._", [], _fd_overall()
                res = save_dab_field_definition(
                    entity_name, column_name,
                    field_definition=dab_def or None,
                    certified=bool(certified),
                )
                info, rows, _, overall = _fd_show_entity(entity_name)
                if not res.get("ok"):
                    return f"_Certify failed: {res.get('error')}_", info, rows, overall
                state = "certified" if certified else "saved (uncertified)"
                return (
                    f"✅ DAB definition {state} for `{entity_name}.{column_name}`. "
                    f"Use **Publish to DAB** to write certified rows to `dab_config.json`.",
                    info, rows, overall,
                )

            def _fd_publish():
                import io, contextlib, sys
                scripts_dir = os.path.join(os.path.dirname(__file__), "scripts")
                if scripts_dir not in sys.path:
                    sys.path.insert(0, scripts_dir)
                try:
                    import sync_db_to_dab_config as _sync
                    _sync.SQLITE_DB_PATH = SQLITE_DB_PATH
                    _sync.DAB_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "dab_config.json")
                    _sync.SQL_MCP_SOURCE_DATABASE = SQL_MCP_SOURCE_DATABASE
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        _sync.sync(dry_run=False)
                    out = buf.getvalue().strip() or "(no output)"
                    return f"📤 **Published certified definitions to `dab_config.json`.**\n\n```\n{out}\n```", _fd_overall()
                except SystemExit as exc:
                    return f"_Publish stopped: {exc}_", _fd_overall()
                except Exception as exc:
                    return f"_Publish failed: {type(exc).__name__}: {exc}_", _fd_overall()

            fd_refresh_btn.click(fn=_fd_load_entities, outputs=fd_entity_dd)
            fd_entity_dd.change(
                fn=_fd_show_entity, inputs=fd_entity_dd,
                outputs=[fd_entity_info, fd_fields_table, fd_col_dd, fd_overall_md],
            )
            fd_load_field_btn.click(
                fn=_fd_load_field, inputs=[fd_entity_dd, fd_col_dd],
                outputs=[fd_display_tb, fd_example_tb, fd_desc_tb,
                         fd_dab_def_tb, fd_certified_chk, fd_status_md],
            )
            fd_gen_btn.click(
                fn=lambda e, c: _fd_generate(e, c, False),
                inputs=[fd_entity_dd, fd_col_dd],
                outputs=[fd_display_tb, fd_example_tb, fd_desc_tb, fd_status_md],
            )
            fd_gen_ai_btn.click(
                fn=lambda e, c: _fd_generate(e, c, True),
                inputs=[fd_entity_dd, fd_col_dd],
                outputs=[fd_display_tb, fd_example_tb, fd_desc_tb, fd_status_md],
            )
            fd_save_btn.click(
                fn=_fd_save,
                inputs=[fd_entity_dd, fd_col_dd, fd_display_tb, fd_example_tb, fd_desc_tb],
                outputs=[fd_status_md, fd_entity_info, fd_fields_table, fd_overall_md],
            )
            fd_certify_btn.click(
                fn=_fd_certify,
                inputs=[fd_entity_dd, fd_col_dd, fd_dab_def_tb, fd_certified_chk],
                outputs=[fd_status_md, fd_entity_info, fd_fields_table, fd_overall_md],
            )
            fd_publish_btn.click(fn=_fd_publish, outputs=[fd_status_md, fd_overall_md])
            demo.load(fn=_fd_load_entities, outputs=fd_entity_dd)
            demo.load(fn=_fd_overall, outputs=fd_overall_md)

        with gr.Tab("🔒 Data Masking"):
            gr.Markdown(
                "### Column Masking Policies — Data Masking Authoring\n"
                "Source: **SQLite** (`column_masking_policies`) overlaid on the "
                "structural schema. This tab is the local stand-in for the "
                "company DAB's masking layer: pick a masking **strategy** per "
                "column, certify it, then **Publish to DAB** to flow certified "
                "policies into `dab_config.json` (each field's `masking` "
                "attribute).\n\n"
                "Strategies: **none** (no masking) · **hash** (irreversible, "
                "stays joinable) · **partial** (keeps part of the value) · "
                "**redact** (fully hidden). _Suggest strategy_ is deterministic "
                "(name-based heuristic, no API cost) — there is no AI on this tab."
            )

            mk_overall_md = gr.Markdown(value="_Loading overall coverage…_")

            with gr.Row():
                mk_entity_dd = gr.Dropdown(
                    label="Entity (table)",
                    choices=[],
                    value=None,
                    interactive=True,
                    scale=2,
                )
                mk_refresh_btn = gr.Button("↺ Refresh", scale=1, size="sm")

            mk_entity_info = gr.Markdown(value="_Select an entity above._")

            mk_fields_table = gr.Dataframe(
                headers=["Column", "Type", "PK", "Masking Strategy", "Rationale", "Certified"],
                datatype=["str", "str", "str", "str", "str", "str"],
                label="Masking Policies (unified schema)",
                interactive=False,
                wrap=True,
            )

            gr.Markdown("---\n#### 🔒 Author / Certify a Masking Policy")

            with gr.Row():
                mk_col_dd = gr.Dropdown(
                    label="Column", choices=[], value=None, interactive=True, scale=2,
                )
                mk_load_field_btn = gr.Button("Load", scale=1, size="sm")

            with gr.Row():
                mk_strategy_dd = gr.Dropdown(
                    label="Masking Strategy",
                    choices=list(MASKING_STRATEGIES),
                    value="none",
                    interactive=True,
                    scale=1,
                )
            mk_rationale_tb = gr.Textbox(
                label="Rationale (why this column is masked this way)",
                lines=3,
            )

            with gr.Row():
                mk_suggest_btn = gr.Button("✨ Suggest Strategy", size="sm")
                mk_save_btn = gr.Button("💾 Save Policy", variant="primary", size="sm")

            gr.Markdown("**DAB certification** — the SME-approved masking policy that publishes downstream.")
            mk_certified_chk = gr.Checkbox(label="Certified", value=False)
            with gr.Row():
                mk_certify_btn = gr.Button("✅ Certify to DAB", variant="primary", size="sm")
                mk_publish_btn = gr.Button("📤 Publish certified → dab_config.json", size="sm")

            mk_status_md = gr.Markdown(value="")

            def _mk_load_entities():
                try:
                    schema = get_unified_schema()
                    names = sorted(schema.keys())
                    first = names[0] if names else None
                    return gr.Dropdown(choices=names, value=first)
                except Exception:
                    return gr.Dropdown(choices=[], value=None)

            def _mk_overall():
                """Global policied/certified masking coverage across every table."""
                try:
                    cov = compute_masking_coverage(get_unified_schema())
                    denom = cov["columns"] or 1
                    pct_p = round(100 * cov["policied"] / denom)
                    pct_c = round(100 * cov["certified"] / denom)
                    return (
                        f"**Overall masking coverage** — {cov['tables']} tables, "
                        f"{cov['columns']} columns · "
                        f"{cov['policied']} policied ({pct_p}%) · "
                        f"{cov['certified']} certified ({pct_c}%)"
                    )
                except Exception as exc:
                    return f"_Overall masking coverage unavailable: {exc}_"

            def _mk_show_entity(entity_name):
                if not entity_name:
                    return "_Select an entity above._", [], gr.Dropdown(choices=[], value=None), _mk_overall()
                try:
                    schema = get_unified_schema()
                    cols = schema.get(entity_name)
                    if not cols:
                        return (
                            f"_Table `{entity_name}` not found in structural schema._",
                            [], gr.Dropdown(choices=[], value=None), _mk_overall(),
                        )
                    col_count = len(cols)
                    policy_count = sum(1 for c in cols.values() if c.get("masking_strategy") is not None)
                    certified_count = sum(1 for c in cols.values() if c.get("masking_certified"))
                    info_md = (
                        f"**Table:** `{entity_name}` — "
                        f"{col_count} column(s), "
                        f"{policy_count} policied, "
                        f"{certified_count} certified"
                    )
                    rows = []
                    for col_name, meta in cols.items():
                        rows.append([
                            col_name,
                            meta.get("type", "—"),
                            "✓" if meta.get("pk") else "",
                            meta.get("masking_strategy") or "—",
                            meta.get("masking_rationale") or "—",
                            "✓" if meta.get("masking_certified") else "",
                        ])
                    col_names = list(cols.keys())
                    first_col = col_names[0] if col_names else None
                    return info_md, rows, gr.Dropdown(choices=col_names, value=first_col), _mk_overall()
                except Exception as exc:
                    return f"_Error loading schema: {exc}_", [], gr.Dropdown(choices=[], value=None), _mk_overall()

            def _mk_load_field(entity_name, column_name):
                """Populate the editor from any saved masking policy."""
                if not entity_name or not column_name:
                    return "none", "", False, "_Pick an entity and column, then Load._"
                rec = get_column_masking_record(entity_name, column_name) or {}
                status = (
                    f"Loaded `{entity_name}.{column_name}`."
                    if rec else
                    f"`{entity_name}.{column_name}` has no saved policy yet — "
                    f"suggest a strategy below."
                )
                return (
                    rec.get("masking_strategy") or "none",
                    rec.get("rationale") or "",
                    bool(rec.get("certified")),
                    status,
                )

            def _mk_suggest(entity_name, column_name):
                if not entity_name or not column_name:
                    return "none", "", "_Pick an entity and column first._"
                try:
                    s = suggest_masking_strategy(entity_name, column_name)
                    status = (
                        f"✨ Deterministic suggestion for `{entity_name}.{column_name}`: "
                        f"**{s.get('masking_strategy')}**. _Review and Save._"
                    )
                    return (
                        s.get("masking_strategy") or "none",
                        s.get("rationale") or "",
                        status,
                    )
                except Exception as exc:
                    return "none", "", f"_Suggestion failed: {exc}_"

            def _mk_save(entity_name, column_name, strategy, rationale):
                if not entity_name or not column_name:
                    return "_Pick an entity and column first._", "_Select an entity above._", [], _mk_overall()
                res = save_column_masking_policy(
                    entity_name, column_name,
                    masking_strategy=strategy or "none",
                    rationale=rationale or None,
                )
                info, rows, _, overall = _mk_show_entity(entity_name)
                if not res.get("ok"):
                    return f"_Save failed: {res.get('error')}_", info, rows, overall
                return f"💾 Saved masking policy for `{entity_name}.{column_name}`.", info, rows, overall

            def _mk_certify(entity_name, column_name, strategy, rationale, certified):
                if not entity_name or not column_name:
                    return "_Pick an entity and column first._", "_Select an entity above._", [], _mk_overall()
                res = certify_column_masking_policy(
                    entity_name, column_name,
                    masking_strategy=strategy or "none",
                    rationale=rationale or None,
                    certified=bool(certified),
                )
                info, rows, _, overall = _mk_show_entity(entity_name)
                if not res.get("ok"):
                    return f"_Certify failed: {res.get('error')}_", info, rows, overall
                state = "certified" if certified else "saved (uncertified)"
                return (
                    f"✅ Masking policy {state} for `{entity_name}.{column_name}`. "
                    f"Use **Publish to DAB** to write certified rows to `dab_config.json`.",
                    info, rows, overall,
                )

            def _mk_publish():
                import io, contextlib, sys
                scripts_dir = os.path.join(os.path.dirname(__file__), "scripts")
                if scripts_dir not in sys.path:
                    sys.path.insert(0, scripts_dir)
                try:
                    import sync_masking_to_dab_config as _sync
                    _sync.SQLITE_DB_PATH = SQLITE_DB_PATH
                    _sync.DAB_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "dab_config.json")
                    _sync.SQL_MCP_SOURCE_DATABASE = SQL_MCP_SOURCE_DATABASE
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        _sync.sync(dry_run=False)
                    out = buf.getvalue().strip() or "(no output)"
                    return f"📤 **Published certified masking policies to `dab_config.json`.**\n\n```\n{out}\n```", _mk_overall()
                except SystemExit as exc:
                    return f"_Publish stopped: {exc}_", _mk_overall()
                except Exception as exc:
                    return f"_Publish failed: {type(exc).__name__}: {exc}_", _mk_overall()

            mk_refresh_btn.click(fn=_mk_load_entities, outputs=mk_entity_dd)
            mk_entity_dd.change(
                fn=_mk_show_entity, inputs=mk_entity_dd,
                outputs=[mk_entity_info, mk_fields_table, mk_col_dd, mk_overall_md],
            )
            mk_load_field_btn.click(
                fn=_mk_load_field, inputs=[mk_entity_dd, mk_col_dd],
                outputs=[mk_strategy_dd, mk_rationale_tb, mk_certified_chk, mk_status_md],
            )
            mk_suggest_btn.click(
                fn=_mk_suggest,
                inputs=[mk_entity_dd, mk_col_dd],
                outputs=[mk_strategy_dd, mk_rationale_tb, mk_status_md],
            )
            mk_save_btn.click(
                fn=_mk_save,
                inputs=[mk_entity_dd, mk_col_dd, mk_strategy_dd, mk_rationale_tb],
                outputs=[mk_status_md, mk_entity_info, mk_fields_table, mk_overall_md],
            )
            mk_certify_btn.click(
                fn=_mk_certify,
                inputs=[mk_entity_dd, mk_col_dd, mk_strategy_dd, mk_rationale_tb, mk_certified_chk],
                outputs=[mk_status_md, mk_entity_info, mk_fields_table, mk_overall_md],
            )
            mk_publish_btn.click(fn=_mk_publish, outputs=[mk_status_md, mk_overall_md])
            demo.load(fn=_mk_load_entities, outputs=mk_entity_dd)
            demo.load(fn=_mk_overall, outputs=mk_overall_md)

        with gr.Tab("🧬 Masking Matrix"):
            gr.Markdown(
                "### Masking Matrix — the deterministic column-masking DAG\n"
                "Source of truth: **SQLite** (`masking_matrix`), mirrored to the "
                "SME-facing root CSV **`masking_matrix.csv`** (the copy SMEs edit and "
                "approve). Edit the grid and **Save** to write SQLite **and** the CSV "
                "in one step. Masking is deterministic — "
                "`SHA-256(value + GEMIN_SALT secret)` truncated to the column's "
                "`field_length`. There is no AI on this tab.\n\n"
                "Only rows with **status `active`** are imported into the receiving "
                "certificate; `static` / `complete` rows are certified/locked.\n\n"
                "This is separate from the **🔒 Data Masking** tab "
                "(`column_masking_policies`, the strategy/rationale surface) — the two "
                "live side by side."
            )

            mm_salt_md = gr.Markdown()
            mm_status_md = gr.Markdown()

            mm_grid = gr.Dataframe(
                headers=list(mmx.MATRIX_COLUMNS),
                datatype=[
                    "str", "str", "str", "str", "str", "str", "str",
                    "number", "number", "str", "str",
                ],
                column_count=(len(mmx.MATRIX_COLUMNS), "fixed"),
                row_count=(1, "dynamic"),
                label="masking_matrix (editable)",
                interactive=True,
                wrap=True,
            )
            with gr.Row():
                mm_reload_btn = gr.Button("↺ Reload from SQLite", size="sm")
                mm_save_btn = gr.Button(
                    "💾 Save to SQLite + CSV", variant="primary", size="sm"
                )

            gr.Markdown("#### 🧪 Preview a masked value")
            with gr.Row():
                mm_prev_dag = gr.Dropdown(
                    label="DAG row (dag_no)", choices=[], value=None, scale=1
                )
                mm_prev_val = gr.Textbox(label="Sample value", scale=2)
                mm_prev_btn = gr.Button("Mask", size="sm", scale=1)
            mm_prev_out = gr.Markdown()

            gr.Markdown(
                "---\n#### Masking types (reference) — `masking_type.csv`\n"
                "The closed lookup of masking types and their `masking_mode` numbers, "
                "used by the matrix above. Edit and save the same way."
            )
            mt_status_md = gr.Markdown()
            mt_grid = gr.Dataframe(
                headers=list(mtx.TYPE_COLUMNS),
                datatype=["str", "number", "str"],
                column_count=(len(mtx.TYPE_COLUMNS), "fixed"),
                row_count=(1, "dynamic"),
                label="masking_type (editable)",
                interactive=True,
                wrap=True,
            )
            with gr.Row():
                mt_reload_btn = gr.Button("↺ Reload types", size="sm")
                mt_save_btn = gr.Button(
                    "💾 Save types to SQLite + CSV", variant="primary", size="sm"
                )

            def _mm_df_to_dicts(grid, columns):
                """Coerce a Gradio Dataframe value (DataFrame or list) to row dicts."""
                out = []
                if grid is None:
                    return out
                try:
                    import pandas as pd
                    if isinstance(grid, pd.DataFrame):
                        g2 = grid.where(pd.notnull(grid), "")
                        for rec in g2.to_dict(orient="records"):
                            out.append({c: rec.get(c, "") for c in columns})
                        return out
                except Exception:
                    pass
                rows_iter = grid if isinstance(grid, list) else []
                for raw in rows_iter:
                    if isinstance(raw, dict):
                        out.append({c: raw.get(c, "") for c in columns})
                    else:
                        out.append(
                            {c: (raw[i] if i < len(raw) else "")
                             for i, c in enumerate(columns)}
                        )
                return out

            def _mm_rows_to_grid(rows, columns):
                """Build a list-of-lists for the grid, coercing None -> ''."""
                return [
                    [("" if r.get(c) is None else r.get(c)) for c in columns]
                    for r in rows
                ]

            def _mm_salt_status():
                # Report only whether the salt is configured — never its value.
                if os.environ.get(mmx.SALT_ENV_VAR):
                    return (
                        f"🔑 Masking salt **configured** (from the "
                        f"`{mmx.SALT_ENV_VAR}` secret). Masking is ready."
                    )
                return (
                    f"⚠️ Masking salt **not set** — set the `{mmx.SALT_ENV_VAR}` "
                    f"secret to enable masking. Editing and saving the matrix still "
                    f"works without it."
                )

            def _mm_load_grid():
                rows = mmx.read_matrix(SQLITE_DB_PATH)
                data = _mm_rows_to_grid(rows, mmx.MATRIX_COLUMNS)
                dags = [r["dag_no"] for r in rows]
                active = sum(1 for r in rows if r.get("status") == "active")
                msg = (
                    f"Loaded **{len(rows)}** row(s) from SQLite · **{active}** active "
                    f"(imported into the receiving certificate)."
                )
                return (
                    data,
                    gr.Dropdown(choices=dags, value=(dags[0] if dags else None)),
                    msg,
                )

            def _mm_save(grid):
                rows = _mm_df_to_dicts(grid, mmx.MATRIX_COLUMNS)
                res = mmx.replace_matrix(
                    rows, db_path=SQLITE_DB_PATH, csv_path=mmx.DEFAULT_CSV_PATH
                )
                if not res.get("ok"):
                    # Keep the SME's pending edits on screen; don't reload from SQLite.
                    return f"❌ Save failed: {res.get('error')}", grid, gr.update()
                data, dd, loaded_msg = _mm_load_grid()
                return (
                    f"💾 Saved **{res.get('saved')}** row(s) to SQLite and wrote "
                    f"**{res.get('csv_written')}** row(s) to `masking_matrix.csv`. "
                    f"{loaded_msg}",
                    data,
                    dd,
                )

            def _mm_preview(dag_no, value):
                if not dag_no:
                    return "_Pick a DAG row first._"
                rows = {r["dag_no"]: r for r in mmx.read_matrix(SQLITE_DB_PATH)}
                row = rows.get(dag_no)
                if not row:
                    return f"_Row `{dag_no}` not found — reload the grid._"
                try:
                    masked = mmx.mask_row_value(row, value)
                except Exception as exc:
                    return f"⚠️ {exc}"
                width = row.get("field_length") or 0
                width_lbl = f"{width}" if width else "full digest (unbounded)"
                return (
                    f"`{row['table_name']}.{row['column_name']}` "
                    f"(type `{row.get('masking_type') or '—'}`) → masked to width "
                    f"**{width_lbl}**:\n\n```\n{masked}\n```"
                )

            def _mt_load_grid():
                rows = mtx.read_types(SQLITE_DB_PATH)
                data = _mm_rows_to_grid(rows, mtx.TYPE_COLUMNS)
                return data, f"Loaded **{len(rows)}** masking type(s) from SQLite."

            def _mt_save(grid):
                rows = _mm_df_to_dicts(grid, mtx.TYPE_COLUMNS)
                res = mtx.replace_types(
                    rows, db_path=SQLITE_DB_PATH, csv_path=mtx.DEFAULT_CSV_PATH
                )
                if not res.get("ok"):
                    # Keep the SME's pending edits on screen; don't reload from SQLite.
                    return f"❌ Save failed: {res.get('error')}", grid
                data, loaded_msg = _mt_load_grid()
                return (
                    f"💾 Saved **{res.get('saved')}** type(s) to SQLite and wrote "
                    f"**{res.get('csv_written')}** row(s) to `masking_type.csv`. "
                    f"{loaded_msg}",
                    data,
                )

            mm_reload_btn.click(
                fn=_mm_load_grid, outputs=[mm_grid, mm_prev_dag, mm_status_md]
            )
            mm_save_btn.click(
                fn=_mm_save, inputs=mm_grid,
                outputs=[mm_status_md, mm_grid, mm_prev_dag],
            )
            mm_prev_btn.click(
                fn=_mm_preview, inputs=[mm_prev_dag, mm_prev_val], outputs=mm_prev_out
            )
            mt_reload_btn.click(fn=_mt_load_grid, outputs=[mt_grid, mt_status_md])
            mt_save_btn.click(
                fn=_mt_save, inputs=mt_grid, outputs=[mt_status_md, mt_grid]
            )

            demo.load(fn=_mm_salt_status, outputs=mm_salt_md)
            demo.load(fn=_mm_load_grid, outputs=[mm_grid, mm_prev_dag, mm_status_md])
            demo.load(fn=_mt_load_grid, outputs=[mt_grid, mt_status_md])

        def _load_erp_note():
            cfg = _get_erp_config()
            note = (
                f"**Active ERP:** `{cfg['erp_instance_name']}` "
                f"*(source: {cfg['erp_instance_name_source']})*"
            )
            if cfg["erp_instance_name_source"] == "default":
                note += (
                    "\n\n> ⚠️ Using the default ERP name. "
                    "Set `ERP_INSTANCE_NAME` to configure your system."
                )
            return note

        demo.load(fn=_load_erp_header, outputs=schema_header_md)
        demo.load(fn=_load_erp_note, outputs=copilot_erp_md)
        demo.load(fn=_load_erp_note, outputs=aaq_erp_md)
    
    return demo


@app.post("/mcp/tools/mrp_term_review_commit")
async def mrp_term_review_commit(run_id: Optional[str] = None, commit: bool = False):
    """Load a staging run and optionally commit approved MRP research terms to mrp_research graph.

    Parameters
    ----------
    run_id : str, optional
        Staging run folder name (e.g. ``20260627T185702Z``).  When omitted the
        most-recent run folder is used.
    commit : bool
        ``True`` to actually write to ArangoDB.  Also requires the environment
        variable ``MRP_ENABLE_GRAPH_COMMIT=true``; otherwise the call is always
        a dry-run regardless of this flag.

    Returns the committer summary dict (decision counts, committed flag, etc.).
    """
    import sys as _sys
    import pathlib as _pl
    _repo_scripts = str(_pl.Path(__file__).parent.parent / "scripts")
    if _repo_scripts not in _sys.path:
        _sys.path.insert(0, _repo_scripts)
    try:
        import mrp_approval_committer as _mac
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"Could not import mrp_approval_committer: {exc}")

    staging_root = _mac.DEFAULT_STAGING_ROOT
    try:
        run_dir = _mac._resolve_run_dir(run_id, staging_root)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        summary = _mac.run(run_dir=run_dir, commit=commit)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Committer error: {exc}")

    return summary


get_db_engine()
initial_tables = get_all_tables()
print(f"SQLite database initialized with {len(initial_tables)} tables")

_dr_static = os.path.join(os.path.dirname(__file__), "static", "define-relationship")

def _dr_index_html() -> str | None:
    """Return the content of the Define Relationship SPA entry HTML.

    Checks, in order:
      1. index.html          (manually maintained / legacy)
      2. index-define-relationship.html  (Vite build output when using named input)
    Returns None when neither file exists.
    """
    for name in ("index.html", "index-define-relationship.html"):
        path = os.path.join(_dr_static, name)
        if os.path.isfile(path):
            with open(path, "r") as f:
                return f.read()
    return None


@app.get("/define-relationship/", response_class=HTMLResponse, include_in_schema=False)
async def serve_define_relationship_root():
    """Serve the Define Relationship SPA index page."""
    html = _dr_index_html()
    if html is not None:
        return html
    raise HTTPException(status_code=404, detail="Define Relationship build not found")

@app.get("/define-relationship/{path:path}", include_in_schema=False)
async def serve_define_relationship_asset(path: str):
    """Serve static assets for the Define Relationship SPA."""
    from fastapi.responses import FileResponse
    import mimetypes
    file_path = os.path.join(_dr_static, path)
    if os.path.isfile(file_path):
        mime, _ = mimetypes.guess_type(file_path)
        return FileResponse(file_path, media_type=mime or "application/octet-stream")
    html = _dr_index_html()
    if html is not None:
        return HTMLResponse(html)
    raise HTTPException(status_code=404, detail="Not found")

gradio_app = create_gradio_interface()
app = gr.mount_gradio_app(app, gradio_app, path="/gradio")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
