"""Baseline success checkers that ProofLoop is compared against.

* **Baseline A - agent self-reporting:** trust the agent's final claim.
* **Baseline B - tool-response verification:** trust the status of the
  agent's state-changing tool/API responses.

Both only output VERIFIED or FAILED; neither can express uncertainty.
"""

from __future__ import annotations

from app.collector.event_schema import TraceEvent
from app.verification.evidence_validator import classify_claim


def baseline_self_report(events: list[TraceEvent]) -> str:
    claims = [e for e in events if e.event_type == "agent_claim"]
    if not claims:
        return "FAILED"
    claim = claims[-1]
    out = claim.output if isinstance(claim.output, dict) else {}
    status = out.get("claimed_status") or classify_claim(claim.message)
    return "VERIFIED" if status == "success" else "FAILED"


def baseline_tool_responses(events: list[TraceEvent], mutating_tools: set[str]) -> str:
    responses = [e for e in events if e.event_type == "tool_response" and (e.tool_name or "") in mutating_tools]
    if not responses:
        return "FAILED"
    return "VERIFIED" if all(e.status == "success" for e in responses) else "FAILED"
