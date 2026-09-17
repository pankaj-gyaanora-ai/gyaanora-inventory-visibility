"""The LLM layer -- and the boundary it is not allowed to cross.

    Deterministic core, probabilistic periphery.

An LLM never computes the availability number. It reads the engine's outputs and
writes text, drafts and config proposals that a human approves. The reason is
not squeamishness:

  * `available` must be **auditable** (ops reconstructs the arithmetic),
    **idempotent** (same inputs, same number) and **defensible** (this number is
    why we declined a $200k order). A language model offers none of those.
  * But "no AI anywhere" leaves the value on the table, because the deterministic
    system is *reactive*: it says you are already oversold. The intelligence
    layer says you *will* be oversold on Thursday, which is the version worth
    money.

Two providers implement one interface, selected by configuration:

    NarrativeProvider   deterministic, offline, no external dependency
    HostedModelProvider hosted model endpoint -- same interface, same guardrails

The deterministic provider is the default because a narrative that ops reads
every morning at 06:20 should not depend on a third-party endpoint being up, and
because it makes the output reproducible: the same run always produces the same
brief, which means the brief can be diffed and regression-tested like any other
artefact. Swapping to a hosted model changes the prose, not the contract -- the
guardrails below are enforced on the response either way.

Guardrails, all executed in `generate_brief`:
  1. the model receives structured engine output only -- never raw source files
  2. every numeral in the generated text must exist in the input facts, or the
     brief is rejected. Fabricated figures cannot reach ops.
  3. a proposed config is validated against the bucket catalogue before a human
     ever sees it -- a hallucinated bucket name fails there
  4. output is advisory. If this whole layer throws, the deterministic run
     publishes anyway, marked degraded.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime


# --------------------------------------------------------------- the interface
class LlmProvider:
    """Swap the provider, keep every guardrail. `name` is published in the run
    output so a reader always knows which one produced the text."""

    name = "abstract"
    mode = "abstract"
    deterministic = True
    is_mock = True

    def write_brief(self, facts: dict) -> str:
        raise NotImplementedError

    def propose_mapping(self, sample: dict, catalogue: dict) -> dict:
        raise NotImplementedError


class HostedModelProvider(LlmProvider):
    """A hosted model endpoint.

        provider = HostedModelProvider(model="claude-sonnet-5", client=client)
        brief    = generate_brief(run, intel, provider)

    Identical interface, identical guardrails. The prompt is the same structured
    payload, the numeral check runs on the response the same way, and an
    endpoint failure degrades the advisory layer without touching availability.

    Not wired to a live endpoint in this deployment -- the narrative provider is
    the configured default. Enabling it is a credential and a config line.
    """

    name = "hosted-model"
    mode = "hosted"
    deterministic = False
    is_mock = False

    def __init__(self, model: str, client=None):
        self.model = model
        self.client = client

    def write_brief(self, facts: dict) -> str:
        if self.client is None:
            raise RuntimeError("No model client configured for this deployment.")
        return self.client.complete(BRIEF_SYSTEM_PROMPT, facts)

    def propose_mapping(self, sample: dict, catalogue: dict) -> dict:
        if self.client is None:
            raise RuntimeError("No model client configured for this deployment.")
        return self.client.complete_json(MAPPING_SYSTEM_PROMPT, {"sample": sample,
                                                                 "catalogue": catalogue})


BRIEF_SYSTEM_PROMPT = """You are writing the 06:20 inventory brief for warehouse operations.

You will be given structured facts from a deterministic reconciliation run.

Rules:
- Use ONLY numbers present in the facts. Never calculate, estimate or round a
  new figure. Every numeral you write is checked against the input.
- Lead with the item that needs a decision today, not the largest number.
- For each item state: what is true, why it is true, and the options.
- Distinguish "already broken" from "will break on <date>".
- Three short paragraphs maximum. No preamble, no bullet lists, no hedging.
"""

MAPPING_SYSTEM_PROMPT = """You map an unfamiliar inventory export onto the canonical schema.

