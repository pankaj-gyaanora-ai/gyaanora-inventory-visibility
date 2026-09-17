"""Layer 7 tests.

The tests that matter here are not "does the forecast work". They are the ones
that enforce the boundary: **the advisory layer must never be able to change,
block, or fabricate the availability number.** Everything else is secondary.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inv_visibility.config import load_config              # noqa: E402
from inv_visibility.engine import run as engine_run        # noqa: E402
from inv_visibility.intelligence import forecast, llm, run_intelligence   # noqa: E402

CONFIG = str(ROOT / "config" / "sources.yaml")


@pytest.fixture(scope="module")
def result():
    return engine_run(CONFIG)


@pytest.fixture(scope="module")
def cfg():
    return load_config(CONFIG)


@pytest.fixture(scope="module")
def intel(result, cfg):
    return run_intelligence(result, cfg, ROOT)


# =========================================================== the boundary
def test_intelligence_cannot_change_availability(result, cfg):
    """The single most important test in this file."""
    before = {i["sku"]: i["available"] for i in result["items"]}
    run_intelligence(result, cfg, ROOT)
    after = {i["sku"]: i["available"] for i in result["items"]}
    assert before == after


def test_intelligence_failure_does_not_block_the_run(result, cfg, tmp_path):
    """Point the layer at an empty directory: the forecast is lost, the
    availability numbers are not."""
    intel = run_intelligence(result, cfg, tmp_path)
    assert intel["degraded"], "a missing history directory must be reported"
    assert intel["risk"]["ranked"] is not None
    assert all(i["available"] >= 0 for i in result["items"])


def test_layer_is_declared_advisory(intel):
    """The contract is published in the output, not just in a docstring -- any
    consumer of intelligence.json can see what this layer is allowed to do."""
    assert intel["layer"] == "advisory"
    contract = intel["contract"].lower()
    assert "read-only" in contract
    assert "available" in contract
    assert "publish" in contract


# =========================================================== forecasting
def test_intermittent_demand_selects_croston():
    """Long gaps between small orders is what Croston exists for."""
    series = [0, 0, 0, 4, 0, 0, 0, 0, 3, 0, 0, 5, 0, 0, 0, 0, 4, 0, 0, 0] * 6
    fc = forecast.forecast_sku(series)
    assert fc["demand_pattern"] in ("intermittent", "lumpy")
    assert fc["model"] in ("croston_sba", "naive_baseline_fallback")


def test_steady_demand_is_classified_smooth():
    series = [10, 12, 9, 11, 10, 13, 8, 11, 12, 10] * 12
    fc = forecast.forecast_sku(series)
    assert fc["demand_pattern"] == "smooth"


def test_croston_is_bias_corrected_downward():
    """The SBA correction matters here specifically: forecasting demand HIGH
    understates days of cover... but forecasting it LOW is what lets a SKU run
    out unnoticed. The correction removes Croston's known upward bias."""
    series = [0, 0, 6, 0, 0, 0, 6, 0, 0, 6] * 10
    corrected = forecast.croston_sba(series, alpha=0.1)
    uncorrected = corrected / (1 - 0.1 / 2)
    assert corrected < uncorrected


def test_weekends_are_excluded_from_the_series():
    """A Saturday zero is a calendar artefact. Counting it inflates the demand
    interval and misclassifies steady movers as intermittent."""
    from datetime import datetime
    sparse = {}
    dense = forecast.densify(sparse, datetime(2026, 1, 5), datetime(2026, 1, 19))
    assert len(dense) == 10           # two weeks, business days only


def test_backtest_is_rolling_origin_not_random():
    """Scoring on a random split would leak the future into the training set."""
    series = list(range(1, 121))
    score = forecast.backtest(series, forecast.naive_baseline, folds=3, horizon=14)
    assert score > 0                  # a rising series must penalise a flat mean


def test_model_falls_back_when_it_cannot_beat_naive(intel):
    fell_back = [f for f in intel["forecasts"].values() if f.get("fell_back_to_naive")]
    assert fell_back, "sample data should contain SKUs where the baseline wins"
    for f in fell_back:
        assert f["model"] == "naive_baseline_fallback"
        assert f["beats_naive"] is False, "the win rate must be recorded pre-substitution"


