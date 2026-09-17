# Inventory_Visibility Distribution — Available Inventory Reconciliation Engine
### Interview-ready design doc: business problem → terminology → architecture → step-by-step with I/O → likely questions

---

## PART 1 — The business problem in plain language

### What's actually happening

Inventory_Visibility has three systems that each hold **one piece** of the inventory truth:

| System | Format | What it knows |
|---|---|---|
| WMS (Warehouse Management System) | CSV | How many units are **physically sitting on the shelf** |
| Order system | JSON | How many of those units are **already promised to customers** |
| Supplier shipments | CSV | How many units are **on the way but not here yet** |

Ops opens the WMS CSV, sees `ABC-100: 120`, and tells the sales team "we have 120."

But 40 of those 120 are already allocated to unshipped customer orders. The real sellable number is **80**. Sales sells 100. Now Inventory_Visibility has promised 140 units of a 120-unit pile.

That's an **oversell**. Consequences:
- Order gets cancelled or short-shipped → customer trust damage
- Retail/B2B contracts carry **chargebacks** (fulfilment penalties) for missing OTIF (On Time In Full) targets
- Ops burns an hour every morning VLOOKUP-ing three files together, and gets it wrong sometimes anyway

### The root cause (say this in the interview)

> This isn't a data volume problem or a technology problem. It's a **semantics** problem. Three systems each publish a number called "quantity," but the three numbers mean completely different things, and nothing in the company owns the definition of "available." The fix is to build one place that owns that definition, and to make every source declare *what kind* of quantity it is contributing.

### What "done" looks like

1. **One number per SKU** — `available` — computed by a defined, auditable formula
2. **Automatic exception flags** — negative stock, missing SKUs, cross-system mismatches, stale data
3. **API + CLI** — plugs into the existing dashboard, no manual spreadsheet step
4. **Source-count agnostic** — returns is coming next month; a 4th, 5th, 6th source must be config, not a rewrite

That last requirement is the real test in this brief. Anyone can write `on_hand - reserved + incoming`. The interviewer is watching whether your design survives the returns system.

---

## PART 2 — Business terminology (learn these, they will be used against you)

### Inventory states

| Term | Meaning | In our system |
|---|---|---|
| **SKU** (Stock Keeping Unit) | Unique code for a sellable item. `ABC-100` | Primary key |
| **On Hand (OH)** | Units physically in the building right now. Includes units already sold-but-not-shipped. | From WMS |
| **Reserved / Allocated / Committed** | On-hand units earmarked to an open customer order. Physically present, commercially spoken for. | From order system |
| **Available / ATS (Available To Sell)** | What you can safely promise **today**. `OH − Reserved − Safety Stock − Unsellable` | **Our output** |
| **ATP (Available To Promise)** | What you can promise **by a future date**, including inbound arriving before that date | Optional second output |
| **Incoming / Inbound / On Order** | Units on a supplier PO, not yet received. **Not available today.** | From shipments |
| **ASN (Advance Ship Notice)** | Supplier's "it's shipped, here's what and when" message | The shipment feed |
| **Safety Stock / Buffer** | Units deliberately held back to absorb demand spikes and count error | Config per SKU |
| **Unsellable / Quarantine / Blocked** | Physically present but not sellable — damaged, expired, awaiting QC | Returns will land here first |
| **Backorder** | Order accepted with no stock, to be filled later | Causes negative available |
| **Shrinkage** | Inventory lost to theft, damage, mis-picks — the reason cycle counts exist |  |
| **Cycle Count** | Periodic physical recount that corrects WMS |  |

### Order and document types

| Term | Meaning |
|---|---|
| **PO** (Purchase Order) | Inbound — Inventory_Visibility buying from a supplier |
| **SO** (Sales Order) | Outbound — a customer buying from Inventory_Visibility |
| **RMA** (Return Merchandise Authorization) | Approved customer return — the new source next month |
| **Goods Receipt / GRN** | The event where an inbound PO becomes on-hand |

### Business/ops metrics

| Term | Meaning |
|---|---|
| **Oversell** | Promising more units than are available. The bug we are fixing. |
| **Fill Rate** | % of ordered lines shipped complete |
| **OTIF** | On Time In Full — the contractual service metric |
| **Chargeback / Penalty** | Money the customer deducts when you miss OTIF |
| **Stockout** | Zero available while demand exists |

