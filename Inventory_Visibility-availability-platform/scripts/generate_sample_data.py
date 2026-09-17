"""Deterministic sample feeds for Inventory Visibility.

250 SKUs across 3 warehouses, with every data-quality trap the design doc
argues about planted somewhere in the data so the demo can prove each one:

  oversell | negative on-hand | orphan SKU in orders | orphan SKU in shipments
  unknown unit of measure | legacy SKU alias | location alias | duplicate row
  non-numeric quantity | shipped-line double count | received-PO double count
  partial PO | overdue inbound | restocked-return double count | snapshot skew

Seeded, so the same numbers appear every time the interview demo is run.
"""
from __future__ import annotations

import csv
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(20260909)
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

AS_OF = datetime(2026, 9, 9, 6, 15)
WAREHOUSES = ["WH1", "WH2", "WH3"]

CATEGORIES = {
    "FASTENERS":   ("Industrial fasteners", ["Hex bolt", "Lock washer", "Anchor set", "Machine screw", "Threaded rod"]),
    "PACKAGING":   ("Packaging & shipping", ["Corrugated carton", "Stretch wrap", "Poly mailer", "Pallet wrap", "Void fill"]),
    "PPE":         ("Safety & PPE",         ["Nitrile glove", "Hi-vis vest", "Safety goggle", "Hard hat", "Ear defender"]),
    "ELECTRICAL":  ("Electrical supply",    ["Conduit clip", "Cable gland", "Junction box", "Terminal block", "Cable tie"]),
    "JANITORIAL":  ("Janitorial",           ["Floor cleaner", "Paper towel", "Bin liner", "Sanitiser", "Microfibre cloth"]),
    "AUTOMOTIVE":  ("Automotive parts",     ["Brake pad set", "Oil filter", "Wiper blade", "Air filter", "Spark plug"]),
}
SUPPLIERS = ["Cordell Industrial", "Northgate Supply", "Verity Packaging",
             "Ambro Components", "Kestrel Distribution", "Lakeside Materials"]
CUSTOMERS = ["Halvern Retail Group", "Bexley Trade Co", "Orion Facilities",
             "Trentwood Motors", "PrimeMart Wholesale", "Kingsford Hospitals",
             "Duvall Construction", "Maple Ridge Schools"]

# ---------------------------------------------------------------- item master
skus: list[dict] = []
cat_keys = list(CATEGORIES)
for n in range(1, 251):
    sku = f"MSD-{2000 + n}"
    cat = cat_keys[n % len(cat_keys)]
    label, nouns = CATEGORIES[cat]
    noun = nouns[n % len(nouns)]
    size = random.choice(["M6", "M8", "12mm", '3/8"', "Large", "Heavy duty", "Standard", "XL"])
    cost = round(random.uniform(0.4, 78.0), 2)
    abc = "A" if n % 9 == 0 else ("B" if n % 3 == 0 else "C")
    skus.append({
        "sku": sku,
        "description": f"{noun} {size}",
        "category": cat,
        "unit_cost": cost,
        "unit_price": round(cost * random.uniform(1.28, 1.85), 2),
        "safety_stock": {"A": random.choice([40, 60, 80]), "B": random.choice([0, 20, 25]), "C": 0}[abc],
        "abc_class": abc,
    })

# Unit-of-measure demonstration SKUs. MSD-3304 has NO conversion rule on purpose.
for i, (code, desc, uom_note) in enumerate([
    ("MSD-3301", "Corrugated carton 400x300 (case of 12)", "CASE"),
    ("MSD-3302", "Nitrile glove box (case of 24)", "CASE"),
    ("MSD-3303", "Stretch wrap roll (pallet of 480)", "PALLET"),
    ("MSD-3304", "Poly mailer 350x450 (case, rule missing)", "CASE"),
]):
    skus.append({"sku": code, "description": desc, "category": "PACKAGING",
                 "unit_cost": round(random.uniform(6, 30), 2),
                 "unit_price": round(random.uniform(12, 55), 2),
                 "safety_stock": 0, "abc_class": "B"})

master_index = {s["sku"]: s for s in skus}

# Two SKUs are deliberately absent from the item master (NOT_IN_ITEM_MASTER).
published_master = [s for s in skus if s["sku"] not in ("MSD-2007", "MSD-2113")]
with open(DATA / "item_master.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["sku", "description", "category", "unit_cost",
                                       "unit_price", "safety_stock", "abc_class"])
    w.writeheader()
    w.writerows(published_master)

# ---------------------------------------------------------------------- WMS
OVERSOLD_SKUS = ["MSD-2018", "MSD-2044", "MSD-2071", "MSD-2096", "MSD-2130",
                 "MSD-2158", "MSD-2187", "MSD-2203", "MSD-2229"]
NEGATIVE_SKUS = ["MSD-2033", "MSD-2166"]
STOCKOUT_SKUS = ["MSD-2052", "MSD-2141", "MSD-2211"]

