"""Historical feeds for the intelligence layer (Layer 7).

The three operational feeds are snapshots -- they say what is true *now*. None of
them carries history, so forecasting needs its own inputs. This is a real
discovery question for the client: *do we have shipment history, or does the
clock start when we go live?* Here we assume 180 days exist.

Three files, each with the shape a distributor's systems actually produce:

  shipment_history.csv    daily units shipped per SKU per warehouse (demand)
  po_receipt_history.csv  promised ETA vs actual receipt date (supplier slip)
  stock_ledger.csv        daily on-hand, receipts, shipments, adjustments
                          -> lets us compute the conservation residual that
                             exposes shrinkage before it becomes an oversell

Demand is deliberately generated in the three patterns a distributor really has,
because the model choice depends on which one a SKU is:

  smooth        A-class movers, demand most days, weekly seasonality
  intermittent  the long tail -- many zero days, small quantities
  lumpy         rare, large, irregular orders

Seeded, so the backtest numbers are the same every time you demo.
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(4242)
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HIST = DATA / "history"
HIST.mkdir(parents=True, exist_ok=True)

AS_OF = datetime(2026, 9, 9)
DAYS = 180
START = AS_OF - timedelta(days=DAYS)

SUPPLIERS = {
    # supplier: (median slip days, spread, share of POs arriving >7 days late)
    "Cordell Industrial":   (0.0, 1.2, 0.03),
    "Northgate Supply":     (1.0, 2.0, 0.08),
    "Verity Packaging":     (0.0, 1.0, 0.02),
    "Ambro Components":     (3.5, 4.5, 0.27),   # the problem supplier
    "Kestrel Distribution": (1.5, 2.5, 0.11),
    "Lakeside Materials":   (0.5, 1.5, 0.05),
}

# SKUs whose WMS count quietly drifts away from reality. This is an oversell
# cause the reconciliation engine alone cannot see: the on-hand number it trusts
# is itself too high.
SHRINKING = {"MSD-2077": -1.2, "MSD-2132": -0.8, "MSD-2189": -1.6}


def read_master() -> list[dict]:
    with open(DATA / "item_master.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def read_locations() -> dict[str, list[str]]:
    locs: dict[str, list[str]] = {}
    with open(DATA / "wms_stock.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            sku = r["sku"].strip().upper()
            loc = r["warehouse"].strip().upper()
            loc = {"WH-1": "WH1", "W2": "WH2", "WAREHOUSE 1": "WH1"}.get(loc, loc)
            locs.setdefault(sku, [])
            if loc not in locs[sku]:
                locs[sku].append(loc)
    return locs


def pattern_for(abc: str, n: int) -> str:
    if abc == "A":
        return "smooth"
    if abc == "B":
        return "intermittent" if n % 3 else "smooth"
    return "lumpy" if n % 7 == 0 else "intermittent"


def demand_series(pattern: str, scale: float) -> list[int]:
    """One SKU-location's daily shipped units for the whole window."""
    out = []
    for d in range(DAYS):
        day = (START + timedelta(days=d))
        weekday = day.weekday()
        if weekday >= 5:                       # distributors ship Mon-Fri
            out.append(0)
            continue
        # mild weekly shape plus a slow upward drift over the window
        season = 1.0 + 0.18 * (1 if weekday in (0, 1) else -0.5)
        trend = 1.0 + 0.25 * (d / DAYS)
        if pattern == "smooth":
            mu = scale * season * trend
            out.append(max(0, int(random.gauss(mu, mu * 0.28))))
        elif pattern == "intermittent":
            out.append(int(max(1, random.gauss(scale * 0.9, scale * 0.4)))
                       if random.random() < 0.34 else 0)
        else:                                   # lumpy
            out.append(int(max(1, random.gauss(scale * 6, scale * 2.5)))
                       if random.random() < 0.07 else 0)
    return out


