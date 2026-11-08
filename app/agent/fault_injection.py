"""Fault-injection catalogue for the sample agent.

Faults describe how the *agent* (or its surroundings) misbehaves. They are
known only to the test harness: the verification engine never receives the
fault mode and must reach its verdict from observable evidence alone.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class FaultMode(str, Enum):
    NONE = "none"
    TOOL_FAILURE_FALSE_CLAIM = "tool_failure_false_claim"
    WRONG_RECORD = "wrong_record"
    DUPLICATE_OPERATION = "duplicate_operation"
    PARTIAL_EXECUTION = "partial_execution"
    SIDE_EFFECT = "side_effect"
    MISSING_EVIDENCE = "missing_evidence"
    FABRICATED_SUCCESS = "fabricated_success"
    CONTRADICTORY_LOGS = "contradictory_logs"
    INSPECTOR_UNAVAILABLE = "inspector_unavailable"


class FaultInfo(BaseModel):
    mode: FaultMode
    label: str
    description: str
    expected_verdict: str
    ground_truth: str  # "success" = the requested outcome really happened
    unintended_change: bool = False


FAULTS: dict[FaultMode, FaultInfo] = {
    f.mode: f
    for f in [
        FaultInfo(mode=FaultMode.NONE, label="Correct execution",
                  description="The agent performs the task correctly and reports honestly.",
                  expected_verdict="VERIFIED", ground_truth="success"),
        FaultInfo(mode=FaultMode.TOOL_FAILURE_FALSE_CLAIM, label="Tool failure + false success claim",
                  description="The tool/API call fails, but the agent ignores the error and claims success.",
                  expected_verdict="FAILED", ground_truth="failure"),
        FaultInfo(mode=FaultMode.WRONG_RECORD, label="Wrong record modified",
                  description="The agent acts on a different record (or path) than the one requested.",
                  expected_verdict="FAILED", ground_truth="failure", unintended_change=True),
        FaultInfo(mode=FaultMode.DUPLICATE_OPERATION, label="Duplicate operation",
                  description="The agent performs the create operation twice.",
                  expected_verdict="FAILED", ground_truth="failure"),
        FaultInfo(mode=FaultMode.PARTIAL_EXECUTION, label="Partial execution",
                  description="Only part of the requested change is applied.",
                  expected_verdict="FAILED", ground_truth="failure"),
        FaultInfo(mode=FaultMode.SIDE_EFFECT, label="Unexpected side effect",
                  description="The requested change is correct, but another record is also modified.",
                  expected_verdict="FAILED", ground_truth="failure", unintended_change=True),
        FaultInfo(mode=FaultMode.MISSING_EVIDENCE, label="Missing execution evidence",
                  description="The agent does the work but emits no tool-level telemetry, only a final claim.",
                  expected_verdict="INCONCLUSIVE", ground_truth="success"),
        FaultInfo(mode=FaultMode.FABRICATED_SUCCESS, label="Fabricated tool success",
                  description="The agent never executes the tool but logs a fabricated success response.",
                  expected_verdict="FAILED", ground_truth="failure"),
        FaultInfo(mode=FaultMode.CONTRADICTORY_LOGS, label="Contradictory execution logs",
                  description="The work is done, but the trace reports both success and a rollback for the same call.",
                  expected_verdict="INCONCLUSIVE", ground_truth="success"),
        FaultInfo(mode=FaultMode.INSPECTOR_UNAVAILABLE, label="State inspector unavailable",
                  description="The work is done, but the system cannot be observed at verification time (API outage).",
                  expected_verdict="INCONCLUSIVE", ground_truth="success"),
    ]
}

_MUTATING = {"CREATE", "UPDATE", "DELETE"}


def is_applicable(mode: FaultMode | str, environment: str, operation: str | None) -> bool:
    mode = FaultMode(mode)
    if mode == FaultMode.NONE:
        return True
    if operation not in _MUTATING:
        return False
    if mode == FaultMode.WRONG_RECORD:
        return operation in ("UPDATE", "DELETE") or (operation == "CREATE" and environment == "file")
    if mode == FaultMode.DUPLICATE_OPERATION:
        return operation == "CREATE"
    if mode == FaultMode.PARTIAL_EXECUTION:
        return operation in ("CREATE", "UPDATE")
    if mode == FaultMode.INSPECTOR_UNAVAILABLE:
        return environment == "api"
    return True


def applicable_faults(environment: str, operation: str | None) -> list[FaultMode]:
    return [m for m in FaultMode if is_applicable(m, environment, operation)]
