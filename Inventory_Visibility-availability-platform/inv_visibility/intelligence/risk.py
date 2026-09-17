"""Risk scoring -- turning a long exception list into a worklist of ten.

The flag engine produces a lot of output on day one. Nobody works 48 items
before their second coffee. This combines the deterministic state with the
forecast into one ranked queue.

Two design rules, both defensive:

  * the formula is a **transparent weighted sum declared in config**, not a
    model. A black-box 0-100 that ops cannot interrogate gets ignored inside a
    week, and then the whole system gets ignored with it.
  * every score carries `reason_codes` and a `contributions` breakdown, so the
    answer to "why is this rank 1?" is on the screen, not in a notebook.

The rows worth demonstrating are the ones where **nothing is wrong today** --
no deterministic flag fires -- and the system still says there is a hole coming
on Thursday. That is the shift from reconciliation to prevention.
"""
from __future__ import annotations

from datetime import datetime


DEFAULT_WEIGHTS = {
    "stockout_probability": 30,
    "oversold_now": 28,
    "revenue_at_risk": 16,
    "penalty_exposure": 12,
    "inbound_slip": 9,
    "data_confidence": 5,
}


def _band(score: float) -> str:
    if score >= 75:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 25:
        return "MEDIUM"
    return "LOW"


def score_all(items, exceptions, forecasts, slip, shrinkage, cfg, as_of) -> dict:
    weights = {**DEFAULT_WEIGHTS, **(cfg.get("risk", {}).get("weights") or {})}
    cap = (cfg.get("risk", {}) or {}).get("revenue_at_risk_cap", 25000)

    exc_by_sku: dict[str, list[dict]] = {}
    for e in exceptions:
        exc_by_sku.setdefault(e["sku"], []).append(e)
    shrink_by_sku = {s["sku"]: s for s in shrinkage}

    ranked = []
    for it in items:
        sku = it["sku"]
        fc = forecasts.get(sku, {})
        sl = slip.get(sku, {})
        excs = exc_by_sku.get(sku, [])
        flags = {e["flag"] for e in excs}
        reasons, contributions = [], {}

        # --- component 1: will it run out, and how soon -----------------------
        cover = fc.get("days_of_cover_p10")
        if cover is None:
            stockout = 0.0
        elif cover <= 0:
            stockout = 1.0
        elif cover <= 14:
            stockout = 1.0 - (cover / 14)
        else:
            stockout = 0.0
        if stockout > 0.35:
            reasons.append("COVER_BELOW_LEAD_TIME")
        elif stockout > 0:
            reasons.append("COVER_UNDER_WATCH")
        contributions["stockout_probability"] = round(stockout * weights["stockout_probability"], 1)

        # --- component 2: already oversold ------------------------------------
        oversold = 1.0 if "OVERSOLD" in flags else (0.45 if "LOCATION_OVERSOLD" in flags else 0.0)
        if oversold:
            reasons.append("OVERSOLD" if "OVERSOLD" in flags else "LOCATION_OVERSOLD")
        contributions["oversold_now"] = round(oversold * weights["oversold_now"], 1)

        # --- component 3: money on the line -----------------------------------
        shortfall = max(sum(e["detail"].get("shortfall", 0) for e in excs), 0)
        demand_14d = (fc.get("daily_demand_mean") or 0) * 14
        exposure_units = shortfall or max(demand_14d - it["available"], 0)
        revenue = exposure_units * (it["unit_price"] or 0)
        contributions["revenue_at_risk"] = round(min(revenue / cap, 1.0) * weights["revenue_at_risk"], 1)
        if revenue > cap * 0.25:
            reasons.append("HIGH_REVENUE_EXPOSURE")

        # --- component 4: contractual penalties -------------------------------
        penalty = any(r.get("otif_penalty") for r in it["reserved_detail"])
        if penalty and (oversold or stockout > 0.4):
            reasons.append("OTIF_PENALTY_ORDER")
        contributions["penalty_exposure"] = round(
            (1.0 if penalty and (oversold or stockout > 0.4) else 0.0) * weights["penalty_exposure"], 1)

        # --- component 5: is the cover actually going to arrive ---------------
        slip_risk = sl.get("worst_slip_risk", 0.0)
        if slip_risk >= 0.4 and sl.get("units_at_slip_risk"):
            reasons.append("INBOUND_SLIP_RISK")
        contributions["inbound_slip"] = round(slip_risk * weights["inbound_slip"], 1)

        # --- component 6: can we even trust the inputs ------------------------
        conf_penalty = {"high": 0.0, "medium": 0.5, "low": 1.0}[it["confidence"]]
        if sku in shrink_by_sku:
            conf_penalty = 1.0
            reasons.append("SHRINKAGE_SUSPECTED")
        if it["confidence"] != "high":
            reasons.append("LOW_DATA_CONFIDENCE")
        contributions["data_confidence"] = round(conf_penalty * weights["data_confidence"], 1)

        score = round(sum(contributions.values()), 1)
        # A row on the worklist that cannot say why it is there is worse than no
        # row at all -- it is the first thing ops learns to scroll past.
        if not reasons or score < 5:
            continue

        no_open_po = not it["inbound_detail"]
        if (oversold or stockout > 0.5) and no_open_po:
            reasons.append("NO_OPEN_PO")

        ranked.append({
            "sku": sku, "description": it["description"],
            "risk_score": score, "band": _band(score),
            "reason_codes": sorted(set(reasons)),
            "contributions": contributions,
            "available": it["available"],
            "days_of_cover_p10": cover,
            "stockout_date_p10": fc.get("stockout_date_p10"),
            "demand_pattern": fc.get("demand_pattern"),
            "forecast_model": fc.get("model"),
            "forecast_confidence": fc.get("confidence"),
            "revenue_at_risk": round(revenue, 2),
            "atp_optimistic": sl.get("atp_optimistic", it["atp_7d"]),
            "atp_risk_adjusted": sl.get("atp_risk_adjusted", it["available"]),
            "next_po": (sl.get("purchase_orders") or [None])[0],
            "shrinkage": shrink_by_sku.get(sku),
            "deterministic_flags": sorted(flags),
            # The demo row: no flag fired, but the hole is still coming.
            "predictive_only": bool(not flags and stockout > 0.35),
        })

    ranked.sort(key=lambda r: -r["risk_score"])
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i

    return {
        "weights": weights,
        "ranked": ranked,
        "summary": {
            "scored": len(ranked),
            "critical": sum(1 for r in ranked if r["band"] == "CRITICAL"),
            "high": sum(1 for r in ranked if r["band"] == "HIGH"),
            "predictive_only": sum(1 for r in ranked if r["predictive_only"]),
            "top_10_revenue_at_risk": round(sum(r["revenue_at_risk"] for r in ranked[:10]), 2),
        },
    }