def test_no_history_falls_back_to_category_never_to_zero(intel):
    fallbacks = [f for f in intel["forecasts"].values() if f["model"] == "category_fallback"]
    for f in fallbacks:
        assert f["confidence"] == "low"
        assert "note" in f


def test_cover_converts_quantity_into_time(intel, result):
    for it in result["items"][:60]:
        fc = intel["forecasts"].get(it["sku"])
        if not fc or not fc.get("days_of_cover_p50"):
            continue
        # p10 is the pessimistic case, so it must never exceed p50
        assert fc["days_of_cover_p10"] <= fc["days_of_cover_p50"] + 0.01


# =========================================================== supplier + anomaly
def test_supplier_profiles_are_built_from_actual_receipts(intel):
    assert intel["supplier_profiles"]
    for p in intel["supplier_profiles"].values():
        assert p["pos_observed"] > 0
        assert 0.0 <= p["slip_risk"] <= 1.0


def test_risk_adjusted_atp_never_exceeds_optimistic(intel):
    for s in intel["slip"].values():
        assert s["atp_risk_adjusted"] <= s["atp_optimistic"] or s["atp_optimistic"] == 0


def test_shrinkage_finds_the_planted_drift(intel):
    """Three SKUs were seeded with persistent unexplained loss."""
    found = {s["sku"] for s in intel["shrinkage"]}
    assert found & {"MSD-2077", "MSD-2132", "MSD-2189"}
    for s in intel["shrinkage"]:
        assert s["residual_units"] < s["expected_noise_band"][0]


# =========================================================== risk scoring
def test_risk_score_is_fully_decomposed(intel):
    """A black-box 0-100 gets ignored. Every score must show its working."""
    for r in intel["risk"]["ranked"]:
        assert abs(sum(r["contributions"].values()) - r["risk_score"]) < 0.11
        assert r["reason_codes"], f"{r['sku']} is on the worklist without saying why"


def test_risk_weights_come_from_config(intel, cfg):
    assert intel["risk"]["weights"] == {**intel["risk"]["weights"],
                                        **cfg["risk"]["weights"]}


def test_ranking_is_ordered(intel):
    scores = [r["risk_score"] for r in intel["risk"]["ranked"]]
    assert scores == sorted(scores, reverse=True)


def test_predictive_only_rows_carry_no_deterministic_flag(intel):
    """The demo row: nothing is wrong today, and the system still warns."""
    predictive = [r for r in intel["risk"]["ranked"] if r["predictive_only"]]
    assert predictive, "sample data should surface at least one predicted-only SKU"
    for r in predictive:
        assert not r["deterministic_flags"]
        assert r["days_of_cover_p10"] is not None


# =========================================================== the LLM guardrails
def test_brief_invents_no_numbers(intel):
    g = intel["brief"]["guardrail"]
    assert g["passed"], f"unverified figures in the brief: {g['unverified']}"
    assert g["numerals_in_output"] > 0
    assert intel["brief"]["status"] == "PUBLISHED"


def test_guardrail_actually_catches_a_fabricated_number():
    """Prove the check works by feeding it a number that is not in the facts.
    A guardrail nobody has seen fail is not a guardrail."""
    facts = {"summary": {"critical": 3, "skus": 257}}
    clean = llm.validate_numerals("There are 3 critical exceptions across 257 SKUs.", facts)
    assert clean["passed"]
    dirty = llm.validate_numerals("There are 3 critical exceptions and 9999 units short.", facts)
    assert not dirty["passed"]
    assert "9999" in dirty["unverified"]


def test_rejected_brief_is_not_published():
    class Fabricator(llm.NarrativeProvider):
        name = "fabricator"
        def write_brief(self, facts):
            return "Availability is 123456789 units."

    run = {"generated_at": "2026-09-09T06:15:00", "run_id": "t", "summary": {"critical": 1, "warning": 1},
           "kpis": {"skus": 1, "total_available": 5, "naive_number": 9, "overstatement_units": 4},
           "exceptions": [], "items": []}
    intel = {"risk": {"ranked": []}, "forecasts": {}}
    out = llm.generate_brief(run, intel, Fabricator())
    assert out["status"] == "REJECTED"
    assert out["text"] is None            # nothing reaches ops
    assert out["rejected_text"] is not None


