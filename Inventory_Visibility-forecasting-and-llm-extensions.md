# Inventory_Visibility — Adding Forecasting and LLM/Agent Features
### Companion to the reconciliation design doc

---

## The one principle that makes this answer good

> **Deterministic core, probabilistic periphery.**
> An LLM must never compute the availability number. It reads the engine's outputs and writes *text, drafts, and config proposals* that a human approves.

Say this before you pitch a single AI feature. The reason:

- The available number needs to be **auditable** (ops must be able to reconstruct the arithmetic), **idempotent** (same inputs → same number), and **defensible** (this number is why we declined a $200k order). An LLM gives you none of those.
- If you propose "an LLM reads the three files and figures out inventory," a good interviewer will fail you on the spot. That's a non-deterministic answer to a chargeback-liability question.
- But if you say "no AI anywhere," you're leaving the actual business value on the table — because the current system is **reactive**. It tells you *you are already oversold*. Forecasting tells you *you will be oversold on Thursday*, which is the version that's actually worth money.

So: **Layer 7 sits on top of the existing pipeline and never inside it.**

```
Steps 1–6  (deterministic)                     Layer 7  (probabilistic)
──────────────────────────────                 ────────────────────────────
adapters → validate → normalize                ┌─ 7a  Demand forecasting
   → aggregate → available/ATP                 ├─ 7b  Supplier ETA prediction
   → flag engine                               ├─ 7c  Anomaly / shrinkage detection
        │                                      ├─ 7d  Risk scoring & prioritisation
        │  inventory.json                      ├─ 7e  LLM: exception triage & briefing
        └──── exceptions.json ────────────────► ├─ 7f  LLM: new-source mapping assistant
               run_manifest.json               ├─ 7g  LLM: natural-language query
                                               └─ 7h  Agent: action proposals (HITL)
                                                          │
                                               all outputs are advisory,
                                               labelled with confidence,
                                               never overwrite `available`
```

---

## PART A — Statistical forecasting (do this before any LLM)

Note this distinction explicitly: **forecasting here is classical statistics/ML, not an LLM.** LLMs are bad at numeric forecasting and you can't backtest a vibe. Using an LLM where Croston belongs is a red flag.

### 7a — Demand forecasting per SKU

**Why it matters commercially:** `available = 80` is meaningless on its own. 80 units is three weeks of cover for one SKU and six hours for another. Forecasting converts a *quantity* into a *time*, which is what ops actually decides on.

**Input** — you need history the current three sources don't provide. This is a scoping question to raise: *do we have order history, or only the current open-order snapshot?* If only snapshots, the system starts accumulating its own history from day one (each immutable run is a datapoint — a nice side benefit of the snapshot design).

```csv
sku,location,date,units_shipped
ABC-100,WH1,2026-09-01,14
ABC-100,WH1,2026-09-02,0
ABC-100,WH1,2026-09-03,9
...
```

**Model choice by demand pattern** (naming these shows domain depth):

| Pattern | Typical SKUs | Model |
|---|---|---|
| Smooth, high volume | A-class movers | ETS / SARIMA / gradient boosting with calendar features |
| **Intermittent / lumpy** | most of a distributor's long tail | **Croston / SBA / TSB** — purpose-built for demand with many zero days |
| New SKU, no history | recent additions | hierarchical fallback: category-level average, flagged `low_confidence` |

For a distributor with thousands of SKUs, the majority is intermittent. A single global Prophet model would be the wrong answer.

**Output**

```json
{
  "sku": "ABC-100", "location": "WH1",
  "available": 80,
  "forecast": {
    "model": "croston_sba", "trained_on": "2026-06-01/2026-09-08",
    "daily_demand_mean": 11.4, "daily_demand_p90": 19.0,
    "days_of_cover_p50": 7.0, "days_of_cover_p10": 4.2,
    "stockout_date_p50": "2026-09-16", "stockout_date_p10": "2026-09-13",
    "confidence": "high", "backtest_wape": 0.22
  }
}
```

### 7b — Supplier ETA prediction

This one usually lands best in an interview because it fixes a flaw in the *existing* design: **ATP is only as trustworthy as the supplier's ETA**, and suppliers are optimistic. My sample data already had `PO-79` overdue by three weeks.

**Input:** historical PO promised-ETA vs actual goods-receipt date, by supplier / lane / SKU class.

**Output:**

```json
{ "po_id": "PO-77", "supplier": "SUP-4", "sku": "ABC-100", "qty": 100,
  "eta_supplier": "2026-09-12",
  "eta_predicted_p50": "2026-09-15", "eta_predicted_p90": "2026-09-21",
  "slip_risk": 0.71,
  "basis": "SUP-4 median slip 3.1 days over last 47 POs; 22% arrive >7 days late" }
```