### Data engineering terms you should use

| Term | Meaning / why it matters here |
|---|---|
| **Canonical model** | One internal record shape all sources are translated into. The extensibility mechanism. |
| **Source of truth / system of record** | Which system wins for a given field. WMS wins for on-hand; order system wins for reserved. |
| **Snapshot / as-of timestamp** | Inventory is a point-in-time fact. Three files exported at three different times = **snapshot skew**. |
| **Idempotency** | Re-running on the same inputs produces the identical output. Required for trust and safe retries. |
| **Lineage / provenance** | For any number, which source record produced it. Required for ops to trust it. |
| **Quarantine / dead-letter** | Bad rows are set aside with a reason, not silently dropped and not crashing the run. |
| **Fail-closed vs fail-open** | Whether a missing input blocks the run or degrades it. Different answer per source type — see below. |

---

## PART 3 — The core model (the actual idea)

### 3.1 Don't write a 3-source formula. Write a ledger.

The naive design:

```python
available = wms.on_hand - orders.reserved + shipments.incoming   # DON'T
```

Two things are wrong with it:
1. `+ incoming` is a bug — inbound stock is not sellable today
2. Adding a returns source means editing this line, editing the loader, editing the API, editing the tests

The design that survives:

**Every source contributes signed quantity records into buckets. The formula is defined over buckets, in config — not over sources, in code.**

Canonical record:

```json
{
  "sku": "ABC-100",
  "location": "WH1",
  "bucket": "ON_HAND",
  "quantity": 120,
  "source": "wms",
  "source_record_id": "wms:ABC-100:WH1",
  "as_of": "2026-09-09T05:00:00Z",
  "meta": { "uom": "EA" }
}
```

Bucket catalogue (extensible):

| Bucket | Sign in `available` | Counted in ATP? | Contributed by |
|---|---|---|---|
| `ON_HAND` | `+` | yes | WMS |
| `RESERVED` | `−` | `−` | Order system |
| `SAFETY_STOCK` | `−` | `−` | Config |
| `UNSELLABLE` | `−` | `−` | WMS damage flag, returns QC |
| `INBOUND` | **0** (excluded) | `+` if ETA ≤ horizon | Supplier shipments |
| `RETURN_IN_TRANSIT` | **0** | optional | **Returns (next month)** |
| `RETURN_RECEIVED_PENDING_QC` | **0** | optional | **Returns (next month)** |

```
available = Σ(quantity × available_sign[bucket])
atp(d)    = available + Σ(INBOUND where eta ≤ d)
```

Now adding returns = **one adapter class + one config block + two bucket definitions.** Zero changes to the aggregation engine, the API, or the flag engine. That is the answer to "don't assume there will always be three sources."

### 3.2 The conservative-bias rule

State this explicitly — it's the judgement call the interviewer wants:

> The cost of overstating availability (chargebacks, lost customer) is much higher than the cost of understating it (a little deferred revenue). So every ambiguity resolves **downward**. Unknown on-hand → treat as 0 and flag. Stale reserved data → keep subtracting the last known value. Never let a data gap inflate a sellable number.

This directly implies:

**Fail-closed on subtractive sources, fail-open on additive sources.**

- The order system feed is missing → we'd lose all `RESERVED` → availability inflates → **abort the run, keep yesterday's published numbers, page ops.**
- The shipments feed is missing → we only lose `INBOUND`/ATP → **complete the run, mark ATP as degraded, flag it.**

### 3.3 Architecture