wms_rows: list[dict] = []
stock_index: dict[tuple[str, str], int] = {}
for s in skus:
    sku = s["sku"]
    n_loc = 1 if sku.startswith("MSD-33") else random.choice([1, 1, 2, 2, 3])
    locs = random.sample(WAREHOUSES, n_loc)
    for loc in locs:
        if sku in NEGATIVE_SKUS and loc == locs[0]:
            qty = -random.randint(3, 18)
        elif sku in STOCKOUT_SKUS:
            qty = 0
        elif sku in OVERSOLD_SKUS:
            qty = random.randint(12, 90)
        else:
            qty = random.randint(20, 1400)
        damaged = random.choice([0, 0, 0, 0, 0, 0, random.randint(1, 14)])
        damaged = min(damaged, max(qty, 0))
        uom = "EA"
        if sku == "MSD-3301": uom, qty = "CASE", random.randint(8, 60)
        if sku == "MSD-3302": uom, qty = "CASE", random.randint(6, 40)
        if sku == "MSD-3303": uom, qty = "PALLET", random.randint(1, 9)
        if sku == "MSD-3304": uom, qty = "CASE", random.randint(10, 45)
        # Location aliasing: some rows arrive in the WMS's legacy code format.
        raw_loc = {"WH1": "wh-1", "WH2": "W2"}.get(loc, loc) if random.random() < 0.06 else loc
        wms_rows.append({"sku": sku, "warehouse": raw_loc, "on_hand": qty, "uom": uom,
                         "damaged_qty": damaged,
                         "last_counted": (AS_OF - timedelta(days=random.randint(0, 40))).isoformat() + "Z"})
        stock_index[(sku, loc)] = qty if uom == "EA" else qty * {"MSD-3301": 12, "MSD-3302": 24, "MSD-3303": 480}.get(sku, 1)

# Planted defects in the WMS export
wms_rows.append({"sku": " msd-2042 ", "warehouse": "Warehouse 1", "on_hand": 640, "uom": "EA",
                 "damaged_qty": 0, "last_counted": AS_OF.isoformat() + "Z"})   # casing + whitespace + alias location
dup = dict(wms_rows[4]); wms_rows.append(dup)                                   # DUPLICATE_RECORD
wms_rows.append({"sku": "MSD-2088", "warehouse": "WH3", "on_hand": "twelve", "uom": "EA",
                 "damaged_qty": 0, "last_counted": AS_OF.isoformat() + "Z"})     # QTY_NOT_NUMERIC
