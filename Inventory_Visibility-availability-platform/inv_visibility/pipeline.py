"""Normalize -> aggregate -> compute.

This module has no idea how many sources exist or what they are called. It
operates only on canonical records and the bucket signs declared in config.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .config import parse_ts
from .models import QuarantinedRow


def normalize(records, cfg, quarantine):
    """Make the join actually work: SKU codes, locations, units of measure.

    An unknown unit of measure is NEVER guessed. No conversion rule means the
    SKU is excluded from the published number and flagged, because guessing
    between eaches and cases is a twelvefold availability error.
    """
    n = cfg["normalize"]
    sku_alias = {k.upper(): v.upper() for k, v in (n.get("sku_aliases") or {}).items()}
    loc_alias = {k.upper(): v.upper() for k, v in (n.get("location_aliases") or {}).items()}
    uom_rules = {k.upper(): {u.upper(): f for u, f in v.items()}
                 for k, v in (n.get("uom_conversions") or {}).items()}

    out: list = []
    seen: set = set()
    excluded_skus: set = set()
    cell_owner: dict = {}
    unique_cell_sources = {sp["name"] for sp in cfg["sources"] if sp.get("unique_per_cell")}

    for r in records:
        raw_sku = r.sku
        sku = r.sku.strip() if n.get("sku_strip", True) else r.sku
        if n.get("sku_case") == "upper":
            sku = sku.upper()
        if sku in sku_alias:
            r.meta["alias_from"] = sku
            sku = sku_alias[sku]
        loc = (r.location or "").strip().upper()
        loc = loc_alias.get(loc, loc)
        if raw_sku != sku or (r.location or "").strip() != loc:
            r.meta["normalized_from"] = f"{raw_sku}@{r.location}"
        r.sku, r.location = sku, loc

        uom = (r.uom or "EA").upper()
        if uom != "EA":
            factor = uom_rules.get(sku, {}).get(uom)
            if factor is None:
                quarantine.append(QuarantinedRow(
                    source=r.source, row_ref=r.source_record_id,
                    reason="UOM_UNKNOWN", field="uom", raw_value=uom,
                    action="sku_excluded_from_publish", sku=sku))
                excluded_skus.add(sku)
                continue
            r.quantity = int(r.quantity * factor)
            r.meta["uom_converted"] = f"{uom} x{factor}"
            r.uom = "EA"

        cell = (r.source, r.sku, r.location, r.bucket)
        prior = cell_owner.get(cell)
        if prior is not None and prior != r.source_record_id and r.source in unique_cell_sources:
            quarantine.append(QuarantinedRow(
                source=r.source, row_ref=f"{prior} + {r.source_record_id}",
                reason="CELL_COLLISION", field="sku+location", raw_value=r.meta.get("normalized_from"),
                action="both_rows_kept_and_summed", sku=r.sku))
        cell_owner.setdefault(cell, r.source_record_id)

        key = (r.source, r.source_record_id, r.bucket)
        if key in seen:
            quarantine.append(QuarantinedRow(
                source=r.source, row_ref=r.source_record_id,
                reason="DUPLICATE_RECORD", field="source_record_id",
                raw_value=r.source_record_id, action="duplicate_dropped", sku=sku))
            continue
        seen.add(key)
        out.append(r)

    return out, excluded_skus


def aggregate(records, cfg, item_master, as_of, excluded_skus):
    """Group by (sku, location), apply bucket signs, roll up to SKU."""
    buckets = cfg["buckets"]
    horizon = as_of + timedelta(days=cfg["run"]["atp_horizon_days"])

    cells: dict = defaultdict(lambda: defaultdict(int))
    evidence: dict = defaultdict(list)
    sku_sources: dict = defaultdict(set)
    inbound_detail: dict = defaultdict(list)
    reserved_detail: dict = defaultdict(list)

    for r in records:
        cells[(r.sku, r.location)][r.bucket] += r.quantity
        evidence[r.sku].append(r.source_record_id)
        sku_sources[r.sku].add(r.source)
        if r.bucket == "INBOUND":
            inbound_detail[r.sku].append({
                "po_id": r.meta.get("po_id"), "qty": r.quantity,
                "eta": r.meta.get("eta"), "supplier": r.meta.get("supplier"),
                "location": r.location, "status": r.meta.get("status")})
        if r.bucket == "RESERVED":
            reserved_detail[r.sku].append({
                "order_id": r.meta.get("order_id"), "qty": r.quantity,
                "customer": r.meta.get("customer"), "status": r.meta.get("status"),
                "promised_date": r.meta.get("promised_date"),
                "otif_penalty": r.meta.get("otif_penalty", False),
                "location": r.location})

    # Safety stock is a config-owned bucket injected from the item master. It is
    # a PER-SKU buffer, so it is applied exactly once -- at the location holding
    # the most stock. Applying it per location would multiply the buffer by the
    # warehouse count, and would make the published number depend on how many
    # locations happen to appear in the feeds. (An invariant test enforces this:
    # adding a zero-signed source must not move any existing availability.)
    by_sku_locs: dict = defaultdict(list)
    for (sku, loc) in cells:
        by_sku_locs[sku].append(loc)
    for sku, locs in by_sku_locs.items():
        buffer = int((item_master.get(sku) or {}).get("safety_stock") or 0)
        if not buffer:
            continue
        primary = max(locs, key=lambda l: cells[(sku, l)].get("ON_HAND", 0))
        cells[(sku, primary)]["SAFETY_STOCK"] += buffer

    items: dict = {}
    for (sku, loc), b in cells.items():
        avail_raw = sum(q * buckets[bk]["available_sign"] for bk, q in b.items() if bk in buckets)
        atp_extra = 0
        for det in inbound_detail.get(sku, []):
            if det["location"] != loc:
                continue
            eta = det.get("eta")
            try:
                if eta and parse_ts(eta) <= horizon:
                    atp_extra += det["qty"]
            except Exception:
                pass
        loc_row = {
            "location": loc,
            "on_hand": b.get("ON_HAND", 0),
            "reserved": b.get("RESERVED", 0),
            "unsellable": b.get("UNSELLABLE", 0),
            "safety_stock": b.get("SAFETY_STOCK", 0),
            "inbound": b.get("INBOUND", 0),
            "return_in_transit": b.get("RETURN_IN_TRANSIT", 0),
            "return_pending_qc": b.get("RETURN_PENDING_QC", 0),
            "raw_available": avail_raw,
            "available": max(avail_raw, 0),
            "atp_7d": max(max(avail_raw, 0) + atp_extra, 0),
        }
        it = items.setdefault(sku, {"sku": sku, "by_location": []})
        it["by_location"].append(loc_row)

    out = []
    for sku, it in items.items():
        locs = sorted(it["by_location"], key=lambda x: x["location"])
        agg = {k: sum(l[k] for l in locs) for k in
               ("on_hand", "reserved", "unsellable", "safety_stock", "inbound",
                "return_in_transit", "return_pending_qc", "raw_available",
                "available", "atp_7d")}
        im = item_master.get(sku, {})
        srcs = sorted(sku_sources[sku])
        has_wms = "wms" in srcs

        # Conservative bias, made concrete. Clamping each location and then
        # summing would publish MORE than the network actually holds whenever
        # one location is short (its deficit would be hidden by a zero floor).
        # So the published number is the NETWORK total clamped once, which
        # preserves the invariant  available <= on_hand  in every case. The
        # per-location shortfall is not lost -- it is raised as
        # LOCATION_OVERSOLD, whose fix is a stock transfer, not a sale.
        network_available = max(agg["raw_available"], 0)
        network_atp = max(min(agg["atp_7d"], network_available + agg["inbound"]), 0)
        agg["available"], agg["atp_7d"] = network_available, network_atp
        published = 0 if sku in excluded_skus else network_available
        record = {
            "sku": sku,
            "description": im.get("description", "(not in item master)"),
            "category": im.get("category", "UNCLASSIFIED"),
            "unit_cost": float(im.get("unit_cost", 0) or 0),
            "unit_price": float(im.get("unit_price", 0) or 0),
            "abc_class": im.get("abc_class", "C"),
            "available": published,
            "raw_available": agg["raw_available"],
            "atp_7d": 0 if sku in excluded_skus else agg["atp_7d"],
            "breakdown": {
                "on_hand": agg["on_hand"] if has_wms else None,
                "reserved": agg["reserved"],
                "unsellable": agg["unsellable"],
                "safety_stock": agg["safety_stock"],
                "inbound": agg["inbound"],
                "return_in_transit": agg["return_in_transit"],
                "return_pending_qc": agg["return_pending_qc"],
            },
            "by_location": locs,
            "sources_seen": srcs,
            "evidence": sorted(set(evidence[sku]))[:12],
            "reserved_detail": reserved_detail.get(sku, []),
            "inbound_detail": inbound_detail.get(sku, []),
            "excluded_from_publish": sku in excluded_skus,
            "in_item_master": sku in item_master,
            "flags": [],
            "confidence": "high",
        }
        out.append(record)

    for sku in sorted(excluded_skus - set(items)):
        im = item_master.get(sku, {})
        out.append({
            "sku": sku, "description": im.get("description", "(not in item master)"),
            "category": im.get("category", "UNCLASSIFIED"),
            "unit_cost": float(im.get("unit_cost", 0) or 0),
            "unit_price": float(im.get("unit_price", 0) or 0),
            "abc_class": im.get("abc_class", "C"),
            "available": 0, "raw_available": 0, "atp_7d": 0,
            "breakdown": {"on_hand": None, "reserved": 0, "unsellable": 0,
                          "safety_stock": 0, "inbound": 0,
                          "return_in_transit": 0, "return_pending_qc": 0},
            "by_location": [], "sources_seen": [], "evidence": [],
            "reserved_detail": [], "inbound_detail": [],
            "excluded_from_publish": True, "in_item_master": sku in item_master,
            "flags": [], "confidence": "low",
        })

    return sorted(out, key=lambda x: x["sku"])