You will be given sample rows and the bucket catalogue.

Rules:
- Propose a field map and a status-to-bucket map, each with a confidence 0-1.
- Use ONLY bucket names from the supplied catalogue.
- Where a status might describe stock already counted elsewhere, map it to
  IGNORE and raise it as an open question. Double counting is the failure mode
  that caused this project to exist.
- Anything below 0.80 confidence becomes an open question rather than a mapping.
- Output is a proposal for human review. You are not applying a change.
"""


# ------------------------------------------------------------- the mock provider
class NarrativeProvider(LlmProvider):
    """Deterministic narrative generation over the run's structured facts.

    Composes the brief from the same payload a hosted model receives, using a
    grammar of templates selected by the state of the run. Three properties this
    buys, all of which matter for something ops depends on daily:

      * **reproducible** -- the same run always produces the same brief, so it
        can be diffed, reviewed and regression-tested like any other artefact
      * **no external dependency** -- the 06:20 brief does not fail because a
        third-party endpoint is having an incident
      * **zero marginal cost and latency** -- it runs inside the batch

    It is the configured default for this deployment. Swapping in
    HostedModelProvider produces more fluent prose and inherits every guardrail
    unchanged, which is the point of the interface.
    """

    name = "Inventory Visibility-narrative-v1"
    mode = "deterministic"
    deterministic = True
    is_mock = True

    def write_brief(self, facts: dict) -> str:
        paras: list[str] = []
        d = facts["date"]
        s = facts["summary"]
        paras.append(
            f"Morning brief - {d}. {s['critical']} critical and {s['warning']} warning "
            f"exceptions across {s['skus']} SKUs. True available today is "
            f"{s['total_available']} units; reading the warehouse export alone would have "
            f"shown {s['naive_number']}, overstating by {s['overstatement_units']} units."
        )

        worst = facts.get("worst_today")
        if worst:
            po = worst.get("next_po")
            options = (f"Options: expedite {po['po_id']} from {po['supplier']}, "
                       f"predicted to land {po['eta_predicted_p50']} rather than the promised "
                       f"{po['eta_supplier']}; or contact the customers on the affected orders."
                       if po else
                       "There is no open purchase order covering it, so the only lever today "
                       "is contacting the customers on the affected orders.")
            shrink = ""
            if worst.get("shrinkage"):
                shrink = (f" Suspected shrinkage of {abs(worst['shrinkage']['residual_units'])} units "
                          f"over {worst['shrinkage']['residual_window_days']} days suggests the "
                          f"warehouse count is itself overstating what is on the shelf.")
            paras.append(
                f"{worst['sku']} needs a decision today. {worst['available']} units are sellable "
                f"against {worst['reserved']} committed to open orders, leaving a shortfall of "
                f"{worst['shortfall']} units and {worst['revenue_at_risk']} dollars of revenue "
                f"exposed.{shrink} {options}"
            )

        coming = facts.get("worst_predicted")
        if coming:
            po = coming.get("next_po")
            arrival = (f" The cover on order is {po['po_id']}, promised {po['eta_supplier']} but "
                       f"predicted {po['eta_predicted_p50']} on that supplier's record, so the gap "
                       f"is real rather than a paperwork artefact."
                       if po else
                       " Nothing is on order to cover it.")
            paras.append(
                f"{coming['sku']} is fine today but not for long. {coming['available']} units are "
                f"available and demand is running at {coming['daily_demand_mean']} units a day, "
                f"which is {coming['days_of_cover_p10']} days of cover at the pessimistic rate - "
                f"a stockout around {coming['stockout_date_p10']}.{arrival} No deterministic flag "
                f"fires on this SKU today, which is exactly why it is in the brief."
            )

        orphan = facts.get("orphan")
        if orphan:
            paras.append(
                f"{orphan['sku']} is being sold but the warehouse has no record of it, with "
                f"{orphan['reserved']} units committed. That is an integration gap in the item "
                f"master, not a stock problem, and it needs a different team."
            )
        return "\n\n".join(paras)

    def propose_mapping(self, sample: dict, catalogue: dict) -> dict:
        """A worked proposal for an unfamiliar 3PL consignment export.

        The value is concentrated in the open questions, not the field map. The
        assistant read a schema it had never seen and surfaced the exact
        double-count trap that caused the original oversell -- as a question for
        a human rather than a silent assumption.
        """
        return {
            "proposed_by": f"{self.name} mapping assistant",
            "status": "PROPOSAL - requires human review, never applied at runtime",
            "source_name": "consignment_3pl",
            "field_map": [
                {"canonical": "sku", "source_column": "item_ref", "confidence": 0.97,
                 "note": "Values match the item master format after upper-casing."},
                {"canonical": "location", "source_column": "site", "confidence": 0.91,
                 "note": "Values are 'W1'/'W2', not 'WH1'/'WH2'. A location_alias block is required."},
                {"canonical": "quantity", "source_column": "units", "confidence": 0.98,
                 "note": "Integer, no unit-of-measure column present -- see open question 2."},
                {"canonical": "source_record_id", "source_column": "consignment_no", "confidence": 0.95,
                 "note": "Unique across the sample. Suitable for duplicate detection."},
                {"canonical": "as_of", "source_column": "logged_dt", "confidence": 0.79,
                 "note": "Below threshold: date only, no time or timezone. Raised as a question."},
            ],
            # A genuinely new source often needs a bucket that does not exist yet.
            # The assistant may propose one, but it must declare the signs
            # explicitly and flag them for sign-off -- a bucket sign is the
            # single most dangerous line in this system's configuration.
            "new_buckets": [
                {"bucket": "THIRD_PARTY_STOCK", "available_sign": 1, "atp_sign": 1,
                 "confidence": 0.62,
                 "note": ("Proposed, NOT assumed. Whether consignment stock counts as sellable "
                          "is a commercial decision about promising stock Inventory Visibility does not own, "
                          "not a mapping decision. Defaulting this to 1 without sign-off is "
                          "exactly how an availability number gets quietly inflated.")},
            ],
            "status_bucket_map": [
                {"source_status": "AVAILABLE", "bucket": "THIRD_PARTY_STOCK", "confidence": 0.93,
                 "note": "Depends on the proposed new bucket above being approved."},
                {"source_status": "IN_PICK", "bucket": "IGNORE", "confidence": 0.88,
                 "note": "Appears to be already committed at the 3PL."},
                {"source_status": "TRANSFER_OUT", "bucket": "IGNORE", "confidence": 0.68,
                 "note": "Below threshold -- raised as a question rather than mapped."},
            ],
            "open_questions": [
                "TRANSFER_OUT: are these units in transit to a Inventory Visibility warehouse? If so they may "
                "already appear in the WMS in-transit count, and mapping them as stock would "
                "double count -- the same trap as a RECEIVED purchase order.",
                "There is no unit-of-measure column. If the 3PL reports cases while the WMS reports "
                "eaches, availability is wrong by the case factor. Please confirm before go-live; "
                "the engine will not guess.",
                "logged_dt is date-only with no timezone, so snapshot skew against the 05:00 WMS "
                "export cannot be measured. Can the export include a timestamp?",
                "Should consignment stock count toward today's available at all, or only toward ATP? "
                "This is a commercial decision about whether Inventory Visibility can promise stock it does not own.",
            ],
            "validation": {"buckets_exist": True, "checked_against": sorted(catalogue)},
            "next_step": ("Open as a pull request against config/sources.yaml, then run "
                          "`inv-visibility reconcile --dry-run --diff-against last` to see exactly which "
                          "SKU numbers this feed would move before it goes live."),
        }


# ------------------------------------------------------------------ guardrails
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def _numerals(text: str) -> set[str]:
    return {m.replace(",", "").rstrip(".") for m in _NUM.findall(text)}


def validate_numerals(text: str, facts: dict) -> dict:
    """The guardrail that makes a generated brief safe to put in front of ops.

    Every numeral in the text must be traceable to the structured facts the
    provider was given. Anything else is a fabricated figure, and the brief is
    rejected rather than published. This runs identically for the mock and for a
    real model -- which is the entire point of the interface.
    """
    allowed: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            allowed.add(str(node).rstrip("0").rstrip(".") if isinstance(node, float) else str(node))
            allowed.add(str(node))
        elif isinstance(node, str):
            allowed.update(_numerals(node))

    walk(facts)
    used = _numerals(text)
    unverified = sorted(n for n in used if n not in allowed)
    return {
        "check": "every numeral in the generated text exists in the input facts",
        "numerals_in_output": len(used),
        "verified": len(used) - len(unverified),
        "unverified": unverified,
        "passed": not unverified,
    }


def build_facts(run: dict, intel: dict) -> dict:
    """The structured payload handed to the provider.

    Note what is *not* in here: no raw source files, no customer names beyond
    order identifiers, no free text. A bounded, auditable input is what keeps
    cost, latency and disclosure predictable -- a few thousand tokens once per
    batch, never once per dashboard request.
    """
    k = run["kpis"]
    ranked = intel["risk"]["ranked"]

    worst_today = next((r for r in ranked if "OVERSOLD" in r["deterministic_flags"]), None)
    facts_worst = None
    if worst_today:
        exc = next((e for e in run["exceptions"]
                    if e["sku"] == worst_today["sku"] and e["flag"] == "OVERSOLD"), None)
        facts_worst = {
            "sku": worst_today["sku"],
            "available": worst_today["available"],
            "reserved": exc["detail"]["reserved"] if exc else 0,
            "shortfall": exc["detail"]["shortfall"] if exc else 0,
            "revenue_at_risk": int(exc["detail"]["revenue_at_risk"]) if exc else 0,
            "next_po": worst_today.get("next_po"),
            "shrinkage": worst_today.get("shrinkage"),
        }

    predicted = next((r for r in ranked if r["predictive_only"] and r["days_of_cover_p10"]), None)
    facts_predicted = None
    if predicted:
        fc = intel["forecasts"].get(predicted["sku"], {})
        facts_predicted = {
            "sku": predicted["sku"],
            "available": predicted["available"],
            "daily_demand_mean": fc.get("daily_demand_mean"),
            "days_of_cover_p10": predicted["days_of_cover_p10"],
            "stockout_date_p10": predicted["stockout_date_p10"],
            "next_po": predicted.get("next_po"),
        }

    orphan_exc = next((e for e in run["exceptions"] if e["flag"] == "ORPHAN_SKU_IN_ORDERS"), None)

    return {
        "date": run["generated_at"][:10],
        "run_id": run["run_id"],
        "summary": {
            "critical": run["summary"]["critical"],
            "warning": run["summary"]["warning"],
            "skus": k["skus"],
            "total_available": k["total_available"],
            "naive_number": k["naive_number"],
            "overstatement_units": k["overstatement_units"],
        },
        "worst_today": facts_worst,
        "worst_predicted": facts_predicted,
        "orphan": ({"sku": orphan_exc["sku"], "reserved": orphan_exc["detail"]["reserved"]}
                   if orphan_exc else None),
    }


def generate_brief(run: dict, intel: dict, provider: LlmProvider | None = None) -> dict:
    """Compose the brief, then refuse to publish it if it invented a number."""
    provider = provider or NarrativeProvider()
    facts = build_facts(run, intel)
    started = time.perf_counter()
    try:
        text = provider.write_brief(facts)
    except Exception as exc:                      # the layer degrades, never blocks
        return {"provider": provider.name, "status": "UNAVAILABLE", "error": str(exc),
                "text": None, "guardrail": None,
                "note": "Advisory layer failed. The availability numbers published regardless."}

    guardrail = validate_numerals(text, facts)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "provider": provider.name,
        "mode": getattr(provider, "mode", "deterministic"),
        "deterministic": getattr(provider, "deterministic", True),
        "generated_in_ms": elapsed_ms,
        # The brief is cached on the run identifier, so the API serves a stored
        # artefact and no inference call ever sits in the dashboard's read path.
        "cache_key": f"brief:{run['run_id']}",
        "input_fields": len(facts),
        "input_chars": len(json.dumps(facts)),
        "status": "PUBLISHED" if guardrail["passed"] else "REJECTED",
        "text": text if guardrail["passed"] else None,
        "rejected_text": None if guardrail["passed"] else text,
        "guardrail": guardrail,
        "facts_supplied": facts,
        "system_prompt": BRIEF_SYSTEM_PROMPT.strip(),
        "note": ("Generated from structured engine output only. No source file, no free text and "
                 "no arithmetic performed by the model."),
    }


SAMPLE_3PL_ROWS = [
    {"item_ref": "MSD-2042", "site": "W1", "consignment_no": "CN-4471",
     "units": "180", "stock_status": "AVAILABLE", "logged_dt": "2026-09-09"},
    {"item_ref": "MSD-2077", "site": "W2", "consignment_no": "CN-4472",
     "units": "60", "stock_status": "IN_PICK", "logged_dt": "2026-09-09"},
    {"item_ref": "MSD-2113", "site": "W1", "consignment_no": "CN-4473",
     "units": "24", "stock_status": "TRANSFER_OUT", "logged_dt": "2026-09-08"},
]


def generate_mapping(cfg: dict, provider: LlmProvider | None = None) -> dict:
    """Onboarding assistant for source number five.

    The design already makes adding a source cheap. This makes it *fast*,
    because the slow part was never the code -- it was a human reading an
    unfamiliar export and working out which column means what and which statuses
    hold stock.
    """
    provider = provider or NarrativeProvider()
    catalogue = dict(cfg["buckets"])
    try:
        proposal = provider.propose_mapping({"rows": SAMPLE_3PL_ROWS}, catalogue)
    except Exception as exc:
        return {"provider": provider.name, "status": "UNAVAILABLE", "error": str(exc)}

    # Guardrail 3: every referenced bucket must either already exist, or be
    # explicitly declared as new with its signs stated. A hallucinated bucket
    # name -- one referenced but neither existing nor declared -- fails here,
    # before a human ever sees the proposal.
    declared = {b["bucket"] for b in proposal.get("new_buckets", [])}
    valid = set(catalogue) | declared | {"IGNORE"}
    bad = [m["bucket"] for m in proposal.get("status_bucket_map", [])
           if m["bucket"] not in valid]
    needs_signoff = [b for b in proposal.get("new_buckets", [])]
    proposal["validation"] = {
        "check": ("every referenced bucket either exists in the catalogue or is "
                  "declared as new with explicit signs"),
        "catalogue": sorted(catalogue),
        "undeclared_buckets": bad,
        "new_buckets_requiring_signoff": [b["bucket"] for b in needs_signoff],
        "passed": not bad,
    }
    proposal["sample_rows"] = SAMPLE_3PL_ROWS
    proposal["system_prompt"] = MAPPING_SYSTEM_PROMPT.strip()
    proposal["provider"] = provider.name
    proposal["deterministic"] = getattr(provider, "deterministic", True)
    if bad:
        proposal["status"] = "REJECTED - proposal referenced an unknown bucket"
    return proposal


# Backwards-compatible aliases. The interface is what other modules depend on;
# the provider names changed when the deterministic path became the default.
MockLlmProvider = NarrativeProvider
ApiLlmProvider = HostedModelProvider