def main() -> int:
    master = read_master()
    locs = read_locations()

    # ---------------------------------------------------------- demand history
    shipments = []
    series_index: dict[tuple[str, str], list[int]] = {}
    for n, item in enumerate(master):
        sku = item["sku"]
        if sku not in locs:
            continue
        pattern = pattern_for(item["abc_class"], n)
        for loc in locs[sku]:
            scale = {"A": random.uniform(9, 26), "B": random.uniform(3, 9),
                     "C": random.uniform(0.8, 4)}[item["abc_class"]]
            series = demand_series(pattern, scale)
            series_index[(sku, loc)] = series
            for d, units in enumerate(series):
                if units:
                    shipments.append({
                        "sku": sku, "location": loc,
                        "date": (START + timedelta(days=d)).date().isoformat(),
                        "units_shipped": units, "demand_pattern": pattern})

    with open(HIST / "shipment_history.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["sku", "location", "date",
                                           "units_shipped", "demand_pattern"])
        w.writeheader()
        w.writerows(shipments)

    # ------------------------------------------------------ supplier PO history
    po_rows = []
    pid = 6000
    for _ in range(520):
        pid += 1
        supplier = random.choice(list(SUPPLIERS))
        med, spread, late_share = SUPPLIERS[supplier]
        item = random.choice(master)
        promised = START + timedelta(days=random.randint(5, DAYS - 5))
        slip = random.gauss(med, spread)
        if random.random() < late_share:
            slip += random.uniform(7, 16)       # the long tail that breaks ATP
        slip = max(round(slip), -2)             # early arrivals happen, rarely
        actual = promised + timedelta(days=slip)
        if actual > AS_OF:
            continue
        po_rows.append({
            "po_id": f"POH-{pid}", "supplier": supplier, "sku": item["sku"],
            "warehouse": random.choice(["WH1", "WH2", "WH3"]),
            "qty": random.choice([50, 100, 150, 200, 300, 500]),
            "eta_promised": promised.date().isoformat(),
            "received_at": actual.date().isoformat(),
            "slip_days": slip})
    with open(HIST / "po_receipt_history.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["po_id", "supplier", "sku", "warehouse", "qty",
                                           "eta_promised", "received_at", "slip_days"])
        w.writeheader()
        w.writerows(po_rows)

    # ------------------------------------------------------------- stock ledger
    # on_hand(t) = on_hand(t-1) + receipts - shipments + adjustments + drift
    # The drift column is what should not exist. Where it persistently
    # negative, stock is leaving the building without a transaction.
    ledger = []
    for (sku, loc), series in series_index.items():
        if random.random() > 0.55:              # ledger covers a sample of cells
            continue
        on_hand = random.randint(150, 1200)
        drift_rate = SHRINKING.get(sku, 0.0)
        for d in range(DAYS):
            date = START + timedelta(days=d)
            shipped = series[d]
            receipts = random.choice([0] * 22 + [random.choice([50, 100, 200])])
            adjustment = random.choice([0] * 40 + [random.randint(-6, 6)])
            drift = 0
            if drift_rate and random.random() < 0.75:
                drift = int(round(random.gauss(drift_rate, 0.6)))
            on_hand = max(on_hand + receipts - shipped + adjustment + drift, 0)
            ledger.append({
                "sku": sku, "location": loc, "date": date.date().isoformat(),
                "opening_on_hand": on_hand - receipts + shipped - adjustment - drift,
                "receipts": receipts, "shipments": shipped,
                "adjustments": adjustment, "closing_on_hand": on_hand})
    with open(HIST / "stock_ledger.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["sku", "location", "date", "opening_on_hand",
                                           "receipts", "shipments", "adjustments",
                                           "closing_on_hand"])
        w.writeheader()
        w.writerows(ledger)

    print(f"shipment_history.csv   {len(shipments):>6} rows  ({DAYS} days)")
    print(f"po_receipt_history.csv {len(po_rows):>6} closed POs across {len(SUPPLIERS)} suppliers")
    print(f"stock_ledger.csv       {len(ledger):>6} rows  ({len(set((r['sku'], r['location']) for r in ledger))} cells)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
