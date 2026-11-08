"""Evidence ledger and execution-trace assessment.

* :class:`EvidenceLedger` gives every piece of evidence an id, a category
  (direct / indirect / agent-reported / derived / missing / contradictory) and
  a SHA-256 integrity hash over its canonical content.
* :func:`assess_trace` analyses the agent's trace *as indirect evidence*:
  hash-chain integrity, contradictions between events, orphan responses, and
  which records each tool call refers to (for side-effect attribution).
* :func:`enforce_direct_evidence` guarantees that no postcondition can pass on
  the strength of logs or claims alone.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from app.collector.event_schema import TraceEvent, TraceIntegrity
from app.utils.hashing import hash_obj
from app.utils.timeutil import utcnow
from app.verification.models import (
    CheckResult,
    CheckStatus,
    EvidenceCategory,
    EvidenceItem,
    TraceAssessment,
)

_FAILURE_WORDS = re.compile(r"\b(fail(ed|ure)?|could ?n[o']t|unable|error|not able|did ?n[o']t|cannot|can't|unsuccessful)\b", re.I)
_SUCCESS_WORDS = re.compile(r"\b(success(ful(ly)?)?|completed?|done|updated|created|deleted|removed|finished|saved|retrieved)\b", re.I)


def classify_claim(text: str | None) -> str:
    if not text:
        return "unknown"
    if _FAILURE_WORDS.search(text):
        return "failure"
    if _SUCCESS_WORDS.search(text):
        return "success"
    return "unknown"


class EvidenceLedger:
    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        self.items: list[EvidenceItem] = []
        self._suffix = execution_id.split("_", 1)[-1][:16]

    def add(
        self,
        evidence_type: str,
        category: EvidenceCategory,
        source: str,
        description: str,
        *,
        resource: str = "",
        result: str = "neutral",
        data: Any = None,
    ) -> EvidenceItem:
        content = {
            "execution_id": self.execution_id,
            "evidence_type": evidence_type,
            "category": category.value,
            "source": source,
            "resource": resource,
            "result": result,
            "description": description,
            "data": data,
        }
        item = EvidenceItem(
            id=f"ev_{self._suffix}_{len(self.items) + 1:03d}",
            execution_id=self.execution_id,
            evidence_type=evidence_type,
            category=category,
            source=source,
            resource=resource,
            result=result,
            description=description,
            data=data,
            integrity_hash=hash_obj(content),
            collected_at=utcnow(),
        )
        self.items.append(item)
        return item

    def by_id(self, evidence_id: str) -> EvidenceItem | None:
        return next((e for e in self.items if e.id == evidence_id), None)

    def verify_integrity(self) -> list[str]:
        """Return ids whose stored hash no longer matches their content."""
        bad = []
        for e in self.items:
            content = {
                "execution_id": e.execution_id, "evidence_type": e.evidence_type, "category": e.category.value,
                "source": e.source, "resource": e.resource, "result": e.result, "description": e.description, "data": e.data,
            }
            if hash_obj(content) != e.integrity_hash:
                bad.append(e.id)
        return bad


RefsFn = Callable[[str | None, Any, Any], list[tuple[str, str]]]


def assess_trace(
    events: list[TraceEvent], integrity: TraceIntegrity, refs_fn: RefsFn, mutating_tools: set[str]
) -> TraceAssessment:
    ta = TraceAssessment(chain_valid=integrity.valid, chain_detail=integrity.detail, total_events=len(events))
    calls: dict[str, TraceEvent] = {}
    responses: dict[str, list[TraceEvent]] = defaultdict(list)
    errors: dict[str, list[TraceEvent]] = defaultdict(list)
    claim_event: TraceEvent | None = None
    for ev in events:
        if ev.event_type == "tool_call":
            ta.tool_calls += 1
            if ev.call_id:
                if ev.call_id in calls:
                    ta.contradictions.append(f"Call id {ev.call_id} was issued more than once.")
                calls[ev.call_id] = ev
        elif ev.event_type == "tool_response":
            ta.tool_responses += 1
            if not ev.call_id or ev.call_id not in calls:
                ta.orphan_responses.append(ev.call_id or f"seq {ev.sequence}")
            else:
                responses[ev.call_id].append(ev)
        elif ev.event_type == "error" and ev.call_id:
            errors[ev.call_id].append(ev)
        elif ev.event_type == "agent_claim":
            claim_event = ev

    for orphan in ta.orphan_responses:
        ta.contradictions.append(f"Tool response {orphan} has no matching tool call in the trace.")

    mutated_collections: set[str] = set()
    for call_id, call in calls.items():
        resp = responses.get(call_id, [])
        statuses = {r.status for r in resp}
        is_mutation = (call.tool_name or "") in mutating_tools
        if not resp:
            ta.calls_without_response.append(call_id)
        if len(statuses) > 1:
            ta.contradictions.append(f"{call_id} ({call.tool_name}) has conflicting responses: {sorted(s or '?' for s in statuses)}.")
        succeeded = "success" in statuses
        if succeeded and errors.get(call_id):
            msgs = "; ".join(e.message for e in errors[call_id])[:200]
            ta.contradictions.append(
                f"{call_id} ({call.tool_name}) is logged as successful but an error event for the same call says: {msgs}"
            )
        output = resp[-1].output if resp else None
        refs = refs_fn(call.tool_name, call.input, output)
        for coll, key in refs:
            ta.referenced_records.setdefault(f"{coll}:{key}", []).append(call_id)
        if is_mutation:
            ta.mutating_calls += 1
            if isinstance(call.input, dict) and call.input.get("table"):
                mutated_collections.add(str(call.input["table"]))
            if succeeded and len(statuses) == 1:
                ta.successful_mutations += 1
                for coll, key in refs:
                    ta.successful_mutation_refs.setdefault(f"{coll}:{key}", []).append(call_id)
            elif resp:
                ta.failed_mutations += 1
    ta.mutated_collections = sorted(mutated_collections)

    if claim_event is not None:
        ta.claim_present = True
        ta.claim_text = claim_event.message
        out = claim_event.output if isinstance(claim_event.output, dict) else {}
        ta.claimed_status = out.get("claimed_status") or classify_claim(claim_event.message)
    ta.claim_contradicted_by_tools = (
        ta.claimed_status == "success" and ta.mutating_calls > 0 and ta.successful_mutations == 0
    )
    return ta


POSITIVE_CATEGORIES = {"precondition", "postcondition", "constraint", "side_effect"}


def enforce_direct_evidence(checks: list[CheckResult], ledger: EvidenceLedger) -> None:
    """A passing state check must cite at least one DIRECT evidence item.

    Checks that would pass on indirect / agent-reported evidence alone are
    downgraded to INCONCLUSIVE - an agent's word is never proof.
    """
    for check in checks:
        if check.status != CheckStatus.PASSED or check.category not in POSITIVE_CATEGORIES:
            continue
        cited = [ledger.by_id(eid) for eid in check.evidence_ids]
        if not any(e is not None and e.category == EvidenceCategory.DIRECT for e in cited):
            check.status = CheckStatus.INCONCLUSIVE
            check.reason_code = "INSUFFICIENT_EVIDENCE"
            check.explanation += " (Downgraded: no direct, independently observed evidence supports this check.)"