```
   wms_stock.csv     orders.json     shipments.csv     returns.json (soon)
        │                 │                │                 │
   ┌────▼─────┐     ┌─────▼────┐    ┌──────▼────┐     ┌──────▼────┐
   │ WmsAdapter│     │OrderAdptr│    │ShipAdapter│     │RetAdapter │   ← plugin layer
   └────┬─────┘     └─────┬────┘    └──────┬────┘     └──────┬────┘
        └──────────────┬──┴────────────────┴─────────────────┘
                       ▼
              [ Canonical records ]
                       ▼
         ┌─────────────────────────────┐
         │ 2. Validate  → quarantine   │
         │ 3. Normalize (SKU/UoM/loc)  │
         │ 4. Aggregate by SKU+location│
         │ 5. Compute available / ATP  │
         │ 6. Flag engine (exceptions) │
         └──────────────┬──────────────┘
                        ▼
         ┌──────────────┴──────────────┐
         │  Immutable run output       │
         │  inventory.json / .csv      │
         │  exceptions.json            │
         │  run_manifest.json          │
         └──────┬───────────────┬──────┘
                ▼               ▼
              CLI            REST API  ──► Ops dashboard
```

One core library. The CLI and the API are both thin callers of it — never duplicate logic between them.

---

## PART 4 — Step by step, with real inputs and outputs

### The three input files

**`wms_stock.csv`** — WMS export, 05:00

```csv
sku,warehouse,on_hand,uom,damaged_qty,last_counted
ABC-100,WH1,120,EA,0,2026-09-09T05:00:00Z
ABC-100,WH2,30,EA,0,2026-09-09T05:00:00Z
abc-200 ,WH1,-5,EA,0,2026-09-09T05:00:00Z
DEF-300,WH1,10,CASE,0,2026-09-09T05:00:00Z
XYZ-900,WH1,50,EA,6,2026-09-09T05:00:00Z
```

**`orders.json`** — order system export, 06:00

```json
{
  "as_of": "2026-09-09T06:00:00Z",
  "orders": [
    { "order_id": "SO-1001", "status": "ALLOCATED",
      "lines": [{ "sku": "ABC-100", "warehouse": "WH1", "qty": 40 }] },
    { "order_id": "SO-1002", "status": "SHIPPED",
      "lines": [{ "sku": "ABC-100", "warehouse": "WH1", "qty": 10 }] },
    { "order_id": "SO-1003", "status": "PENDING",
      "lines": [{ "sku": "NEW-555",  "warehouse": "WH1", "qty": 5 }] },
    { "order_id": "SO-1004", "status": "CANCELLED",
      "lines": [{ "sku": "XYZ-900", "warehouse": "WH1", "qty": 20 }] },
    { "order_id": "SO-1005", "status": "ALLOCATED",
      "lines": [{ "sku": "XYZ-900", "warehouse": "WH1", "qty": 44 }] }
  ]
}
```

**`supplier_shipments.csv`** — 05:30

```csv
po_id,sku,warehouse,qty_ordered,qty_received,eta,status
PO-77,ABC-100,WH1,100,0,2026-09-12,IN_TRANSIT
PO-78,XYZ-900,WH1,50,50,2026-09-01,RECEIVED
PO-79,GHOST-1,WH1,20,0,2026-08-20,IN_TRANSIT
```

---

### Step 1 — Source adapters: read and translate

**Each adapter answers one question: what signed bucket quantities does my source contribute?**

Interface every source implements:

```python
class SourceAdapter(Protocol):
    name: str
    criticality: Literal["required", "optional"]   # drives fail-closed/fail-open
    def as_of(self) -> datetime: ...
    def load(self) -> Iterable[CanonicalRecord]: ...
```

Config (`sources.yaml`) — this is what changes when a source is added:

```yaml
sources:
  - name: wms
    adapter: adapters.wms.WmsCsvAdapter
    path: ./data/wms_stock.csv
    criticality: required
  - name: orders
    adapter: adapters.orders.OrderJsonAdapter
    path: ./data/orders.json
    criticality: required
    reserving_statuses: [PENDING, ALLOCATED, PICKING]   # business rule, not code
  - name: shipments
    adapter: adapters.shipments.ShipmentCsvAdapter
    path: ./data/supplier_shipments.csv
    criticality: optional
    open_statuses: [IN_TRANSIT, PARTIAL]
```

**The two double-count traps — call these out, they earn points:**

- `SO-1002` is `SHIPPED`. Those 10 units already physically left, so WMS on-hand of 120 **already excludes them.** Subtracting them again would understate by 10. → only `reserving_statuses` create `RESERVED` records.
- `PO-78` is `RECEIVED`. Those 50 units are already inside the WMS 50. Counting them as `INBOUND` would double-count. → only `open_statuses` create `INBOUND` records.

