"""Demand forecasting -- classical statistics, deliberately not an LLM.

Two reasons that distinction matters and is worth stating out loud:
  * these models are backtestable. WAPE against a naive baseline is a number you
    can defend. You cannot backtest a language model's opinion about demand.
  * a distributor's catalogue is mostly *intermittent* demand -- long runs of
    zero days punctuated by small orders. Croston and its SBA correction exist
    precisely for that shape. A single global model over all SKUs would be the
    wrong answer, and naming why is the point.

The output converts a quantity into a time. `available = 80` is meaningless on
its own: it is three weeks of cover for one SKU and six hours for another. Days
of cover is what ops actually decides on.

Nothing here ever touches `available`. Every field produced is advisory.
"""
from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


# --------------------------------------------------------------------- loading
def load_demand(path: Path) -> dict[tuple[str, str], dict[str, int]]:
    """(sku, location) -> {date: units}. Sparse; zero days are simply absent."""
    out: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            out[(r["sku"], r["location"])][r["date"]] = int(r["units_shipped"])
    return out


def densify(sparse: dict[str, int], start: datetime, end: datetime,
            business_days_only: bool = True) -> list[int]:
    """Fill in the zero days -- they matter enormously for intermittent demand.

    Weekends are excluded by default. Inventory Visibility ships Monday to Friday, so a
    Saturday zero is a calendar artefact, not a demand signal. Including them
    inflates the average demand interval and would misclassify every steady
    A-class mover as intermittent, which then selects the wrong model.
    """
    days = (end - start).days
    out = []
    for d in range(days):
        day = start + timedelta(days=d)
        if business_days_only and day.weekday() >= 5:
            continue
        out.append(sparse.get(day.date().isoformat(), 0))
    return out


# ------------------------------------------------------------- classification
def classify(series: list[int]) -> tuple[str, float, float]:
    """Syntetos-Boylan-Croston classification.

    ADI  = average interval between non-zero demands
    CV2  = squared coefficient of variation of the non-zero sizes

    The cut-offs (ADI 1.32, CV2 0.49) are the standard ones from the literature,
    which is worth saying -- they are not numbers invented for this demo.
    """
    nz = [v for v in series if v > 0]
    if len(nz) < 2:
        return "no_demand", 0.0, 0.0
    adi = len(series) / len(nz)
    mean = statistics.fmean(nz)
    cv2 = (statistics.pstdev(nz) / mean) ** 2 if mean else 0.0
    if adi < 1.32 and cv2 < 0.49:
        pattern = "smooth"
    elif adi >= 1.32 and cv2 < 0.49:
        pattern = "intermittent"
    elif adi < 1.32:
        pattern = "erratic"
    else:
        pattern = "lumpy"
    return pattern, round(adi, 2), round(cv2, 2)


# ------------------------------------------------------------------- the models
def croston_sba(series: list[int], alpha: float = 0.1) -> float:
    """Croston with the Syntetos-Boylan approximation.

    Plain Croston is known to be biased high; the (1 - alpha/2) factor corrects
    it. Forecasting high is exactly the direction that oversells, so on this
    system the correction is not optional.
    """
    nz = [(i, v) for i, v in enumerate(series) if v > 0]
    if len(nz) < 2:
        return statistics.fmean(series) if series else 0.0
    level = float(nz[0][1])
    interval = float(nz[1][0] - nz[0][0]) or 1.0
    prev = nz[0][0]
    for idx, val in nz[1:]:
        gap = max(idx - prev, 1)
        level += alpha * (val - level)
        interval += alpha * (gap - interval)
        prev = idx
    return (level / interval) * (1 - alpha / 2) if interval else 0.0


def seasonal_naive(series: list[int], period: int = 7) -> float:
    """Baseline for smooth SKUs: mean of the same weekday over recent weeks."""
    if len(series) < period * 2:
        return statistics.fmean(series) if series else 0.0
    recent = series[-period * 6:]
    return statistics.fmean(recent)


def naive_baseline(series: list[int]) -> float:
    """The benchmark every model must beat, or we ship the benchmark instead."""
    tail = series[-28:] or series
    return statistics.fmean(tail) if tail else 0.0


def _wape(actual: list[int], predicted: float) -> float:
    """Weighted absolute percentage error over the CUMULATIVE horizon.

    Deliberately not daily point accuracy. For intermittent demand, daily WAPE
    is close to meaningless -- predicting 0.8 units on a day that saw 0 and a
    day that saw 12 is "wrong" twice, yet the two-week total may be exactly
    right. And the two-week total is what the decision actually depends on:
    ops is asking "will this cover me until the PO lands", not "how many will
    ship next Tuesday". Scoring the horizon aggregate is both the honest metric
    and the decision-relevant one.
    """
    actual_total = sum(actual)
    predicted_total = predicted * len(actual)
    if actual_total == 0:
        return 0.0 if predicted_total == 0 else 1.0
    return abs(actual_total - predicted_total) / actual_total


def backtest(series: list[int], fn, folds: int = 3, horizon: int = 14) -> float:
    """Rolling-origin backtest. Train on the past, score the next `horizon` days,
    walk forward, average. Not a random split -- that would leak the future."""
    scores = []
    for f in range(folds):
        cut = len(series) - horizon * (f + 1)
        if cut < 30:
            continue
        pred = fn(series[:cut])
        scores.append(_wape(series[cut:cut + horizon], pred))
    return round(statistics.fmean(scores), 3) if scores else 0.0


