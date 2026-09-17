"""Orchestration: the one place a run is defined.

The CLI and the API are both thin callers of this module. Logic is never
duplicated between them.
"""
from __future__ import annotations

import csv
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adapters import build_adapter
from .config import load_config, parse_ts
from .flags import evaluate
from .models import SourceLoadError
from .pipeline import aggregate, normalize


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else "MISSING"


def load_item_master(cfg, root) -> dict:
    path = root / cfg["item_master"]["path"]
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out[row["sku"].strip().upper()] = {
                "description": row.get("description", ""),
                "category": row.get("category", ""),
                "unit_cost": row.get("unit_cost") or 0,
                "unit_price": row.get("unit_price") or 0,
                "safety_stock": int(row.get("safety_stock") or 0),
                "abc_class": row.get("abc_class") or "C",
            }
    return out


def run(config_path: str, skip_sources: list[str] | None = None) -> dict:
    """Execute one full reconciliation. Idempotent: same inputs, same output."""
    cfg = load_config(config_path)
    root = Path(cfg["_root"])
    skip = set(skip_sources or [])
    as_of = parse_ts(cfg["run"]["as_of_override"]) if cfg["run"].get("as_of_override") \
        else datetime.now(timezone.utc)

    quarantine: list = []
    records: list = []
    sources_meta: list = []
    aborted_reason = None
    degraded: list[str] = []

    for spec in cfg["sources"]:
        meta = {"name": spec["name"], "label": spec.get("label", spec["name"]),
                "criticality": spec.get("criticality", "optional"),
                "as_of": spec["export_as_of"], "path": spec["path"],
                "sla_hours": spec.get("sla_hours", cfg["run"]["staleness_sla_hours"]),
                "authority": spec.get("authority", []), "loaded": False,
                "stats": {}, "error": None,
                "file_hash": _hash(root / spec["path"])}
        if spec["name"] in skip:
            meta["error"] = "feed not delivered (simulated)"
            sources_meta.append(meta)
            # Fail closed on subtractive feeds, fail open on additive ones.
            if meta["criticality"] == "required":
                aborted_reason = (
                    f"Required feed '{spec['name']}' did not arrive. Losing it would erase "
                    f"{'/'.join(spec.get('authority', []))} and INFLATE availability, so the run "
                    f"is aborted and the previous published run stays live.")
            else:
                degraded.append(spec["name"])
            continue
        try:
            adapter = build_adapter(spec, root)
            loaded = list(adapter.load(quarantine))
            records.extend(loaded)
            meta.update(loaded=True, stats=adapter.stats, records=len(loaded))
        except (SourceLoadError, FileNotFoundError) as exc:
            meta["error"] = str(exc)
            if meta["criticality"] == "required":
                aborted_reason = f"Required feed '{spec['name']}' failed to load: {exc}"
            else:
                degraded.append(spec["name"])
        sources_meta.append(meta)

    run_id = f"run_{as_of.strftime('%Y-%m-%dT%H%M%SZ')}_{uuid.uuid5(uuid.NAMESPACE_DNS, str(as_of)).hex[:4]}"

    if aborted_reason:
        return {"run_id": run_id, "generated_at": as_of.isoformat(), "status": "ABORTED",
                "abort_reason": aborted_reason, "engine_version": __version__,
                "sources": sources_meta, "items": [], "exceptions": [],
                "summary": {"critical": 0, "warning": 0, "info": 0, "skus_evaluated": 0},
                "exit_code": 3, "config": _config_view(cfg)}

    rows_in = len(records) + len(quarantine)
    normalized, excluded = normalize(records, cfg, quarantine)
    q_pct = (len(quarantine) / rows_in * 100) if rows_in else 0.0
    if q_pct > cfg["run"]["quarantine_abort_pct"]:
        return {"run_id": run_id, "generated_at": as_of.isoformat(), "status": "ABORTED",
                "abort_reason": (f"Quarantine rate {q_pct:.1f}% exceeds the "
                                 f"{cfg['run']['quarantine_abort_pct']}% threshold. A broken export "
                                 f"must not silently become an availability error."),
                "engine_version": __version__, "sources": sources_meta, "items": [],
                "exceptions": [], "quarantine": [q.to_dict() for q in quarantine],
                "summary": {"critical": 0, "warning": 0, "info": 0, "skus_evaluated": 0},
                "exit_code": 3, "config": _config_view(cfg)}

    item_master = load_item_master(cfg, root)
    items = aggregate(normalized, cfg, item_master, as_of, excluded)
    exceptions = evaluate(items, cfg, sources_meta, quarantine, as_of, item_master)

    from .models import Exception_
    for d in degraded:
        exceptions.insert(0, Exception_(
            sku="*", flag="DEGRADED_RUN", severity="warning",
            message=f"Optional feed '{d}' was unavailable. Run completed; ATP figures are degraded.",
            evidence=[d], detail={"source": d},
            suggested_action=("Fail-open policy: an additive feed can only understate "
                              "availability, never oversell.")))

    summary = {"critical": sum(1 for e in exceptions if e.severity == "critical"),
               "warning": sum(1 for e in exceptions if e.severity == "warning"),
               "info": sum(1 for e in exceptions if e.severity == "info"),
               "skus_evaluated": len(items)}
    exit_code = 2 if summary["critical"] else (1 if summary["warning"] else 0)

    kpis = _kpis(items, exceptions)
    return {
        "run_id": run_id,
        "generated_at": as_of.isoformat(),
        "engine_version": __version__,
        "status": "DEGRADED" if degraded else "OK",
        "degraded_sources": degraded,
        "sources": sources_meta,
        "items": items,
        "exceptions": [e.to_dict() for e in exceptions],
        "quarantine": [q.to_dict() for q in quarantine],
        "summary": summary,
        "kpis": kpis,
        "exit_code": exit_code,
        "config": _config_view(cfg),
        "manifest": {"rows_in": rows_in, "accepted": len(normalized),
                     "quarantined": len(quarantine), "quarantine_pct": round(q_pct, 3),
                     "inputs": {s["name"]: s["file_hash"] for s in sources_meta}},
    }


