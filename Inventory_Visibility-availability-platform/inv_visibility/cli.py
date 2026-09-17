"""Command line interface -- for the 06:00 cron and for ops debugging.

Exit codes are the contract with the scheduler:
    0 clean | 1 warnings | 2 critical exceptions | 3 run aborted
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import run, write_outputs

C = {"red": "\033[91m", "yel": "\033[93m", "grn": "\033[92m", "dim": "\033[2m",
     "bold": "\033[1m", "cyan": "\033[96m", "end": "\033[0m"}


def _c(text, colour):
    return f"{C[colour]}{text}{C['end']}"


def cmd_reconcile(args) -> int:
    result = run(args.config, skip_sources=args.skip_source)
    print(_c(f"\nInventory Visibility availability run  {result['run_id']}", "bold"))
    print(_c(f"as of {result['generated_at']}  engine v{result['engine_version']}\n", "dim"))

    if result["status"] == "ABORTED":
        print(_c("  RUN ABORTED", "red"))
        print(f"  {result['abort_reason']}\n")
        print(_c("  Previous published run remains live. Ops has been paged.\n", "dim"))
        return result["exit_code"]

    for s in result["sources"]:
        if s.get("loaded"):
            mark, col = "OK ", "grn"
            stats = ", ".join(f"{k}={v}" for k, v in s["stats"].items())
        else:
            mark, col = "-- ", "yel"
            stats = s.get("error", "")
        print(f"  {_c(mark, col)} {s['name']:<10} {s['as_of']}  {_c(stats, 'dim')}")

    m = result["manifest"]
    print(f"\n  {m['rows_in']} rows in, {m['accepted']} accepted, "
          f"{m['quarantined']} quarantined ({m['quarantine_pct']}%)")

    k, sm = result["kpis"], result["summary"]
    print(f"  {sm['skus_evaluated']} SKUs evaluated  |  "
          f"{_c(str(sm['critical']) + ' CRITICAL', 'red')}  "
          f"{_c(str(sm['warning']) + ' WARNING', 'yel')}  {sm['info']} INFO")
    true_avail = _c(format(k["total_available"], ","), "cyan")
    overstate = _c(f"{k['overstatement_units']:,} units ({k['overstatement_pct']}%)", "red")
    print(f"  naive WMS+inbound view: {k['naive_number']:,}   "
          f"true available: {true_avail}   overstatement: {overstate}")
    print(f"  {k['oversold_skus']} SKUs oversold, {k['units_short']:,} units short, "
          f"${k['revenue_at_risk']:,.0f} revenue at risk")

    out = Path(args.out) / "latest"
    write_outputs(result, out)
    print(_c(f"\n  -> {out}/{{run.json, inventory.csv, exceptions.csv}}", "dim"))
    print(_c(f"  exit code {result['exit_code']}\n", "dim"))
    return result["exit_code"]


def cmd_intelligence(args) -> int:
    """Layer 7: forecasting, supplier slip, shrinkage, risk ranking, brief.

    Runs strictly on top of a published run. It cannot change availability.
    """
    from pathlib import Path as _P
    from .config import load_config
    from .intelligence import run_intelligence

    out = _P(args.out) / "latest"
    data = json.loads((out / "run.json").read_text())
    cfg = load_config(args.config)
    intel = run_intelligence(data, cfg, _P(cfg["_root"]))
    (out / "intelligence.json").write_text(json.dumps(intel, indent=2, default=str))

    q, r = intel["forecast_quality"], intel["risk"]["summary"]
    print(_c(f"\nIntelligence layer (advisory)  {intel['run_id']}", "bold"))
    print(_c("  deterministic availability untouched\n", "dim"))
    print(f"  forecast   {q['skus_forecast']} SKUs, median WAPE {q.get('median_wape')} "
          f"vs naive {q.get('median_naive_wape')}, beats naive on "
          f"{_c(str(q.get('beats_naive_pct')) + '%', 'grn')} of backtested SKUs")
    print(f"             {q.get('fell_back_to_naive')} SKUs fell back to the naive baseline "
          f"because the model did not beat it")
    print(f"             patterns: " + ", ".join(f"{k} {v}" for k, v in q["by_pattern"].items()))
    print(f"             models:   " + ", ".join(f"{k} {v}" for k, v in q["by_model"].items()))
    print(f"  suppliers  {len(intel['supplier_profiles'])} profiled from PO receipt history")
    print(f"  shrinkage  {len(intel['shrinkage'])} SKU/location pairs with unexplained loss")
    print(f"  risk       {r['scored']} scored | {_c(str(r['critical']) + ' CRITICAL', 'red')} "
          f"{_c(str(r['high']) + ' HIGH', 'yel')} | "
          f"{_c(str(r['predictive_only']) + ' predicted-only', 'cyan')} "
          f"(no deterministic flag fires, but a hole is coming)")

    print(_c("\n  Top of the worklist", "bold"))
    for row in intel["risk"]["ranked"][:5]:
        tag = _c("PREDICTED", "cyan") if row["predictive_only"] else _c("ACTIVE", "red")
        print(f"    {row['rank']}. {row['sku']:<11} score {row['risk_score']:>5}  "
              f"{row['band']:<8} {tag}  {', '.join(row['reason_codes'][:3])}")

    b = intel["brief"]
    print(_c(f"\n  Morning brief  (provider: {b['provider']}, {b['status']})", "bold"))
    g = b["guardrail"]
    mark = _c("PASS", "grn") if g["passed"] else _c("FAIL", "red")
    print(_c(f"  guardrail: {g['verified']}/{g['numerals_in_output']} numerals traced "
             f"to engine output  [{mark}{C['dim']}]", "dim"))
    for para in (b["text"] or "").split("\n\n"):
        print()
        for line in _wrap(para, 92):
            print("    " + line)
    print(_c(f"\n  -> {out}/intelligence.json\n", "dim"))
    return 0


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def cmd_explain(args) -> int:
    """Lineage on demand -- the answer to 'I don't believe this number'."""
    data = json.loads((Path(args.out) / "latest" / "run.json").read_text())
    sku = args.sku.upper()
    item = next((i for i in data["items"] if i["sku"] == sku), None)
    if not item:
        print(f"{sku} not found in {data['run_id']}")
        return 1
    b = item["breakdown"]
    print(_c(f"\n{sku}  {item['description']}", "bold"))
    print(_c(f"run {data['run_id']}  confidence={item['confidence']}\n", "dim"))
    print(f"  available = on_hand - reserved - unsellable - safety_stock")
    print(f"            = {b['on_hand']} - {b['reserved']} - {b['unsellable']} - {b['safety_stock']}"
          f"  =  {_c(str(item['raw_available']), 'cyan')}"
          + ("  -> published 0 (clamped)" if item["raw_available"] < 0 else ""))
    print(f"  atp_7d    = {item['atp_7d']}   (+{b['inbound']} inbound, excluded from today)\n")
    for l in item["by_location"]:
        print(f"    {l['location']}: on_hand {l['on_hand']:>6}  reserved {l['reserved']:>5}"
              f"  unsellable {l['unsellable']:>4}  ->  available "
              + _c(f"{l['available']:>6}", "grn"))
    if item["reserved_detail"]:
        print(_c("\n  reserved by:", "dim"))
        for r in item["reserved_detail"][:8]:
            pen = _c(" [OTIF penalty]", "red") if r.get("otif_penalty") else ""
            print(f"    {r['order_id']:<12} {r['qty']:>5} u  {r.get('customer','')}  "
                  f"{_c(r.get('status',''), 'dim')}{pen}")
    if item["inbound_detail"]:
        print(_c("\n  inbound:", "dim"))
        for d in item["inbound_detail"][:8]:
            print(f"    {d['po_id']:<12} {d['qty']:>5} u  eta {d['eta']}  {d.get('supplier','')}")
    if item["flags"]:
        print(_c(f"\n  flags: {', '.join(item['flags'])}", "yel"))
    print()
    return 0


