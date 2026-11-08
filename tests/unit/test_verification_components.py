"""Comparator, side-effect detector, evidence validation and verdict policy."""

from __future__ import annotations

from datetime import datetime

from app.collector.event_schema import TraceEvent, TraceIntegrity
from app.environments.base import EnvironmentSchema, FieldSpec
from app.environments.database_environment import SCHEMA as DB
from app.interpretation.plan_validator import build_plan
from app.interpretation.rule_parser import parse_instruction
from app.verification.evidence_validator import (
    EvidenceLedger,
    assess_trace,
    classify_claim,
    enforce_direct_evidence,
)
from app.verification.models import ChangeKind, CheckResult, CheckStatus, EvidenceCategory, TraceAssessment, Verdict
from app.verification.side_effect_detector import classify_changes
from app.verification.state_comparator import diff_snapshots, values_equal
from app.verification.verdict_engine import decide_verdict

UPDATE = "Update the email address of employee ID 1042 to new@example.com"


def _plan(text=UPDATE):
    return build_plan(parse_instruction(text, DB), DB, text, "rules")


def _label(c, k):
    return f"{c}:{k}"


def test_diff_detects_added_removed_modified():
    before = {"t": {"1": {"a": 1}, "2": {"a": 2}}}
    after = {"t": {"1": {"a": 9}, "3": {"a": 3}}}
    d = diff_snapshots(before, after)
    kinds = {(c.key, c.change_type) for c in d.changes}
    assert kinds == {("1", "modified"), ("2", "removed"), ("3", "added")}
    assert d.get("t", "1").changed_fields["a"].after == 9


def test_values_equal_rules():
    assert values_equal("A@B.example ", "a@b.example", FieldSpec(name="email", type="email"))
    assert values_equal("Gold", "gold", FieldSpec(name="tier", type="enum"))
    assert not values_equal("Hello ", "Hello", FieldSpec(name="content", type="text"))  # every byte matters
    assert values_equal("42", 42, FieldSpec(name="id", type="integer"))
    assert not values_equal(None, "x")
    assert values_equal(None, None)


def _ta(**kw) -> TraceAssessment:
    return TraceAssessment(chain_valid=True, **kw)


def test_side_effects_permitted_dependencies_and_attribution():
    plan = _plan()
    before = {"employees": {"1042": {"id": 1042, "email": "old@x", "phone": "1", "updated_at": "t0"},
                            "1043": {"id": 1043, "email": "e@x", "phone": "2", "updated_at": "t0"}},
              "audit_log": {}}
    after = {"employees": {"1042": {"id": 1042, "email": "new@example.com", "phone": "1", "updated_at": "t1"},
                           "1043": {"id": 1043, "email": "e@x", "phone": "9", "updated_at": "t1"}},
             "audit_log": {"1": {"id": 1, "table_name": "employees", "record_id": 1042},
                           "2": {"id": 2, "table_name": "employees", "record_id": 1043}}}
    trace = _ta(referenced_records={"employees:1043": ["call_7"]})
    changes = classify_changes(diff_snapshots(before, after), plan, DB, {("employees", "1042")}, set(), trace, _label)
    by = {(c.key, c.kind) for c in changes}
    assert ("1042", ChangeKind.EXPECTED) in by
    assert ("1042", ChangeKind.PERMITTED_DEPENDENCY) in by  # updated_at
    assert ("1043", ChangeKind.SIDE_EFFECT) in by
    assert ("1", ChangeKind.PERMITTED_DEPENDENCY) in by and ("2", ChangeKind.PERMITTED_DEPENDENCY) in by
    side = next(c for c in changes if c.key == "1043")
    assert side.attributed_calls == ["call_7"]


def test_unattributed_changes_are_uncertain_without_exclusive_access():
    plan = _plan()
    schema = EnvironmentSchema(**{**DB.model_dump(), "exclusive_access": False})
    before = {"employees": {"1043": {"phone": "2"}}}
    after = {"employees": {"1043": {"phone": "9"}}}
    changes = classify_changes(diff_snapshots(before, after), plan, schema, set(), set(), _ta(), _label)
    assert changes[0].kind == ChangeKind.UNCERTAIN