def _config_view(cfg) -> dict:
    """The parts of config the dashboard shows -- the formula, made visible."""
    return {"buckets": cfg["buckets"], "run": cfg["run"],
            "sources": [{k: v for k, v in s.items() if k != "adapter"} for s in cfg["sources"]],
            "normalize": cfg["normalize"]}


def _kpis(items, exceptions) -> dict:
    oversold = [e for e in exceptions if e.flag == "OVERSOLD"]
    revenue_at_risk = sum(e.detail.get("revenue_at_risk", 0) for e in oversold)
    units_short = sum(e.detail.get("shortfall", 0) for e in oversold)
    penalty_orders = {o for e in oversold for o in e.detail.get("penalty_orders", [])}
    total_on_hand = sum((i["breakdown"]["on_hand"] or 0) for i in items)
    total_reserved = sum(i["breakdown"]["reserved"] for i in items)
    total_available = sum(i["available"] for i in items)
    naive = total_on_hand + sum(i["breakdown"]["inbound"] for i in items)
    return {
        "skus": len(items),
        "total_on_hand": total_on_hand,
        "total_reserved": total_reserved,
        "total_available": total_available,
        "total_inbound": sum(i["breakdown"]["inbound"] for i in items),
        "naive_number": naive,
        "overstatement_units": naive - total_available,
        "overstatement_pct": round((naive - total_available) / naive * 100, 1) if naive else 0,
        "oversold_skus": len(oversold),
        "units_short": units_short,
        "revenue_at_risk": round(revenue_at_risk, 2),
        "orders_with_penalty_exposure": len(penalty_orders),
        "stockout_skus": sum(1 for i in items if "STOCKOUT" in i["flags"]),
        "low_confidence_skus": sum(1 for i in items if i["confidence"] != "high"),
    }


def write_outputs(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps(result, indent=2, default=str))

    with open(out_dir / "inventory.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "description", "available", "raw_available", "atp_7d",
                    "on_hand", "reserved", "unsellable", "safety_stock", "inbound",
                    "confidence", "flags", "sources_seen"])
        for i in result.get("items", []):
            b = i["breakdown"]
            w.writerow([i["sku"], i["description"], i["available"], i["raw_available"],
                        i["atp_7d"], b["on_hand"], b["reserved"], b["unsellable"],
                        b["safety_stock"], b["inbound"], i["confidence"],
                        "|".join(i["flags"]), "|".join(i["sources_seen"])])

    with open(out_dir / "exceptions.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["severity", "flag", "sku", "message", "suggested_action", "evidence"])
        for e in result.get("exceptions", []):
            w.writerow([e["severity"], e["flag"], e["sku"], e["message"],
                        e["suggested_action"], "|".join(e["evidence"])])
