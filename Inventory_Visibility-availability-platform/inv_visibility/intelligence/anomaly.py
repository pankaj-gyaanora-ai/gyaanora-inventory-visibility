"""Shrinkage detection from the conservation identity.

Inventory obeys an identity that has nothing to do with statistics:

    on_hand(today) = on_hand(yesterday) + receipts - shipments + adjustments

Anything left over is unexplained. A persistent negative residual means stock is
leaving the building without a transaction -- theft, damage, mis-picks, or a
broken integration.

Why this belongs in an *availability* system: it is the one oversell cause the
reconciliation engine cannot see. Every other flag says "your number is being
used wrongly". This one says "the WMS number you are trusting is itself drifting
above reality", which is a different and more uncomfortable problem.

Every immutable run is a datapoint, so a client with no history today starts
accumulating this from go-live.
"""
from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path


def detect(path: Path, window_days: int = 30, z_threshold: float = 3.0) -> list[dict]:
    if not path.exists():
        return []

    rows: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows[(r["sku"], r["location"])].append(r)

    findings = []
    for (sku, loc), entries in rows.items():
        entries.sort(key=lambda r: r["date"])
        residuals = []
        for r in entries:
            expected = (int(r["opening_on_hand"]) + int(r["receipts"])
                        - int(r["shipments"]) + int(r["adjustments"]))
            residuals.append(int(r["closing_on_hand"]) - expected)

        recent = residuals[-window_days:]
        if len(recent) < 10:
            continue
        total = sum(recent)
        sigma = statistics.pstdev(residuals) or 1.0
        # Noise band for a *sum* of n independent daily residuals scales with
        # sqrt(n), not n -- getting that wrong would flag half the catalogue.
        band = round(z_threshold * sigma * (len(recent) ** 0.5), 1)
        if total >= -band:
            continue

        per_day = total / len(recent)
        findings.append({
            "sku": sku, "location": loc,
            "flag": "SHRINKAGE_SUSPECTED",
            "severity": "warning",
            "residual_window_days": len(recent),
            "residual_units": total,
            "expected_noise_band": [-band, band],
            "z": round(total / (sigma * (len(recent) ** 0.5)), 2),
            "units_per_day": round(per_day, 2),
            "note": (f"Unexplained loss of about {abs(per_day):.1f} units/day at {loc}. "
                     f"The WMS count likely overstates sellable stock, so availability "
                     f"computed from it is optimistic."),
            "suggested_action": f"Raise a cycle count for {sku} at {loc} and review pick accuracy.",
        })

    findings.sort(key=lambda f: f["residual_units"])
    return findings
