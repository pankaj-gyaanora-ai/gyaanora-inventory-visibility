# Inventory Visibility — ATP Reconciliation Engine

<p align="center">
  <img src="gyaanoraai.png" alt="GyaanoraAI" height="80"/>
</p>

<p align="center">
  <strong>One true "available to sell" number per SKU — computed, auditable, and automatic.</strong><br/>
  <em>Powered by GyaanoraAI · Learn. Explore. Grow with AI.</em>
</p>

---

## The Business Problem

### What breaks without this

Every distribution business runs on three systems that each hold **one piece** of the inventory truth — and none of them agree on what "available" means.

| System | What it knows | The dangerous assumption it makes |
|---|---|---|
| **WMS** (Warehouse) | Units physically on the shelf | Treats allocated stock as sellable |
| **Order System** | Units promised to open orders | Has no idea what's physically there |
| **Supplier Feed** | Inbound shipments in transit | Has no idea what's already been sold |

### The daily damage

> An ops manager opens the WMS, sees **120 units** of SKU-1001, and tells the sales team "we have 120."
>
> But **40 of those 120 are already allocated** to open customer orders. The real sellable quantity is **80**.
>
> Sales sells **100**. Now the business has promised **140 units** of a **120-unit pile**.

That gap — 20 units — triggers:

- **Order cancellations or short-shipments** → customer trust damage
- **Chargebacks** — retail and B2B contracts penalise missed OTIF (On Time In Full) targets. These can run into tens of thousands of dollars per incident.
- **Manual reconciliation** — ops burns an hour every morning VLOOKUPing three files together, and still gets it wrong sometimes.

### The root cause

> This is not a data-volume problem or a technology problem. It is a **semantics problem.**
>
> Three systems each publish a column called `quantity`. The three numbers mean completely different things. Nothing in the business owns the definition of *"available."*

### What "solved" looks like

| Before | After |
|---|---|
| Three spreadsheets, one human, 60 minutes | One run, zero humans, 2 minutes |
| No single "available" definition | One auditable formula per SKU |
| Oversells discovered after the fact | Flagged before the order is confirmed |
| Adding a new source = a dev sprint | Adding a new source = 5 lines of YAML |

---

## What We Are Solving

**Inventory Visibility** owns the single definition of `available` and computes it deterministically from all sources — warehouse, orders, and supplier feeds — on every scheduled run.

```
available = Σ(on_hand)  −  Σ(reserved)  +  Σ(incoming)
```

Every number is traceable back to its source row. Every anomaly is flagged automatically. The operations dashboard always shows the **last good published run** — never a stale spreadsheet, never an inflated WMS number.

### Real numbers from a live run

| Metric | Value |
|---|---|
| SKUs evaluated | 257 |
| WMS naive view (what ops sees today) | 309,308 units |
| True ATP (what is actually sellable) | **209,160 units** |
| Overstatement caught | **100,148 units — 32.4%** |
| Revenue at risk flagged | **$109,484** |
| Critical exceptions auto-detected | 27 |

---

## Architecture

![Inventory Visibility Architecture](architecture.svg)

### How it works — step by step

```
Sources (WMS · Orders · Suppliers)
        │
        ▼
[STEP 1-2] Adapter Layer
  · Each source declares its quantity_type in sources.yaml
  · Schema validated, rows tagged: on_hand | reserved | incoming
        │
        ▼
[STEP 3-4] Normalize + Aggregate
  · Common schema across all sources
  · Sum by (SKU, quantity_type)
  · available = Σ on_hand − Σ reserved + Σ incoming
        │
        ▼
[STEP 5-6] Flag Engine
  · Negative ATP (oversell)          · SKU in orders, missing from WMS
  · Stale source data                · Cross-system qty mismatch
        │
        ├──────────────────────────┐
        ▼                          ▼
  REST API (FastAPI)          Layer 7 — Intelligence
  Ops Dashboard               Forecasting · Anomaly · LLM triage
```

### Design principle

> **Deterministic core. Probabilistic periphery.**

Steps 1–6 are fully deterministic — same inputs always produce the same `available` number, with full audit trail in `run_manifest.json`. The LLM/forecasting layer (Layer 7) reads engine outputs only — it **never computes the ATP number.**

This is the key architectural decision. The available number must be:
- **Auditable** — ops can reconstruct the arithmetic from source files
- **Idempotent** — same inputs → same number, every time
- **Defensible** — this is the number that justifies declining a $200k order