Now you can publish a **risk-adjusted ATP** alongside the raw one:

```json
{ "sku": "ABC-100", "atp_7d_optimistic": 180, "atp_7d_risk_adjusted": 80,
  "flags": ["INBOUND_SLIP_RISK"] }
```

Keep both. Ops needs the optimistic number for planning and the adjusted one for promising.

### 7c — Anomaly detection on the data itself

Inventory obeys a conservation identity:

```
on_hand(today) ≈ on_hand(yesterday) + receipts − shipments + adjustments
residual = actual − expected     ← unexplained loss = shrinkage or a system error
```

Because every run is an immutable snapshot, you can compute that residual per SKU per day for free. Then:

- Persistent negative residual on one SKU/location → **shrinkage** (theft, damage, mis-pick) or a broken integration
- Sudden step change → a bad WMS export or an unrecorded cycle count
- Residual spikes clustered by picker/zone → an ops process problem

**Output:** a new flag class that is *predictive of future oversells* rather than reactive.

```json
{ "sku": "XYZ-900", "location": "WH1", "flag": "SHRINKAGE_SUSPECTED",
  "residual_30d": -37, "expected_noise_band": [-6, 6], "z": -5.8,
  "severity": "warning",
  "note": "Unexplained loss of ~1.2 units/day. WMS count likely overstates sellable stock." }
```

This is genuinely valuable: it says *the WMS number you're trusting is drifting upward from reality*, which is an oversell cause the reconciliation engine alone cannot see.

### 7d — Risk scoring: turning 400 exceptions into a ranked worklist

The flag engine will produce a lot of output on day one. Ops can't work 400 items. Combine deterministic state + forecast into a single prioritised queue:

```
risk_score = f(stockout_probability_14d,
               revenue_at_risk,          ← units × margin × customer tier
               penalty_exposure,         ← is this SKU on an OTIF contract?
               inbound_slip_risk,
               data_confidence)
```

**Output — the actual morning worklist:**

```json
{ "run_id": "run_2026-09-09T06:15:02Z_a4f9",
  "ranked": [
    { "rank": 1, "sku": "XYZ-900", "risk_score": 94, "band": "CRITICAL",
      "reason_codes": ["OVERSOLD", "SHRINKAGE_SUSPECTED", "NO_OPEN_PO"],
      "revenue_at_risk": 18400, "penalty_exposure": 5200 },
    { "rank": 2, "sku": "ABC-100", "risk_score": 78, "band": "HIGH",
      "reason_codes": ["COVER_BELOW_LEAD_TIME", "INBOUND_SLIP_RISK"],
      "stockout_date_p10": "2026-09-13", "eta_predicted_p50": "2026-09-15",
      "note": "Predicted stockout precedes predicted arrival by 2 days." }
  ] }
```

Rank 2 is the whole pitch in one row: **nothing is wrong today**, no deterministic flag fires, and the system still tells you there's a two-day hole coming. That's the shift from reconciliation to prevention.

Keep the scoring formula transparent and weighted in config, with `reason_codes` on every score. A black-box 0–100 that ops can't interrogate gets ignored within a week.

---

## PART B — Where an LLM genuinely earns its place

### 7e — Exception triage and the morning brief

**The problem it solves:** `exceptions.json` with 60 entries is still work. Ops wants three sentences and a decision.

**Input:** the run's exceptions + rankings + breakdowns (structured data only — cheap, bounded, no PII beyond order IDs).

**Output:**

> **Morning brief — 09 Sep, 06:20.** 3 critical, 2 warnings across 5 SKUs.
>
> **XYZ-900 needs a decision today.** 50 on hand, but 6 are damaged and 44 are committed to SO-1005 — nothing sellable, and no open PO. Suspected shrinkage of ~37 units over 30 days suggests the WMS count may still be overstating. Options: expedite from SUP-4 (lead time 5 days) or contact the SO-1005 customer.
>
> **ABC-100 is fine today but not on Friday.** 110 available network-wide, but WH1 covers ~4 days at p10 demand and PO-77's supplier is running 3 days late on average. If it slips, WH1 goes short around the 13th while stock sits in WH2. Consider a WH2→WH1 transfer now rather than an expedite later.
>
> **NEW-555 is being sold but doesn't exist in the WMS item master.** 5 units committed on SO-1003. This is an integration gap, not a stock problem.

That's an LLM doing what it's actually good at: turning structured facts into prioritised prose with causal framing. It invents no numbers — every figure is passed in, and you can validate that by asserting all numerals in the output appear in the input.

**Delivery:** Slack/email at 06:20, plus a `Inventory_Visibility brief` CLI command and a `GET /v1/brief/latest` endpoint.

### 7f — New-source onboarding assistant (the best fit for this brief)

