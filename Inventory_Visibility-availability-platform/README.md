# Inventory Visibility Availability Reconciliation Platform

One true **available-to-sell** number per SKU, reconciled from every registered
system, with automatic exception flags, full lineage, a CLI for the scheduler
and a REST API for the dashboard.

Built for the brief: *"Our WMS stock CSV, order system JSON and supplier shipment
CSVs don't talk to each other… we've oversold multiple times this quarter… and
we're adding a returns system next month — don't assume there will always be
just three sources."*

---

## Run the demo

```bash
python3 -m pip install pyyaml                # the engine's only dependency
python3 scripts/generate_sample_data.py      # 250 SKUs, 3 warehouses, seeded
python3 scripts/generate_history.py          # 180 days of demand + PO history
python3 -m inv_visibility.cli reconcile            # steps 1-6, the deterministic run
python3 -m inv_visibility.cli intelligence         # layer 7, advisory only
python3 scripts/build_console.py             # renders ops-console.html
open ops-console.html                        # zero-install ops dashboard
```

`ops-console.html` is self-contained — no server, no network, no build step.
Every figure on screen traces back to a source record in `data/`.

Optional, for the live API:

```bash
python3 -m pip install fastapi uvicorn
uvicorn inv_visibility.api:app --reload            # OpenAPI docs at /docs
```

---

## The problem, in one paragraph

Inventory Visibility's problem was never data volume. Three systems each publish a field
called *quantity*, the three mean different things, and nobody owned the
definition of *available*. Ops read `120` off the WMS export and told sales
"we have 120" — when 40 were already allocated to unshipped orders and 100 of
the rest were on a truck that hadn't landed. In this sample dataset that habit
overstates availability by **33%**.

## The core idea

Do not write a three-source formula:

```python
available = wms.on_hand - orders.reserved + shipments.incoming   # wrong twice
```

`+ incoming` is the oversell, and adding returns means editing that line, the
loader, the API and the tests.

Instead: **every source contributes signed quantities into buckets, and the
formula is defined over buckets in configuration — not over sources in code.**

```
available = Σ(quantity × available_sign[bucket])
atp(d)    = available + Σ(INBOUND where eta ≤ d)
```

| Bucket | In `available` | In `atp` | Contributed by |
|---|---|---|---|
| `ON_HAND` | `+1` | `+1` | WMS |
| `RESERVED` | `−1` | `−1` | Order system |
| `UNSELLABLE` | `−1` | `−1` | WMS damage flag |
| `SAFETY_STOCK` | `−1` | `−1` | Item master |
| `INBOUND` | **`0`** | `+1` | Supplier ASN |
| `RETURN_IN_TRANSIT` | **`0`** | `0` | Returns portal |
| `RETURN_PENDING_QC` | **`0`** | `0` | Returns portal |

Adding a source is **one config block plus one adapter class**. Nothing in the
pipeline, the flag engine, the CLI or the API changes. The returns feed in this
repository was added exactly that way, and a test enforces the claim:
dropping it must not move a single published number.

---

## Policies the engine enforces

**Conservative bias.** Overstating availability costs chargebacks and a customer;
understating costs a little deferred revenue. So every ambiguity resolves
downward. Unknown on-hand publishes as zero, with a flag.

**Fail closed on subtractive feeds, fail open on additive ones.** Losing the
order feed would erase `RESERVED` and inflate every number — the run aborts and
the previous publication stays live. Losing the supplier feed only degrades ATP,
which can only understate — the run completes and raises `DEGRADED_RUN`.

**Null is not zero.** `on_hand: null` means *unknown* — an item-master sync
problem. `on_hand: 0` means genuinely out of stock. Different fixes.

**Nothing is double counted.** A `SHIPPED` order line already left the building
so WMS excludes it; a `RECEIVED` PO is already inside the WMS count; a
`QC_PASSED_RESTOCKED` return likewise. All three are excluded by the same
status-mapping mechanism, and the manifest reports how many of each were dropped.

**Units are never guessed.** No conversion rule means the SKU is excluded from
publish and flagged. Guessing between eaches and cases is a twelvefold error in
the direction that oversells.

**The published number is the network total, clamped once.** Clamping each
warehouse and then summing would publish more than the business holds. A single
short warehouse surfaces as `LOCATION_OVERSOLD`, whose fix is a stock transfer,
not a sale.

