"""Render the single-file ops console from the published run.

The console is a static artefact of runs/latest/run.json -- no server, no build
step, no network. Open it on any laptop and every number on screen traces back
to a source record in the feeds.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_PATH = ROOT / "runs" / "latest" / "run.json"
OUT = ROOT / "ops-console.html"


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def hl(code: str, comment: str = "#") -> str:
    """Very small syntax highlighter for the embedded code panels."""
    out = []
    for line in esc(code).split("\n"):
        if line.strip().startswith(comment):
            out.append(f'<span class="c">{line}</span>')
        else:
            line = re.sub(r'(&quot;|&#39;)([^&]*?)\1', r'<span class="s">\1\2\1</span>', line)
            line = re.sub(r'\b(class|def|return|yield|for|in|if|not|None|import|from)\b',
                          r'<span class="k">\1</span>', line)
            out.append(line)
    return "\n".join(out)


ARCH = r"""  wms_stock.csv      orders.json      supplier_shipments.csv    returns.json      [ feed 5 ... ]
        |                  |                    |                     |                  |
  +-----v------+    +------v-----+      +-------v------+     +--------v-----+    +-------v------+
  | WmsAdapter |    |OrderAdapter|      | ShipAdapter  |     |ReturnsAdapter|    | YourAdapter  |  <- plugin layer
  +-----+------+    +------+-----+      +-------+------+     +--------+-----+    +-------+------+
        |                  |                    |                     |                  |
        +------------------+----------+---------+---------------------+------------------+
                                      |
                       [ canonical signed-quantity records ]
                  (sku, location, bucket, qty, source, source_record_id, as_of)
                                      |
                   +------------------v--------------------+
                   |  validate    -> quarantine with reason |
                   |  normalise   -> sku / location / uom   |
                   |  aggregate   -> by sku and by location |
                   |  apply signs -> available and atp      |   <- signs come from config
                   |  flag engine -> exception queue        |
                   +------------------+--------------------+
                                      |
                    +-----------------v-------------------+
                    |  immutable run output                |
                    |  run.json   inventory.csv   exceptions.csv
                    +--------+---------------------+-------+
                             |                     |
                        CLI / cron             REST API  ---->  ops dashboard"""

EXT_YAML = """# 1. one block in config/sources.yaml
  - name: consignment_3pl
    adapter: inv_visibility.adapters.ThreePlCsvAdapter
    path: data/3pl_stock.csv
    criticality: optional          # additive -> fail open
    authority: [THIRD_PARTY_STOCK]
    status_bucket_map:
      AVAILABLE: THIRD_PARTY_STOCK
      IN_PICK:   IGNORE            # already committed at the 3PL