The client said *don't assume there will always be three sources*. The design already makes adding one cheap. An LLM makes it **fast**, because the slow part isn't the code — it's a human reading an unfamiliar export and figuring out which column means what and which statuses hold stock.

**Input:** a sample of the new file plus the canonical schema and bucket catalogue.

```csv
item_ref,site,rma_no,units,rtn_status,logged_dt
ABC100,W1,RMA-9,3,IN_TRANS,2026-09-08
ABC100,W1,RMA-10,2,RCV_QC_HOLD,2026-09-08
ABC100,W1,RMA-11,4,RESTOCKED,2026-09-07
```

**Output — a draft config for human review, not a live change:**

```yaml
# PROPOSED by mapping assistant — requires review before commit
- name: returns
  adapter: adapters.generic.CsvAdapter
  path: ./data/returns_export.csv
  criticality: optional
  field_map:
    sku:      item_ref     # confidence 0.97
    location: site         # confidence 0.91 — NOTE: values are "W1" not "WH1"
    quantity: units        # confidence 0.98
    as_of:    logged_dt    # confidence 0.84
    source_record_id: rma_no
  status_bucket_map:
    IN_TRANS:    RETURN_IN_TRANSIT      # confidence 0.93
    RCV_QC_HOLD: RETURN_PENDING_QC      # confidence 0.88
    RESTOCKED:   IGNORE                 # confidence 0.71  ← verify
  location_alias: { W1: WH1, W2: WH2 }
```

> **Open questions for the business (3):**
> 1. `RESTOCKED` — I've mapped this to `IGNORE` on the assumption these units are already inside the WMS on-hand count. If returns are restocked before the WMS export runs, that's correct; if after, we're understating. Please confirm.
> 2. `logged_dt` is date-only with no timezone; snapshot skew can't be measured precisely. Can the export include a timestamp?
> 3. Should `RCV_QC_HOLD` units count toward 14-day ATP, or stay fully excluded?

The value is concentrated in those open questions. The LLM read an unfamiliar schema and surfaced the exact double-count trap that caused the original oversell problem — as a question for a human, not a silent assumption.

**Guardrails that make this safe:**
- Output is a **config file diff in a PR**, reviewed and merged by a human. Never applied at runtime.
- The proposal is validated against the config JSON schema before a human ever sees it — a hallucinated bucket name fails there.
- `Inventory_Visibility reconcile --dry-run --diff-against last` shows exactly which SKU numbers the new source would move, before it goes live.
- Anything below a confidence threshold is escalated as a question rather than mapped.

**Related use:** orphan SKU resolution. `NEW-555` unknown to the WMS — an LLM plus fuzzy matching over the item master can propose `NEW-555 → NEW-0555 (0.94)` as an alias candidate for human confirmation, which is a genuinely tedious job today.

### 7g — Natural-language query over the outputs

Ops asks in the dashboard: *"which SKUs in WH1 will run out before Friday if PO-77 slips?"*

Correct implementation: the LLM translates to a **structured query against a whitelisted API surface**, not free-form SQL against production.

```json
{ "endpoint": "/v1/inventory/at_risk",
  "params": { "location": "WH1", "horizon_date": "2026-09-11",
              "assume_inbound_slip": ["PO-77"], "order_by": "risk_score" } }
```

Then the deterministic engine answers and the LLM narrates the result. The numbers come from the engine; the LLM handles language on the way in and on the way out. This pattern — **LLM at the edges, engine in the middle** — is the one to name.

### 7h — An agent, scoped honestly

An agent is worth proposing only if you're clear about what it may and may not do.

**Read-only tools:** query inventory, query exceptions, pull supplier history, check open POs, check other warehouses' stock, look up customer contract tier.

**Write tools, all gated behind human approval:** create a ticket, draft an expedite email to the supplier, draft a customer notification, propose an inter-warehouse transfer, propose a config change.

**Never:** change `available`, cancel or reallocate a customer order, place a purchase order, edit WMS data.

Example run:

```
Agent: XYZ-900 — 0 available, 44 committed to SO-1005, no open PO.
  Checked: WH2 has 0. SUP-4 lead time 5d (p90 8d). SO-1005 ships in 3d.
  Customer tier: contract with OTIF penalty (~$5.2k exposure).
  Proposed actions:
    1. [draft ready] Expedite request to SUP-4 for 50 units
    2. [draft ready] Heads-up email to SO-1005 customer, offer partial ship
    3. [ticket ready] Cycle count request for XYZ-900/WH1 (shrinkage suspected)
  Approve which? [1] [2] [3] [all] [none]
```

The honest framing: this is a **triage assistant that prepares work**, not an autonomous inventory controller. Say so — proposing autonomous order cancellation on a first engagement is how you lose the client's trust and the interview.