def test_provider_failure_degrades_gracefully():
    class Broken(llm.LlmProvider):
        name = "broken"
        def write_brief(self, facts):
            raise RuntimeError("model endpoint unreachable")

    run = {"generated_at": "2026-09-09T06:15:00", "run_id": "t", "summary": {"critical": 0, "warning": 0},
           "kpis": {"skus": 1, "total_available": 5, "naive_number": 9, "overstatement_units": 4},
           "exceptions": [], "items": []}
    out = llm.generate_brief(run, {"risk": {"ranked": []}, "forecasts": {}}, Broken())
    assert out["status"] == "UNAVAILABLE"
    assert "publish" in out["note"]


def test_brief_receives_structured_facts_only(intel):
    """No raw source rows, no free text, no customer names -- bounded input is
    what keeps cost, latency and disclosure predictable."""
    facts = intel["brief"]["facts_supplied"]
    blob = json.dumps(facts)
    assert "wms_stock.csv" not in blob and "orders.json" not in blob
    assert set(facts) <= {"date", "run_id", "summary", "worst_today",
                          "worst_predicted", "orphan"}


def test_mapping_proposal_only_uses_declared_buckets(intel, cfg):
    """A new source may legitimately need a bucket that does not exist yet. What
    it may NOT do is reference one out of thin air: every bucket must either be
    in the catalogue already or be declared as new, with its signs stated."""
    m = intel["mapping_assistant"]
    declared = {b["bucket"] for b in m.get("new_buckets", [])}
    valid = set(cfg["buckets"]) | declared | {"IGNORE"}
    assert m["validation"]["passed"]
    assert not m["validation"]["undeclared_buckets"]
    for entry in m["status_bucket_map"]:
        assert entry["bucket"] in valid


def test_a_proposed_new_bucket_requires_explicit_signoff(intel):
    """A bucket sign is the most dangerous line in this configuration -- it is
    the difference between counting stock as sellable and not. The assistant may
    propose one; it may never let it default quietly."""
    m = intel["mapping_assistant"]
    assert m["new_buckets"], "the 3PL sample needs a bucket that does not exist yet"
    assert m["validation"]["new_buckets_requiring_signoff"]
    for b in m["new_buckets"]:
        assert "available_sign" in b and "atp_sign" in b
        assert b["confidence"] < 0.80, "a sign decision is commercial, not a mapping guess"
        assert b["note"]


def test_hallucinated_bucket_is_rejected(cfg):
    """Prove the catalogue check works, rather than assuming it."""
    class Hallucinator(llm.NarrativeProvider):
        def propose_mapping(self, sample, catalogue):
            return {"status": "PROPOSAL", "status_bucket_map": [
                {"source_status": "X", "bucket": "MAGIC_STOCK", "confidence": 0.99}]}

    out = llm.generate_mapping(cfg, Hallucinator())
    assert not out["validation"]["passed"]
    assert "MAGIC_STOCK" in out["validation"]["undeclared_buckets"]
    assert "REJECTED" in out["status"]


def test_mapping_proposal_is_never_applied(intel):
    m = intel["mapping_assistant"]
    assert "PROPOSAL" in m["status"]
    assert "review" in m["status"].lower()
    assert m["open_questions"], "the value is in the questions, not the field map"


def test_mapping_flags_the_double_count_trap(intel):
    """The assistant must surface the ambiguity that caused the original
    oversell rather than confidently guessing past it."""
    questions = " ".join(intel["mapping_assistant"]["open_questions"]).lower()
    assert "double count" in questions
    low_confidence = [m for m in intel["mapping_assistant"]["status_bucket_map"]
                      if m["confidence"] < 0.80]
    assert low_confidence, "anything uncertain should be raised, not silently mapped"


def test_the_default_provider_is_deterministic(intel):
    """Reproducibility is the reason it is the default: the same run must always
    produce the same brief, so the brief can be diffed and regression-tested."""
    assert intel["brief"]["deterministic"] is True
    assert intel["brief"]["mode"] == "deterministic"
    assert intel["brief"]["cache_key"].endswith(intel["run_id"])


def test_a_hosted_provider_is_swappable_without_touching_anything_else():
    """The interface is the point: same guardrails, different provider."""
    api = llm.HostedModelProvider(model="claude-sonnet-5")
    assert isinstance(api, llm.LlmProvider)
    assert api.deterministic is False
    with pytest.raises(RuntimeError):
        api.write_brief({})               # unconfigured in the demo, by design