**Idempotent.** Full recompute from immutable snapshots, input hashes in the
manifest. Same inputs, byte-identical output. Safe to retry.

---

## Repository map

| Path | Role |
|---|---|
| `config/sources.yaml` | Feeds, bucket signs, statuses, unit conversions, thresholds. **The formula lives here.** |
| `inv-visibility/models.py` | The canonical record every source is translated into |
| `inv-visibility/adapters.py` | One adapter per feed — the plugin layer |
| `inv-visibility/pipeline.py` | Normalise → aggregate → apply bucket signs |
| `inv-visibility/flags.py` | Exception rules, each returning severity, message and evidence |
| `inv-visibility/engine.py` | Orchestration, fail-closed policy, run manifest, outputs |
| `inv-visibility/cli.py` | `reconcile`, `explain`, `exceptions`, `sources` |
| `inv-visibility/api.py` | FastAPI service over the last good published run |
| `scripts/generate_sample_data.py` | Seeded 250-SKU dataset with every trap planted |
| `scripts/build_console.py` | Renders the single-file ops console |
| `tests/test_engine.py` | Invariants, double-count scenarios, run policies |
| `inv-visibility/intelligence/forecast.py` | Demand classification and Croston/SBA forecasting |
| `inv-visibility/intelligence/supplier.py` | Supplier slip prediction and risk-adjusted ATP |
| `inv-visibility/intelligence/anomaly.py` | Conservation-identity shrinkage detection |
| `inv-visibility/intelligence/risk.py` | Transparent weighted risk score with reason codes |
| `inv-visibility/intelligence/llm.py` | Provider interface, deterministic mock, and the guardrails |
| `scripts/generate_history.py` | 180 days of seeded demand, PO receipt and ledger history |
| `tests/test_intelligence.py` | Layer 7 boundary, forecast and guardrail tests |
| `runs/latest/` | `run.json`, `intelligence.json`, `inventory.csv`, `exceptions.csv` |
| `ops-console.html` | The deliverable ops open every morning |

---

## Exception catalogue

| Flag | Severity | Business meaning |
|---|---|---|
| `OVERSOLD` | critical | Units already promised beyond stock. Chargeback risk today. |
| `NEGATIVE_ON_HAND` | critical | WMS reports impossible stock at a location. Needs a cycle count. |
| `ORPHAN_SKU_IN_ORDERS` | critical | Selling something the warehouse has no record of. |
| `UOM_UNKNOWN` | critical | No conversion rule. Excluded from publish rather than guessed. |
| `STALE_SOURCE` | critical | A feed is past its freshness SLA. |
| `LOCATION_OVERSOLD` | warning | One warehouse is short while the network is not. Raise a transfer. |
| `ORPHAN_SKU_IN_SHIPMENTS` | warning | Inbound for a SKU missing from the item master. |
| `OVERDUE_INBOUND` | warning | A PO past its ETA is quietly inflating ATP. |
| `BELOW_SAFETY_STOCK` | warning | Reorder trigger. |
| `SNAPSHOT_SKEW` | warning | Feeds describe different moments in time. |
| `DUPLICATE_RECORD` | warning | Identical export row seen twice; the copy was dropped. |
| `CELL_COLLISION` | warning | Two rows land on the same SKU+location after normalisation. Summed, not dropped, and raised for a human. |
| `DEGRADED_RUN` | warning | An optional feed was unavailable; ATP degraded. |

Every exception carries an `evidence` array — the exact source record IDs behind
it. That is what lets ops trust a number that contradicts their spreadsheet, and
what `inv-visibility explain <sku>` prints on demand.

---

---

## Layer 7 — the intelligence layer

> **Deterministic core, probabilistic periphery.**

Everything in `inv-visibility/intelligence/` sits **on top of** a completed run and
never inside it. It reads `run.json`, writes `intelligence.json`, and cannot
change `available`. If the whole layer throws, the deterministic numbers publish
anyway. A test enforces exactly that.

That ordering is the answer to *"would you use AI here?"*. Yes — but not in the
number. The number is what justifies declining a customer's order, so it has to
be auditable, idempotent and defensible. A language model is none of those.

But refusing AI entirely leaves the value on the table, because the
deterministic system is **reactive**: it says you are *already* oversold. This
layer says you *will* be oversold on Thursday.