---

## PART C — The guardrail table (have this ready)

| Task | Deterministic or LLM/ML | Why |
|---|---|---|
| Compute `available` | **Deterministic. Always.** | Auditable, idempotent, legally defensible |
| Apply flag rules | Deterministic | Same reason; rules are business policy |
| Demand forecast | **Statistical/ML, not LLM** | Backtestable, measurable WAPE; LLMs can't be validated numerically |
| Supplier ETA prediction | Statistical/ML | Same |
| Shrinkage/anomaly detection | Statistical | Same |
| Risk score | Deterministic weighted formula | Must be explainable to ops |
| Rank and summarise exceptions | LLM | Language + prioritisation, no arithmetic |
| Morning brief narrative | LLM | Text generation from given facts |
| Map a new source's schema | LLM → **human-reviewed config PR** | Proposal only; never live |
| Resolve orphan SKU aliases | LLM + fuzzy match → human confirm | Suggestion only |
| Natural-language query | LLM → structured API call | Engine computes, LLM translates |
| Draft supplier/customer comms | LLM → human sends | Never auto-send |
| Mutate inventory or orders | **Neither. Humans only.** | Irreversible commercial actions |

### Evaluation and failure modes — expect to be asked

- **Forecasts:** rolling-origin backtest, WAPE and bias per SKU class, benchmarked against a naive baseline. If the model can't beat naive-seasonal, ship naive-seasonal. Track forecast-attributable stockouts.
- **LLM briefs:** assert every numeral in the output exists in the input (catches fabricated figures automatically); human thumbs-up/down logged; a small golden set of runs with reviewed reference briefs, re-run on prompt or model change.
- **Mapping assistant:** golden set of source files with known-correct mappings; measure field-level accuracy and, more importantly, **whether it flags the ambiguous cases** rather than confidently guessing. Silent confidence is the dangerous failure, not a wrong guess that was surfaced as a question.
- **Cost/latency:** all of this runs once per batch, not per request. Structured inputs only, so a few thousand tokens per run. Cache the brief keyed on `run_id` — the API serves a stored brief, so no LLM call sits in the dashboard's read path.
- **Degradation:** if the LLM or forecast layer fails, the deterministic outputs publish anyway, with the advisory fields absent and marked degraded. Layer 7 can never block the availability number. That's the payoff of keeping it strictly on top.

---

## PART D — How to phase it

| Phase | Feature | Prerequisite | Business value |
|---|---|---|---|
| 5 | Days-of-cover + reorder point (7a) | order history | Turns quantity into time; enables reordering |
| 5 | Supplier ETA prediction (7b) | PO receipt history | Makes ATP trustworthy |
| 6 | Risk scoring & ranked worklist (7d) | 7a + 7b | Ops can work 10 items instead of 400 |
| 6 | Shrinkage/anomaly detection (7c) | ~60 days of own snapshots | Catches WMS drift *before* an oversell |
| 6 | LLM morning brief (7e) | flag engine | Adoption — ops reads three sentences, not a CSV |
| 7 | Mapping assistant (7f) | — | Cuts new-source onboarding from days to hours |
| 7 | NL query (7g) | API | Self-service for ops and sales |
| 8 | Action-proposal agent (7h) | all of the above | Prepared remediation, human-approved |

Note the dependency chain: **7c and 7a both need history the client may not have today.** Raise that in discovery — "do we have shipment history, or does the clock start when we go live?" If the clock starts now, the immutable-snapshot design means you're accumulating the training data from day one, which is a good reason to ship phases 1–3 quickly.

---

## Sound bites for the interview

**If asked "would you use AI here?"**
> Yes, but not in the number. The availability figure has to be deterministic and auditable — it's the thing that justifies declining a customer order. What AI adds is the layer above: statistical demand forecasting to convert `available: 80` into "four days of cover," supplier ETA models so ATP isn't built on optimistic promises, and an LLM to triage the exception queue into a morning brief ops will actually read. And given the client explicitly warned that source count will grow, an LLM schema-mapping assistant that drafts the adapter config for a new feed — as a reviewed pull request, never a live change — is the highest-leverage AI feature in the whole system.

**If asked "what's the single biggest upgrade?"**
> Moving from reactive to predictive. Today the system says "you are already oversold" — useful, but the damage is done. Add demand forecasting plus supplier slip risk and it says "WH1 runs dry on the 13th, PO-77 lands on the 15th, and you have stock in WH2 you could transfer today." Same engine, same data model, but you're preventing the chargeback instead of documenting it.

**If asked "what would you refuse to do?"**
> Let an LLM read the source files and estimate inventory, or let an agent cancel or reallocate customer orders on its own. Both are non-deterministic answers to questions with contractual money attached.
