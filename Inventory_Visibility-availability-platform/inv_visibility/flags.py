"""Exception (flag) engine.

Rules are data, not `if` statements scattered through the aggregator. Each rule
returns SKU + severity + human message + EVIDENCE. The evidence array is the
lineage that makes ops believe a number that contradicts their spreadsheet.
"""
from __future__ import annotations

from datetime import timedelta

from .config import parse_ts
from .models import Exception_

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def evaluate(items, cfg, sources_meta, quarantine, as_of, item_master):
    exceptions: list[Exception_] = []
    fcfg = cfg.get("flags", {})
    required_sources = {s["name"] for s in cfg["sources"]}

    # ---- Run-level flags: stale feeds and snapshot skew --------------------
    sla_default = cfg["run"]["staleness_sla_hours"]
    stale_sources, as_ofs = [], []
    for s in sources_meta:
        if not s.get("loaded"):
            continue
        ts = parse_ts(s["as_of"])
        as_ofs.append(ts)
        age_h = (as_of - ts).total_seconds() / 3600.0
        s["age_hours"] = round(age_h, 2)
        s["stale"] = age_h > s.get("sla_hours", sla_default)
        if s["stale"]:
            stale_sources.append(s["name"])
            exceptions.append(Exception_(
                sku="*", flag="STALE_SOURCE", severity="critical",
                message=f"Feed '{s['name']}' is {age_h:.1f}h old, past its {s.get('sla_hours', sla_default)}h SLA.",
                evidence=[f"{s['name']}@{s['as_of']}"],
                detail={"source": s["name"], "age_hours": round(age_h, 2)},
                suggested_action=f"Check the {s['name']} export job before trusting today's numbers."))

    if len(as_ofs) > 1:
        skew_min = (max(as_ofs) - min(as_ofs)).total_seconds() / 60.0
        if skew_min > cfg["run"]["snapshot_skew_warn_minutes"]:
            exceptions.append(Exception_(
                sku="*", flag="SNAPSHOT_SKEW", severity="warning",
                message=f"Feeds describe moments {skew_min:.0f} minutes apart.",
                evidence=[f"{s['name']}@{s['as_of']}" for s in sources_meta if s.get("loaded")],
                detail={"skew_minutes": round(skew_min, 1)},
                suggested_action="Align export schedules to a common snapshot time."))

    for q in quarantine:
        if q.reason == "UOM_UNKNOWN":
            exceptions.append(Exception_(
                sku=q.sku or "*", flag="UOM_UNKNOWN", severity="critical",
                message=f"No conversion rule for unit '{q.raw_value}'. SKU excluded from publish rather than guessed.",
                evidence=[q.row_ref],
                detail={"uom": q.raw_value, "source": q.source},
                suggested_action="Add a uom_conversions rule in sources.yaml, then re-run."))
        elif q.reason == "DUPLICATE_RECORD":
            exceptions.append(Exception_(
                sku=q.sku or "*", flag="DUPLICATE_RECORD", severity="warning",
                message=f"Record {q.row_ref} appeared twice in '{q.source}'; the copy was dropped.",
                evidence=[q.row_ref], detail={"source": q.source},
                suggested_action="Export bug -- risks double subtraction. Raise with the source system owner."))
        elif q.reason == "CELL_COLLISION":
            exceptions.append(Exception_(
                sku=q.sku or "*", flag="CELL_COLLISION", severity="warning",
                message=(f"Two '{q.source}' rows describe the same SKU and location after "
                         f"normalisation. Both were kept and summed."),
                evidence=[q.row_ref], detail={"source": q.source, "normalized_from": q.raw_value},
                suggested_action=("Confirm with the warehouse whether these are bin-level rows "
                                  "(summing is correct) or a duplicated export (stock is overstated). "
                                  "Summing is the non-destructive default; dropping silently is not.")))
        elif q.reason == "QTY_NOT_NUMERIC":
            exceptions.append(Exception_(
                sku=q.sku or "*", flag="BAD_INPUT_ROW", severity="warning",
                message=f"Non-numeric {q.field} '{q.raw_value}' in '{q.source}'; row quarantined.",
                evidence=[q.row_ref], detail={"source": q.source},
                suggested_action="Fix the source row; the run continued without it."))

    # ---- Per-SKU flags -----------------------------------------------------
    for it in items:
        sku, b = it["sku"], it["breakdown"]
        flags: list[str] = []
        on_hand = b["on_hand"]
        sellable_on_hand = (on_hand or 0) - b["unsellable"]

        neg_locs = [l for l in it["by_location"] if l["on_hand"] < 0]
        if neg_locs:
            flags.append("NEGATIVE_ON_HAND")
            it["confidence"] = "low"
            worst = min(neg_locs, key=lambda l: l["on_hand"])
            exceptions.append(Exception_(
                sku=sku, flag="NEGATIVE_ON_HAND", severity="critical",
                message=(f"WMS reports {worst['on_hand']} units on hand at {worst['location']}. "
                         f"Physically impossible."),
                evidence=it["evidence"][:4],
                detail={"location": worst["location"], "on_hand": worst["on_hand"],
                        "sku_total_on_hand": on_hand},
                suggested_action="Raise a cycle count at that location. Availability published as 0 meanwhile."))

        if b["reserved"] > sellable_on_hand and on_hand is not None:
            shortfall = b["reserved"] - sellable_on_hand
            penalty_orders = [r["order_id"] for r in it["reserved_detail"] if r.get("otif_penalty")]
            exposure = round(shortfall * (it["unit_price"] or 0), 2)
            flags.append("OVERSOLD")
            exceptions.append(Exception_(
                sku=sku, flag="OVERSOLD", severity="critical",
                message=f"{b['reserved']} units promised against {sellable_on_hand} sellable. Short {shortfall}.",
                evidence=it["evidence"][:6],
                detail={"shortfall": shortfall, "reserved": b["reserved"],
                        "sellable_on_hand": sellable_on_hand,
                        "revenue_at_risk": exposure,
                        "penalty_orders": penalty_orders,
                        "next_inbound": (it["inbound_detail"][0] if it["inbound_detail"] else None)},
                suggested_action=("Expedite " + it["inbound_detail"][0]["po_id"]
                                  if it["inbound_detail"] else
                                  "No inbound cover -- contact customers on the affected orders today.")))

        loc_short = [l for l in it["by_location"] if l["raw_available"] < 0]
        if loc_short and "OVERSOLD" not in flags:
            worst = min(loc_short, key=lambda l: l["raw_available"])
            flags.append("LOCATION_OVERSOLD")
            exceptions.append(Exception_(
                sku=sku, flag="LOCATION_OVERSOLD", severity="warning",
                message=(f"{worst['location']} is short {abs(worst['raw_available'])} units, "
                         f"but the network total is {it['raw_available']}. Coverable by transfer."),
                evidence=[r["order_id"] for r in it["reserved_detail"]
                          if r["location"] == worst["location"]][:5],
                detail={"location": worst["location"],
                        "location_short": abs(worst["raw_available"]),
                        "network_available": it["raw_available"],
                        "surplus_locations": [l["location"] for l in it["by_location"]
                                              if l["raw_available"] > 0]},
                suggested_action="Raise an inter-warehouse transfer rather than short-shipping."))

        if "orders" in it["sources_seen"] and "wms" not in it["sources_seen"]:
            flags.append("ORPHAN_SKU_IN_ORDERS")
            it["confidence"] = "low"
            exceptions.append(Exception_(
                sku=sku, flag="ORPHAN_SKU_IN_ORDERS", severity="critical",
                message=f"{b['reserved']} units are on customer orders but the warehouse has no record of this SKU.",
                evidence=[r["order_id"] for r in it["reserved_detail"]][:6],
                detail={"reserved": b["reserved"]},
                suggested_action="Create the item master / WMS record, or cancel the affected orders."))

        if it["sources_seen"] and "wms" not in it["sources_seen"] and "orders" not in it["sources_seen"]:
            if "shipments" in it["sources_seen"]:
                flags.append("ORPHAN_SKU_IN_SHIPMENTS")
                it["confidence"] = "low"
                exceptions.append(Exception_(
                    sku=sku, flag="ORPHAN_SKU_IN_SHIPMENTS", severity="warning",
                    message="Inbound stock is arriving for a SKU the warehouse does not know about.",
                    evidence=[d["po_id"] for d in it["inbound_detail"]][:6],
                    detail={"inbound": b["inbound"]},
                    suggested_action="Likely a new product missing from the item master. Set it up before receipt."))

        missing = sorted(required_sources - set(it["sources_seen"]))
        it["missing_sources"] = missing
        for m in missing:
            flags.append(f"MISSING_IN_SOURCE:{m}")
        if "wms" in missing and "orders" not in missing:
            pass   # already covered by ORPHAN_SKU_IN_ORDERS, do not double report

        grace = timedelta(days=fcfg.get("overdue_inbound_grace_days", 1))
        for d in it["inbound_detail"]:
            try:
                if d.get("eta") and parse_ts(d["eta"]) + grace < as_of:
                    flags.append("OVERDUE_INBOUND")
                    exceptions.append(Exception_(
                        sku=sku, flag="OVERDUE_INBOUND", severity="warning",
                        message=f"{d['po_id']} was due {d['eta']} and has not landed. ATP is inflated by {d['qty']} units.",
                        evidence=[d["po_id"]],
                        detail={"po_id": d["po_id"], "eta": d["eta"], "qty": d["qty"],
                                "supplier": d.get("supplier")},
                        suggested_action=f"Chase {d.get('supplier') or 'the supplier'} or remove the PO from the ATP horizon."))
                    break
            except Exception:
                pass

        ss = b["safety_stock"]
        if ss and it["available"] < ss and "OVERSOLD" not in flags:
            flags.append("BELOW_SAFETY_STOCK")
            exceptions.append(Exception_(
                sku=sku, flag="BELOW_SAFETY_STOCK", severity="warning",
                message=f"Available {it['available']} is under the {ss}-unit buffer.",
                evidence=it["evidence"][:3],
                detail={"available": it["available"], "safety_stock": ss},
                suggested_action="Reorder trigger. Raise a PO."))

        if it["excluded_from_publish"]:
            flags.append("EXCLUDED_FROM_PUBLISH")
            it["confidence"] = "low"

        if not it["in_item_master"]:
            flags.append("NOT_IN_ITEM_MASTER")
            it["confidence"] = "low" if it["confidence"] == "high" else it["confidence"]

        if it["available"] == 0 and "OVERSOLD" not in flags and (on_hand or 0) <= 0 and on_hand is not None:
            flags.append("STOCKOUT")

        it["flags"] = flags
        if it["confidence"] == "high" and len(it["sources_seen"]) < 2:
            it["confidence"] = "medium"

    exceptions.sort(key=lambda e: (SEVERITY_ORDER[e.severity], e.flag, e.sku))
    return exceptions
