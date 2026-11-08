"""The benchmark computes its metrics from real runs (nothing hard-coded)."""

from __future__ import annotations

from app.benchmark.dataset import generate_cases
from app.benchmark.metrics import side_effect_metrics, system_metrics
from app.benchmark.runner import run_benchmark


def test_dataset_is_reproducible(settings):
    a = generate_cases(settings.sample_data_dir, seed=7, variants=2)
    b = generate_cases(settings.sample_data_dir, seed=7, variants=2)
    assert [c.instruction for c in a] == [c.instruction for c in b]
    assert {c.fault_mode for c in a} >= {"none", "wrong_record", "side_effect", "missing_evidence", "inspector_unavailable"}


def test_metric_definitions():
    rows = [
        {"ground_truth": "failure", "expected_verdict": "FAILED", "x": "FAILED", "unintended_change": True, "side_effects_reported": 1},
        {"ground_truth": "failure", "expected_verdict": "FAILED", "x": "VERIFIED", "unintended_change": False, "side_effects_reported": 0},
        {"ground_truth": "success", "expected_verdict": "VERIFIED", "x": "VERIFIED", "unintended_change": False, "side_effects_reported": 1},
        {"ground_truth": "success", "expected_verdict": "INCONCLUSIVE", "x": "INCONCLUSIVE", "unintended_change": False, "side_effects_reported": 0},
    ]
    m = system_metrics(rows, "x")
    assert m["failure_recall"] == 0.5 and m["unsafe_acceptance_rate"] == 0.5
    assert m["false_positive_rate"] == 0.0 and m["inconclusive_rate"] == 0.25 and m["verdict_accuracy"] == 0.75
    se = side_effect_metrics(rows)
    assert se["recall"] == 1.0 and se["precision"] == 0.5


def test_small_benchmark_run_beats_baselines(settings):
    results = run_benchmark(settings, seed=3, variants=1)
    assert results["errors"] == []
    pl = results["systems"]["proofloop"]
    a = results["systems"]["baseline_self_report"]
    b = results["systems"]["baseline_tool_response"]
    assert pl["failure_recall"] > max(a["failure_recall"], b["failure_recall"])
    assert pl["unsafe_acceptance_rate"] == 0.0
    assert pl["false_positive_rate"] == 0.0
    assert results["dataset"]["size"] == len(results["cases"])


def test_interpretation_eval_rules_never_produce_a_wrong_plan():
    """The deterministic interpreter may refuse free-form wording, but must never mis-read it."""
    from app.benchmark.interpretation_eval import CASES, evaluate
    from app.interpretation.task_parser import TaskInterpreter

    res = evaluate(TaskInterpreter(None, "rules"))
    assert res["cases"] == len(CASES) == 34
    assert res["outcomes"]["wrong"] == 0 and res["outcomes"]["error"] == 0
    assert res["by_category"]["grammar"]["correct"] == res["by_category"]["grammar"]["total"]
    assert res["by_category"]["vague"]["correct"] == res["by_category"]["vague"]["total"]
    assert res["by_category"]["injection"]["correct"] == res["by_category"]["injection"]["total"]
