"""
seed_erp_synthetic.py — Synthetic aerospace ERP data seed script.

Populates the purchasing / WIP digital twin tables in manufacturing.db:
  part, suppliers (top-up), purchase_order, po_line, receiving,
  payables, certification, work_order, operation, material_issue,
  labor_ticket, shop_resource, service

Run from repo root:
    python hf-space-inventory-sqlgen/scripts/seed_erp_synthetic.py

Or with --clear to wipe and reseed all ERP tables:
    python hf-space-inventory-sqlgen/scripts/seed_erp_synthetic.py --clear

NOTE: this seeder emits plain multiples-of-10 sequences, forces every operation
to status='Q', never sets operation.close_date, emits older work_order.status
labels (Open/Released/Closed), and does NOT set operation.operation_type_id or seed
the operation-level `requirement` table. After a from-scratch reseed, run (all
idempotent, in this order):
  1. migrations/add_operation_type.py — (re)stamp operation rows with their
     operation_type (CNC, Paint, NDT, …); backfills only rows still NULL.
  2. migrations/regap_and_seed_requirements.py — renumber sequences into realistic
     gapped values (e.g. 20, 80, 220), keep labor_ticket aligned, and seed MATERIAL
     requirements tied to specific operations.
  3. migrations/relabel_work_order_status.py — map work_order.status onto the real
     planner vocabulary (unreleased / firmed / released / closed).
  4. migrations/backfill_operation_progress.py — derive realistic, sequence-ordered
     job progress (operation.status Q/S/C + close_date) from each work order's
     status, so progress is measured by status/close_date, not by sequence_no.
  5. migrations/backfill_operation_schedule.py — build a coherent routing schedule
     (operation.sched_start_date / sched_finish_date) where each step starts on/after
     the prior step's close, and derive the work-order window (work_order.sched_start_date
     / sched_finish_date / desired_rls_date) from it. Needs close_date from step 4.
  6. migrations/backfill_supplier_rating_and_wo_actuals.py — roll the recognized
     operation estimates up into work_order.act_lab_cost / act_bur_cost / act_ser_cost
     and score suppliers. Needs operation.status from step 4.
  7. migrations/backfill_operation_actuals.py — distribute the work-order cost
     rollups from step 6 back DOWN to the operations (operation.act_atl_lab_cost /
     act_atl_bur_cost / act_atl_ser_cost) so the operation actuals reconcile exactly
     to the work-order rollups. Needs the rollups from step 6.
  8. migrations/backfill_labor_chain.py — rebuild the labor_ticket detail BOTTOM-UP
     so all three layers tie out: one aggregate labor posting per progressed in-house
     step (labor anchors the unchanged work-order headline), burden re-derived
     rate-consistently as hours x bur_per_hr_run, and the work_order burden rollup
     recomputed from the operations. Must run LAST (needs the operation actuals from
     step 7); fails closed if any layer does not reconcile.
"""

import argparse
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# ── resolve DB path ──────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH   = REPO_ROOT / "hf-space-inventory-sqlgen" / "app_schema" / "manufacturing.db"

random.seed(42)

# ─────────────────────────────────────────────────────────────────────────────
# Reference data — aerospace flavour
# ─────────────────────────────────────────────────────────────────────────────

PART_CATALOG = [
    # (part_id, description, part_class, uom, unit_cost, lead_days, mat_spec)
    ("P-10010", "Titanium Bulkhead Frame 6Al-4V",        "MAKE",     "EA",  4250.00, 45, "AMS 4928"),
    ("P-10011", "Aluminum Rib Assembly 7075-T6",          "MAKE",     "EA",  1875.00, 30, "AMS 2770"),
    ("P-10012", "Stainless Steel Bracket 304 SS",         "MAKE",     "EA",   320.00, 14, "ASTM A240"),
    ("P-10013", "Carbon Fiber Panel 0.063 thick",         "BUY",      "EA",  2100.00, 60, "BMS 8-276"),
    ("P-10014", "Inconel 718 Turbine Blade Blank",        "BUY",      "EA",  6800.00, 90, "AMS 5663"),
    ("P-10015", "Aluminum Extrusion 6061-T6 2x2",        "RAW",      "FT",    18.50, 10, "AMS 2770"),
    ("P-10016", "Ti-6Al-4V Bar Stock 1.5 dia",            "RAW",      "FT",   145.00, 21, "AMS 4928"),
    ("P-10017", "Inconel Bar Stock 1.0 dia",              "RAW",      "FT",   210.00, 35, "AMS 5663"),
    ("P-10018", "304 SS Sheet 0.050 thick",               "RAW",      "LB",    12.50,  7, "ASTM A240"),
    ("P-10019", "MS35337 Washer Flat",                    "HARDWARE", "EA",     0.08,  3, "MS35337"),
    ("P-10020", "NAS1149 Washer Hi-Strength",             "HARDWARE", "EA",     0.45,  5, "NAS1149"),
    ("P-10021", "AN3-5A Bolt 10-32x5/16",                 "HARDWARE", "EA",     0.35,  3, "AN SPEC"),
    ("P-10022", "MS21042 Nut Self-Locking",               "HARDWARE", "EA",     0.55,  3, "MS21042"),
    ("P-10023", "Hi-Lok HL10-8-10 Fastener",              "HARDWARE", "EA",     2.20,  7, "HL SPEC"),
    ("P-10024", "Machined Housing — Fuel Control",        "MAKE",     "EA",  8500.00, 60, "AMS 4894"),
    ("P-10025", "Actuator Bracket Assembly",               "MAKE",     "EA",   950.00, 21, "AMS 2770"),
    ("P-10026", "Weldment — Thrust Reverser Link",        "MAKE",     "EA",  3200.00, 45, "AMS 5510"),
    ("P-10027", "Anodize — Type II Alodine Coated",       "OUTSIDE_SERVICE", "EA", 85.00, 5, None),
    ("P-10028", "Heat Treat — Solution + Age 6061",       "OUTSIDE_SERVICE", "EA", 45.00, 3, "AMS 2770"),
    ("P-10029", "NDT Inspection — Fluorescent Penetrant", "OUTSIDE_SERVICE", "EA", 65.00, 2, "AMS 2647"),
    ("P-10030", "Chemical Film — Alodine 1200",           "OUTSIDE_SERVICE", "EA", 35.00, 3, "MIL-DTL-5541"),
    ("P-10031", "Seal Assembly — Hydraulic 3000PSI",      "BUY",      "EA",   280.00, 14, "MS28774"),
    ("P-10032", "Bearing — Angular Contact 25mm",         "BUY",      "EA",   185.00, 21, "ABMA 20"),
    ("P-10033", "O-Ring AS568-214",                       "HARDWARE", "EA",     1.20,  3, "AS568"),
    ("P-10034", "Titanium Sheet 0.032 thick",             "RAW",      "LB",   195.00, 28, "AMS 4901"),
    ("P-10035", "Kevlar Honeycomb Panel 1/4 cell",        "BUY",      "EA",  1350.00, 45, "BMS 4-17"),
    ("P-10036", "Machined Spar Cap Al 7075",              "MAKE",     "EA",  2800.00, 35, "AMS 2770"),
    ("P-10037", "Composite Fuselage Skin Section",        "BUY",      "EA",  9200.00, 90, "BMS 8-79"),
    ("P-10038", "Hydraulic Fitting AN816-4D",             "HARDWARE", "EA",    12.50,  5, "AN816"),
    ("P-10039", "Aluminum Casting LM Bracket",            "BUY",      "EA",   420.00, 30, "ASTM B85"),
    ("P-10040", "Electrical Harness — Avionics Bay",      "BUY",      "EA",  3600.00, 60, "MIL-W-22759"),
    # Shop consumables (commodity variety — tooling and shop supplies)
    ("C-20001", "Carbide End Mill 0.500 4FL TiAlN",          "BUY", "EA",  42.50,  5, "ISO 1832"),
    ("C-20002", "Coolant Concentrate Semi-Synthetic 55 Gal", "BUY", "DR", 780.00,  7, "MIL-PRF-680"),
    ("C-20003", "Abrasive Disc 120 Grit 5 in (50 Pack)",     "BUY", "PK",  28.40,  5, "ANSI B74.18"),
    ("C-20004", "CMM Stylus Ruby 4mm M3",                    "BUY", "EA",  96.00, 10, "ISO 10360"),
]