def weekly(series: list[int], weeks: int = 16) -> list[int]:
    """Roll the business-day series into weekly totals, most recent last."""
    per_week = 5
    tail = series[-(weeks * per_week):]
    return [sum(tail[i:i + per_week]) for i in range(0, len(tail), per_week)]


# ------------------------------------------------------------------- forecasting
def forecast_sku(series: list[int]) -> dict:
    pattern, adi, cv2 = classify(series)
    model_fn = croston_sba if pattern in ("intermittent", "lumpy") else seasonal_naive
    model_name = "croston_sba" if model_fn is croston_sba else "seasonal_naive"

    mu = model_fn(series)
    nz = [v for v in series if v > 0]
    sigma = statistics.pstdev(nz) if len(nz) > 1 else mu * 0.5

    model_wape = backtest(series, model_fn)
    naive_wape = backtest(series, naive_baseline)

    # Reported honestly: whether the CHOSEN model beat the benchmark on its own
    # merits, recorded before any substitution. Measuring this after the
    # fallback would make the win rate 100% by construction, which is the kind
    # of number that quietly destroys trust in every other number on the page.
    beats_naive = bool(naive_wape) and model_wape <= naive_wape
    fell_back = False

    # If the model cannot beat the benchmark, ship the benchmark. Saying this
    # out loud is worth more than any model choice.
    if naive_wape and model_wape > naive_wape:
        mu, model_name = naive_baseline(series), "naive_baseline_fallback"
        model_wape, fell_back = naive_wape, True

    if len(nz) < 4:
        confidence = "low"
    elif model_wape is not None and model_wape <= 0.25:
        confidence = "high"
    elif model_wape is not None and model_wape <= 0.50:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "demand_pattern": pattern, "adi": adi, "cv2": cv2,
        "model": model_name,
        "daily_demand_mean": round(mu, 2),
        "daily_demand_p90": round(mu + 1.28 * sigma, 2),
        "backtest_wape": model_wape,
        "naive_wape": naive_wape,
        "beats_naive": beats_naive,
        "fell_back_to_naive": fell_back,
        "confidence": confidence,
        "observations": len(series), "non_zero_days": len(nz),
    }


def cover(available: int, fc: dict, as_of: datetime) -> dict:
    """Quantity -> time. p50 uses mean demand, p10 uses the p90 demand rate,
    which is the pessimistic case ops should plan against."""
    mu = fc["daily_demand_mean"]
    hi = fc["daily_demand_p90"] or mu
    if mu <= 0:
        return {"days_of_cover_p50": None, "days_of_cover_p10": None,
                "stockout_date_p50": None, "stockout_date_p10": None,
                "stockout_within_14d": False}
    d50 = available / mu
    d10 = available / hi if hi else d50
    return {
        "days_of_cover_p50": round(d50, 1),
        "days_of_cover_p10": round(d10, 1),
        "stockout_date_p50": (as_of + timedelta(days=math.floor(d50))).date().isoformat(),
        "stockout_date_p10": (as_of + timedelta(days=math.floor(d10))).date().isoformat(),
        "stockout_within_14d": d10 <= 14,
    }


def build(items: list[dict], history_path: Path, as_of: datetime,
          start: datetime) -> dict[str, dict]:
    """Forecast every SKU that has history. SKUs without it fall back to their
    category average and are marked low confidence -- never silently zero."""
    demand = load_demand(history_path)

    by_sku: dict[str, list[int]] = {}
    for (sku, _loc), sparse in demand.items():
        series = densify(sparse, start, as_of)
        if sku in by_sku:
            by_sku[sku] = [a + b for a, b in zip(by_sku[sku], series)]
        else:
            by_sku[sku] = series

    out: dict[str, dict] = {}
    category_rates: dict[str, list[float]] = defaultdict(list)
    for it in items:
        series = by_sku.get(it["sku"])
        if not series:
            continue
        fc = forecast_sku(series)
        fc.update(cover(it["available"], fc, as_of))
        # A compact weekly roll-up so the console can plot demand history
        # against the forecast rate without shipping 180 daily points per SKU.
        fc["weekly_history"] = weekly(series, weeks=16)
        fc["weekly_forecast"] = round(fc["daily_demand_mean"] * 5, 1)   # 5 shipping days
        out[it["sku"]] = fc
        category_rates[it["category"]].append(fc["daily_demand_mean"])

    # Hierarchical fallback for SKUs with no history of their own.
    for it in items:
        if it["sku"] in out:
            continue
        peers = category_rates.get(it["category"]) or [0.0]
        mu = statistics.fmean(peers)
        fc = {"demand_pattern": "unknown", "adi": 0.0, "cv2": 0.0,
              "model": "category_fallback", "daily_demand_mean": round(mu, 2),
              "daily_demand_p90": round(mu * 1.8, 2), "backtest_wape": None,
              "naive_wape": None, "beats_naive": None, "confidence": "low",
              "observations": 0, "non_zero_days": 0,
              "weekly_history": [], "weekly_forecast": round(mu * 5, 1),
              "note": f"No history. Using the {it['category']} category average."}
        fc.update(cover(it["available"], fc, as_of))
        out[it["sku"]] = fc
    return out