### Statistics, not an LLM

| Component | Method | Why not an LLM |
|---|---|---|
| Demand forecast | Croston/SBA for intermittent, seasonal-naive for smooth, category fallback for new SKUs | Backtestable. WAPE against a naive baseline is a number you can defend; you cannot backtest an opinion about demand. |
| Supplier ETA | Median and p90 slip per supplier from closed PO receipts | Measurable against actual receipt dates. |
| Shrinkage | Conservation-identity residual with a `sqrt(n)` noise band | An identity with a threshold, not a narrative. |
| Risk score | Weighted sum declared in `sources.yaml`, fully decomposed | Must be explainable line by line or ops ignores it. |

A distributor's catalogue is mostly intermittent demand — long runs of zero days
punctuated by small orders. Croston exists for that shape; one global model over
all SKUs would be the wrong answer. In this dataset: 177 intermittent, 71
smooth, 4 lumpy.

**Honesty in the metrics.** Backtesting is rolling-origin, never a random split,
and scored on the *cumulative 14-day horizon* rather than daily point accuracy —
because "will this cover me until the PO lands" is the actual decision. Median
WAPE 0.233 against 0.284 for the naive baseline. The chosen model beats the
baseline on 58% of SKUs; on the other 104 **the baseline ships**, and the win
rate is measured before that substitution. Measuring it after would report 100%
by construction.

### Where the LLM genuinely earns its place

Two features, both behind one provider interface:

| Provider | Mode | Notes |
|---|---|---|
| `NarrativeProvider` | deterministic, offline | **The configured default.** Reproducible output, no external dependency, zero marginal latency. |
| `HostedModelProvider` | hosted model endpoint | Same interface, same verification. Not wired to an endpoint in this deployment. |

The deterministic provider is the default for reasons that hold in production,
not just in a demo: a brief ops reads every morning at 06:20 should not fail
because a third-party endpoint is having an incident, and reproducible output
means the brief can be diffed and regression-tested like any other artefact.
Swapping providers changes the prose, not the contract — every guardrail below
is enforced on the response either way.

**Morning brief.** Structured engine output in, three paragraphs out. Language
and prioritisation, no arithmetic.

**Onboarding assistant.** Reads an unfamiliar export and drafts the adapter
config — as a reviewable proposal, never a live change. Given the client warned
that source count will grow, this is the highest-leverage AI feature in the
system. Its value is concentrated in the *open questions*: on the sample 3PL
file it surfaced the same double-count trap that caused Inventory Visibility's original
oversell, as a question rather than a silent assumption.

### The guardrails, all executed on every run

1. The provider receives **structured engine output only** — no source files, no
   free text, no customer names beyond order identifiers.
2. **Every numeral in the generated text must exist in the input facts.** A
   fabricated figure fails the check and the brief is withheld rather than
   published. A test proves the check catches one.
3. A proposed config is **validated against the bucket catalogue**. A referenced
   bucket must already exist or be explicitly declared as new *with its signs
   stated* — a bucket sign is the most dangerous line in this configuration, and
   it is never allowed to default quietly.
4. Anything below 0.80 confidence becomes an **open question**, not a mapping.
   Silent confidence is the dangerous failure, not a wrong guess that was raised.
5. The layer is **advisory**. Provider failure degrades it and publishes nothing
   false; the availability number is unaffected.

Honest framing for the interview: the narrative you see in the console is
generated by the deterministic provider, not by a hosted model. That is a real
engineering choice with real reasons — and it is also what makes the
verification step demonstrable, since the check does not know or care which
provider produced the text.

### What it would never do

Let a model read the source files and estimate inventory, or let an agent
cancel or reallocate customer orders. Both are non-deterministic answers to
questions with contractual money attached.

---

## Exit codes

`0` clean · `1` warnings · `2` critical exceptions · `3` run aborted.
The scheduler acts on these without parsing stdout.

## Tests

```bash
python3 -m pytest tests/ -q
```

Three layers: adapter behaviour, the specific business traps that cause
oversells (shipped lines, received POs, restocked returns, unknown units,
aliased SKUs), and invariants that must hold for every run —
`available ≥ 0`, `available ≤ on_hand`, `atp ≥ available`, location rows
reconcile to SKU totals, the run is idempotent, and adding a zero-signed
source never moves an existing number.
