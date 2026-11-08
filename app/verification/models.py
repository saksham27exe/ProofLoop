"""Data models for verification: evidence, checks, changes and verdicts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"


class EvidenceCategory(str, Enum):
    DIRECT = "direct"  # independently observed system state
    INDIRECT = "indirect"  # execution logs: describe actions, do not prove outcomes
    AGENT_REPORTED = "agent_reported"  # the agent's own statements
    DERIVED = "derived"  # deterministic computations over other evidence
    MISSING = "missing"  # evidence that should exist but does not
    CONTRADICTORY = "contradictory"  # evidence that conflicts with other evidence


class EvidenceItem(BaseModel):
    id: str
    execution_id: str
    evidence_type: str
    category: EvidenceCategory
    source: str
    resource: str = ""
    result: str = "neutral"  # supports | contradicts | neutral | unavailable
    description: str
    data: Any = None
    integrity_hash: str
    collected_at: datetime


class CheckResult(BaseModel):
    id: str
    name: str
    category: str  # precondition | postcondition | constraint | side_effect | evidence | consistency
    decisive: bool = True
    status: CheckStatus
    expected: Any = None
    actual: Any = None
    explanation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    reason_code: str | None = None


class FieldDelta(BaseModel):
    before: Any = None
    after: Any = None


class RecordChange(BaseModel):
    collection: str
    key: str
    change_type: str  # added | removed | modified
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    changed_fields: dict[str, FieldDelta] = Field(default_factory=dict)


class StateDiff(BaseModel):
    changes: list[RecordChange] = Field(default_factory=list)

    def for_collection(self, collection: str) -> list[RecordChange]:
        return [c for c in self.changes if c.collection == collection]

    def get(self, collection: str, key: str) -> RecordChange | None:
        for c in self.changes:
            if c.collection == collection and c.key == key:
                return c
        return None

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for c in self.changes:
            bucket = out.setdefault(c.collection, {"added": 0, "removed": 0, "modified": 0})
            bucket[c.change_type] += 1
        return out


class ChangeKind(str, Enum):
    EXPECTED = "expected"
    PERMITTED_DEPENDENCY = "permitted_dependency"
    DUPLICATE = "duplicate"
    SIDE_EFFECT = "side_effect"
    UNCERTAIN = "uncertain"


class DetectedChange(BaseModel):
    collection: str
    key: str
    label: str
    change_type: str
    kind: ChangeKind
    fields: dict[str, FieldDelta] = Field(default_factory=dict)
    reason: str
    attributed_calls: list[str] = Field(default_factory=list)
    attribution: str = ""


class VerdictReason(BaseModel):
    code: str
    message: str
    check_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class VerdictDecision(BaseModel):
    verdict: Verdict
    reasons: list[VerdictReason]
    rule: str  # which policy rule decided


class TraceAssessment(BaseModel):
    chain_valid: bool
    chain_detail: str = ""
    total_events: int = 0
    tool_calls: int = 0
    tool_responses: int = 0
    mutating_calls: int = 0
    successful_mutations: int = 0
    failed_mutations: int = 0
    contradictions: list[str] = Field(default_factory=list)
    orphan_responses: list[str] = Field(default_factory=list)
    calls_without_response: list[str] = Field(default_factory=list)
    claim_present: bool = False
    claimed_status: str | None = None
    claim_text: str | None = None
    claim_contradicted_by_tools: bool = False
    referenced_records: dict[str, list[str]] = Field(default_factory=dict)  # "collection:key" -> [call_ids]
    successful_mutation_refs: dict[str, list[str]] = Field(default_factory=dict)
    mutated_collections: list[str] = Field(default_factory=list)


class VerificationOutcome(BaseModel):
    execution_id: str
    verdict: Verdict
    rule: str
    reasons: list[VerdictReason]
    summary: str
    diagnosis: list[str] = Field(default_factory=list)
    checks: list[CheckResult]
    evidence: list[EvidenceItem]
    changes: list[DetectedChange]
    trace: TraceAssessment
    expected_state: dict[str, Any] = Field(default_factory=dict)
    actual_state: dict[str, Any] = Field(default_factory=dict)
    missing_evidence: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    diff_summary: dict[str, dict[str, int]] = Field(default_factory=dict)
    duration_ms: int = 0