SUPPLIER_CATALOG = [
    # (name, category, cert_level, payment_terms, lead_days, outside_svc)
    ("Precision Aero Metals Inc",      "material",        "AS9100D",       "Net30", 21, 0),
    ("Skyline Fastener Supply",        "hardware",        "ISO9001",        "Net30",  7, 0),
    ("Advanced Heat Treat LLC",        "outside_service", "NADCAP/HT",      "Net45",  5, 1),
    ("Pacific Anodize Corp",           "outside_service", "NADCAP/CHEM",    "Net30",  4, 1),
    ("Composite Structures Ltd",       "material",        "AS9100D",        "Net60", 60, 0),
    ("Tri-State Metals Warehouse",     "material",        "ISO9001",         "Net30", 14, 0),
    ("FlightCraft NDT Services",       "outside_service", "NADCAP/NDT",      "Net30",  3, 1),
    ("Titanium International Inc",     "material",        "AS9100D",         "Net45", 28, 0),
    ("West Coast Bearing Supply",      "hardware",        "ISO9001",         "Net30", 10, 0),
    ("Aerospace Seal Solutions",       "hardware",        "AS9100D",         "Net30", 14, 0),
    ("Summit Machining Partners",      "outside_service", "AS9100D",         "Net45", 21, 1),
    ("Global Avionics Distribution",   "material",        "AS9100D",         "Net60", 45, 0),
    ("High Desert Welding LLC",        "outside_service", "AWS/AS9100D",     "Net30", 10, 1),
    ("Eagle Composites Corp",          "material",        "AS9100D",         "Net60", 60, 0),
    ("Continental Chemical Proc",      "outside_service", "NADCAP/CHEM",     "Net30",  4, 1),
]

SHOP_RESOURCES = [
    # (resource_id, description, type, run_$/hr, burden_$/hr)
    ("MC-001", "Haas VF-4 VMC 5-Axis",           "M", 185.0, 95.0),
    ("MC-002", "Mazak Integrex 300 Turn-Mill",    "M", 210.0, 105.0),
    ("MC-003", "DMG Mori NHX 4000 HMC",          "M", 195.0, 100.0),
    ("MC-004", "Doosan Lynx 220 CNC Lathe",       "M", 145.0,  75.0),
    ("MC-005", "Haas SL-40 CNC Lathe",            "M", 140.0,  72.0),
    ("MC-006", "Waterjet OMAX 60120",              "M", 120.0,  60.0),
    ("MC-007", "CMM Zeiss Contura G2",             "M",  95.0,  48.0),
    ("LB-001", "CNC Machinist — Level III",        "L",  65.0,  32.5),
    ("LB-002", "CNC Machinist — Level II",         "L",  52.0,  26.0),
    ("LB-003", "Assembly Technician — Level II",   "L",  48.0,  24.0),
    ("LB-004", "Quality Inspector — Level II",     "L",  55.0,  27.5),
    ("LB-005", "Welder AWS D1.1",                  "L",  58.0,  29.0),
    ("SV-001", "Outside — Anodize",                "S",   0.0,   0.0),
    ("SV-002", "Outside — Heat Treat",             "S",   0.0,   0.0),
    ("SV-003", "Outside — NDT",                    "S",   0.0,   0.0),
    ("SV-004", "Outside — Chemical Film",          "S",   0.0,   0.0),
    ("SV-005", "Outside — Painting / Prime",       "S",   0.0,   0.0),
]

OUTSIDE_SERVICES = [
    # (service_id, description, base_charge)
    ("SVC-ANODIZE",   "Anodize Type II / Type III",              85.0),
    ("SVC-CHEM-FILM", "Chemical Film Alodine 1200",              35.0),
    ("SVC-HT-SOLN",   "Heat Treat Solution + Age",               45.0),
    ("SVC-HT-ANNEAL", "Heat Treat Anneal",                       30.0),
    ("SVC-NDT-FPI",   "NDT Fluorescent Penetrant Inspection",    65.0),
    ("SVC-NDT-XRAY",  "NDT X-Ray Radiography",                  110.0),
    ("SVC-PAINT",     "Prime + Topcoat MIL-PRF-85285",           95.0),
    ("SVC-WELD-TIG",  "TIG Weld Aerospace Grade",               125.0),
]

ROUTING_TEMPLATES = {
    "MACHINED": [
        (10, "MC-001", None, None, 0.5, 2.5),
        (20, "MC-007", None, None, 0.25, 1.0),
        (30, "SV-001", "SVC-ANODIZE", None, 0.0, 0.0),
        (40, "LB-004", None, None, 0.25, 0.5),
    ],
    "WELDMENT": [
        (10, "LB-005", None, None, 1.0, 4.0),
        (20, "SV-002", "SVC-HT-SOLN", None, 0.0, 0.0),
        (30, "SV-003", "SVC-NDT-FPI", None, 0.0, 0.0),
        (40, "MC-002", None, None, 0.5, 1.5),
        (50, "LB-004", None, None, 0.25, 0.5),
    ],
    "AIRFRAME": [
        (10, "MC-003", None, None, 1.0, 5.0),
        (20, "LB-003", None, None, 2.0, 8.0),
        (30, "SV-001", "SVC-ANODIZE", None, 0.0, 0.0),
        (40, "SV-004", "SVC-CHEM-FILM", None, 0.0, 0.0),
        (50, "LB-004", None, None, 0.5, 1.0),
    ],
    "FASTENER": [
        (10, "MC-005", None, None, 0.1, 0.25),
        (20, "LB-004", None, None, 0.1, 0.2),
    ],
}

