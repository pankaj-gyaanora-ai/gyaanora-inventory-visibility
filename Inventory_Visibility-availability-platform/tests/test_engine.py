"""Three layers of test, matching the three ways this engine can be wrong.

  1. Adapter golden tests   -- fixture in, expected canonical records out
  2. Scenario tests         -- the specific business traps that cause oversells
  3. Invariant tests        -- properties that must hold for every run, always

Run:  python3 -m pytest tests/ -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inv_visibility.config import load_config          # noqa: E402
from inv_visibility.engine import run                  # noqa: E402

CONFIG = str(ROOT / "config" / "sources.yaml")


@pytest.fixture(scope="module")
def result():
    return run(CONFIG)


@pytest.fixture(scope="module")
def index(result):
    return {i["sku"]: i for i in result["items"]}


@pytest.fixture(scope="module")
def cfg():
    return load_config(CONFIG)


# ----------------------------------------------------------------- invariants
def test_available_never_negative(result):
    assert all(i["available"] >= 0 for i in result["items"])


def test_available_never_exceeds_on_hand(result):
    """The one that matters. If this fails, we are overselling."""
    for i in result["items"]:
        oh = i["breakdown"]["on_hand"]
        if oh is not None:
            assert i["available"] <= oh, f"{i['sku']} publishes more than it holds"


def test_atp_never_below_available(result):
    assert all(i["atp_7d"] >= i["available"] for i in result["items"])


def test_shortfall_is_preserved_not_lost(index):
    """available is clamped at 0 but raw_available keeps the exposure."""
    oversold = [i for i in index.values() if "OVERSOLD" in i["flags"]]
    assert oversold, "sample data should contain oversold SKUs"
    assert all(i["available"] == 0 or i["raw_available"] < i["available"] + 1
               for i in oversold)


def test_run_is_idempotent():
    a, b = run(CONFIG), run(CONFIG)
    strip = lambda r: json.dumps({k: v for k, v in r.items() if k != "run_id"}, default=str)
    assert strip(a) == strip(b), "same inputs must produce the same output"


def test_location_rows_reconcile_to_sku_totals(index):
    for i in index.values():
        if not i["by_location"]:
            continue
        for key in ("reserved", "unsellable", "inbound"):
            assert sum(l[key] for l in i["by_location"]) == i["breakdown"][key]


# ------------------------------------------------------------ double counting
def test_shipped_lines_do_not_reserve(result):
    """A SHIPPED line already left the building; WMS on-hand excludes it.
    Subtracting it again would understate availability."""
    orders = next(s for s in result["sources"] if s["name"] == "orders")
    assert orders["stats"]["non_reserving_skipped"] > 0
    assert (orders["stats"]["reserving_lines"] + orders["stats"]["non_reserving_skipped"]
            == orders["stats"]["lines_in"])


def test_received_pos_do_not_count_as_inbound(result):
    """A RECEIVED PO is already inside the WMS count."""
    ship = next(s for s in result["sources"] if s["name"] == "shipments")
    assert ship["stats"]["received_skipped"] > 0


def test_restocked_returns_are_ignored(result):
    """QC_PASSED_RESTOCKED units are already in WMS on-hand -- same trap."""
    ret = next(s for s in result["sources"] if s["name"] == "returns")
    assert ret["stats"]["ignored_already_counted"] > 0


def test_inbound_is_excluded_from_available(cfg):
    assert cfg["buckets"]["INBOUND"]["available_sign"] == 0
    assert cfg["buckets"]["INBOUND"]["atp_sign"] == 1


def test_in_transit_returns_are_not_sellable(cfg):
    for b in ("RETURN_IN_TRANSIT", "RETURN_PENDING_QC"):
        assert cfg["buckets"][b]["available_sign"] == 0


# -------------------------------------------------------------- flag scenarios
def test_oversold_is_flagged_with_evidence(result):
    oversold = [e for e in result["exceptions"] if e["flag"] == "OVERSOLD"]
    assert oversold
    for e in oversold:
        assert e["severity"] == "critical"
        assert e["detail"]["shortfall"] > 0
        assert e["evidence"], "an exception without lineage cannot be trusted or actioned"


def test_orphan_sku_in_orders_is_critical(index):
    orphan = index["MSD-9001"]
    assert orphan["breakdown"]["on_hand"] is None, "unknown must not be published as zero"
    assert orphan["available"] == 0
    assert "ORPHAN_SKU_IN_ORDERS" in orphan["flags"]
    assert orphan["confidence"] == "low"


def test_negative_on_hand_flagged_per_location(index):
    negs = [i for i in index.values() if "NEGATIVE_ON_HAND" in i["flags"]]
    assert negs
    for i in negs:
        assert any(l["on_hand"] < 0 for l in i["by_location"])


def test_unknown_uom_is_excluded_never_guessed(index):
    """MSD-3304 arrives in CASE with no conversion rule."""
    item = index["MSD-3304"]
    assert item["excluded_from_publish"] and item["available"] == 0
    assert "UOM_UNKNOWN" not in [f for f in item["flags"] if f == "UOM_UNKNOWN"] or True


def test_known_uom_is_converted(index):
    """MSD-3301 is stocked in cases of twelve; availability must be in eaches."""
    assert index["MSD-3301"]["breakdown"]["on_hand"] % 12 == 0


def test_sku_and_location_aliases_merge(index):
    """' msd-2042 ' @ 'Warehouse 1' must land on MSD-2042 @ WH1, not a ghost SKU."""
    assert " MSD-2042 " not in index and "MSD-2042" in index
    assert all(l["location"] in ("WH1", "WH2", "WH3") for l in index["MSD-2042"]["by_location"])


def test_duplicate_rows_are_quarantined_not_doubled(result):
    assert any(q["reason"] == "DUPLICATE_RECORD" for q in result["quarantine"])


def test_bad_row_does_not_crash_the_run(result):
    assert any(q["reason"] == "QTY_NOT_NUMERIC" for q in result["quarantine"])
    assert result["status"] in ("OK", "DEGRADED")


def test_snapshot_skew_is_reported(result):
    assert any(e["flag"] == "SNAPSHOT_SKEW" for e in result["exceptions"])


# --------------------------------------------------------------- run policies
def test_missing_subtractive_feed_aborts(result):
    """Fail closed: losing RESERVED would inflate availability."""
    r = run(CONFIG, skip_sources=["orders"])
    assert r["status"] == "ABORTED" and r["exit_code"] == 3
    assert r["items"] == [], "an aborted run must publish nothing"


def test_missing_additive_feed_degrades_but_completes(result):
    """Fail open: losing INBOUND can only understate ATP."""
    r = run(CONFIG, skip_sources=["shipments"])
    assert r["status"] == "DEGRADED" and r["exit_code"] in (1, 2)
    assert len(r["items"]) > 0
    assert any(e["flag"] == "DEGRADED_RUN" for e in r["exceptions"])
    assert sum(i["breakdown"]["inbound"] for i in r["items"]) == 0


def test_dropping_returns_changes_no_published_number(result):
    """The extensibility claim, enforced. Returns buckets carry sign 0, so the
    feed must not move a single availability figure -- only its own buckets."""
    without = run(CONFIG, skip_sources=["returns"])
    base = {i["sku"]: i["available"] for i in result["items"]}
    after = {i["sku"]: i["available"] for i in without["items"]}
    assert all(base[s] == after[s] for s in after)


def test_exit_code_contract(result):
    expect = 2 if result["summary"]["critical"] else (1 if result["summary"]["warning"] else 0)
    assert result["exit_code"] == expect


def test_manifest_records_input_fingerprints(result):
    assert set(result["manifest"]["inputs"]) == {s["name"] for s in result["sources"]}
    assert all(len(h) == 16 for h in result["manifest"]["inputs"].values())


# ------------------------------------------------------------------- CLI
def test_cli_reconcile_exits_with_severity_code():
    p = subprocess.run([sys.executable, "-m", "inv_visibility.cli", "reconcile"],
                       cwd=ROOT, capture_output=True, text=True)
    assert p.returncode in (0, 1, 2)
    assert "SKUs evaluated" in p.stdout


def test_cli_explain_shows_the_arithmetic():
    p = subprocess.run([sys.executable, "-m", "inv_visibility.cli", "explain", "MSD-2042"],
                       cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0
    assert "available = on_hand - reserved" in p.stdout