`SO-1004` is `CANCELLED` → releases its reservation → contributes nothing.

**Output of Step 1 (canonical records, abbreviated):**

| sku | location | bucket | qty | source | source_record_id |
|---|---|---|---|---|---|
| ABC-100 | WH1 | ON_HAND | 120 | wms | wms:ABC-100:WH1 |
| ABC-100 | WH2 | ON_HAND | 30 | wms | wms:ABC-100:WH2 |
| abc-200␣ | WH1 | ON_HAND | −5 | wms | wms:abc-200:WH1 |
| DEF-300 | WH1 | ON_HAND | 10 (CASE) | wms | wms:DEF-300:WH1 |
| XYZ-900 | WH1 | ON_HAND | 50 | wms | wms:XYZ-900:WH1 |
| XYZ-900 | WH1 | UNSELLABLE | 6 | wms | wms:XYZ-900:WH1:dmg |
| ABC-100 | WH1 | RESERVED | 40 | orders | SO-1001/1 |
| NEW-555 | WH1 | RESERVED | 5 | orders | SO-1003/1 |
| XYZ-900 | WH1 | RESERVED | 44 | orders | SO-1005/1 |
| ABC-100 | WH1 | INBOUND | 100 (eta 09-12) | shipments | PO-77 |
| GHOST-1 | WH1 | INBOUND | 20 (eta 08-20) | shipments | PO-79 |

---

### Step 2 — Validate, quarantine bad rows

Row-level schema validation (pydantic). A malformed row must never crash a 5,000-SKU run.

Input row: `ABC-100,WH1,twelve,EA,0,2026-09-09T05:00:00Z`

Output → quarantine, not exception:

```json
{ "source": "wms", "row": 7, "reason": "QTY_NOT_NUMERIC",
  "field": "on_hand", "raw_value": "twelve", "action": "row_dropped" }
```

And the run manifest records `wms: 5 rows in, 4 accepted, 1 quarantined`. If quarantine rate exceeds a threshold (say 2%), **abort** — a broken export shouldn't silently become a 2% availability error.

---

### Step 3 — Normalize

Three normalizations, all needed to make the join actually work:

| Problem | Input | Output |
|---|---|---|
| SKU casing/whitespace | `"abc-200 "` | `"ABC-200"` |
| SKU aliases (legacy codes) | `"ABC100"` → alias map | `"ABC-100"` |
| Unit of Measure | `DEF-300: 10 CASE`, 1 CASE = 12 EA | `120 EA` |
| Location codes | `"wh-1"`, `"WH1"`, `"Warehouse 1"` | `"WH1"` |

If a UoM has no conversion rule → **do not guess**. Emit `UOM_UNKNOWN`, exclude the SKU from published availability, and flag it. Guessing here is how you oversell by 12×.

---

### Step 4 + 5 — Aggregate and compute

Group by `(sku, location)`, then roll up to SKU.

`ABC-100 / WH1`:
```
ON_HAND      120
RESERVED     −40
UNSELLABLE     0
SAFETY_STOCK  −0
INBOUND        0  (excluded from available; eta 09-12)
──────────────────
available     80
atp_7d       180
```

**Final per-SKU output (`inventory.json`):**