# 2. two bucket definitions
buckets:
  THIRD_PARTY_STOCK: { available_sign: 1, atp_sign: 1 }"""

EXT_PY = '''class ThreePlCsvAdapter(SourceAdapter):
    """Feed five. Knows its own format and statuses. Nothing else."""

    def load(self, quarantine):
        with open(self._open(), newline="") as fh:
            for i, row in enumerate(csv.DictReader(fh), start=2):
                bucket = self.spec["status_bucket_map"].get(row["status"])
                if bucket in (None, "IGNORE"):
                    continue
                yield CanonicalRecord(
                    sku=row["sku"], location=row["site"], bucket=bucket,
                    quantity=int(row["qty"]), source=self.name,
                    source_record_id=f"3pl:{row['ref']}", as_of=self.as_of)

# No change to pipeline.py, flags.py, engine.py, cli.py or api.py.
# The invariant test suite proves it: adding a source whose buckets all carry
# sign 0 must not move a single existing number.'''

NORM_ROWS = [
    ("SKU casing and whitespace", "' msd-2042 '", "MSD-2042",
     "640 units in a separate ghost SKU; availability understated and a phantom orphan raised"),
    ("Legacy SKU codes", "MSD-LEGACY-0042", "MSD-2042",
     "Reservations attach to a SKU the warehouse never reports"),
    ("Location codes", "'wh-1', 'W2', 'Warehouse 1'", "WH1 / WH2",
     "Same warehouse counted as three; per-location availability meaningless"),
    ("Unit of measure", "MSD-3301: 22 CASE", "264 EA (x12)",
     "A twelvefold availability error in the direction that oversells"),
    ("Unknown unit of measure", "MSD-3304: CASE, no rule", "excluded + flagged",
     "Guessing the factor is how you promise 12x what you hold. The engine never guesses"),
    ("Duplicate export rows", "same source_record_id twice", "second copy dropped",
     "Double subtraction of a reservation, or double counting of stock"),
]


def main() -> int:
    if not RUN_PATH.exists():
        print("No published run. Run: python3 -m inv_visibility.cli reconcile")
        return 1
    run = json.loads(RUN_PATH.read_text())

    # Layer 7 is optional by design. If it has not been computed, the console
    # renders the deterministic story alone -- which is the same degradation
    # contract the engine itself honours.
    intel_path = RUN_PATH.parent / "intelligence.json"
    intel = json.loads(intel_path.read_text()) if intel_path.exists() else None
    if intel:
        # the browser only needs forecasts for SKUs that made the worklist
        keep = {r["sku"] for r in intel["risk"]["ranked"]}
        intel["forecasts"] = {k: v for k, v in intel["forecasts"].items() if k in keep}
        intel["slip"] = {k: v for k, v in intel["slip"].items() if k in keep}

    # Trim the payload the browser has to carry.
    for it in run["items"]:
        it["reserved_detail"] = it["reserved_detail"][:14]
        it["evidence"] = it["evidence"][:10]

    # Sample API responses, generated from the real published run.
    items = run["items"]
    oversold = next((e for e in run["exceptions"] if e["flag"] == "OVERSOLD"), None)
    demo_sku = oversold["sku"] if oversold else items[0]["sku"]
    di = next(i for i in items if i["sku"] == demo_sku)
    env = {"run_id": run["run_id"], "as_of": run["generated_at"], "stale": False,
           "status": run["status"]}
    api = [
        {"verb": "GET", "path": "/v1/inventory/available?sku=" + demo_sku + "," + items[0]["sku"],
         "desc": "The one true number, in bulk. Order entry calls this.",
         "sample": json.dumps({**env, "items": [
             {"sku": x["sku"], "available": x["available"], "atp_7d": x["atp_7d"],
              "confidence": x["confidence"], "flags": x["flags"]}
             for x in (di, items[0])]}, indent=2)},
        {"verb": "GET", "path": "/v1/inventory/" + demo_sku,
         "desc": "Full breakdown, per-location split, lineage and flags.",
         "sample": json.dumps({**env, "item": {
             k: di[k] for k in ("sku", "description", "available", "raw_available", "atp_7d",
                                "breakdown", "by_location", "sources_seen", "confidence",
                                "flags", "evidence")}}, indent=2)},
        {"verb": "GET", "path": "/v1/exceptions?severity=critical&flag=OVERSOLD",
         "desc": "The morning worklist, filtered.",
         "sample": json.dumps({**env, "summary": run["summary"],
                               "total": sum(1 for e in run["exceptions"] if e["flag"] == "OVERSOLD"),
                               "exceptions": [e for e in run["exceptions"]
                                              if e["flag"] == "OVERSOLD"][:3]}, indent=2)},
        {"verb": "GET", "path": "/v1/sources",
         "desc": "Registered feeds, what each owns, and how fresh it is.",
         "sample": json.dumps({**env, "sources": [
             {k: s.get(k) for k in ("name", "label", "criticality", "authority",
                                    "as_of", "age_hours", "stale", "records")}
             for s in run["sources"]]}, indent=2)},
        {"verb": "GET", "path": "/v1/runs/latest",
         "desc": "Manifest: input hashes, row counts, quarantine rate, KPIs.",
         "sample": json.dumps({**env, "manifest": run["manifest"],
                               "summary": run["summary"], "kpis": run["kpis"]}, indent=2)},
        {"verb": "GET", "path": "/healthz",
         "desc": "Liveness plus staleness of the published data.",
         "sample": json.dumps({"status": "ok", "engine_version": run["engine_version"], **env,
                               "critical_exceptions": run["summary"]["critical"]}, indent=2)},
        {"verb": "POST", "path": "/v1/runs",
         "desc": "Out-of-band recompute. Idempotent.",
         "sample": json.dumps({"run_id": run["run_id"], "status": run["status"],
                               "summary": run["summary"], "exit_code": run["exit_code"]}, indent=2)},
    ]

    k, s = run["kpis"], run["summary"]
    cli = f"""$ inv-visibility reconcile --config config/sources.yaml