CERT_TYPES      = ["CoC", "FAI", "PPAP", "8130-3", "Material_Test_Report"]
WO_STATUSES     = ["Open", "Released", "Closed", "Cancelled"]
PO_STATUSES     = ["Open", "Partial", "Closed", "Cancelled"]
INSP_STATUSES   = ["Passed", "Passed", "Passed", "Pending", "Failed", "Waived"]

# ── Wave 4: traceability spine reference data ─────────────────────────────────
SITES = [
    # (site_id, site_name, region)
    ("SITE-1", "Main Assembly Plant — Wichita KS", "US-Central"),
    ("SITE-2", "Machining Center — Phoenix AZ",    "US-Southwest"),
    ("SITE-3", "Composite Fab — Tacoma WA",        "US-Northwest"),
]

CUSTOMERS = [
    "Boeing Commercial Airplanes", "Lockheed Martin Aeronautics", "Northrop Grumman",
    "Raytheon Technologies", "Gulfstream Aerospace", "Bombardier Aviation",
    "Textron Aviation", "Spirit AeroSystems", "Collins Aerospace", "GE Aviation",
]
CO_STATUSES     = ["Open", "Shipped", "Closed", "Cancelled"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def rand_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


def fmt(d) -> str:
    return d.isoformat() if d else None


# ─────────────────────────────────────────────────────────────────────────────
# Seed functions
# ─────────────────────────────────────────────────────────────────────────────

def seed_shop_resources(cur):
    cur.executemany(
        "INSERT OR IGNORE INTO shop_resource "
        "(resource_id, description, resource_type, run_cost_per_hr, bur_per_hr_run, active) "
        "VALUES (?,?,?,?,?,1)",
        [(r[0], r[1], r[2], r[3], r[4]) for r in SHOP_RESOURCES],
    )
    print(f"  shop_resource: {cur.rowcount} rows inserted")


def seed_services(cur):
    svc_vendor_map = {
        "SVC-ANODIZE":   "Pacific Anodize Corp",
        "SVC-CHEM-FILM": "Continental Chemical Proc",
        "SVC-HT-SOLN":   "Advanced Heat Treat LLC",
        "SVC-HT-ANNEAL": "Advanced Heat Treat LLC",
        "SVC-NDT-FPI":   "FlightCraft NDT Services",
        "SVC-NDT-XRAY":  "FlightCraft NDT Services",
        "SVC-PAINT":     "Continental Chemical Proc",
        "SVC-WELD-TIG":  "High Desert Welding LLC",
    }
    cur.executemany(
        "INSERT OR IGNORE INTO service (service_id, description, base_charge) VALUES (?,?,?)",
        [(s[0], s[1], s[2]) for s in OUTSIDE_SERVICES],
    )
    print(f"  service: {cur.rowcount} rows inserted")


def seed_suppliers(cur):
    existing_names = {r[0] for r in cur.execute("SELECT supplier_name FROM suppliers").fetchall()}
    # determine next S-NNN id
    existing_ids = [r[0] for r in cur.execute("SELECT supplier_id FROM suppliers").fetchall()]
    max_num = 0
    for sid in existing_ids:
        try:
            max_num = max(max_num, int(str(sid).replace("S-", "")))
        except ValueError:
            pass
    next_num = max_num + 1

    rows = []
    for name, cat, cert, terms, lead, os_flag in SUPPLIER_CATALOG:
        if name in existing_names:
            continue
        rating = round(random.uniform(3.5, 5.0), 2)
        sup_id = f"S-{next_num:03d}"
        next_num += 1
        rows.append((sup_id, name, cat, cert, terms, lead, os_flag, rating))
    if rows:
        cur.executemany(
            "INSERT INTO suppliers "
            "(supplier_id, supplier_name, category, certification_level, payment_terms, "
            "lead_time_days, outside_service, performance_rating, active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            rows,
        )
    print(f"  suppliers: {cur.rowcount} new rows inserted ({len(rows)} attempted)")


def seed_parts(cur):
    rows = []
    cage_pool = ["1HHH9", "32499", "75272", "CAGE1", "F3234", "K0579", "59012", "28143"]
    for p in PART_CATALOG:
        part_id, desc, cls, uom, cost, lead, mat_spec = p
        cage = random.choice(cage_pool) if cls not in ("OUTSIDE_SERVICE",) else None
        drw  = f"DWG-{part_id[2:]}-A" if cls in ("MAKE", "BUY") else None
        rows.append((part_id, desc, cls, uom, cost, lead,
                     round(cost * random.uniform(0.5, 3.0), 2),
                     round(random.uniform(5, 100), 1),
                     "A", cage, drw, mat_spec, 1))
    cur.executemany(
        "INSERT OR IGNORE INTO part "
        "(part_id, part_description, part_class, unit_of_measure, unit_cost, "
        "lead_time_days, reorder_point, on_hand_qty, revision, cage_code, "
        "drawing_number, material_spec, active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    print(f"  part: {cur.rowcount} rows inserted")


def seed_work_orders(cur, n=15):
    existing_count = cur.execute("SELECT COUNT(*) FROM work_order").fetchone()[0]
    if existing_count >= n:
        print(f"  work_order: already {existing_count} rows — skipping")
        return

    make_parts = [p for p in PART_CATALOG if p[2] == "MAKE"]
    templates  = list(ROUTING_TEMPLATES.keys())
    start_d    = date(2024, 1, 1)
    end_d      = date(2025, 12, 31)

    rows = []
    for i in range(existing_count + 1, n + 1):
        p          = random.choice(make_parts)
        wo_id      = f"WO-{i:05d}"
        wo_type    = random.choices(["M", "W"], weights=[85, 15])[0]
        open_d     = rand_date(start_d, end_d)
        req_d      = open_d + timedelta(days=random.randint(14, 90))
        closed     = random.random() > 0.4
        close_d    = rand_date(open_d + timedelta(days=7), req_d + timedelta(days=10)) if closed else None
        status     = "Closed" if closed else random.choice(["Open", "Released"])
        template   = random.choice(templates)
        qty        = random.choice([1, 2, 4, 5, 10, 25, 50])
        rows.append((wo_id, wo_type, p[0], p[1], qty, status,
                     fmt(open_d), fmt(close_d), fmt(req_d), template,
                     0.0, 0.0, 0.0, 0.0, 0, "SITE-1"))

    cur.executemany(
        "INSERT OR IGNORE INTO work_order "
        "(wo_id, workorder_type, part_id, part_description, quantity, status, "
        "open_date, close_date, required_date, routing_template, "
        "act_lab_cost, act_bur_cost, act_ser_cost, act_mat_cost, "
        "outside_service, site_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    print(f"  work_order: {cur.rowcount} rows inserted")


def seed_operations(cur):
    existing_wo_ids = {r[0] for r in cur.execute(
        "SELECT DISTINCT wo_id FROM operation").fetchall()}
    wo_rows = cur.execute(
        "SELECT wo_id, routing_template FROM work_order WHERE routing_template IS NOT NULL"
    ).fetchall()

    ops = []
    for wo_id, tmpl in wo_rows:
        if wo_id in existing_wo_ids:
            continue
        steps = ROUTING_TEMPLATES.get(tmpl, ROUTING_TEMPLATES["MACHINED"])
        wo_type = cur.execute(
            "SELECT workorder_type FROM work_order WHERE wo_id=?", (wo_id,)
        ).fetchone()[0]
        for seq, res, svc, vend, setup, run in steps:
            is_outside = res.startswith("SV-")
            # pick a supplier for outside ops
            if is_outside and svc:
                svc_sup = cur.execute(
                    "SELECT supplier_id FROM suppliers WHERE outside_service=1 "
                    "ORDER BY RANDOM() LIMIT 1"
                ).fetchone()
                vend_id = str(svc_sup[0]) if svc_sup else None
            else:
                vend_id = None
            ops.append((wo_id, wo_type, seq, res, svc, vend_id, "HR",
                         setup, run, 0.0, 0.0,
                         round(setup * 52.0, 2), round(run * 52.0, 2), 0.0,
                         0.0, 0.0, 0.0, "Q"))
    if ops:
        cur.executemany(
            "INSERT OR IGNORE INTO operation "
            "(wo_id, workorder_type, sequence_no, resource_id, service_id, vendor_id, "
            "run_type, setup_hrs, run_hrs, act_setup_hrs, act_run_hrs, "
            "est_atl_lab_cost, est_atl_bur_cost, est_atl_ser_cost, "
            "act_atl_lab_cost, act_atl_bur_cost, act_atl_ser_cost, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ops,
        )
    print(f"  operation: {cur.rowcount} rows inserted")


def seed_purchase_orders(cur, n=15):
    existing_count = cur.execute("SELECT COUNT(*) FROM purchase_order").fetchone()[0]
    if existing_count >= n:
        print(f"  purchase_order: already {existing_count} rows — skipping")
        return

    all_supplier_ids = [r[0] for r in cur.execute(
        "SELECT supplier_id FROM suppliers WHERE active=1").fetchall()]
    mat_sup = [r[0] for r in cur.execute(
        "SELECT supplier_id FROM suppliers WHERE active=1 AND outside_service=0").fetchall()]
    svc_sup = [r[0] for r in cur.execute(
        "SELECT supplier_id FROM suppliers WHERE active=1 AND outside_service=1").fetchall()]

    start_d = date(2024, 1, 1)
    end_d   = date(2025, 12, 31)

    rows = []
    for i in range(existing_count + 1, n + 1):
        po_id   = f"PO-{i:06d}"
        po_type = random.choices(["material", "outside_service"], weights=[70, 30])[0]
        sup_id  = str(random.choice(svc_sup if po_type == "outside_service" else mat_sup)
                      if (svc_sup if po_type == "outside_service" else mat_sup)
                      else random.choice(all_supplier_ids))
        po_d    = rand_date(start_d, end_d)
        req_d   = po_d + timedelta(days=random.randint(7, 60))
        status  = random.choices(PO_STATUSES, weights=[25, 15, 50, 10])[0]
        rows.append((po_id, sup_id, po_type, fmt(po_d), fmt(req_d), status,
                     0.0, None, None, "BUYER-1", "SITE-1"))

    cur.executemany(
        "INSERT OR IGNORE INTO purchase_order "
        "(po_id, supplier_id, po_type, po_date, required_date, status, "
        "total_cost, wo_id, service_id, buyer_id, site_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    print(f"  purchase_order: {cur.rowcount} rows inserted")


def seed_po_lines(cur):
    existing_po_ids = {r[0] for r in cur.execute(
        "SELECT DISTINCT po_id FROM po_line").fetchall()}
    mat_parts = [p for p in PART_CATALOG if p[2] in ("RAW", "HARDWARE", "BUY")]

    po_rows = cur.execute(
        "SELECT po_id, po_type FROM purchase_order WHERE status != 'Cancelled'"
    ).fetchall()

    lines = []
    for po_id, po_type in po_rows:
        if po_id in existing_po_ids:
            continue
        n_lines = random.randint(1, 4)
        for _ in range(n_lines):
            if po_type == "material":
                p      = random.choice(mat_parts)
                part_id, desc, _, uom, unit_cost, *_ = p
            else:
                svc_parts = [p for p in PART_CATALOG if p[2] == "OUTSIDE_SERVICE"]
                p         = random.choice(svc_parts) if svc_parts else random.choice(mat_parts)
                part_id, desc, _, uom, unit_cost, *_ = p
            qty        = random.choice([1, 2, 5, 10, 25, 50, 100])
            unit_cost  = round(unit_cost * random.uniform(0.9, 1.1), 2)
            line_total = round(qty * unit_cost, 2)
            lines.append((po_id, part_id, desc, qty, unit_cost, line_total))

    if lines:
        cur.executemany(
            "INSERT INTO po_line (po_id, part_id, part_description, quantity, unit_cost, line_total) "
            "VALUES (?,?,?,?,?,?)",
            lines,
        )
        # update total_cost on POs
        cur.execute("""
            UPDATE purchase_order SET total_cost = (
                SELECT COALESCE(SUM(line_total), 0) FROM po_line WHERE po_line.po_id = purchase_order.po_id
            )
        """)
    print(f"  po_line: {cur.rowcount} rows inserted")


def seed_receiving(cur):
    existing_po_ids = {r[0] for r in cur.execute(
        "SELECT DISTINCT po_id FROM receiving").fetchall()}

    exclude_clause = ""
    params: list = []
    if existing_po_ids:
        exclude_clause = f" AND pl.po_id NOT IN ({','.join('?' for _ in existing_po_ids)})"
        params = list(existing_po_ids)

    lines = cur.execute(
        "SELECT pl.po_id, pl.part_id, pl.quantity, po.supplier_id "
        "FROM po_line pl JOIN purchase_order po USING(po_id) "
        "WHERE po.status IN ('Partial','Closed')" + exclude_clause,
        params,
    ).fetchall()

    start_d = date(2024, 2, 1)
    end_d   = date(2025, 12, 31)

    cert_parts = {"P-10013", "P-10014", "P-10024", "P-10036", "P-10037"}
    rows = []
    for po_id, part_id, qty_ord, sup_id in lines:
        recv_pct   = random.uniform(0.5, 1.0)
        qty_recv   = round(qty_ord * recv_pct, 1)
        recv_d     = rand_date(start_d, end_d)
        insp       = random.choice(INSP_STATUSES)
        cert_req   = 1 if part_id in cert_parts else 0
        rows.append((po_id, str(sup_id), part_id, qty_ord, qty_recv, fmt(recv_d), insp, cert_req))

    if rows:
        cur.executemany(
            "INSERT INTO receiving "
            "(po_id, supplier_id, part_id, quantity_ordered, quantity_received, "
            "receipt_date, inspection_status, cert_required) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    print(f"  receiving: {cur.rowcount} rows inserted")


def seed_invoices(cur):
    existing_po_ids = {r[0] for r in cur.execute(
        "SELECT DISTINCT po_id FROM payables").fetchall()}

    po_rows = cur.execute(
        "SELECT po_id, supplier_id, total_cost, po_date "
        "FROM purchase_order WHERE status IN ('Partial','Closed') "
        "AND total_cost > 0"
    ).fetchall()

    rows = []
    inv_num = cur.execute("SELECT COUNT(*) FROM payables").fetchone()[0] + 1
    for po_id, sup_id, total, po_date_str in po_rows:
        if po_id in existing_po_ids:
            continue
        po_d    = date.fromisoformat(po_date_str) if po_date_str else date(2024, 6, 1)
        inv_d   = po_d + timedelta(days=random.randint(7, 30))
        due_d   = inv_d + timedelta(days=30)
        status  = random.choices(["Open", "Paid", "Disputed"], weights=[20, 70, 10])[0]
        pay_d   = rand_date(inv_d, due_d + timedelta(days=15)) if status == "Paid" else None
        # Deterministic rule: a Disputed invoice is disputed BECAUSE the
        # three-way match failed — always carries an Exception match status.
        if status == "Disputed":
            match = "Exception"
        else:
            match = random.choices(
                ["Matched", "Pending", "Exception"], weights=[65, 25, 10])[0]
        rows.append((po_id, str(sup_id), f"INV-{inv_num:06d}",
                     fmt(inv_d), fmt(due_d), round(total, 2),
                     status, fmt(pay_d), match))
        inv_num += 1

    if rows:
        cur.executemany(
            "INSERT INTO payables "
            "(po_id, supplier_id, invoice_number, invoice_date, due_date, "
            "amount_dollars, status, payment_date, three_way_match_status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    print(f"  payables: {cur.rowcount} rows inserted")


def seed_certifications(cur):
    recv_rows = cur.execute(
        "SELECT receipt_id, part_id, supplier_id, receipt_date "
        "FROM receiving WHERE cert_required=1"
    ).fetchall()

    existing_recv_ids = {r[0] for r in cur.execute(
        "SELECT receipt_id FROM certification WHERE receipt_id IS NOT NULL").fetchall()}

    rows = []
    for receipt_id, part_id, sup_id, recv_date_str in recv_rows:
        if receipt_id in existing_recv_ids:
            continue
        recv_d   = date.fromisoformat(recv_date_str) if recv_date_str else date(2024, 6, 1)
        cert_t   = random.choice(CERT_TYPES)
        expiry_d = recv_d + timedelta(days=365) if cert_t in ("CoC", "FAI") else None
        status   = random.choices(["Active", "Expired", "Revoked"], weights=[80, 15, 5])[0]
        rows.append((receipt_id, part_id, str(sup_id), cert_t,
                     fmt(recv_d), fmt(expiry_d), status))

    if rows:
        cur.executemany(
            "INSERT INTO certification "
            "(receipt_id, part_id, supplier_id, cert_type, issued_date, expiry_date, status) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
    print(f"  certification: {cur.rowcount} rows inserted")


def seed_material_issues(cur):
    existing_wo_ids = {r[0] for r in cur.execute(
        "SELECT DISTINCT wo_id FROM material_issue").fetchall()}

    wo_rows = cur.execute(
        "SELECT wo_id, part_id, quantity FROM work_order "
        "WHERE status IN ('Released','Closed')"
    ).fetchall()

    raw_parts = [p for p in PART_CATALOG if p[2] == "RAW"]
    employees = [f"EMP-{i:03d}" for i in range(1, 21)]
    start_d   = date(2024, 1, 1)
    end_d     = date(2025, 12, 31)

    rows = []
    for wo_id, wo_part_id, wo_qty in wo_rows:
        if wo_id in existing_wo_ids:
            continue
        n_issues = random.randint(1, 3)
        for _ in range(n_issues):
            p         = random.choice(raw_parts)
            part_id   = p[0]
            unit_cost = round(p[4] * random.uniform(0.95, 1.05), 2)
            qty       = round(wo_qty * random.uniform(0.8, 1.5), 2)
            total     = round(qty * unit_cost, 2)
            issue_d   = rand_date(start_d, end_d)
            rows.append((wo_id, part_id, p[1], qty, unit_cost, total,
                         fmt(issue_d), random.choice(employees)))

    if rows:
        cur.executemany(
            "INSERT INTO material_issue "
            "(wo_id, part_id, part_description, quantity, unit_cost, total_cost, "
            "issue_date, issued_by) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    print(f"  material_issue: {cur.rowcount} rows inserted")


def seed_labor_tickets(cur):
    existing_wo_ids = {r[0] for r in cur.execute(
        "SELECT DISTINCT wo_id FROM labor_ticket").fetchall()}

    ops = cur.execute(
        "SELECT o.wo_id, o.sequence_no, o.resource_id, o.run_hrs, o.setup_hrs "
        "FROM operation o "
        "JOIN work_order w ON o.wo_id = w.wo_id "
        "WHERE w.status IN ('Released','Closed') "
        "AND o.resource_id LIKE 'LB-%'"
    ).fetchall()

    employees = [f"EMP-{i:03d}" for i in range(1, 21)]
    resource_rates = {r[0]: (r[3], r[4]) for r in SHOP_RESOURCES}

    rows = []
    for wo_id, seq, resource_id, run_hrs, setup_hrs in ops:
        if wo_id in existing_wo_ids:
            continue
        total_hrs  = round((run_hrs + setup_hrs) * random.uniform(0.85, 1.15), 2)
        rates       = resource_rates.get(resource_id, (52.0, 26.0))
        labor_cost  = round(total_hrs * rates[0], 2)
        burden_cost = round(total_hrs * rates[1], 2)
        clock_in    = datetime(2024, random.randint(1, 12), random.randint(1, 28),
                               random.randint(6, 14), 0, 0)
        clock_out   = clock_in + timedelta(hours=total_hrs)
        rows.append((wo_id, seq, random.choice(employees), resource_id,
                     clock_in.isoformat(), clock_out.isoformat(),
                     total_hrs, labor_cost, burden_cost))

    if rows:
        cur.executemany(
            "INSERT INTO labor_ticket "
            "(wo_id, sequence_no, employee_id, resource_id, clock_in, clock_out, "
            "total_hours, labor_cost, burden_cost) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    print(f"  labor_ticket: {cur.rowcount} rows inserted")


def seed_schema_edges(cur):
    cur.execute("SELECT COUNT(*) FROM schema_edges")
    count = cur.fetchone()[0]
    if count > 0:
        print(f"  schema_edges: already {count} rows — skipping")
        return

    rows = [
        (1,  "po_line",       "purchase_order", "FOREIGN_KEY", "po_id",        1, "PO lines belong to a purchase order"),
        (2,  "po_line",       "part",           "FOREIGN_KEY", "part_id",       1, "PO line references a part"),
        (3,  "receiving",     "purchase_order", "FOREIGN_KEY", "po_id",         1, "Receipt closes against a purchase order"),
        (4,  "receiving",     "suppliers",      "FOREIGN_KEY", "supplier_id",   1, "Receipt records the delivering supplier"),
        (5,  "receiving",     "part",           "FOREIGN_KEY", "part_id",       1, "Receipt records the received part"),
        (6,  "payables","purchase_order", "FOREIGN_KEY", "po_id",         1, "Invoice matches to a purchase order"),
        (7,  "payables","suppliers",      "FOREIGN_KEY", "supplier_id",   1, "Invoice issued by a supplier"),
        (8,  "purchase_order","suppliers",      "FOREIGN_KEY", "supplier_id",   1, "Purchase order placed with a supplier"),
        (9,  "certification", "receiving",      "FOREIGN_KEY", "receipt_id",    1, "Cert attached to a receiving line"),
        (10, "certification", "part",           "FOREIGN_KEY", "part_id",       1, "Cert covers a specific part"),
        (11, "work_order",    "part",           "FOREIGN_KEY", "part_id",       1, "Work order manufactures a part"),
        (12, "operation",     "work_order",     "FOREIGN_KEY", "wo_id",         1, "Routing step belongs to a work order"),
        (13, "operation",     "shop_resource",  "FOREIGN_KEY", "resource_id",   1, "Routing step runs on a shop resource"),
        (14, "material_issue","work_order",     "FOREIGN_KEY", "wo_id",         1, "Material issued to a work order"),
        (15, "material_issue","part",           "FOREIGN_KEY", "part_id",       1, "Material issue consumes a part"),
        (16, "labor_ticket",  "work_order",     "FOREIGN_KEY", "wo_id",         1, "Labor ticket posted against a work order"),
        (17, "purchase_order","work_order",     "FOREIGN_KEY", "wo_id",         1, "Outside-service PO tied to a work order"),
        (18, "operation",     "service",        "FOREIGN_KEY", "service_id",    1, "Outside-service op uses a service definition"),
        (19, "operation",     "suppliers",      "FOREIGN_KEY", "vendor_id",     1, "Outside-service op dispatched to a supplier"),
        (20, "receiving_line","receiving",      "FOREIGN_KEY", "receipt_id",    1, "Receipt line belongs to a receiving header"),
        (21, "receiving_line","po_line",        "FOREIGN_KEY", "po_line_id",    1, "Receipt line closes against a purchase-order line"),
        (22, "receiving_line","part",           "FOREIGN_KEY", "part_id",       1, "Receipt line records the received part"),
        (23, "payable_line",  "po_line",        "FOREIGN_KEY", "po_line_id",    1, "Payable line matches a purchase-order line"),
        (24, "payable_line",  "receiving_line", "FOREIGN_KEY", "receipt_line_id", 1, "Payable line matches a receipt line (three-way match)"),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO schema_edges "
        "(edge_id, from_table, to_table, relationship_type, join_column, weight, natural_language_alias) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    print(f"  schema_edges: {cur.rowcount} rows inserted")


def seed_schema_nodes(cur):
    extra = [
        ("part",        "Table", "Part / item master — description, class, UOM, cost, revision, material spec, CAGE code"),
        ("shop_resource","Table","Shop work centers and outside-service buckets (machine, labor, service types)"),
        ("service",     "Table", "Outside service definitions (anodize, heat treat, NDT, plating, painting)"),
        ("receiving_line", "Table", "Line items on a goods receipt — one row per part received, linked to the PO line for line-level three-way match"),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO schema_nodes (table_name, table_type, description) VALUES (?,?,?)",
        extra,
    )
    print(f"  schema_nodes: {cur.rowcount} extra rows merged")


# ─────────────────────────────────────────────────────────────────────────────
# Wave 4 — traceability spine seed functions
# ─────────────────────────────────────────────────────────────────────────────

def seed_sites(cur):
    cur.executemany(
        "INSERT OR IGNORE INTO site (site_id, site_name, region) VALUES (?,?,?)",
        SITES,
    )
    print(f"  site: {cur.rowcount} rows inserted")


def seed_customer_orders(cur, n=15):
    existing = cur.execute("SELECT COUNT(*) FROM customer_order").fetchone()[0]
    if existing >= n:
        print(f"  customer_order: already {existing} rows — skipping")
        return
    site_ids = [s[0] for s in SITES]
    start_d, end_d = date(2024, 1, 1), date(2025, 12, 31)
    rows = []
    for i in range(existing + 1, n + 1):
        order_id = f"CO-{i:05d}"
        cust     = random.choice(CUSTOMERS)
        order_d  = rand_date(start_d, end_d)
        site     = random.choice(site_ids)
        status   = random.choices(CO_STATUSES, weights=[20, 35, 35, 10])[0]
        rows.append((order_id, cust, fmt(order_d), site, status))
    cur.executemany(
        "INSERT OR IGNORE INTO customer_order "
        "(order_id, customer_name, order_date, site_id, status) VALUES (?,?,?,?,?)",
        rows,
    )
    print(f"  customer_order: {cur.rowcount} rows inserted")


def seed_customer_order_lines(cur):
    existing_orders = {r[0] for r in cur.execute(
        "SELECT DISTINCT order_id FROM customer_order_line").fetchall()}
    orders = cur.execute(
        "SELECT order_id, site_id FROM customer_order WHERE status != 'Cancelled'"
    ).fetchall()
    sellable = [p for p in PART_CATALOG if p[2] in ("MAKE", "BUY")]

    lines = []
    for order_id, site_id in orders:
        if order_id in existing_orders:
            continue
        for line_no in range(1, random.randint(1, 3) + 1):
            p     = random.choice(sellable)
            qty   = random.choice([1, 2, 4, 5, 10])
            price = round(p[4] * random.uniform(1.15, 1.45), 2)
            lines.append((order_id, line_no, p[0], site_id, qty, price))
    if lines:
        cur.executemany(
            "INSERT INTO customer_order_line "
            "(order_id, line_no, part_id, site_id, order_qty, unit_price) "
            "VALUES (?,?,?,?,?,?)",
            lines,
        )
    print(f"  customer_order_line: {cur.rowcount} rows inserted")


def seed_inventory_transactions(cur):
    """Movement ledger with all R/A/I classes and I/O types.

    Roles are encoded by (class, type, wo_id, po_id) so downstream genealogy /
    trace functions can recover them without in-memory state:
      - R/I + po_id        -> raw material receipt from a PO
      - I/O + wo_id        -> raw material issue to a work order
      - R/I + wo_id        -> finished-good receipt from a closed work order
      - A/I or A/O         -> inventory adjustment
      - I/O (no wo/po)     -> outbound customer shipment
    """
    if cur.execute("SELECT COUNT(*) FROM inventory_transaction").fetchone()[0] > 0:
        print("  inventory_transaction: already populated — skipping")
        return
    site_ids = [s[0] for s in SITES]
    start_d, end_d = date(2024, 1, 1), date(2025, 12, 31)
    rows = []

    # (1) Raw material RECEIPTS from PO receiving lines  (R / I)
    for po_id, part_id, qty, recv_d in cur.execute(
        "SELECT po_id, part_id, quantity_received, receipt_date FROM receiving"
    ).fetchall():
        rows.append(("R", "I", part_id, None, po_id,
                     random.choice(site_ids), qty, recv_d))

    # (2) Raw material ISSUES to work orders  (I / O)
    for wo_id, part_id, qty, issue_d in cur.execute(
        "SELECT wo_id, part_id, quantity, issue_date FROM material_issue"
    ).fetchall():
        rows.append(("I", "O", part_id, wo_id, None, "SITE-1", qty, issue_d))

    # (3) Finished-good RECEIPTS from closed work orders  (R / I)
    for wo_id, part_id, qty, close_d, site_id in cur.execute(
        "SELECT wo_id, part_id, quantity, close_date, site_id "
        "FROM work_order WHERE status='Closed' AND close_date IS NOT NULL"
    ).fetchall():
        rows.append(("R", "I", part_id, wo_id, None, site_id or "SITE-1", qty, close_d))

    # (4) Inventory ADJUSTMENTS  (A / I or O)
    parts = [p[0] for p in PART_CATALOG]
    for _ in range(40):
        rows.append(("A", random.choice(["I", "O"]), random.choice(parts),
                     None, None, random.choice(site_ids),
                     round(random.uniform(1, 25), 1), fmt(rand_date(start_d, end_d))))

    # (5) Outbound SHIPMENTS for shipped/closed customer order lines  (I / O)
    for part_id, site_id, qty in cur.execute(
        "SELECT col.part_id, col.site_id, col.order_qty "
        "FROM customer_order_line col JOIN customer_order co USING(order_id) "
        "WHERE co.status IN ('Shipped','Closed')"
    ).fetchall():
        rows.append(("I", "O", part_id, None, None,
                     site_id or "SITE-1", qty, fmt(rand_date(start_d, end_d))))

    cur.executemany(
        "INSERT INTO inventory_transaction "
        "(class, type, part_id, wo_id, po_id, site_id, quantity, trans_date) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    print(f"  inventory_transaction: {cur.rowcount} rows inserted")


def seed_trace(cur):
    """One traced finished-good lot per work-order finished-good receipt."""
    if cur.execute("SELECT COUNT(*) FROM trace").fetchone()[0] > 0:
        print("  trace: already populated — skipping")
        return
    receipts = cur.execute(
        "SELECT transaction_id, wo_id, part_id, quantity, trans_date, site_id "
        "FROM inventory_transaction "
        "WHERE class='R' AND type='I' AND wo_id IS NOT NULL"
    ).fetchall()
    rows = []
    for tid, wo_id, part_id, qty, trans_d, site_id in receipts:
        lot_id    = f"LOT-{wo_id}"
        serial_id = f"SN-{wo_id}-{tid}"
        exp_d     = (fmt(date.fromisoformat(trans_d) + timedelta(days=730))
                     if trans_d else None)
        rows.append((part_id, lot_id, serial_id, qty, 0.0, trans_d, exp_d, site_id))
    cur.executemany(
        "INSERT INTO trace "
        "(part_id, lot_id, serial_id, in_qty, out_qty, production_date, "
        "expiration_date, site_id) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    print(f"  trace: {cur.rowcount} rows inserted")


def seed_trace_inventory_trace(cur):
    """Bridge each lot to the finished-good receipt that produced it."""
    if cur.execute("SELECT COUNT(*) FROM trace_inventory_trace").fetchone()[0] > 0:
        print("  trace_inventory_trace: already populated — skipping")
        return
    rows = cur.execute(
        "SELECT t.part_id, t.trace_id, it.transaction_id, t.in_qty "
        "FROM trace t "
        "JOIN inventory_transaction it "
        "  ON it.wo_id = REPLACE(t.lot_id, 'LOT-', '') "
        " AND it.class='R' AND it.type='I' AND it.wo_id IS NOT NULL"
    ).fetchall()
    cur.executemany(
        "INSERT INTO trace_inventory_trace "
        "(part_id, trace_id, transaction_id, qty) VALUES (?,?,?,?)",
        rows,
    )
    print(f"  trace_inventory_trace: {cur.rowcount} rows inserted")


def seed_inv_trans_dist(cur):
    """Lot genealogy: distribute each finished-good receipt (IN) across the raw
    material issues (OUT) of the same work order, creating the recursive
    IN-trans <-> OUT-trans lineage links."""
    if cur.execute("SELECT COUNT(*) FROM inv_trans_dist").fetchone()[0] > 0:
        print("  inv_trans_dist: already populated — skipping")
        return
    rows = cur.execute(
        "SELECT r.transaction_id, i.transaction_id, i.quantity "
        "FROM inventory_transaction r "
        "JOIN inventory_transaction i ON i.wo_id = r.wo_id "
        "WHERE r.class='R' AND r.type='I' AND r.wo_id IS NOT NULL "
        "  AND i.class='I' AND i.type='O' AND i.wo_id IS NOT NULL"
    ).fetchall()
    cur.executemany(
        "INSERT INTO inv_trans_dist (in_trans_id, out_trans_id, dist_qty) "
        "VALUES (?,?,?)",
        rows,
    )
    print(f"  inv_trans_dist: {cur.rowcount} rows inserted")


def seed_payable_lines(cur):
    """AP payable detail — one line per PO line behind each invoice header."""
    existing_inv = {r[0] for r in cur.execute(
        "SELECT DISTINCT invoice_id FROM payable_line").fetchall()}
    invoices = cur.execute(
        "SELECT invoice_id, po_id FROM payables"
    ).fetchall()

    lines = []
    for invoice_id, po_id in invoices:
        if invoice_id in existing_inv:
            continue
        po_lines = cur.execute(
            "SELECT part_id, quantity, line_total FROM po_line WHERE po_id=?",
            (po_id,),
        ).fetchall()
        for line_no, (part_id, qty, line_total) in enumerate(po_lines, start=1):
            lines.append((invoice_id, line_no, po_id, part_id, qty, line_total))
    if lines:
        cur.executemany(
            "INSERT INTO payable_line "
            "(invoice_id, line_no, po_id, part_id, qty, amount) VALUES (?,?,?,?,?,?)",
            lines,
        )
    print(f"  payable_line: {cur.rowcount} rows inserted")


def seed_receiving_lines(cur):
    """One receiving_line per receiving header row that has none (line_no=1)."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS receiving_line (
            receipt_line_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id        INTEGER NOT NULL REFERENCES receiving(receipt_id),
            line_no           INTEGER NOT NULL,
            po_line_id        INTEGER REFERENCES po_line(line_id),
            part_id           TEXT NOT NULL REFERENCES part(part_id),
            quantity_ordered  REAL NOT NULL,
            quantity_received REAL NOT NULL,
            inspection_status TEXT NOT NULL DEFAULT 'Pending',
            cert_required     INTEGER DEFAULT 0,
            created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (receipt_id, line_no)
        )
    """)
    cur.execute("""
        INSERT INTO receiving_line
            (receipt_id, line_no, po_line_id, part_id, quantity_ordered,
             quantity_received, inspection_status, cert_required)
        SELECT r.receipt_id, 1,
               (SELECT MIN(pl.line_id) FROM po_line pl
                 WHERE pl.po_id = r.po_id AND pl.part_id = r.part_id),
               r.part_id, r.quantity_ordered, r.quantity_received,
               r.inspection_status, r.cert_required
        FROM receiving r
        WHERE NOT EXISTS (SELECT 1 FROM receiving_line rl
                          WHERE rl.receipt_id = r.receipt_id)
    """)
    print(f"  receiving_line: {cur.rowcount} rows inserted")


def seed_payable_line_links(cur):
    """Line-level three-way match: link payables to PO lines and receipt lines."""
    for col in ("po_line_id", "receipt_line_id"):
        have = {r[1] for r in cur.execute("PRAGMA table_info(payable_line)")}
        if col not in have:
            ref = ("po_line(line_id)" if col == "po_line_id"
                   else "receiving_line(receipt_line_id)")
            cur.execute(f"ALTER TABLE payable_line ADD COLUMN {col} INTEGER "
                        f"REFERENCES {ref}")
    cur.execute("""
        UPDATE payable_line SET po_line_id = (
            SELECT MIN(pl.line_id) FROM po_line pl
            WHERE pl.po_id = payable_line.po_id
              AND pl.part_id = payable_line.part_id)
        WHERE po_line_id IS NULL AND po_id IS NOT NULL AND part_id IS NOT NULL
    """)
    cur.execute("""
        UPDATE payable_line SET receipt_line_id = (
            SELECT MIN(rl.receipt_line_id)
            FROM receiving_line rl JOIN receiving r ON r.receipt_id = rl.receipt_id
            WHERE r.po_id = payable_line.po_id
              AND rl.part_id = payable_line.part_id)
        WHERE receipt_line_id IS NULL AND po_id IS NOT NULL AND part_id IS NOT NULL
    """)
    print("  payable_line: line-level PO/receipt links backfilled")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

ERP_TABLES = [
    "part", "certification", "payables", "labor_ticket", "material_issue",
    "operation", "po_line", "purchase_order", "receiving", "service",
    "shop_resource", "work_order",
    # Wave 4 — traceability spine (topological order)
    "site", "customer_order", "customer_order_line", "inventory_transaction",
    "trace", "trace_inventory_trace", "inv_trans_dist", "payable_line",
    "receiving_line",
]

def clear_erp_tables(cur):
    print("  Clearing ERP tables…")
    for tbl in reversed(ERP_TABLES):
        cur.execute(f"DELETE FROM {tbl}")
    cur.execute("DELETE FROM schema_edges")
    print("  Done.")


def main():
    parser = argparse.ArgumentParser(description="Seed synthetic aerospace ERP data")
    parser.add_argument("--clear", action="store_true",
                        help="Wipe ERP tables before seeding")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"Seeding {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()

    try:
        if args.clear:
            clear_erp_tables(cur)

        # master data first
        seed_shop_resources(cur)
        seed_services(cur)
        seed_suppliers(cur)
        seed_parts(cur)
        # dependent transactional data
        seed_work_orders(cur)
        seed_operations(cur)
        seed_purchase_orders(cur)
        seed_po_lines(cur)
        seed_receiving(cur)
        seed_invoices(cur)
        seed_certifications(cur)
        seed_material_issues(cur)
        seed_labor_tickets(cur)
        # Wave 4 — traceability spine (topological order)
        seed_sites(cur)
        seed_customer_orders(cur)
        seed_customer_order_lines(cur)
        seed_inventory_transactions(cur)
        seed_trace(cur)
        seed_trace_inventory_trace(cur)
        seed_inv_trans_dist(cur)
        seed_payable_lines(cur)
        seed_receiving_lines(cur)
        seed_payable_line_links(cur)
        # metadata
        seed_schema_edges(cur)
        seed_schema_nodes(cur)

        con.commit()
        print("\nSeed complete. Row counts:")
        for tbl in ERP_TABLES + ["schema_edges", "schema_nodes"]:
            n = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl:<20} {n:>5}")
    except Exception as exc:
        con.rollback()
        print(f"ERROR — rolled back: {exc}", file=sys.stderr)
        raise
    finally:
        con.close()


if __name__ == "__main__":
    main()