def cmd_exceptions(args) -> int:
    data = json.loads((Path(args.out) / "latest" / "run.json").read_text())
    rows = [e for e in data["exceptions"]
            if (not args.severity or e["severity"] == args.severity)
            and (not args.flag or e["flag"] == args.flag)]
    print(_c(f"\n{len(rows)} exceptions  ({data['run_id']})\n", "bold"))
    for e in rows[:args.limit]:
        col = {"critical": "red", "warning": "yel", "info": "dim"}[e["severity"]]
        print(f"  {_c(e['severity'].upper()[:4], col):<12} {e['flag']:<24} {e['sku']:<14} {e['message']}")
        if e["suggested_action"]:
            print(_c(f"      -> {e['suggested_action']}", "dim"))
    print()
    return 0


def cmd_sources(args) -> int:
    data = json.loads((Path(args.out) / "latest" / "run.json").read_text())
    print(_c(f"\nFeed health  ({data['run_id']})\n", "bold"))
    for s in data["sources"]:
        state = "STALE" if s.get("stale") else ("OK" if s.get("loaded") else "MISSING")
        col = "red" if state != "OK" else "grn"
        print(f"  {_c(state, col):<12} {s['name']:<11} {s['criticality']:<9} "
              f"as_of {s['as_of']}  age {s.get('age_hours','?')}h  "
              f"{_c('owns ' + ','.join(s['authority']), 'dim')}")
    print()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser("inv_visibility", description="Inventory Visibility availability engine")
    p.add_argument("--config", default="config/sources.yaml")
    p.add_argument("--out", default="runs")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reconcile", help="run the full reconciliation")
    r.add_argument("--skip-source", action="append", default=[],
                   help="simulate a feed not arriving (demonstrates fail-closed policy)")
    r.set_defaults(func=cmd_reconcile)

    e = sub.add_parser("explain", help="show the arithmetic and lineage for one SKU")
    e.add_argument("sku")
    e.set_defaults(func=cmd_explain)

    x = sub.add_parser("exceptions", help="the morning worklist")
    x.add_argument("--severity", choices=["critical", "warning", "info"])
    x.add_argument("--flag")
    x.add_argument("--limit", type=int, default=30)
    x.set_defaults(func=cmd_exceptions)

    s = sub.add_parser("sources", help="feed freshness and ownership")
    s.set_defaults(func=cmd_sources)

    i = sub.add_parser("intelligence", help="advisory layer: forecast, risk, brief")
    i.set_defaults(func=cmd_intelligence)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
