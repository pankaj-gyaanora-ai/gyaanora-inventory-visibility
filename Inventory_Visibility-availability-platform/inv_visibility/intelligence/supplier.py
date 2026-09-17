"""Supplier ETA prediction and risk-adjusted ATP.

This layer fixes a flaw in the deterministic engine rather than adding a
feature to it: **ATP is only as trustworthy as the supplier's ETA, and suppliers
are optimistic.** The engine already flags a PO that is provably late
(OVERDUE_INBOUND). This predicts the ones that are *going* to be late, from that
supplier's own track record.

Output is published alongside the raw ATP, never instead of it. Ops needs the
optimistic number for planning and the adjusted one for promising.
"""
from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def supplier_profiles(path: Path) -> dict[str, dict]:
    """One row per supplier: how late do they actually run?"""
    slips: dict[str, list[float]] = defaultdict(list)
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            slips[r["supplier"]].append(float(r["slip_days"]))

    profiles = {}
    for supplier, values in slips.items():
        very_late = sum(1 for v in values if v > 7) / len(values)
        profiles[supplier] = {
            "supplier": supplier,
            "pos_observed": len(values),
            "median_slip_days": round(statistics.median(values), 1),
            "p90_slip_days": round(_percentile(values, 0.90), 1),
            "share_over_7_days_late": round(very_late, 3),
            "on_time_rate": round(sum(1 for v in values if v <= 0) / len(values), 3),
            # A transparent 0-1 score. Deliberately a formula, not a model:
            # ops has to be able to interrogate why a PO was called risky.
            "slip_risk": round(min(
                0.55 * min(max(statistics.median(values), 0) / 5, 1.0)
                + 0.45 * very_late, 1.0), 2),
        }
    return profiles


def predict_arrivals(items: list[dict], profiles: dict[str, dict],
                     as_of: datetime, horizon_days: int) -> dict[str, dict]:
    """Per SKU: which inbound units can honestly be counted on inside the
    horizon, once the supplier's history is taken into account?"""
    horizon = as_of + timedelta(days=horizon_days)
    out: dict[str, dict] = {}

    for it in items:
        if not it["inbound_detail"]:
            continue
        pos, at_risk_units, confident_units = [], 0, 0
        for d in it["inbound_detail"]:
            prof = profiles.get(d.get("supplier") or "", {})
            median = prof.get("median_slip_days", 0.0)
            p90 = prof.get("p90_slip_days", 0.0)
            risk = prof.get("slip_risk", 0.0)
            try:
                eta = datetime.fromisoformat(str(d["eta"]))
            except (TypeError, ValueError):
                continue
            eta_p50 = eta + timedelta(days=median)
            eta_p90 = eta + timedelta(days=p90)
            lands_p50 = eta_p50 <= horizon
            lands_p90 = eta_p90 <= horizon
            qty = d["qty"]
            if lands_p90:
                confident_units += qty
            elif lands_p50:
                at_risk_units += qty
            pos.append({
                "po_id": d["po_id"], "supplier": d.get("supplier"), "qty": qty,
                "eta_supplier": str(d["eta"]),
                "eta_predicted_p50": eta_p50.date().isoformat(),
                "eta_predicted_p90": eta_p90.date().isoformat(),
                "slip_risk": risk,
                "inside_horizon_p50": lands_p50, "inside_horizon_p90": lands_p90,
                "basis": (f"{d.get('supplier')} median slip {median}d over "
                          f"{prof.get('pos_observed', 0)} POs; "
                          f"{prof.get('share_over_7_days_late', 0) * 100:.0f}% arrive >7 days late")
                if prof else "no history for this supplier",
            })
        if not pos:
            continue
        optimistic = it["atp_7d"]
        adjusted = it["available"] + confident_units
        out[it["sku"]] = {
            "atp_optimistic": optimistic,
            "atp_risk_adjusted": adjusted,
            "units_at_slip_risk": at_risk_units,
            "worst_slip_risk": max((p["slip_risk"] for p in pos), default=0.0),
            "purchase_orders": pos,
        }
    return out
