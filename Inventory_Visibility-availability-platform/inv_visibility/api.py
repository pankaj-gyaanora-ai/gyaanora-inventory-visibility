"""Read-only REST API for the ops dashboard.

Deliberately serves the LAST GOOD PUBLISHED RUN rather than computing on
request. Two consequences that matter:
  * the read path is fast and stable under dashboard load
  * a failing 06:00 run can never make the dashboard show inflated numbers
Every response carries run_id and `stale`, so the dashboard can grey itself out
rather than quietly lying.

    pip install fastapi uvicorn
    uvicorn inv_visibility.api:app --reload
    -> interactive OpenAPI docs at http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from . import __version__
from .engine import run as engine_run, write_outputs

RUN_DIR = Path("runs/latest")
CONFIG = "config/sources.yaml"

app = FastAPI(
    title="Inventory Visibility Availability API",
    version=__version__,
    description="One true available-inventory number per SKU, with lineage.",
)


def _load() -> dict:
    path = RUN_DIR / "run.json"
    if not path.exists():
        raise HTTPException(503, "No published run yet. Run `inv-visibility reconcile` first.")
    return json.loads(path.read_text())


def _envelope(data: dict) -> dict:
    generated = datetime.fromisoformat(data["generated_at"])
    age_h = (datetime.now(timezone.utc) - generated).total_seconds() / 3600
    return {"run_id": data["run_id"], "as_of": data["generated_at"],
            "stale": age_h > data["config"]["run"]["staleness_sla_hours"],
            "status": data["status"]}


@app.get("/healthz", tags=["ops"])
def healthz():
    """Liveness plus staleness of the published data."""
    try:
        data = _load()
    except HTTPException:
        return {"status": "no_data", "engine_version": __version__}
    env = _envelope(data)
    return {"status": "degraded" if env["stale"] or data["status"] != "OK" else "ok",
            "engine_version": __version__, **env,
            "critical_exceptions": data["summary"]["critical"]}


@app.get("/v1/inventory/available", tags=["inventory"])
def available(sku: str = Query(..., description="Comma separated SKUs")):
    """The one true number, in bulk. This is what the order-entry screen calls."""
    data = _load()
    wanted = {s.strip().upper() for s in sku.split(",")}
    index = {i["sku"]: i for i in data["items"]}
    items = [{"sku": s, "available": index[s]["available"], "atp_7d": index[s]["atp_7d"],
              "confidence": index[s]["confidence"], "flags": index[s]["flags"]}
             if s in index else {"sku": s, "available": 0, "confidence": "unknown",
                                 "flags": ["SKU_NOT_FOUND"]}
             for s in sorted(wanted)]
    return {**_envelope(data), "items": items}


@app.get("/v1/inventory", tags=["inventory"])
def inventory(flag: str | None = None, confidence: str | None = None,
              limit: int = 200, offset: int = 0):
    """Paged listing for the dashboard grid."""
    data = _load()
    rows = data["items"]
    if flag:
        rows = [i for i in rows if any(f.startswith(flag) for f in i["flags"])]
    if confidence:
        rows = [i for i in rows if i["confidence"] == confidence]
    return {**_envelope(data), "total": len(rows), "items": rows[offset:offset + limit]}


@app.get("/v1/inventory/{sku}", tags=["inventory"])
def inventory_detail(sku: str):
    """Full breakdown, by-location split, lineage evidence and flags."""
    data = _load()
    item = next((i for i in data["items"] if i["sku"] == sku.upper()), None)
    if not item:
        raise HTTPException(404, f"{sku} not present in run {data['run_id']}")
    return {**_envelope(data), "item": item}


@app.get("/v1/exceptions", tags=["exceptions"])
def exceptions(severity: str | None = None, flag: str | None = None, limit: int = 200):
    """The morning worklist."""
    data = _load()
    rows = data["exceptions"]
    if severity:
        rows = [e for e in rows if e["severity"] == severity]
    if flag:
        rows = [e for e in rows if e["flag"] == flag]
    return {**_envelope(data), "summary": data["summary"],
            "total": len(rows), "exceptions": rows[:limit]}


@app.get("/v1/sources", tags=["ops"])
def sources():
    """Registered feeds, what each one owns, and how fresh it is."""
    data = _load()
    return {**_envelope(data), "sources": data["sources"]}


@app.get("/v1/runs/latest", tags=["ops"])
def latest_run():
    """Manifest: input hashes, row counts, quarantine rate, KPIs."""
    data = _load()
    return {**_envelope(data), "manifest": data["manifest"],
            "summary": data["summary"], "kpis": data["kpis"]}


@app.post("/v1/runs", tags=["ops"], status_code=202)
def trigger_run():
    """Out-of-band recompute. Idempotent: same inputs produce the same run."""
    result = engine_run(CONFIG)
    write_outputs(result, RUN_DIR)
    return {"run_id": result["run_id"], "status": result["status"],
            "summary": result["summary"], "exit_code": result["exit_code"]}