---

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| **Language** | Python 3.10+ | Standard in data/supply-chain tooling; stdlib handles most of the engine |
| **API** | FastAPI + Uvicorn | Async, auto-generates OpenAPI docs, minimal overhead |
| **Data formats** | CSV · JSON · YAML | Matches real WMS and OMS export formats; no DB dependency |
| **Config** | PyYAML (`sources.yaml`) | Declarative source registration — ops adds a source without touching code |
| **Forecasting** | Python `statistics` stdlib | Classical linear regression and exponential smoothing — fully backtestable, no black box |
| **Anomaly detection** | Conservation identity math | Deterministic inventory identity check, not ML — auditable and explainable |
| **LLM / NL layer** | Pluggable (OpenAI / Anthropic) | Layer 7 only — never touches the ATP computation |
| **Frontend** | Vanilla HTML + CSS + JS | Zero build step; opens directly in browser; no framework dependency |
| **Testing** | pytest | Unit tests for engine, pipeline, and intelligence modules |
| **No database** | Flat files (`runs/latest/`) | Every run is a self-contained JSON snapshot — portable, auditable, git-diffable |

### Why no database?

The engine intentionally avoids a database. Each reconciliation run produces a self-contained snapshot in `runs/latest/`. This means:
- Any run can be replayed from its source files
- The audit trail is plain JSON — no SQL needed to inspect it
- The ops dashboard reads a file, not a query — fast and stable under load

---

## Project Structure

```
Inventory_Visibility-availability-platform/
├── inv_visibility/             # Core engine package
│   ├── adapters.py             # Source loaders (WMS, orders, suppliers)
│   ├── pipeline.py             # Normalize + aggregate logic
│   ├── engine.py               # Orchestrates a full reconciliation run
│   ├── flags.py                # Exception detection rules
│   ├── models.py               # Pydantic schemas
│   ├── api.py                  # FastAPI read-only REST API
│   ├── cli.py                  # Command-line interface
│   ├── config.py               # Config loader
│   └── intelligence/           # Layer 7 — probabilistic advisory
│       ├── forecast.py         # Demand forecasting
│       ├── anomaly.py          # Shrinkage / drift detection
│       ├── risk.py             # Oversell risk scoring
│       ├── supplier.py         # ETA prediction
│       └── llm.py              # Exception triage & NL query
├── config/
│   └── sources.yaml            # Add new sources here — no code change needed
├── data/                       # Sample input data
├── runs/                       # Run outputs (gitignored)
├── tests/
├── scripts/
└── requirements.txt
```

---

## Quickstart

```bash
# 1. Install
pip install -r requirements.txt

# 2. Run a reconciliation
cd Inventory_Visibility-availability-platform
python -m inv_visibility.cli reconcile

# 3. Start the API
uvicorn inv_visibility.api:app --reload
# → Interactive docs at http://127.0.0.1:8000/docs

# 4. Open the ops dashboard
open ops-console.html
```

---

## Key Outputs

| File | Contents |
|---|---|
| `runs/latest/inventory.json` | ATP per SKU with full source lineage |
| `runs/latest/exceptions.json` | All anomalies with severity and evidence |
| `runs/latest/run_manifest.json` | Source file hashes, timestamps, run metadata |

---

## Adding a New Source

Edit `config/sources.yaml` — **no code changes required:**

```yaml
sources:
  - name: returns_system
    type: csv
    path: data/returns.csv
    quantity_type: incoming      # on_hand | reserved | incoming
    sku_field: item_code
    qty_field: return_qty
```

This is the key extensibility test. The engine is source-count agnostic — a 4th, 5th, or 6th source is config, not a rewrite.

---

## Intelligence Layer (Layer 7)

Layer 7 sits **on top of** the deterministic pipeline and never inside it.

| Module | What it does |
|---|---|
| `forecast.py` | Predicts future ATP based on demand history — alerts before stockout |
| `anomaly.py` | Detects shrinkage, sudden drops, and data drift across runs |
| `risk.py` | Scores each SKU by oversell and stockout probability |
| `supplier.py` | Predicts inbound ETA from historical supplier lead times |
| `llm.py` | Writes exception triage briefs, answers NL inventory queries |

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /v1/inventory` | Full ATP inventory, all SKUs |
| `GET /v1/inventory/{sku}` | Single SKU with full lineage |
| `GET /v1/exceptions` | All flagged anomalies with severity |
| `GET /v1/runs/latest` | Latest run metadata and summary |
| `GET /healthz` | Health check |

---

## License

MIT
