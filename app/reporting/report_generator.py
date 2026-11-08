"""Builds the structured (machine-readable) verification report.

The report separates *observed facts* (checks, evidence, verdict - all
computed deterministically) from optional *model-generated* narrative text.
"""

from __future__ import annotations

from typing import Any

from app.collector.event_schema import ExecutionTrace
from app.interpretation.verification_plan import VerificationPlan
from app.utils.hashing import hash_obj, to_jsonable
from app.utils.timeutil import iso, utcnow
from app.verification.models import ChangeKind, CheckStatus, VerificationOutcome

REPORT_SCHEMA_VERSION = "1.0"


def build_report(
    *,
    report_id: str,
    execution: Any,
    plan: VerificationPlan,
    outcome: VerificationOutcome,
    trace: ExecutionTrace,
    narrative: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks = [c.model_dump(mode="json") for c in outcome.checks]
    side = [c.model_dump(mode="json") for c in outcome.changes if c.kind in (ChangeKind.SIDE_EFFECT, ChangeKind.UNCERTAIN)]
    report: dict[str, Any] = {
        "report_id": report_id,
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": iso(utcnow()),
        "execution_id": execution.id,
        "instruction": execution.task_description,
        "environment": execution.environment,
        "agent": {
            "name": execution.agent_name,
            "framework": execution.agent_framework,
            "configuration": execution.agent_config,
        },
        "agent_claim": {
            "text": execution.agent_claim,
            "claimed_status": outcome.trace.claimed_status,
            "treated_as": "agent-reported information (never proof)",
        },
        "interpretation": {
            "interpreter": plan.interpreter,
            "headline": plan.headline(),
            "requirements": [r.model_dump() for r in plan.requirements],
            "warnings": plan.warnings,
            "ambiguities": plan.ambiguities,
            "required_approval": plan.requires_approval,
            "approved_by": execution.approved_by,
        },
        "expected_state": outcome.expected_state,
        "actual_state": outcome.actual_state,
        "verdict": outcome.verdict.value,
        "decision_rule": outcome.rule,
        "summary": outcome.summary,
        "diagnosis": outcome.diagnosis,
        "reasons": [r.model_dump() for r in outcome.reasons],
        "checks": checks,
        "passed_checks": [c["id"] for c in checks if c["status"] == CheckStatus.PASSED.value],
        "failed_checks": [c["id"] for c in checks if c["status"] == CheckStatus.FAILED.value],
        "inconclusive_checks": [c["id"] for c in checks if c["status"] == CheckStatus.INCONCLUSIVE.value],
        "side_effects": side,
        "changes": [c.model_dump(mode="json") for c in outcome.changes],
        "state_diff_summary": outcome.diff_summary,
        "evidence": [e.model_dump(mode="json") for e in outcome.evidence],
        "missing_evidence": outcome.missing_evidence,
        "trace_assessment": outcome.trace.model_dump(mode="json"),
        "next_steps": outcome.next_steps,
        "durations_ms": {
            "interpretation": execution.interpretation_ms,
            "execution": execution.execution_ms,
            "verification": outcome.duration_ms,
        },
        "integrity": {
            "trace_events": len(trace.events),
            "trace_chain_valid": trace.integrity.valid,
            "trace_head_hash": trace.events[-1].event_hash if trace.events else None,
            "evidence_items": len(outcome.evidence),
        },
        "facts_vs_model": {
            "observed_facts": "Verdict, checks, side effects and evidence are computed deterministically from "
            "independently observed state and the recorded trace. No language model participates in the verdict.",
            "model_generated_narrative": narrative,
        },
    }
    report = to_jsonable(report)
    report["integrity"]["report_hash"] = hash_obj({k: v for k, v in report.items() if k != "integrity"})
    return report