```json
{
  "run_id": "run_2026-09-09T06:15:02Z_a4f9",
  "generated_at": "2026-09-09T06:15:02Z",
  "items": [
    {
      "sku": "ABC-100",
      "available": 110,
      "atp_7d": 210,
      "breakdown": { "on_hand": 150, "reserved": 40, "unsellable": 0,
                     "safety_stock": 0, "inbound": 100 },
      "by_location": [
        { "location": "WH1", "on_hand": 120, "reserved": 40, "available": 80,  "atp_7d": 180 },
        { "location": "WH2", "on_hand": 30,  "reserved": 0,  "available": 30,  "atp_7d": 30 }
      ],
      "sources_seen": ["wms", "orders", "shipments"],
      "confidence": "high",
      "flags": []
    },
    {
      "sku": "ABC-200",
      "available": 0,
      "breakdown": { "on_hand": -5, "reserved": 0, "inbound": 0 },
      "sources_seen": ["wms"],
      "confidence": "low",
      "flags": ["NEGATIVE_ON_HAND", "MISSING_IN_SOURCE:orders"]
    },
    {
      "sku": "XYZ-900",
      "available": 0,
      "raw_available": -44,
      "breakdown": { "on_hand": 50, "reserved": 44, "unsellable": 6, "inbound": 0 },
      "sources_seen": ["wms", "orders", "shipments"],
      "confidence": "high",
      "flags": ["OVERSOLD"]
    },
    {
      "sku": "NEW-555",
      "available": 0,
      "raw_available": -5,
      "breakdown": { "on_hand": null, "reserved": 5 },
      "sources_seen": ["orders"],
      "confidence": "low",
      "flags": ["ORPHAN_SKU_IN_ORDERS", "MISSING_IN_SOURCE:wms"]
    },
    {
      "sku": "GHOST-1",
      "available": 0,
      "breakdown": { "on_hand": null, "inbound": 20 },
      "sources_seen": ["shipments"],
      "confidence": "low",
      "flags": ["ORPHAN_SKU_IN_SHIPMENTS", "OVERDUE_INBOUND"]
    }
  ]
}
```

Three details worth defending out loud:

1. **`available` is clamped at 0, but `raw_available` keeps the negative.** Ops must never see a sellable number below zero — but the −44 *is* the business signal (that's the exposure they have to fix). Publish both.
2. **`XYZ-900`: 50 on hand, 6 damaged, 44 reserved.** On the old spreadsheet method ops would read "50, we're fine." Actually 0 available and a hidden shortfall. This is exactly the oversell they described.
3. **`on_hand: null` is not `on_hand: 0`.** Null means *unknown* — a different problem with a different fix (missing SKU master sync) than a genuine zero.

---

### Step 6 — Flag engine (the exception report)

Rules are data, not `if` statements scattered in the aggregator. Each emits SKU + severity + reason + evidence.

| Flag | Trigger | Severity | Business meaning |
|---|---|---|---|
| `OVERSOLD` | `reserved > on_hand − unsellable` | **CRITICAL** | You have already promised units you don't have. Chargeback risk **today**. |
| `NEGATIVE_ON_HAND` | WMS on_hand < 0 | CRITICAL | WMS data error; needs a cycle count |
| `ORPHAN_SKU_IN_ORDERS` | SKU reserved, no WMS record | CRITICAL | Selling something the warehouse doesn't know exists |
| `ORPHAN_SKU_IN_SHIPMENTS` | Inbound for unknown SKU | WARNING | New product not yet in item master |
| `MISSING_IN_SOURCE:<src>` | SKU absent from one source | INFO/WARNING | Coverage gap; may be legitimate |
| `STALE_SOURCE` | `as_of` older than SLA (e.g. 6h) | CRITICAL | Numbers may be based on yesterday |
| `SNAPSHOT_SKEW` | max−min `as_of` across sources > threshold | WARNING | The three files describe three different moments |
| `DUPLICATE_RECORD` | same `source_record_id` twice | WARNING | Export bug; risks double-subtraction |
| `UOM_UNKNOWN` | No conversion rule | CRITICAL | Excluded from publish rather than guessed |
| `OVERDUE_INBOUND` | `eta < today`, still open | WARNING | ATP is inflated by a PO that never landed |
| `BELOW_SAFETY_STOCK` | `available < safety_stock` | WARNING | Reorder trigger |
| `QTY_MISMATCH` | Two sources both claim on-hand, differ | WARNING | Precedence rule applied; needs investigation |

```json
{
  "run_id": "run_2026-09-09T06:15:02Z_a4f9",
  "summary": { "critical": 3, "warning": 2, "info": 1, "skus_evaluated": 5 },
  "exceptions": [
    { "sku": "XYZ-900", "flag": "OVERSOLD", "severity": "critical",
      "shortfall": 44, "sellable_on_hand": 44, "reserved": 44,
      "evidence": ["wms:XYZ-900:WH1", "SO-1005/1", "wms:XYZ-900:WH1:dmg"],
      "suggested_action": "Expedite PO or contact customer on SO-1005" },
    { "sku": "NEW-555", "flag": "ORPHAN_SKU_IN_ORDERS", "severity": "critical",
      "reserved": 5, "evidence": ["SO-1003/1"],
      "suggested_action": "Create item master record or cancel SO-1003" }
  ]
}
```

The `evidence` array is the lineage. When ops says "I don't believe this number," you click through to the exact source rows. That's how the system earns trust in month one.

---

### Step 7 — Expose it: CLI and API

**CLI** (for the nightly cron and for ops debugging):

```bash
# Full run
$ Inventory_Visibility reconcile --config sources.yaml --out ./runs/
✔ wms         2026-09-09T05:00Z   5 rows,  1 quarantined
✔ orders      2026-09-09T06:00Z   5 orders, 3 reserving
✔ shipments   2026-09-09T05:30Z   3 POs,    2 open
→ 5 SKUs evaluated | 3 CRITICAL, 2 WARNING
→ ./runs/run_2026-09-09T06:15:02Z_a4f9/{inventory.json,exceptions.csv,manifest.json}
exit code: 2   (nonzero when CRITICAL exists → cron alerts)

# Single SKU, human readable
$ Inventory_Visibility explain ABC-100
ABC-100  available=110  atp_7d=210
  WH1: on_hand 120 − reserved 40 = 80   (+100 inbound PO-77 eta 09-12)
  WH2: on_hand  30 − reserved  0 = 30

# Dry-run a config change before trusting it
$ Inventory_Visibility reconcile --config sources.yaml --dry-run --diff-against last
```

Exit codes matter: `0` clean, `1` warnings, `2` criticals, `3` run aborted (required source missing). Cron and CI can act on those.

**API** (FastAPI — for the dashboard):

```
GET  /v1/inventory/available?sku=ABC-100,XYZ-900   → the one true number, bulk
GET  /v1/inventory/ABC-100                          → full breakdown + by_location + flags
GET  /v1/exceptions?severity=critical&flag=OVERSOLD → the morning worklist
GET  /v1/runs/latest | /v1/runs/{run_id}            → manifest, source as_of, counts
POST /v1/runs                                        → trigger an out-of-band recompute
GET  /v1/sources                                     → registered sources + freshness
GET  /healthz                                        → liveness + staleness of published data
```

```json
GET /v1/inventory/available?sku=ABC-100
{
  "run_id": "run_2026-09-09T06:15:02Z_a4f9",
  "as_of": "2026-09-09T06:15:02Z",
  "stale": false,
  "items": [{ "sku": "ABC-100", "available": 110, "confidence": "high" }]
}
```

The API **serves the last good published run** — it doesn't compute on request. Read path is fast and stable; a failing 06:00 run can never make the dashboard show inflated numbers. Every response carries `run_id` and `stale`, so the dashboard can grey itself out when the data is old rather than lying quietly.

---

### Step 8 — Adding the returns system (the requirement that matters)

Next month, returns arrives:

```json
{ "as_of": "...", "returns": [
  { "rma_id": "RMA-9", "sku": "ABC-100", "warehouse": "WH1",
    "qty": 3, "status": "IN_TRANSIT" },
  { "rma_id": "RMA-10", "sku": "ABC-100", "warehouse": "WH1",
    "qty": 2, "status": "RECEIVED_PENDING_QC" },
  { "rma_id": "RMA-11", "sku": "ABC-100", "warehouse": "WH1",
    "qty": 4, "status": "QC_PASSED_RESTOCKED" }
]}
```

Total change required:

**1. Config:**
```yaml
  - name: returns
    adapter: adapters.returns.ReturnsJsonAdapter
    path: ./data/returns.json
    criticality: optional
    status_bucket_map:
      IN_TRANSIT:            RETURN_IN_TRANSIT
      RECEIVED_PENDING_QC:   RETURN_PENDING_QC
      QC_PASSED_RESTOCKED:   IGNORE     # already counted in WMS on_hand
```

**2. Buckets:**
```yaml
buckets:
  RETURN_IN_TRANSIT:   { available_sign: 0, atp_sign: 0 }
  RETURN_PENDING_QC:   { available_sign: 0, atp_sign: 0 }
```

**3. A ~30 line adapter class.**

Nothing else. No change to validation, normalization, aggregation, the available formula, the flag engine, the CLI, or the API. Note `QC_PASSED_RESTOCKED → IGNORE`: restocked returns are already inside the WMS count — the same double-count trap as `RECEIVED` POs, caught by the same mechanism.

If the business later decides pending-QC returns should count toward 14-day ATP, that's `atp_sign: +1` in a YAML file, reviewed by ops, no deployment of new logic.

---

## PART 5 — Questions the interviewer will ask (with answers)

**Q: Why not `on_hand − reserved + incoming`?**
Because inbound isn't sellable today. Mixing it into `available` recreates the exact oversell they're paying penalties for. Keep it as a separate `atp` field with a date horizon so the dashboard can show "80 now, 180 by Friday."

**Q: A SKU is in orders but not in WMS. What do you do?**
Do not default on-hand to 0 silently and do not default to "assume it exists." Set `available = 0`, `on_hand = null`, flag `ORPHAN_SKU_IN_ORDERS` as CRITICAL. Null and zero are different failures with different remediations — one is an item-master sync problem, the other is genuinely out of stock.

**Q: The order system file didn't arrive. Run or abort?**
Abort. Missing `RESERVED` inflates availability, which is the failure mode with the real cost. Keep serving the previous run marked stale, and alert. If the *shipments* file is missing, complete the run — you only lose ATP, which is additive. **Fail-closed on subtractive sources, fail-open on additive ones.**

**Q: Two systems both report on-hand and disagree.**
Precedence is declared in config (`on_hand.authority: wms`), the winner is used, and `QTY_MISMATCH` is flagged with both values so someone reconciles the systems. Never average them — an averaged number is wrong in both systems and traceable to neither.

**Q: The three files are exported at 05:00, 05:30 and 06:00. Is that a problem?**
Yes — snapshot skew. An order placed at 05:30 is in the orders file but the WMS file predates any allocation. Short term: measure the skew, flag it above a threshold, and let the conservative bias absorb it (we subtract reservations WMS hasn't seen, which understates — the safe direction). Properly: get all sources to export against a common snapshot time, or move to event-driven with a watermark.

**Q: Batch or real-time?**
Batch first — the stated need is "ops isn't doing spreadsheets every morning," and thousands of SKUs recompute in seconds. Design for the upgrade though: because the core is a signed-record ledger, moving to a CDC/event stream means changing how records arrive, not what they mean. Bear in mind that a 6-hour-old availability number can still oversell; if their order volume warrants it, an intraday cadence or a reservation hook is phase 2.

**Q: How do you make it idempotent?**
Full recompute from immutable snapshots each run, keyed by `run_id`, with input file hashes in the manifest. No incremental deltas, no in-place mutation. Same inputs → byte-identical output. Re-running after a failure is always safe.

**Q: How do you test it?**
Three layers. Golden-file tests per adapter (fixture in → expected canonical records out). Scenario tests for the nasty cases: shipped-order double count, received-PO double count, cancelled order, negative on-hand, orphan SKU, unknown UoM, duplicate row. And property/invariant tests: `available ≤ on_hand` always; `sum(by_location.available) == available`; `available ≥ 0`; adding a source with all-zero-sign buckets never changes any existing number.

**Q: Scale? 5,000 SKUs vs 5 million?**
At thousands, pandas or polars in memory, single process, seconds. The adapter + canonical-record boundary means at millions you swap the aggregation step for SQL in their warehouse (or dbt models) and keep every adapter, flag rule and API contract unchanged. Don't build for 5M on day one.

**Q: How will ops trust the number when it contradicts what they've been reading?**
Lineage. Every published number drills down to the source records that produced it, plus a `Inventory_Visibility explain <sku>` command showing the arithmetic. Run it in shadow mode for two weeks alongside the manual spreadsheet, and reconcile the differences — every difference will be a real oversell they were missing, which is also the ROI evidence for the project.

**Q: What are the failure modes of your own system?**
A silently stale publish (mitigated by `stale` flag + healthz + staleness alerting), a config error changing a bucket sign (mitigated by config in version control, `--dry-run --diff-against last`, and CI invariants), and quarantine drift where a slowly-degrading export loses rows unnoticed (mitigated by quarantine-rate thresholds that abort the run).

**Q: How do you measure success?**
Overselling incidents per month → target zero. Fulfilment penalty spend. Minutes of ops manual reconciliation per day → target zero. Count of open CRITICAL exceptions trending down (that's data-quality debt being paid off). Fill rate / OTIF.

---

## PART 6 — Questions you should ask the client (ask these first, in the interview)

Asking these is usually worth more than the design itself — it shows you know the formula is a business decision, not a coding one.

**Definitional**
1. Which order statuses actually hold stock? (Does a `PENDING` order with no payment reserve inventory?)
2. Is available **per warehouse** or **network-wide**? Can WH2 stock fulfil a WH1 order — and if not, a network-level number is misleading.
3. Do you hold safety stock? Per SKU, per class, or none?
4. Should damaged/quarantine stock reduce available? Is it in the WMS count at all?
5. When an inbound PO is `RECEIVED`, does WMS on-hand already include it? (Answers the double-count question.)

**Data and operational**
6. Are SKU codes identical across all three systems, or does each have its own coding? Is there an item master?
7. Units of measure — does any system report cases or pallets while another reports eaches?
8. What are the export times and SLAs for each file? What happens today when one is late?
9. Is there history of negative on-hand, and what's the current process for it?
10. Volume: how many SKUs, warehouses, open order lines?

**Scope and change**
11. How fresh must the number be — is daily at 06:00 enough, or does order volume demand intraday?
12. Is this **read-only reporting**, or will it eventually **block** an order from being taken? (Very different reliability bar.)
13. For the returns system: does a return count as available before QC, after QC, or only once restocked?
14. Who owns the exception queue, and what's the escalation path for a CRITICAL flag at 06:00?
15. Beyond returns, what other sources are plausible in 12 months — 3PL, consignment, in-transit between warehouses, kits/BOM?

That last one matters: **kits/bundles** are the usual next surprise. If a SKU is assembled from components, its availability is `min(component_available / qty_per_kit)` — a different computation, not just another source. Worth naming as a known future extension rather than being blindsided by it.

---

## PART 7 — Delivery plan (if asked "how would you phase this?")

| Phase | Scope | Duration |
|---|---|---|
| 0 | Discovery: the 15 questions above; profile all three files; document current manual process | 3–5 days |
| 1 | Canonical model + 3 adapters + available formula + CLI + JSON/CSV output. Run in **shadow mode** against the spreadsheet. | 2 weeks |
| 2 | Flag engine + exception report + run manifest + lineage/`explain`. Ops starts working the exception queue. | 1 week |
| 3 | REST API + dashboard integration + scheduling, alerting, staleness monitoring. Manual spreadsheet retired. | 1–2 weeks |
| 4 | Returns adapter (proves the extensibility claim), ATP horizons, safety stock, reorder signals | 1 week |

Shadow mode in phase 1 is the important one: it de-risks the cutover and produces the "here are the N overselling events you'd have avoided" number that justifies the rest.

---

## One-paragraph summary (if you get 60 seconds)

> Inventory_Visibility's problem isn't three files, it's three different meanings of the word "quantity" with nobody owning the definition of "available." I'd build a small reconciliation engine with a canonical signed-quantity ledger: each source gets a pluggable adapter that translates its rows into `(sku, location, bucket, qty, provenance, as_of)` records, and the availability formula is defined over *buckets* in config rather than over *sources* in code. `available = on_hand − reserved − unsellable − safety_stock`, with inbound kept separate as ATP so it can never inflate today's sellable number. Every ambiguity resolves downward, because overselling costs chargebacks and understating costs a little deferred revenue — which also means we fail-closed when a subtractive source is missing and fail-open when an additive one is. A rules-based flag engine emits the exception queue (oversold, negative, orphan SKU, stale, mismatched), every number carries lineage back to source rows so ops can trust it, and one core library is exposed through both a CLI for the cron job and a read-only API serving the last good run to the dashboard. Adding the returns system is a config block, two bucket definitions and a thirty-line adapter — no change to the formula, the flags, or the API.
