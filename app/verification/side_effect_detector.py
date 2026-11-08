"""Side-effect detection: classify every observed state change.

Each change between the before- and after-snapshots is classified as:

* ``expected``             - the change the task asked for;
* ``permitted_dependency`` - a legitimate by-product (system-managed fields
  such as ``updated_at``; audit rows written by triggers for a changed record);
* ``duplicate``            - an extra copy of the requested record;
* ``side_effect``          - a change outside the task scope, established
  because the environment was exclusively held during the execution window;
* ``uncertain``            - outside the task scope in an environment without
  exclusive access, so it cannot be attributed with certainty.

Changes are attributed to logged tool calls when the trace references the
affected record.
"""

from __future__ import annotations

from collections.abc import Callable

from app.environments.base import EnvironmentSchema
from app.interpretation.verification_plan import VerificationPlan
from app.verification.models import (
    ChangeKind,
    DetectedChange,
    FieldDelta,
    StateDiff,
    TraceAssessment,
)

LabelFn = Callable[[str, str], str]


def classify_changes(
    diff: StateDiff,
    plan: VerificationPlan,
    schema: EnvironmentSchema,
    expected_keys: set[tuple[str, str]],
    duplicate_keys: set[tuple[str, str]],
    trace: TraceAssessment,
    label: LabelFn,
    candidate_keys: set[tuple[str, str]] | None = None,
) -> list[DetectedChange]:
    """``candidate_keys``: records that might be the intended target when the
    target is ambiguous. Changes to them cannot be judged in or out of scope."""
    candidate_keys = candidate_keys or set()
    results: list[DetectedChange] = []
    classified: dict[tuple[str, str], ChangeKind] = {}
    managed = set(plan.constraints.managed_fields)
    permitted = set(plan.constraints.permitted_fields)
    exclusive = schema.exclusive_access

    def attribution(coll: str, key: str) -> tuple[list[str], str]:
        calls = trace.referenced_records.get(f"{coll}:{key}", [])
        if calls:
            return calls, f"Referenced by logged tool call(s): {', '.join(calls)}"
        if exclusive:
            return [], "Not referenced by any logged tool call; attributed to this execution (exclusive execution window)"
        return [], "Not referenced by any logged tool call; another process may have made this change"

    outside_kind = ChangeKind.SIDE_EFFECT if exclusive else ChangeKind.UNCERTAIN

    # Values of parent rows the task legitimately deleted, per (parent collection, parent field): child rows that
    # the data source's own ON DELETE CASCADE / SET NULL rules removed or detached are consequences, not side effects.
    deleted_parent_values: dict[tuple[str, str], set[str]] = {}
    if plan.operation == "DELETE" and schema.cascades:
        for change in diff.changes:
            if change.change_type == "removed" and (change.collection, change.key) in expected_keys:
                for spec in schema.cascades:
                    if spec.parent_collection == change.collection:
                        value = (change.before or {}).get(spec.parent_field)
                        if value is not None:
                            deleted_parent_values.setdefault((spec.parent_collection, spec.parent_field), set()).add(str(value))

    def cascade_reason(change) -> str | None:
        for spec in schema.cascades:
            if spec.child_collection != change.collection:
                continue
            parents = deleted_parent_values.get((spec.parent_collection, spec.parent_field), set())
            before_val = (change.before or {}).get(spec.child_field)
            if before_val is None or str(before_val) not in parents:
                continue
            if spec.action == "CASCADE" and change.change_type == "removed":
                return f"Removed by the data source's ON DELETE CASCADE rule ({spec.child_field} -> deleted {spec.parent_collection} {before_val})"
            if (spec.action == "SET NULL" and change.change_type == "modified"
                    and set(change.changed_fields) == {spec.child_field} and (change.after or {}).get(spec.child_field) is None):
                return f"Detached by the data source's ON DELETE SET NULL rule (deleted {spec.parent_collection} {before_val})"
        return None

    # Pass 1: primary collections -------------------------------------------
    for change in diff.changes:
        if schema.dependency(change.collection):
            continue
        ident = (change.collection, change.key)
        calls, attr = attribution(*ident)
        lbl = label(change.collection, change.key)
        if ident in expected_keys:
            if change.change_type == "modified" and plan.operation in ("UPDATE", "CREATE"):
                expected_fields = {f: d for f, d in change.changed_fields.items() if f in permitted}
                unrequested = {f: d for f, d in change.changed_fields.items() if f not in permitted}
                exp_only = {f: d for f, d in expected_fields.items() if f not in managed}
                mgd_only = {f: d for f, d in expected_fields.items() if f in managed}
                if exp_only:
                    results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                                  change_type="modified", kind=ChangeKind.EXPECTED, fields=exp_only,
                                                  reason="Requested change on the target", attributed_calls=calls,
                                                  attribution=attr))
                if mgd_only:
                    results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                                  change_type="modified", kind=ChangeKind.PERMITTED_DEPENDENCY,
                                                  fields=mgd_only, reason="System-managed field(s) updated as a consequence of the change",
                                                  attributed_calls=calls, attribution=attr))
                if unrequested:
                    results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                                  change_type="modified", kind=outside_kind, fields=unrequested,
                                                  reason="Field(s) on the target changed although the task did not request it",
                                                  attributed_calls=calls, attribution=attr))
                classified[ident] = ChangeKind.EXPECTED
                continue
            expected_type = {"CREATE": "added", "DELETE": "removed"}.get(plan.operation or "", None)
            if change.change_type == expected_type or (plan.operation == "CREATE" and change.change_type == "modified"):
                results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                              change_type=change.change_type, kind=ChangeKind.EXPECTED,
                                              fields=_fields_of(change), reason="Requested change on the target",
                                              attributed_calls=calls, attribution=attr))
                classified[ident] = ChangeKind.EXPECTED
                continue
        if ident in candidate_keys:
            results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                          change_type=change.change_type, kind=ChangeKind.UNCERTAIN,
                                          fields=_fields_of(change),
                                          reason="This record is one of several candidates for the ambiguous target, so the change cannot be judged",
                                          attributed_calls=calls, attribution=attr))
            classified[ident] = ChangeKind.UNCERTAIN
            continue
        reason_cascade = cascade_reason(change) if ident not in expected_keys else None
        if reason_cascade:
            results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                          change_type=change.change_type, kind=ChangeKind.PERMITTED_DEPENDENCY,
                                          fields=_fields_of(change), reason=reason_cascade,
                                          attributed_calls=calls, attribution="Consequence of an expected deletion"))
            classified[ident] = ChangeKind.PERMITTED_DEPENDENCY
            continue
        if ident in duplicate_keys:
            results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                          change_type=change.change_type, kind=ChangeKind.DUPLICATE,
                                          fields=_fields_of(change), reason="Duplicate of the requested record",
                                          attributed_calls=calls, attribution=attr))
            classified[ident] = ChangeKind.DUPLICATE
            continue
        verb = {"added": "was created", "removed": "was deleted", "modified": "was modified"}[change.change_type]
        reason = (
            f"The target {verb}, which the task did not request"
            if ident in expected_keys
            else f"{lbl[:1].upper() + lbl[1:]} {verb}, but it is outside the task scope"
        )
        # In a shared (non-exclusive) source an unexplained change may come from another process - unless the
        # agent's own successful tool call touched that very record, which attributes it to the agent.
        kind = ChangeKind.SIDE_EFFECT if calls else outside_kind
        results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                      change_type=change.change_type, kind=kind, fields=_fields_of(change),
                                      reason=reason,
                                      attributed_calls=calls, attribution=attr))
        classified[ident] = kind

    # Pass 2: dependent collections (e.g. trigger-written audit rows) ------------
    for change in diff.changes:
        dep = schema.dependency(change.collection)
        if dep is None:
            continue
        row = change.after if change.change_type != "removed" else change.before
        row = row or {}
        parent = (str(row.get(dep.parent_collection_field)), str(row.get(dep.parent_key_field)))
        lbl = label(change.collection, change.key)
        parent_kind = classified.get(parent)
        if change.change_type == "added" and parent_kind is not None:
            reason = (
                f"Audit/dependency row generated by the change to {label(*parent)}"
                + (" (itself reported above)" if parent_kind != ChangeKind.EXPECTED else "")
            )
            results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                          change_type=change.change_type, kind=ChangeKind.PERMITTED_DEPENDENCY,
                                          fields=_fields_of(change), reason=reason, attribution="Dependency of a classified change"))
        else:
            calls, attr = attribution(change.collection, change.key)
            results.append(DetectedChange(collection=change.collection, key=change.key, label=lbl,
                                          change_type=change.change_type, kind=outside_kind, fields=_fields_of(change),
                                          reason="Dependency-collection change that does not correspond to any observed record change",
                                          attributed_calls=calls, attribution=attr))
    return results


def _fields_of(change) -> dict[str, FieldDelta]:
    if change.change_type == "modified":
        return dict(change.changed_fields)
    if change.change_type == "added":
        return {k: FieldDelta(before=None, after=v) for k, v in (change.after or {}).items()}
    return {k: FieldDelta(before=v, after=None) for k, v in (change.before or {}).items()}


def side_effects(changes: list[DetectedChange]) -> list[DetectedChange]:
    return [c for c in changes if c.kind == ChangeKind.SIDE_EFFECT]


def uncertain(changes: list[DetectedChange]) -> list[DetectedChange]:
    return [c for c in changes if c.kind == ChangeKind.UNCERTAIN]
