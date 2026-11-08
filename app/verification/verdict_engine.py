"""Explicit, ordered verdict policy. No LLM is involved.

The policy is asymmetric on purpose:

* **FAILED** requires that a violation is *established* by direct evidence -
  the observed state contradicts a requirement.
* **VERIFIED** requires that *every* decisive requirement is positively
  established, including that the change is accounted for by a consistent
  execution trace.
* Anything in between is **INCONCLUSIVE** - missing evidence is never
  silently turned into a failure, and never into a success.

Rules are evaluated in order; the first that applies decides.

====  ======================================================  =============
Rule  Condition                                               Verdict
====  ======================================================  =============
R1    Execution window not exclusive (state changed by        INCONCLUSIVE
      something else between run and verification)
R2    Task interpretation is ambiguous / not verifiable       INCONCLUSIVE
R3    Any decisive check FAILED (violation established)       FAILED
R4    Any decisive check INCONCLUSIVE (missing, ambiguous,    INCONCLUSIVE
      contradictory or uncertain evidence)
R5    No decisive check passed (nothing was established)      INCONCLUSIVE
R6    All decisive checks passed                              VERIFIED
====  ======================================================  =============
"""

from __future__ import annotations

from app.interpretation.verification_plan import VerificationPlan
from app.verification.models import (
    CheckResult,
    CheckStatus,
    Verdict,
    VerdictDecision,
    VerdictReason,
)

REASON_TEXT = {
    "ENVIRONMENT_WINDOW_VIOLATED": "The environment changed after this execution finished and before verification, so the observed state cannot be attributed to it.",
    "AMBIGUOUS_TASK": "The task could not be interpreted unambiguously, so there is no reliable expected state.",
    "POSTCONDITION_FAILED": "A required postcondition is not satisfied in the observed state.",
    "TARGET_NOT_FOUND": "The target record does not exist.",
    "DUPLICATE_OPERATION": "The operation was applied more than once.",
    "SIDE_EFFECT_DETECTED": "Changes outside the task scope were observed.",
    "CONSTRAINT_VIOLATED": "An explicit task constraint was violated.",
    "STATE_UNAVAILABLE": "The system state could not be independently observed.",
    "AMBIGUOUS_TARGET": "The target could not be uniquely identified.",
    "NO_MATCHING_RECORDS": "No record matched the task's condition before execution.",
    "CONTRADICTORY_EVIDENCE": "The execution evidence contradicts itself.",
    "TRACE_INTEGRITY_FAILURE": "The execution trace failed its integrity check.",
    "MISSING_EXECUTION_EVIDENCE": "No execution evidence accounts for the observed change.",
    "UNCERTAIN_SIDE_EFFECT": "Changes outside the task scope could not be attributed with certainty.",
    "INSUFFICIENT_EVIDENCE": "There is not enough independent evidence to establish the outcome.",
    "ALL_CHECKS_PASSED": "Every requirement was established by independent evidence.",
}


def _reasons_from(checks: list[CheckResult], default_code: str) -> list[VerdictReason]:
    grouped: dict[str, VerdictReason] = {}
    for c in checks:
        code = c.reason_code or default_code
        reason = grouped.setdefault(code, VerdictReason(code=code, message=REASON_TEXT.get(code, code)))
        reason.check_ids.append(c.id)
        for eid in c.evidence_ids:
            if eid not in reason.evidence_ids:
                reason.evidence_ids.append(eid)
    return list(grouped.values())


def decide_verdict(
    plan: VerificationPlan,
    checks: list[CheckResult],
    *,
    window_intact: bool = True,
    window_evidence: list[str] | None = None,
) -> VerdictDecision:
    if not window_intact:
        return VerdictDecision(
            verdict=Verdict.INCONCLUSIVE, rule="R1",
            reasons=[VerdictReason(code="ENVIRONMENT_WINDOW_VIOLATED", message=REASON_TEXT["ENVIRONMENT_WINDOW_VIOLATED"],
                                   evidence_ids=window_evidence or [])],
        )
    if not plan.verifiable:
        return VerdictDecision(
            verdict=Verdict.INCONCLUSIVE, rule="R2",
            reasons=[VerdictReason(code="AMBIGUOUS_TASK",
                                   message=REASON_TEXT["AMBIGUOUS_TASK"] + " " + " ".join(plan.ambiguities))],
        )
    decisive = [c for c in checks if c.decisive]
    failed = [c for c in decisive if c.status == CheckStatus.FAILED]
    if failed:
        return VerdictDecision(verdict=Verdict.FAILED, rule="R3", reasons=_reasons_from(failed, "POSTCONDITION_FAILED"))
    inconclusive = [c for c in decisive if c.status == CheckStatus.INCONCLUSIVE]
    if inconclusive:
        return VerdictDecision(verdict=Verdict.INCONCLUSIVE, rule="R4",
                               reasons=_reasons_from(inconclusive, "INSUFFICIENT_EVIDENCE"))
    passed = [c for c in decisive if c.status == CheckStatus.PASSED]
    if not passed:
        return VerdictDecision(
            verdict=Verdict.INCONCLUSIVE, rule="R5",
            reasons=[VerdictReason(code="INSUFFICIENT_EVIDENCE", message=REASON_TEXT["INSUFFICIENT_EVIDENCE"])],
        )
    reason = VerdictReason(code="ALL_CHECKS_PASSED", message=REASON_TEXT["ALL_CHECKS_PASSED"],
                           check_ids=[c.id for c in passed])
    for c in passed:
        for eid in c.evidence_ids:
            if eid not in reason.evidence_ids:
                reason.evidence_ids.append(eid)
    return VerdictDecision(verdict=Verdict.VERIFIED, rule="R6", reasons=[reason])
