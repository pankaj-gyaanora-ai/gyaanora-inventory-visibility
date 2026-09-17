"""Layer 7 -- the intelligence layer.

    Deterministic core, probabilistic periphery.

Everything in this package sits **on top of** a completed reconciliation run and
never inside it. It reads `run.json` and writes `intelligence.json`. It cannot
change `available`, and if the whole package throws, the deterministic numbers
publish anyway with the advisory fields absent and the run marked degraded.

That ordering is the answer to "would you use AI here?". Yes -- but not in the
number. The number is what justifies declining a customer's order.

    Steps 1-6 (deterministic)              Layer 7 (advisory)
    ---------------------------            ---------------------------------
    adapters -> validate -> normalise      demand forecasting      (statistics)
      -> aggregate -> available/ATP        supplier slip           (statistics)
      -> flag engine                       shrinkage detection     (statistics)
             |                             risk scoring            (weighted config)
             |  run.json                   morning brief           (LLM)
             +---------------------------> schema mapping          (LLM, reviewed)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from ..config import parse_ts
from . import anomaly, forecast, llm, risk, supplier

__all__ = ["run_intelligence", "anomaly", "forecast", "llm", "risk", "supplier"]

HISTORY_WINDOW_DAYS = 180


def run_intelligence(run: dict, cfg: dict, root: Path,
                     provider: llm.LlmProvider | None = None) -> dict:
    """Compute the advisory layer for a published run.

    Each stage degrades independently: a missing history file costs you the
    forecast, not the run.
    """
    as_of = parse_ts(run["generated_at"]).replace(tzinfo=None)
    start = as_of - timedelta(days=HISTORY_WINDOW_DAYS)
    items = run["items"]
    hist = root / "data" / "history"
    degraded: list[str] = []

    try:
        forecasts = forecast.build(items, hist / "shipment_history.csv", as_of, start)
    except (FileNotFoundError, OSError) as exc:
        forecasts, _ = {}, degraded.append(f"forecast unavailable: {exc}")

    try:
        profiles = supplier.supplier_profiles(hist / "po_receipt_history.csv")
        slip = supplier.predict_arrivals(items, profiles, as_of,
                                         cfg["run"]["atp_horizon_days"])
    except (FileNotFoundError, OSError) as exc:
        profiles, slip = {}, {}
        degraded.append(f"supplier slip unavailable: {exc}")

    try:
        shrinkage = anomaly.detect(hist / "stock_ledger.csv")
    except (FileNotFoundError, OSError) as exc:
        shrinkage = []
        degraded.append(f"shrinkage detection unavailable: {exc}")

    scored = risk.score_all(items, run["exceptions"], forecasts, slip,
                            shrinkage, cfg, as_of)

    intel = {
        "run_id": run["run_id"],
        "generated_at": run["generated_at"],
        "layer": "advisory",
        "contract": ("Read-only over the published run. Nothing here can change "
                     "`available`; if this layer fails the deterministic numbers "
                     "publish regardless."),
        "degraded": degraded,
        "forecasts": forecasts,
        "supplier_profiles": profiles,
        "slip": slip,
        "shrinkage": shrinkage,
        "risk": scored,
    }

    intel["brief"] = llm.generate_brief(run, intel, provider)
    intel["mapping_assistant"] = llm.generate_mapping(cfg, provider)
    intel["forecast_quality"] = _quality(forecasts)
    return intel


def _quality(forecasts: dict) -> dict:
    """Model performance, aggregated. If the models cannot beat the naive
    baseline overall, that is a finding to report, not to hide."""
    scored = [f for f in forecasts.values() if f.get("backtest_wape") is not None]
    if not scored:
        return {"skus_forecast": 0}
    beat = [f for f in scored if f.get("beats_naive")]
    patterns: dict[str, int] = {}
    models: dict[str, int] = {}
    for f in forecasts.values():
        patterns[f["demand_pattern"]] = patterns.get(f["demand_pattern"], 0) + 1
        models[f["model"]] = models.get(f["model"], 0) + 1
    return {
        "skus_forecast": len(forecasts),
        "skus_backtested": len(scored),
        "median_wape": round(sorted(f["backtest_wape"] for f in scored)[len(scored) // 2], 3),
        "median_naive_wape": round(sorted(f["naive_wape"] for f in scored)[len(scored) // 2], 3),
        "beats_naive_pct": round(len(beat) / len(scored) * 100, 1),
        "fell_back_to_naive": sum(1 for f in scored if f.get("fell_back_to_naive")),
        "by_pattern": dict(sorted(patterns.items(), key=lambda kv: -kv[1])),
        "by_model": dict(sorted(models.items(), key=lambda kv: -kv[1])),
    }
