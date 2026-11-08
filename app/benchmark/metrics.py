"""Benchmark metric definitions (computed only from recorded results).

Definitions (GT = ground truth from the fault-injection harness):

* failure_recall      = |GT failure AND verdict FAILED| / |GT failure|
  ("verification recall": genuinely failed executions correctly identified)
* unsafe_acceptance   = |GT failure AND verdict VERIFIED| / |GT failure|
* false_positive_rate = |GT success AND verdict FAILED| / |GT success|
* precision           = |GT failure AND verdict FAILED| / |verdict FAILED|
* success_acceptance  = |GT success AND verdict VERIFIED| / |GT success|
* inconclusive_rate   = |verdict INCONCLUSIVE| / |all cases|
* verdict_accuracy    = |verdict == expected verdict| / |all cases|
* side-effect precision/recall: "ProofLoop reported >= 1 side effect" vs
  "the harness injected an unintended change to another record".
* evidence_coverage   = share of decisive, non-skipped checks citing >= 1
  evidence item, averaged over cases.
"""

from __future__ import annotations

import statistics
from typing import Any


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def system_metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    gt_fail = [r for r in rows if r["ground_truth"] == "failure"]
    gt_ok = [r for r in rows if r["ground_truth"] == "success"]
    failed = [r for r in rows if r[key] == "FAILED"]
    return {
        "failure_recall": _ratio(sum(r[key] == "FAILED" for r in gt_fail), len(gt_fail)),
        "unsafe_acceptance_rate": _ratio(sum(r[key] == "VERIFIED" for r in gt_fail), len(gt_fail)),
        "false_positive_rate": _ratio(sum(r[key] == "FAILED" for r in gt_ok), len(gt_ok)),
        "precision": _ratio(sum(r["ground_truth"] == "failure" for r in failed), len(failed)),
        "success_acceptance_rate": _ratio(sum(r[key] == "VERIFIED" for r in gt_ok), len(gt_ok)),
        "inconclusive_rate": _ratio(sum(r[key] == "INCONCLUSIVE" for r in rows), len(rows)),
        "verdict_accuracy": _ratio(sum(r[key] == r["expected_verdict"] for r in rows), len(rows)),
        "counts": {
            "VERIFIED": sum(r[key] == "VERIFIED" for r in rows),
            "FAILED": sum(r[key] == "FAILED" for r in rows),
            "INCONCLUSIVE": sum(r[key] == "INCONCLUSIVE" for r in rows),
        },
    }


def side_effect_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(1 for r in rows if r["unintended_change"] and r["side_effects_reported"] > 0)
    fp = sum(1 for r in rows if not r["unintended_change"] and r["side_effects_reported"] > 0)
    fn = sum(1 for r in rows if r["unintended_change"] and r["side_effects_reported"] == 0)
    return {"precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn), "true_positives": tp,
            "false_positives": fp, "false_negatives": fn}


def latency(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {"mean": round(statistics.fmean(ordered), 1), "p50": round(statistics.median(ordered), 1),
            "p95": round(p95, 1), "max": round(ordered[-1], 1)}


def per_fault(rows: list[dict[str, Any]], systems: list[str]) -> list[dict[str, Any]]:
    out = []
    for fault in sorted({r["fault_mode"] for r in rows}):
        subset = [r for r in rows if r["fault_mode"] == fault]
        entry: dict[str, Any] = {"fault_mode": fault, "cases": len(subset), "expected_verdict": subset[0]["expected_verdict"]}
        for s in systems:
            entry[s] = {v: sum(r[s] == v for r in subset) for v in ("VERIFIED", "FAILED", "INCONCLUSIVE")}
            entry[f"{s}_correct"] = sum(r[s] == r["expected_verdict"] for r in subset)
        out.append(entry)
    return out
