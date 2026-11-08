"""Benchmark runner: executes every case for real and scores three systems.

Each case runs in an isolated temporary data directory (fresh evidence
store + sandboxes reset to seed), through the same pipeline the dashboard
uses: interpret -> run agent -> capture state -> verify.
"""

from __future__ import annotations

import json
import platform
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app import __version__
from app.benchmark.baselines import baseline_self_report, baseline_tool_responses
from app.benchmark.dataset import BenchmarkCase, as_dicts, describe, generate_cases
from app.benchmark.metrics import (
    latency,
    per_fault,
    side_effect_metrics,
    system_metrics,
)
from app.config import Settings
from app.database.connection import Database
from app.database.models import Execution
from app.database.repositories import get_report
from app.environments.registry import EnvironmentRegistry
from app.llm.model_manager import ModelManager
from app.services.execution_service import ExecutionService
from app.services.runtime_settings import RuntimeSettings
from app.utils.timeutil import Stopwatch, iso, utcnow

SYSTEMS = ["proofloop", "baseline_self_report", "baseline_tool_response"]
TARGETS = {
    "failure_recall_min": 0.85,
    "false_positive_rate_max": 0.05,
    "improvement_over_best_baseline_pp_min": 20.0,
    "evidence_coverage_min": 0.95,
    "side_effect_recall_min": 0.85,
}


def run_benchmark(
    base: Settings,
    *,
    seed: int = 42,
    variants: int = 3,
    interpreter: str = "rules",
    model: str | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cases: list[BenchmarkCase] | None = None,
) -> dict[str, Any]:
    cases = cases if cases is not None else generate_cases(base.sample_data_dir, seed=seed, variants=variants)
    tmp = Path(tempfile.mkdtemp(prefix="proofloop-bench-"))
    total_timer = Stopwatch()
    try:
        settings = Settings(data_dir=tmp, sample_data_dir=base.sample_data_dir, ollama_url=base.ollama_url,
                            ollama_model=model or base.ollama_model, ollama_timeout_seconds=base.ollama_timeout_seconds,
                            allow_remote_ollama=base.allow_remote_ollama, password_iterations=1000)
        settings.ensure_dirs()
        db = Database(settings.db_path)
        db.create_all()
        registry = EnvironmentRegistry(settings.sandbox_dir, settings.sample_data_dir)
        registry.initialize()
        runtime = RuntimeSettings(ollama_model=model or base.ollama_model, interpreter_mode=interpreter,  # type: ignore[arg-type]
                                  llm_enabled=interpreter != "rules", llm_narrative_enabled=False)
        svc = ExecutionService(settings, db, registry, ModelManager(settings), runtime_override=runtime)

        rows: list[dict[str, Any]] = []
        for i, case in enumerate(cases, start=1):
            if progress:
                progress(i, len(cases), case.case_id)
            env = registry.get(case.environment)
            env.reset()
            svc.bump_env_version(case.environment)
            row: dict[str, Any] = {**as_dicts([case])[0]}
            try:
                exec_id = svc.create_execution(instruction=case.instruction, environment=case.environment,
                                               fault_mode=case.fault_mode, scenario_id=case.case_id)
                report = svc.run_and_verify(exec_id, approved=True, force=True)
                trace = svc.storage.load(exec_id)
                decisive = [c for c in report["checks"] if c["decisive"] and c["status"] != "skipped"]
                covered = sum(1 for c in decisive if c["evidence_ids"])
                with db.session() as s:
                    if get_report(s, exec_id) is None:
                        raise RuntimeError("report was not persisted")
                    ex = s.get(Execution, exec_id)
                    interp_ms, exec_ms, verify_ms = ex.interpretation_ms, ex.execution_ms, ex.verification_ms
                row.update(
                    execution_id=exec_id,
                    proofloop=report["verdict"],
                    baseline_self_report=baseline_self_report(trace.events),
                    baseline_tool_response=baseline_tool_responses(trace.events, env.schema.mutating_tools),
                    side_effects_reported=sum(1 for s in report["side_effects"] if s["kind"] == "side_effect"),
                    evidence_coverage=round(covered / len(decisive), 4) if decisive else 1.0,
                    interpreter=report["interpretation"]["interpreter"],
                    interpretation_ms=interp_ms, execution_ms=exec_ms, verification_ms=verify_ms,
                    summary=report["summary"], error=None,
                )
            except Exception as exc:  # noqa: BLE001 - recorded as a benchmark error, never hidden
                row.update(execution_id=None, proofloop="ERROR", baseline_self_report="ERROR",
                           baseline_tool_response="ERROR", side_effects_reported=0, evidence_coverage=0.0,
                           interpreter=interpreter, interpretation_ms=None, execution_ms=None, verification_ms=None,
                           summary="", error=f"{type(exc).__name__}: {exc}")
            rows.append(row)
        db.dispose()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    scored = [r for r in rows if r["proofloop"] != "ERROR"]
    systems = {s: system_metrics(scored, s) for s in SYSTEMS}
    pl = systems["proofloop"]
    best_baseline_recall = max(systems["baseline_self_report"]["failure_recall"] or 0,
                               systems["baseline_tool_response"]["failure_recall"] or 0)
    improvement_pp = round(((pl["failure_recall"] or 0) - best_baseline_recall) * 100, 1)
    side = side_effect_metrics(scored)
    coverage = round(sum(r["evidence_coverage"] for r in scored) / len(scored), 4) if scored else None
    results = {
        "generated_at": iso(utcnow()),
        "proofloop_version": __version__,
        "config": {"seed": seed, "variants": variants, "interpreter": interpreter,
                   "model": (model or base.ollama_model) if interpreter != "rules" else None,
                   "python": platform.python_version(), "platform": platform.platform(terse=True)},
        "dataset": describe(cases),
        "systems": systems,
        "improvement_over_best_baseline_pp": improvement_pp,
        "side_effect_detection": side,
        "evidence_coverage": coverage,
        "latency_ms": {
            "verification": latency([r["verification_ms"] for r in scored if r["verification_ms"] is not None]),
            "interpretation": latency([r["interpretation_ms"] for r in scored if r["interpretation_ms"] is not None]),
            "agent_execution": latency([r["execution_ms"] for r in scored if r["execution_ms"] is not None]),
        },
        "cost_per_verification_usd": 0.0,
        "cost_note": "All inference and verification run locally; no paid API is called.",
        "per_fault": per_fault(scored, SYSTEMS),
        "targets": {
            "failure_recall": {"target": f">= {TARGETS['failure_recall_min']}", "actual": pl["failure_recall"],
                               "met": (pl["failure_recall"] or 0) >= TARGETS["failure_recall_min"]},
            "false_positive_rate": {"target": f"<= {TARGETS['false_positive_rate_max']}", "actual": pl["false_positive_rate"],
                                    "met": (pl["false_positive_rate"] or 0) <= TARGETS["false_positive_rate_max"]},
            "improvement_over_best_baseline_pp": {"target": f">= {TARGETS['improvement_over_best_baseline_pp_min']}",
                                                  "actual": improvement_pp,
                                                  "met": improvement_pp >= TARGETS["improvement_over_best_baseline_pp_min"]},
            "evidence_coverage": {"target": f">= {TARGETS['evidence_coverage_min']}", "actual": coverage,
                                  "met": (coverage or 0) >= TARGETS["evidence_coverage_min"]},
            "side_effect_recall": {"target": f">= {TARGETS['side_effect_recall_min']}", "actual": side["recall"],
                                   "met": (side["recall"] or 0) >= TARGETS["side_effect_recall_min"]},
        },
        "errors": [{"case_id": r["case_id"], "error": r["error"]} for r in rows if r["error"]],
        "total_duration_ms": total_timer.ms,
        "cases": rows,
    }
    return results