random.shuffle(wms_rows)
with open(DATA / "wms_stock.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["sku", "warehouse", "on_hand", "uom", "damaged_qty", "last_counted"])
    w.writeheader(); w.writerows(wms_rows)

# ------------------------------------------------------------------- orders
RESERVING = ["PENDING", "ALLOCATED", "PICKING", "PACKED"]
NON_RESERVING = ["SHIPPED", "CANCELLED", "DRAFT", "CLOSED"]
orders: list[dict] = []
oid = 4000

def add_order(status, lines, penalty=None):
    global oid
    oid += 1
    orders.append({
        "order_id": f"SO-{oid}",
        "status": status,
        "customer": random.choice(CUSTOMERS),
        "promised_date": (AS_OF + timedelta(days=random.randint(0, 9))).date().isoformat(),
        "otif_penalty": random.random() < 0.28 if penalty is None else penalty,
        "lines": lines,
    })

# Oversell scenarios: reserve MORE than the sellable on-hand.
for sku in OVERSOLD_SKUS:
    locs = [l for (s, l) in stock_index if s == sku]
    loc = locs[0]
    onhand = max(stock_index[(sku, loc)], 0)
    add_order("ALLOCATED", [{"sku": sku, "warehouse": loc, "qty": onhand + random.randint(15, 120)}],
              penalty=True)

# Orphan SKUs: promised to customers, unknown to the warehouse.
add_order("ALLOCATED", [{"sku": "MSD-9001", "warehouse": "WH1", "qty": 60}], penalty=True)
add_order("PICKING",   [{"sku": "MSD-9002", "warehouse": "WH2", "qty": 24}], penalty=False)

# Legacy SKU code still emitted by the order system -> alias-mapped to MSD-2042.
add_order("ALLOCATED", [{"sku": "MSD-LEGACY-0042", "warehouse": "WH1", "qty": 45}])

# The double-count trap: SHIPPED lines are already out of the WMS count.
for _ in range(90):
    s = random.choice(skus[:250])
    locs = [l for (sk, l) in stock_index if sk == s["sku"]] or ["WH1"]
    add_order(random.choice(NON_RESERVING),
              [{"sku": s["sku"], "warehouse": random.choice(locs), "qty": random.randint(5, 200)}])

# Normal reserving demand.
for _ in range(330):
    n_lines = random.choice([1, 1, 1, 2, 2, 3])
    lines = []
    for _ in range(n_lines):
        s = random.choice(skus[:250])
        locs = [l for (sk, l) in stock_index if sk == s["sku"]] or ["WH1"]
        loc = random.choice(locs)
        cap = max(stock_index.get((s["sku"], loc), 0), 0)
        lines.append({"sku": s["sku"], "warehouse": loc,
                      "qty": max(1, int(cap * random.uniform(0.02, 0.42))) if cap else random.randint(2, 25)})
    add_order(random.choice(RESERVING), lines)

random.shuffle(orders)
(DATA / "orders.json").write_text(json.dumps(
    {"as_of": "2026-09-09T06:00:00Z", "source_system": "NetSuite OM",
     "export_id": "OM-EXP-20260909-0600", "orders": orders}, indent=1))

# ---------------------------------------------------------------- shipments
ship_rows: list[dict] = []
pid = 7700
for _ in range(120):
    pid += 1
    s = random.choice(skus[:250])
    loc = random.choice(WAREHOUSES)
    ordered = random.choice([50, 100, 150, 200, 250, 400, 600])
    status = random.choices(["IN_TRANSIT", "RECEIVED", "PARTIAL", "BOOKED"],
                            weights=[42, 34, 12, 12])[0]
    received = {"RECEIVED": ordered, "PARTIAL": int(ordered * random.uniform(0.3, 0.7))}.get(status, 0)
    eta = AS_OF + timedelta(days=random.randint(-2, 21))
    ship_rows.append({"po_id": f"PO-{pid}", "sku": s["sku"], "warehouse": loc,
                      "qty_ordered": ordered, "qty_received": received,
                      "eta": eta.date().isoformat(), "status": status,
                      "supplier": random.choice(SUPPLIERS), "uom": "EA"})

# Inbound cover for some of the oversold SKUs -- gives ops an action to take.
for sku in OVERSOLD_SKUS[:5]:
    pid += 1
    ship_rows.append({"po_id": f"PO-{pid}", "sku": sku, "warehouse": "WH1",
                      "qty_ordered": random.choice([150, 200, 300]), "qty_received": 0,
                      "eta": (AS_OF + timedelta(days=random.randint(2, 6))).date().isoformat(),
                      "status": "IN_TRANSIT", "supplier": random.choice(SUPPLIERS), "uom": "EA"})

# Overdue inbound: due days ago, still open, quietly inflating ATP.
for sku, days in [("MSD-2101", 11), ("MSD-2175", 6)]:
    pid += 1
    ship_rows.append({"po_id": f"PO-{pid}", "sku": sku, "warehouse": "WH2",
                      "qty_ordered": 250, "qty_received": 0,
                      "eta": (AS_OF - timedelta(days=days)).date().isoformat(),
                      "status": "IN_TRANSIT", "supplier": "Ambro Components", "uom": "EA"})

# Inbound for a SKU the warehouse has never heard of.
pid += 1
ship_rows.append({"po_id": f"PO-{pid}", "sku": "MSD-9500", "warehouse": "WH1",
                  "qty_ordered": 180, "qty_received": 0,
                  "eta": (AS_OF + timedelta(days=4)).date().isoformat(),
                  "status": "IN_TRANSIT", "supplier": "Kestrel Distribution", "uom": "EA"})

random.shuffle(ship_rows)
with open(DATA / "supplier_shipments.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["po_id", "sku", "warehouse", "qty_ordered",
                                       "qty_received", "eta", "status", "supplier", "uom"])
    w.writeheader(); w.writerows(ship_rows)

# ------------------------------------------------------- returns (feed four)
returns = []
for i in range(46):
    s = random.choice(skus[:250])
    status = random.choices(
        ["IN_TRANSIT", "RECEIVED_PENDING_QC", "QC_PASSED_RESTOCKED", "QC_FAILED_SCRAP"],
        weights=[30, 30, 28, 12])[0]
    returns.append({"rma_id": f"RMA-{5100 + i}", "sku": s["sku"],
                    "warehouse": random.choice(WAREHOUSES),
                    "qty": random.randint(1, 40), "status": status,
                    "reason": random.choice(["Damaged in transit", "Wrong item shipped",
                                             "Customer overordered", "Quality complaint"]),
                    "received_at": (AS_OF - timedelta(days=random.randint(0, 12))).date().isoformat()})
(DATA / "returns.json").write_text(json.dumps(
    {"as_of": "2026-09-09T04:10:00Z", "source_system": "Returns portal v2",
     "returns": returns}, indent=1))

print(f"item_master.csv        {len(published_master):>5} SKUs")
print(f"wms_stock.csv          {len(wms_rows):>5} rows")
print(f"orders.json            {len(orders):>5} orders, {sum(len(o['lines']) for o in orders)} lines")
print(f"supplier_shipments.csv {len(ship_rows):>5} POs")
print(f"returns.json           {len(returns):>5} RMAs")