def _ev(seq, etype, call_id=None, status=None, tool="db_update", inp=None, out=None, msg=""):
    return TraceEvent(sequence=seq, event_type=etype, source="agent", tool_name=tool, call_id=call_id, status=status,
                      input=inp, output=out, message=msg, timestamp=datetime(2026, 1, 1), prev_hash="0", event_hash="0")


def test_trace_contradictions_and_orphans():
    events = [
        _ev(1, "tool_call", "call_1", inp={"table": "employees", "record_id": 1042}),
        _ev(2, "tool_response", "call_1", "success"),
        _ev(3, "error", "call_1", "error", msg="rolled back"),
        _ev(4, "tool_response", "call_9", "success"),
        _ev(5, "agent_claim", None, "success", tool=None, out={"claimed_status": "success"}, msg="done"),
    ]
    refs = lambda tool, args, out: [("employees", str(args["record_id"]))] if args else []  # noqa: E731
    ta = assess_trace(events, TraceIntegrity(valid=True, checked_events=5), refs, {"db_update"})
    assert len(ta.contradictions) == 2
    assert ta.orphan_responses == ["call_9"]
    assert ta.successful_mutation_refs == {"employees:1042": ["call_1"]}
    assert ta.claimed_status == "success"


def test_claim_contradicted_by_tools():
    events = [_ev(1, "tool_call", "call_1", inp={}), _ev(2, "tool_response", "call_1", "error"),
              _ev(3, "agent_claim", out={"claimed_status": "success"}, tool=None)]
    ta = assess_trace(events, TraceIntegrity(valid=True, checked_events=3), lambda *a: [], {"db_update"})
    assert ta.claim_contradicted_by_tools


def test_claim_classifier():
    assert classify_claim("Successfully updated the record") == "success"
    assert classify_claim("I could not complete the task") == "failure"
    assert classify_claim("Hmm.") == "unknown"


def test_passing_check_without_direct_evidence_is_downgraded():
    ledger = EvidenceLedger("exec_test")
    log = ledger.add("tool_invocation", EvidenceCategory.INDIRECT, "trace", "tool says ok")
    claim = ledger.add("agent_claim", EvidenceCategory.AGENT_REPORTED, "agent", "done")
    snap = ledger.add("state_snapshot", EvidenceCategory.DIRECT, "verifier", "after")
    weak = CheckResult(id="a", name="a", category="postcondition", status=CheckStatus.PASSED, evidence_ids=[log.id, claim.id])
    strong = CheckResult(id="b", name="b", category="postcondition", status=CheckStatus.PASSED, evidence_ids=[snap.id])
    enforce_direct_evidence([weak, strong], ledger)
    assert weak.status == CheckStatus.INCONCLUSIVE and strong.status == CheckStatus.PASSED
    assert ledger.verify_integrity() == []


def _c(status, category="postcondition", decisive=True, code=None):
    return CheckResult(id=f"{status}-{category}", name="x", category=category, status=status, decisive=decisive, reason_code=code)


def test_verdict_policy_order():
    plan = _plan()
    P, F, I = CheckStatus.PASSED, CheckStatus.FAILED, CheckStatus.INCONCLUSIVE  # noqa: E741
    assert decide_verdict(plan, [_c(P), _c(P, "evidence")]).verdict == Verdict.VERIFIED
    # A failure established by direct evidence wins over missing evidence.
    assert decide_verdict(plan, [_c(F), _c(I, "evidence")]).verdict == Verdict.FAILED
    # Missing evidence is never turned into a failure...
    assert decide_verdict(plan, [_c(P), _c(I, "evidence", code="MISSING_EXECUTION_EVIDENCE")]).verdict == Verdict.INCONCLUSIVE
    # ...and informational checks never decide.
    assert decide_verdict(plan, [_c(P), _c(F, "consistency", decisive=False)]).verdict == Verdict.VERIFIED
    assert decide_verdict(plan, []).verdict == Verdict.INCONCLUSIVE
    assert decide_verdict(plan, [_c(F)], window_intact=False).rule == "R1"
    ambiguous = build_plan(parse_instruction("Update the employee's email", DB), DB, "x", "rules")
    assert decide_verdict(ambiguous, [_c(F)]).rule == "R2"