def save_results(results: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "latest.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    stamp = results["generated_at"].replace(":", "").replace("-", "").split(".")[0]
    (out_dir / f"benchmark_{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return path


def markdown_summary(results: dict[str, Any]) -> str:
    s = results["systems"]
    names = {"proofloop": "ProofLoop", "baseline_self_report": "Baseline A (agent self-report)",
             "baseline_tool_response": "Baseline B (tool responses)"}

    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v * 100:.1f}%"

    lines = [
        f"Benchmark run {results['generated_at']} - {results['dataset']['size']} cases, interpreter: {results['config']['interpreter']}",
        "",
        "| System | Failure recall | Unsafe acceptance | False positive rate | Precision | Inconclusive | Verdict accuracy |",
        "|---|---|---|---|---|---|---|",
    ]
    for key in SYSTEMS:
        m = s[key]
        lines.append(f"| {names[key]} | {pct(m['failure_recall'])} | {pct(m['unsafe_acceptance_rate'])} | "
                     f"{pct(m['false_positive_rate'])} | {pct(m['precision'])} | {pct(m['inconclusive_rate'])} | {pct(m['verdict_accuracy'])} |")
    se = results["side_effect_detection"]
    lines += [
        "",
        f"Improvement in failure recall over the best baseline: {results['improvement_over_best_baseline_pp']} percentage points.",
        f"Side-effect detection: precision {pct(se['precision'])}, recall {pct(se['recall'])}.",
        f"Evidence coverage: {pct(results['evidence_coverage'])}. Verification latency p50 "
        f"{results['latency_ms']['verification']['p50']} ms, p95 {results['latency_ms']['verification']['p95']} ms.",
    ]
    return "\n".join(lines)