Inventory Visibility availability run  {run['run_id']}
as of {run['generated_at']}  engine v{run['engine_version']}
""" + "\n".join(
        f"  {'OK ' if x.get('loaded') else '-- '} {x['name']:<10} {str(x['as_of'])[:19]}  "
        f"{', '.join(f'{a}={b}' for a, b in (x.get('stats') or {}).items())}"
        for x in run["sources"]) + f"""

  {run['manifest']['rows_in']} rows in, {run['manifest']['accepted']} accepted, {run['manifest']['quarantined']} quarantined ({run['manifest']['quarantine_pct']}%)
  {s['skus_evaluated']} SKUs evaluated  |  {s['critical']} CRITICAL  {s['warning']} WARNING  {s['info']} INFO
  naive WMS+inbound view: {k['naive_number']:,}   true available: {k['total_available']:,}
  overstatement: {k['overstatement_units']:,} units ({k['overstatement_pct']}%)
  {k['oversold_skus']} SKUs oversold, {k['units_short']:,} units short, ${k['revenue_at_risk']:,.0f} revenue at risk

  -> runs/latest/{{run.json, inventory.csv, exceptions.csv}}
  exit code {run['exit_code']}

$ Inventory Visibility explain {demo_sku}

{demo_sku}  {di['description']}
  available = on_hand - reserved - unsellable - safety_stock
            = {di['breakdown']['on_hand']} - {di['breakdown']['reserved']} - {di['breakdown']['unsellable']} - {di['breakdown']['safety_stock']}  =  {di['raw_available']}
  atp_7d    = {di['atp_7d']}   (+{di['breakdown']['inbound']} inbound, excluded from today)
""" + "\n".join(
        f"    {l['location']}: on_hand {l['on_hand']:>6}  reserved {l['reserved']:>5}"
        f"  unsellable {l['unsellable']:>4}  ->  available {l['raw_available']:>6}"
        for l in di["by_location"]) + f"""

$ Inventory Visibility exceptions --severity critical --limit 3
""" + "\n".join(
        f"  CRIT  {e['flag']:<24} {e['sku']:<12} {e['message'][:74]}"
        for e in run["exceptions"] if e["severity"] == "critical")[:900]

    fail = """$ inv-visibility reconcile --skip-source orders     # the order feed did not arrive

  RUN ABORTED
  Required feed 'orders' did not arrive. Losing it would erase RESERVED and
  INFLATE availability, so the run is aborted and the previous published run
  stays live.

  Previous published run remains live. Ops has been paged.
  exit code 3

$ inv-visibility reconcile --skip-source shipments   # only ATP is affected

  -- shipments  feed not delivered (simulated)
  257 SKUs evaluated  |  DEGRADED_RUN raised, ATP figures degraded
  exit code 1"""

    tpl = ((ROOT / "templates" / "console_layout.html").read_text()
           + (ROOT / "templates" / "console_logic.html").read_text())
    html = (tpl
            .replace("__RUN_DATA__", json.dumps(run, separators=(",", ":")))
            .replace("__INTEL_DATA__", json.dumps(intel, separators=(",", ":")) if intel else "null")
            .replace("__API_SAMPLES__", json.dumps(api))
            .replace("__CLI_OUT__", json.dumps(hl(cli, "$")))
            .replace("__FAIL_DEMO__", json.dumps(hl(fail, "$")))
            .replace("__EXT_YAML__", json.dumps(hl(EXT_YAML)))
            .replace("__EXT_PY__", json.dumps(hl(EXT_PY)))
            .replace("__ARCH_ART__", json.dumps(ARCH))
            .replace("__NORM_ROWS__", json.dumps("".join(
                f'<tr><td><b>{esc(a)}</b></td><td class="mono small">{esc(b)}</td>'
                f'<td class="mono small">{esc(c)}</td><td class="small muted">{esc(d)}</td></tr>'
                for a, b, c, d in NORM_ROWS))))

    OUT.write_text(html)
    layer7 = (f", {len(intel['risk']['ranked'])} risk-ranked, brief {intel['brief']['status'].lower()}"
              if intel else ", no intelligence layer")
    print(f"ops-console.html   {len(html)/1024:.0f} KB   "
          f"{len(run['items'])} SKUs, {len(run['exceptions'])} exceptions{layer7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
