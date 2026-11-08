"""Deterministic implementations of the verification requirements.

Each requirement id produced by :func:`app.interpretation.plan_validator.derive_requirements`
maps to exactly one check function here. Unknown requirements yield an
INCONCLUSIVE check - the engine never pretends to have verified something it
has no rule for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.environments.base import EntitySpec, EnvironmentSchema, FieldSpec
from app.interpretation.verification_plan import VerificationPlan
from app.verification.models import (
    ChangeKind,
    CheckResult,
    CheckStatus,
    DetectedChange,
    StateDiff,
    TraceAssessment,
)
from app.verification.state_comparator import (
    content_sha256,
    find_by_conditions,
    find_by_match,
    match_score,
    values_equal,
)
from app.verification.state_inspector import SnapshotResult


@dataclass
class CheckContext:
    plan: VerificationPlan
    schema: EnvironmentSchema
    entity: EntitySpec
    before: SnapshotResult
    after: SnapshotResult
    diff: StateDiff | None
    trace: TraceAssessment
    label: Callable[[str, str], str]
    ev: dict[str, Any] = field(default_factory=dict)  # named evidence ids
    tool_evidence: dict[str, str] = field(default_factory=dict)  # call_id -> evidence id
    # Resolution results
    target_status: str = "n/a"  # resolved | not_found | ambiguous | unknown | n/a
    target_key: str | None = None
    target_candidates: list[str] = field(default_factory=list)
    created_key: str | None = None
    full_matches: list[str] = field(default_factory=list)
    duplicate_keys: list[str] = field(default_factory=list)
    misplaced_keys: list[str] = field(default_factory=list)
    selected_keys: list[str] = field(default_factory=list)  # bulk tasks: every record matching the conditions (before)
    changes: list[DetectedChange] = field(default_factory=list)

    @property
    def bulk(self) -> bool:
        return bool(self.plan.target and self.plan.target.bulk)

    @property
    def collection(self) -> str:
        return self.entity.collection

    @property
    def specs(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.entity.fields}

    def rows(self, snap: SnapshotResult) -> dict[str, dict[str, Any]]:
        return (snap.state or {}).get(self.collection, {}) if snap.available else {}

    def noun(self) -> str:
        return self.entity.display_name.lower()

    def tlabel(self, key: str | None = None) -> str:
        return self.label(self.collection, key or self.target_key or "?")

    def eids(self, *names: str) -> list[str]:
        out = []
        for n in names:
            v = self.ev.get(n)
            if isinstance(v, list):
                out.extend(v)
            elif v:
                out.append(v)
        return out


def _check(cid: str, name: str, category: str, status: CheckStatus, explanation: str, *, evidence: list[str] | None = None,
           expected: Any = None, actual: Any = None, code: str | None = None, decisive: bool = True) -> CheckResult:
    explanation = explanation[:1].upper() + explanation[1:] if explanation else explanation
    return CheckResult(id=cid, name=name, category=category, status=status, explanation=explanation,
                       evidence_ids=evidence or [], expected=expected, actual=actual, reason_code=code, decisive=decisive)


P, F, INC, S = CheckStatus.PASSED, CheckStatus.FAILED, CheckStatus.INCONCLUSIVE, CheckStatus.SKIPPED


# --------------------------------------------------------------------------
# Target resolution (runs before the checks)
# --------------------------------------------------------------------------
def resolve(ctx: CheckContext) -> None:
    plan = ctx.plan
    op = plan.operation
    before_rows, after_rows = ctx.rows(ctx.before), ctx.rows(ctx.after)
    if op == "CREATE":
        expected = plan.expected_state
        if ctx.entity.key_type == "path":
            key = plan.target.key if plan.target else None
            ctx.target_key = key
            if ctx.after.available and key in after_rows:
                ctx.created_key = key
            if ctx.diff is not None:
                want = content_sha256(str(expected.get("content", "")))
                others = [c.key for c in ctx.diff.for_collection(ctx.collection)
                          if c.change_type == "added" and c.key != key and (c.after or {}).get("sha256") == want]
                if ctx.created_key:
                    ctx.duplicate_keys = others
                else:
                    ctx.misplaced_keys = others
            return
        if ctx.diff is not None:
            added = [c for c in ctx.diff.for_collection(ctx.collection) if c.change_type == "added"]
            ctx.full_matches = [c.key for c in added
                                if all(values_equal((c.after or {}).get(f), v, ctx.specs.get(f)) for f, v in expected.items())]
            if ctx.full_matches:
                ctx.created_key = ctx.full_matches[0]
                ctx.duplicate_keys = ctx.full_matches[1:]
            elif added:
                best = max(added, key=lambda c: match_score(c.after or {}, expected, ctx.specs))
                ctx.created_key = best.key
        return
    # UPDATE / DELETE / READ
    target = plan.target
    if target is None:
        ctx.target_status = "unknown"
        return
    if target.bulk:
        # The selection is computed by the verifier from the independent BEFORE snapshot - never from the
        # agent's own queries - so a wrong filter by the agent shows up as missed or extra records.
        if not ctx.before.available:
            ctx.target_status = "unknown"
            return
        ctx.selected_keys = find_by_conditions(before_rows, target.conditions, ctx.specs)
        ctx.target_status = "resolved" if ctx.selected_keys else "not_found"
        return
    if target.key is not None:
        ctx.target_key = target.key
        if ctx.before.available:
            ctx.target_status = "resolved" if target.key in before_rows else "not_found"
        else:
            ctx.target_status = "unknown"
        return
    if not ctx.before.available:
        ctx.target_status = "unknown"
        return
    matches = find_by_match(before_rows, target.match, ctx.specs)
    ctx.target_candidates = matches
    if len(matches) == 1:
        ctx.target_status, ctx.target_key = "resolved", matches[0]
    elif not matches:
        ctx.target_status = "not_found"
    else:
        ctx.target_status = "ambiguous"


def expected_keys(ctx: CheckContext) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if ctx.bulk:
        return {(ctx.collection, k) for k in ctx.selected_keys}
    if ctx.plan.operation == "CREATE":
        if ctx.created_key:
            keys.add((ctx.collection, ctx.created_key))
    elif ctx.target_key and ctx.target_status in ("resolved", "unknown"):
        keys.add((ctx.collection, ctx.target_key))
    return keys


# --------------------------------------------------------------------------
# Check implementations
# --------------------------------------------------------------------------
def check_target_identified(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    status = ctx.target_status
    t = ctx.plan.target
    described = t.describe(ctx.noun()) if t else ctx.noun()
    if status == "resolved":
        return _check(rid, desc, "precondition", P, f"{ctx.tlabel()} exists in the independently captured before-state.",
                      evidence=ctx.eids("before", "target_before"), actual=ctx.target_key)
    if status == "not_found":
        return _check(rid, desc, "precondition", F, f"No record matching {described} exists in the before-state.",
                      evidence=ctx.eids("before"), code="TARGET_NOT_FOUND", expected=described, actual=None)
    if status == "ambiguous":
        keys = ", ".join(ctx.target_candidates)
        return _check(rid, desc, "precondition", INC,
                      f"{len(ctx.target_candidates)} records match {described} ({keys}); the intended target cannot be determined.",
                      evidence=ctx.eids("before"), code="AMBIGUOUS_TARGET", expected="exactly one match",
                      actual=ctx.target_candidates)
    return _check(rid, desc, "precondition", INC, "The before-state is unavailable, so the target's prior existence cannot be confirmed.",
                  evidence=ctx.eids("before"), code="STATE_UNAVAILABLE")


def check_field_matches(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    fname = rid.split(":", 1)[1]
    expected = ctx.plan.expected_state.get(fname)
    spec = ctx.specs.get(fname)
    if ctx.plan.operation == "CREATE":
        key = ctx.created_key
        if not ctx.after.available:
            return _check(rid, desc, "postcondition", INC, "The after-state is unavailable.", evidence=ctx.eids("after"),
                          code="STATE_UNAVAILABLE", expected=expected)
        if key is None:
            return _check(rid, desc, "postcondition", S, f"No new {ctx.noun()} record exists to inspect (see record_created).",
                          expected=expected, decisive=False)
    else:
        if ctx.target_status in ("not_found", "ambiguous"):
            return _check(rid, desc, "postcondition", S, "Target not uniquely identified; field cannot be checked.",
                          expected=expected, decisive=False)
        key = ctx.target_key
        if not ctx.after.available:
            return _check(rid, desc, "postcondition", INC, "The after-state is unavailable, so the field value cannot be observed.",
                          evidence=ctx.eids("after"), code="STATE_UNAVAILABLE", expected=expected)
    after_rows = ctx.rows(ctx.after)
    if key not in after_rows:
        return _check(rid, desc, "postcondition", F, f"{ctx.tlabel(key)} no longer exists after execution.",
                      evidence=ctx.eids("after"), code="POSTCONDITION_FAILED", expected=expected, actual=None)
    actual = after_rows[key].get(fname)
    before_val = ctx.rows(ctx.before).get(key, {}).get(fname) if ctx.before.available else None
    if values_equal(actual, expected, spec):
        note = " (it already had this value before execution)" if (
            ctx.plan.operation == "UPDATE" and ctx.before.available and values_equal(before_val, expected, spec)) else ""
        return _check(rid, desc, "postcondition", P, f"{ctx.tlabel(key)}.{fname} = {actual!r} as required{note}.",
                      evidence=ctx.eids("after", "target_after", "diff"), expected=expected, actual=actual)
    unchanged = ctx.before.available and before_val == actual and ctx.plan.operation == "UPDATE"
    tail = " The value was not changed by the execution." if unchanged else ""
    return _check(rid, desc, "postcondition", F, f"Expected {fname} = {expected!r}, observed {actual!r}.{tail}",
                  evidence=ctx.eids("after", "target_after", "diff"), code="POSTCONDITION_FAILED", expected=expected, actual=actual)


def check_record_created(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if ctx.diff is None:
        if not ctx.after.available:
            return _check(rid, desc, "postcondition", INC, "Neither a before/after comparison nor the after-state is available.",
                          evidence=ctx.eids("before", "after"), code="STATE_UNAVAILABLE")
        matches = find_by_match(ctx.rows(ctx.after), ctx.plan.expected_state, ctx.specs)
        if matches:
            return _check(rid, desc, "postcondition", INC,
                          "A matching record exists, but without a before-state it cannot be shown to be new.",
                          evidence=ctx.eids("after", "before"), code="STATE_UNAVAILABLE", actual=matches)
        return _check(rid, desc, "postcondition", F, f"No {ctx.noun()} matching the requested values exists after execution.",
                      evidence=ctx.eids("after"), code="POSTCONDITION_FAILED", expected=ctx.plan.expected_state)
    added = [c.key for c in ctx.diff.for_collection(ctx.collection) if c.change_type == "added"]
    if not added:
        return _check(rid, desc, "postcondition", F, f"No new {ctx.noun()} record was created (before/after comparison shows no insertions).",
                      evidence=ctx.eids("diff", "before", "after"), code="POSTCONDITION_FAILED", expected="1 new record", actual=0)
    return _check(rid, desc, "postcondition", P, f"{len(added)} new {ctx.noun()} record(s) observed: {', '.join(added)}.",
                  evidence=ctx.eids("diff", "created"), expected="1 new record", actual=added)


def check_exactly_one_new(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if ctx.diff is None:
        return _check(rid, desc, "constraint", INC, "Duplicate detection requires a before/after comparison, which is unavailable.",
                      evidence=ctx.eids("before", "after"), code="STATE_UNAVAILABLE")
    if ctx.entity.key_type == "path":
        if not ctx.created_key:
            return _check(rid, desc, "constraint", S, "The requested file does not exist (see file_exists).", decisive=False)
        if ctx.duplicate_keys:
            return _check(rid, desc, "constraint", F,
                          f"Identical copies were also created: {', '.join(ctx.duplicate_keys)}.",
                          evidence=ctx.eids("diff", "duplicates"), code="DUPLICATE_OPERATION", expected=1,
                          actual=1 + len(ctx.duplicate_keys))
        return _check(rid, desc, "constraint", P, "Exactly one copy of the file was written.", evidence=ctx.eids("diff"),
                      expected=1, actual=1)
    n = len(ctx.full_matches)
    if n == 0:
        return _check(rid, desc, "constraint", S, "No fully matching record was created (see field checks).", decisive=False,
                      expected=1, actual=0)
    if n > 1:
        return _check(rid, desc, "constraint", F,
                      f"{n} identical {ctx.noun()} records were created ({', '.join(ctx.full_matches)}); exactly one was requested.",
                      evidence=ctx.eids("diff", "duplicates"), code="DUPLICATE_OPERATION", expected=1, actual=n)
    return _check(rid, desc, "constraint", P, f"Exactly one matching {ctx.noun()} was created ({ctx.full_matches[0]}).",
                  evidence=ctx.eids("diff", "created"), expected=1, actual=1)


def _scope_check(ctx: CheckContext, rid: str, desc: str, *, on_target: bool) -> CheckResult:
    if ctx.diff is None:
        return _check(rid, desc, "side_effect", INC, "Side effects cannot be ruled out without both before- and after-snapshots.",
                      evidence=ctx.eids("before", "after"), code="STATE_UNAVAILABLE")
    if on_target and ctx.target_status in ("not_found", "ambiguous"):
        return _check(rid, desc, "side_effect", S, "Target not uniquely identified.", decisive=False)
    exp = expected_keys(ctx)
    relevant = [c for c in ctx.changes if ((c.collection, c.key) in exp) == on_target and not ctx.schema.dependency(c.collection)]
    if not on_target:
        relevant += [c for c in ctx.changes if ctx.schema.dependency(c.collection)]
    bad = [c for c in relevant if c.kind == ChangeKind.SIDE_EFFECT]
    unsure = [c for c in relevant if c.kind == ChangeKind.UNCERTAIN]
    if bad:
        parts = [f"{c.label} ({c.change_type}{': ' + ', '.join(c.fields) if c.change_type == 'modified' else ''})" for c in bad]
        return _check(rid, desc, "side_effect", F, "Unintended change(s): " + "; ".join(parts) + ".",
                      evidence=ctx.eids("diff", "side_effects"), code="SIDE_EFFECT_DETECTED",
                      expected="no changes outside the task scope", actual=[c.label for c in bad])
    if unsure:
        return _check(rid, desc, "side_effect", INC,
                      "Change(s) outside the task scope could not be attributed: " + "; ".join(c.label for c in unsure) + ".",
                      evidence=ctx.eids("diff", "side_effects"), code="UNCERTAIN_SIDE_EFFECT")
    deps = [c for c in relevant if c.kind == ChangeKind.PERMITTED_DEPENDENCY]
    extra = f" {len(deps)} permitted dependent change(s) (e.g. audit rows, system timestamps) were recognised." if deps else ""
    what = "No unrequested field of the target changed." if on_target else "No records outside the task scope changed."
    return _check(rid, desc, "side_effect", P, what + extra, evidence=ctx.eids("diff"))


def check_no_unrequested_target_changes(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    return _scope_check(ctx, rid, desc, on_target=True)


def check_no_changes_to_other_records(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    return _scope_check(ctx, rid, desc, on_target=False)


def check_target_removed(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if ctx.target_status in ("not_found", "ambiguous"):
        return _check(rid, desc, "postcondition", S, "Target not uniquely identified.", decisive=False)
    if not ctx.after.available:
        return _check(rid, desc, "postcondition", INC, "The after-state is unavailable.", evidence=ctx.eids("after"),
                      code="STATE_UNAVAILABLE")
    if ctx.target_key in ctx.rows(ctx.after):
        return _check(rid, desc, "postcondition", F, f"{ctx.tlabel()} still exists after execution.",
                      evidence=ctx.eids("after", "target_after"), code="POSTCONDITION_FAILED", expected="absent", actual="present")
    return _check(rid, desc, "postcondition", P, f"{ctx.tlabel()} is absent from the after-state.",
                  evidence=ctx.eids("after", "diff"), expected="absent", actual="absent")


def check_file_exists(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if not ctx.after.available:
        return _check(rid, desc, "postcondition", INC, "The after-state is unavailable.", evidence=ctx.eids("after"),
                      code="STATE_UNAVAILABLE")
    key = ctx.target_key
    if key in ctx.rows(ctx.after):
        return _check(rid, desc, "postcondition", P, f"'{key}' exists after execution.", evidence=ctx.eids("after", "target_after"),
                      expected=key, actual=key)
    extra = f" Matching content was written to: {', '.join(ctx.misplaced_keys)}." if ctx.misplaced_keys else ""
    return _check(rid, desc, "postcondition", F, f"'{key}' does not exist after execution.{extra}",
                  evidence=ctx.eids("after", "diff"), code="POSTCONDITION_FAILED", expected=key, actual=None)


def _file_after(ctx: CheckContext) -> dict[str, Any] | None:
    return ctx.rows(ctx.after).get(ctx.target_key or "")


def check_content_sha(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if not ctx.after.available:
        return _check(rid, desc, "postcondition", INC, "The after-state is unavailable.", evidence=ctx.eids("after"),
                      code="STATE_UNAVAILABLE")
    rec = _file_after(ctx)
    expected_content = str(ctx.plan.expected_state.get("content", ""))
    want = content_sha256(expected_content)
    if rec is None:
        return _check(rid, desc, "postcondition", S, "File missing (see file_exists).", decisive=False, expected=want)
    if rec.get("sha256") == want:
        return _check(rid, desc, "postcondition", P, f"SHA-256 matches ({want[:16]}...).",
                      evidence=ctx.eids("after", "checksum"), expected=want, actual=rec.get("sha256"))
    observed = rec.get("content")
    detail = f" Observed content: {observed[:80]!r}." if isinstance(observed, str) else ""
    return _check(rid, desc, "postcondition", F, f"Checksum mismatch: expected {want[:16]}..., observed {str(rec.get('sha256'))[:16]}....{detail}",
                  evidence=ctx.eids("after", "checksum"), code="POSTCONDITION_FAILED", expected=want, actual=rec.get("sha256"))


def check_size(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if not ctx.after.available:
        return _check(rid, desc, "postcondition", INC, "The after-state is unavailable.", evidence=ctx.eids("after"),
                      code="STATE_UNAVAILABLE")
    rec = _file_after(ctx)
    want = len(str(ctx.plan.expected_state.get("content", "")).encode("utf-8"))
    if rec is None:
        return _check(rid, desc, "postcondition", S, "File missing (see file_exists).", decisive=False, expected=want)
    status = P if rec.get("size") == want else F
    return _check(rid, desc, "postcondition", status, f"Expected {want} bytes, observed {rec.get('size')} bytes.",
                  evidence=ctx.eids("after", "checksum"), code=None if status == P else "POSTCONDITION_FAILED",
                  expected=want, actual=rec.get("size"))


def check_no_accidental_overwrite(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if not ctx.before.available:
        return _check(rid, desc, "constraint", INC, "The before-state is unavailable.", evidence=ctx.eids("before"),
                      code="STATE_UNAVAILABLE")
    key = ctx.target_key
    before = ctx.rows(ctx.before).get(key or "")
    if before is None:
        return _check(rid, desc, "constraint", P, f"'{key}' did not exist before execution.", evidence=ctx.eids("before"))
    after = _file_after(ctx)
    changed = after is None or after.get("sha256") != before.get("sha256")
    msg = (f"'{key}' already existed and its previous content was overwritten." if changed
           else f"'{key}' already existed before execution, so it was not created by this task.")
    return _check(rid, desc, "constraint", F, msg, evidence=ctx.eids("before", "after"), code="CONSTRAINT_VIOLATED",
                  expected="file absent before execution", actual="file present before execution")


def check_no_state_change(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if ctx.diff is None:
        return _check(rid, desc, "side_effect", INC, "No before/after comparison is available.", evidence=ctx.eids("before", "after"),
                      code="STATE_UNAVAILABLE")
    if ctx.diff.changes:
        labels = [ctx.label(c.collection, c.key) for c in ctx.diff.changes[:5]]
        return _check(rid, desc, "side_effect", F, "A read-only task changed state: " + "; ".join(labels) + ".",
                      evidence=ctx.eids("diff", "side_effects"), code="SIDE_EFFECT_DETECTED")
    return _check(rid, desc, "side_effect", P, "No state changed.", evidence=ctx.eids("diff"))


def _keys_text(keys: list[str], limit: int = 12) -> str:
    return ", ".join(keys[:limit]) + (f" (+{len(keys) - limit} more)" if len(keys) > limit else "")


def check_targets_selected(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    cond = ctx.plan.target.describe(ctx.noun()) if ctx.plan.target else ctx.noun()
    if ctx.target_status == "resolved":
        n = len(ctx.selected_keys)
        return _check(rid, desc, "precondition", P,
                      f"{n} {ctx.noun()} record(s) matched {cond} in the independent before-state: {_keys_text(ctx.selected_keys)}.",
                      evidence=ctx.eids("before", "selection"), expected="at least 1", actual=n)
    if ctx.target_status == "not_found":
        return _check(rid, desc, "precondition", INC,
                      f"No record matched {cond} before execution, so there was nothing to change and nothing to verify.",
                      evidence=ctx.eids("before"), code="NO_MATCHING_RECORDS", expected="at least 1", actual=0)
    return _check(rid, desc, "precondition", INC, "The before-state is unavailable, so the selection cannot be determined.",
                  evidence=ctx.eids("before"), code="STATE_UNAVAILABLE")


def check_bulk_field_matches(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    fname = rid.split(":", 1)[1]
    expected = ctx.plan.expected_state.get(fname)
    if not ctx.selected_keys:
        return _check(rid, desc, "postcondition", S, "No records were selected (see targets_selected).", expected=expected,
                      decisive=False)
    if not ctx.after.available:
        return _check(rid, desc, "postcondition", INC, "The after-state is unavailable.", evidence=ctx.eids("after"),
                      code="STATE_UNAVAILABLE", expected=expected)
    after_rows = ctx.rows(ctx.after)
    spec = ctx.specs.get(fname)
    missing = [k for k in ctx.selected_keys if k not in after_rows]
    wrong = [k for k in ctx.selected_keys if k in after_rows and not values_equal(after_rows[k].get(fname), expected, spec)]
    n = len(ctx.selected_keys)
    if not missing and not wrong:
        return _check(rid, desc, "postcondition", P, f"All {n} selected {ctx.noun()} record(s) have {fname} = {expected!r}.",
                      evidence=ctx.eids("after", "selection_after", "diff"), expected=expected, actual=f"{n}/{n}")
    parts = []
    if wrong:
        sample = ", ".join(f"{k} ({after_rows[k].get(fname)!r})" for k in wrong[:6])
        parts.append(f"{len(wrong)} of {n} still have a different {fname}: {sample}{' ...' if len(wrong) > 6 else ''}")
    if missing:
        parts.append(f"{len(missing)} selected record(s) no longer exist: {_keys_text(missing)}")
    return _check(rid, desc, "postcondition", F, "Not every selected record was updated - " + "; ".join(parts) + ".",
                  evidence=ctx.eids("after", "selection_after", "diff"), code="POSTCONDITION_FAILED", expected=expected,
                  actual=f"{n - len(wrong) - len(missing)}/{n} correct")


def check_all_targets_removed(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if not ctx.selected_keys:
        return _check(rid, desc, "postcondition", S, "No records were selected (see targets_selected).", decisive=False)
    if not ctx.after.available:
        return _check(rid, desc, "postcondition", INC, "The after-state is unavailable.", evidence=ctx.eids("after"),
                      code="STATE_UNAVAILABLE")
    after_rows = ctx.rows(ctx.after)
    left = [k for k in ctx.selected_keys if k in after_rows]
    n = len(ctx.selected_keys)
    if not left:
        return _check(rid, desc, "postcondition", P, f"All {n} selected {ctx.noun()} record(s) are absent from the after-state.",
                      evidence=ctx.eids("after", "diff"), expected="absent", actual=f"{n}/{n} removed")
    return _check(rid, desc, "postcondition", F, f"{len(left)} of {n} selected record(s) still exist: {_keys_text(left)}.",
                  evidence=ctx.eids("after", "diff"), code="POSTCONDITION_FAILED", expected="absent",
                  actual=f"{n - len(left)}/{n} removed")


def check_execution_evidence(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    tr = ctx.trace
    if tr.tool_calls == 0 and tr.tool_responses == 0:
        return _check(rid, desc, "evidence", INC,
                      "The trace contains no tool invocations, so no execution evidence accounts for the observed state.",
                      evidence=ctx.eids("missing_trace", "claim"), code="MISSING_EXECUTION_EVIDENCE")
    if ctx.bulk:
        changed = [c.key for c in ctx.changes if c.collection == ctx.collection and c.kind == ChangeKind.EXPECTED]
        covered = [k for k in changed if tr.successful_mutation_refs.get(f"{ctx.collection}:{k}")]
        call_ids = sorted({cid for k in covered for cid in tr.successful_mutation_refs.get(f"{ctx.collection}:{k}", [])})
        if changed and len(covered) == len(changed):
            return _check(rid, desc, "evidence", P,
                          f"Successful tool call(s) {', '.join(call_ids)} reference all {len(changed)} changed record(s).",
                          evidence=[ctx.tool_evidence[c] for c in call_ids if c in ctx.tool_evidence], actual=call_ids)
        if not changed:
            return _check(rid, desc, "evidence", S, "No requested change was observed to attribute (see postconditions).",
                          decisive=False)
        return _check(rid, desc, "evidence", INC,
                      f"Only {len(covered)} of {len(changed)} changed record(s) are referenced by a successful tool call.",
                      evidence=ctx.eids("tools"), code="MISSING_EXECUTION_EVIDENCE")
    key = ctx.created_key if ctx.plan.operation == "CREATE" else ctx.target_key
    calls = tr.successful_mutation_refs.get(f"{ctx.collection}:{key}", []) if key else []
    if calls:
        return _check(rid, desc, "evidence", P,
                      f"Successful tool call(s) {', '.join(calls)} reference {ctx.tlabel(key)}.",
                      evidence=[ctx.tool_evidence[c] for c in calls if c in ctx.tool_evidence], actual=calls)
    if tr.successful_mutations == 0:
        return _check(rid, desc, "evidence", INC, "No successful state-changing tool call was recorded.",
                      evidence=ctx.eids("tools"), code="MISSING_EXECUTION_EVIDENCE")
    return _check(rid, desc, "evidence", INC, "Recorded tool calls do not reference the affected record.",
                  evidence=ctx.eids("tools"), code="MISSING_EXECUTION_EVIDENCE")


def check_trace_consistent(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    tr = ctx.trace
    if not tr.chain_valid:
        return _check(rid, desc, "evidence", INC, f"Trace integrity check failed: {tr.chain_detail}",
                      evidence=ctx.eids("integrity"), code="TRACE_INTEGRITY_FAILURE")
    if tr.contradictions:
        return _check(rid, desc, "evidence", INC, "Contradictory execution evidence: " + " ".join(tr.contradictions),
                      evidence=ctx.eids("integrity", "contradictions"), code="CONTRADICTORY_EVIDENCE")
    if tr.tool_calls == 0:
        return _check(rid, desc, "evidence", S, "No tool events to cross-check.", decisive=False,
                      evidence=ctx.eids("integrity"))
    return _check(rid, desc, "evidence", P, f"Hash chain intact over {tr.total_events} events; no contradictions found.",
                  evidence=ctx.eids("integrity"))


REGISTRY: dict[str, Callable[[CheckContext, str, str], CheckResult]] = {
    "targets_selected": check_targets_selected,
    "all_targets_removed": check_all_targets_removed,
    "target_identified": check_target_identified,
    "record_created": check_record_created,
    "exactly_one_new_record": check_exactly_one_new,
    "no_unrequested_target_changes": check_no_unrequested_target_changes,
    "no_changes_to_other_records": check_no_changes_to_other_records,
    "target_removed": check_target_removed,
    "file_exists": check_file_exists,
    "content_sha256_matches": check_content_sha,
    "size_matches": check_size,
    "no_accidental_overwrite": check_no_accidental_overwrite,
    "no_state_change": check_no_state_change,
    "execution_evidence_present": check_execution_evidence,
    "trace_consistent": check_trace_consistent,
}


def run_check(ctx: CheckContext, rid: str, desc: str) -> CheckResult:
    if rid.startswith("field_matches:"):
        return check_field_matches(ctx, rid, desc)
    if rid.startswith("bulk_field_matches:"):
        return check_bulk_field_matches(ctx, rid, desc)
    fn = REGISTRY.get(rid)
    if fn is None:
        return _check(rid, desc, "constraint", INC, f"No deterministic rule exists for requirement '{rid}'.",
                      code="INSUFFICIENT_EVIDENCE")
    return fn(ctx, rid, desc)
