"""Validate an untrusted :class:`InterpretationDraft` into a :class:`VerificationPlan`.

The validator is the trust boundary between interpretation (possibly an LLM)
and verification. It:

* resolves the entity and fields strictly against the environment schema;
* rejects system-managed fields, unknown fields, invalid enum values and
  unsafe paths (recording them as ambiguities - never silently fixing them);
* coerces values to the schema types;
* derives verification requirements *deterministically* from the operation;
* flags destructive operations for explicit human approval.
"""

from __future__ import annotations

import re
from typing import Any

from app.environments.base import EntitySpec, EnvironmentSchema, FieldSpec
from app.interpretation.verification_plan import (
    Condition,
    InterpretationDraft,
    PlanConstraints,
    Target,
    VerificationPlan,
    VerificationRequirement,
)
from app.utils.security import UnsafePathError, normalize_relative_path

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d\s().\-]{5,24}(?:\s*(?:x|ext\.?)\s*\d{1,6})?$", re.I)
_ENUM_SYNONYMS = {
    "in progress": "in_progress", "in-progress": "in_progress", "inprogress": "in_progress", "wip": "in_progress",
    "urgent": "critical", "normal": "medium", "done": "resolved", "complete": "resolved", "completed": "resolved",
    "reopened": "open", "premium": "platinum", "basic": "standard",
}


def _resolve_field(entity: EntitySpec, name: str) -> FieldSpec | None:
    spec = entity.field(name)
    if spec:
        return spec
    needle = name.strip().lower().replace("_", " ")
    for f in entity.fields:
        if needle == f.name.replace("_", " ") or needle in [s.lower() for s in f.synonyms]:
            return f
    return None


def coerce_value(value: Any, spec: FieldSpec) -> tuple[Any, str | None, str | None]:
    """Return (coerced_value, ambiguity, warning)."""
    if value is None:
        if spec.required_on_create:
            return None, f"'{spec.name}' cannot be empty.", None
        return None, None, None
    if spec.type == "text":
        return (value if isinstance(value, str) else str(value)), None, None
    if spec.type == "integer":
        try:
            return int(str(value).strip()), None, None
        except ValueError:
            return value, f"'{spec.name}' must be an integer (got {value!r}).", None
    if spec.type == "number":
        try:
            number = float(str(value).strip().replace(",", ""))
        except ValueError:
            return value, f"'{spec.name}' must be a number (got {value!r}).", None
        return (int(number) if number.is_integer() and "." not in str(value) else number), None, None
    text = str(value).strip()
    if spec.type == "enum":
        norm = text.lower().replace("-", " ").strip()
        norm = _ENUM_SYNONYMS.get(norm, norm).replace(" ", "_")
        if norm not in spec.enum_values:
            return text, f"'{text}' is not a valid {spec.name}; expected one of {', '.join(spec.enum_values)}.", None
        return norm, None, None
    if spec.type == "email":
        warning = None if _EMAIL_RE.match(text) else f"'{text}' does not look like a valid e-mail address."
        return text, None, warning
    if spec.type == "phone" and text:
        warning = None if _PHONE_RE.match(text) else f"'{text}' does not look like a valid phone number."
        return text, None, warning
    if not text:
        return text, f"'{spec.name}' cannot be empty.", None
    return text, None, None


def derive_requirements(op: str, entity: EntitySpec, expected: dict[str, Any], constraints: PlanConstraints,
                        *, bulk: bool = False) -> list[VerificationRequirement]:
    reqs: list[VerificationRequirement] = []
    label = entity.display_name.lower()

    def add(rid: str, desc: str) -> None:
        reqs.append(VerificationRequirement(id=rid, description=desc))

    if bulk:
        add("targets_selected", f"At least one {label} matched the condition before execution")
        if op == "UPDATE":
            for f in expected:
                add(f"bulk_field_matches:{f}", f"Every selected {label} has {f} = {expected[f]!r} after execution")
            add("no_unrequested_target_changes", f"No other field of the selected {label} records changed (system fields excepted)")
            add("no_changes_to_other_records", f"No {label} outside the selection (and no other record) changed")
        elif op == "DELETE":
            add("all_targets_removed", f"Every selected {label} no longer exists")
            add("no_changes_to_other_records", f"No {label} outside the selection (and no other record) changed")
        elif op == "READ":
            add("no_state_change", "Reading did not change any state")
    elif entity.key_type == "path":
        if op in ("CREATE", "UPDATE"):
            if op == "UPDATE":
                add("target_identified", "The file existed before execution")
            add("file_exists", "The file exists at the requested path after execution")
            add("content_sha256_matches", "SHA-256 of the file equals SHA-256 of the requested content")
            add("size_matches", "File size equals the byte length of the requested content")
            if op == "CREATE" and constraints.must_not_preexist:
                add("no_accidental_overwrite", "No pre-existing file was overwritten")
            if op == "CREATE":
                add("exactly_one_new_record", "No duplicate copies of the file were created")
            add("no_changes_to_other_records", "No other file was created, modified or deleted")
        elif op == "DELETE":
            add("target_identified", "The file existed before execution")
            add("target_removed", "The file no longer exists after execution")
            add("no_changes_to_other_records", "No other file was created, modified or deleted")
        elif op == "READ":
            add("target_identified", "The file exists")
            add("no_state_change", "Reading did not change any file")
    else:
        if op == "CREATE":
            add("record_created", f"A new {label} record exists after execution")
            for f in expected:
                add(f"field_matches:{f}", f"New {label}.{f} equals {expected[f]!r}")
            add("exactly_one_new_record", f"Exactly one matching {label} was created (no duplicates)")
            add("no_changes_to_other_records", "No other records were created, modified or deleted")
        elif op == "UPDATE":
            add("target_identified", f"The target {label} exists and is uniquely identified")
            for f in expected:
                add(f"field_matches:{f}", f"{entity.display_name}.{f} equals {expected[f]!r} after execution")
            add("no_unrequested_target_changes", f"No other field of the target {label} changed (system fields excepted)")
            add("no_changes_to_other_records", "No other records were created, modified or deleted")
        elif op == "DELETE":
            add("target_identified", f"The target {label} existed before execution and is uniquely identified")
            add("target_removed", f"The target {label} no longer exists")
            add("no_changes_to_other_records", "No other records were created, modified or deleted")
        elif op == "READ":
            add("target_identified", f"The target {label} exists")
            add("no_state_change", "Reading did not change any state")
    if op in ("CREATE", "UPDATE", "DELETE"):
        add("execution_evidence_present", "The execution trace contains a successful tool invocation that accounts for the change")
        add("trace_consistent", "The execution trace is intact and free of contradictions")
    return reqs


def build_plan(
    draft: InterpretationDraft,
    schema: EnvironmentSchema,
    instruction: str,
    interpreter: str,
    warnings: list[str] | None = None,
) -> VerificationPlan:
    plan = VerificationPlan(
        instruction=instruction,
        environment=schema.name,
        interpreter=interpreter,
        ambiguities=list(dict.fromkeys(draft.ambiguities)),
        warnings=list(warnings or []),
    )

    def ambiguous(msg: str) -> None:
        if msg not in plan.ambiguities:
            plan.ambiguities.append(msg)

    entity = schema.entity(draft.entity)
    if entity is None:
        if draft.entity:
            ambiguous(f"'{draft.entity}' is not a known entity in the {schema.title}.")
        elif not plan.ambiguities:
            ambiguous("Could not determine which kind of record the instruction refers to.")
        return plan
    plan.entity, plan.entity_display, plan.collection = entity.name, entity.display_name, entity.collection

    op = draft.operation
    if op is None:
        if not any("operation" in a for a in plan.ambiguities):
            ambiguous("Could not determine the intended operation.")
        return plan
    if op not in entity.operations:
        ambiguous(f"{op} is not supported for {entity.display_name.lower()} records.")
        return plan
    plan.operation = op

    # -- target --------------------------------------------------------
    key: str | None = None
    match: dict[str, Any] = {}
    bulk = draft.target_scope == "all" and op in ("UPDATE", "DELETE", "READ") and entity.key_type != "path"
    conditions: list[Condition] = []
    if entity.key_type == "path":
        if draft.target_id:
            try:
                key = normalize_relative_path(draft.target_id)
            except UnsafePathError as exc:
                ambiguous(f"Unsafe or invalid file path: {exc}")
        elif not any("path" in a for a in plan.ambiguities):
            ambiguous("No file path was identified.")
    elif bulk:
        # Every record satisfying the conditions in the BEFORE state is a target. Equality pairs given as
        # target_match are accepted as conditions too.
        raw = [Condition(field=f, op="=", value=v) for f, v in (draft.target_match or {}).items()] + list(draft.conditions)
        for cond in raw:
            spec = _resolve_field(entity, cond.field)
            if spec is None:
                ambiguous(f"Unknown field '{cond.field}' in the selection condition.")
                continue
            if cond.op == "contains":
                value: Any = str(cond.value if cond.value is not None else "").strip()
                if not value:
                    ambiguous(f"The 'contains' condition on {spec.name} has no value.")
                    continue
            else:
                value, problem, _ = coerce_value(cond.value, spec) if cond.value is not None else (None, None, None)
                if problem:
                    ambiguous(problem)
                    continue
            conditions.append(Condition(field=spec.name, op=cond.op, value=value))
        if not conditions and not any("condition" in a for a in plan.ambiguities):
            ambiguous(f"A task on several {entity.display_name.lower()} records needs a condition that selects them "
                      "(for example 'all customers in Berlin').")
    else:
        if draft.target_id is not None:
            if entity.key_type == "string" and draft.target_id.strip():
                key = draft.target_id.strip()
            elif re.fullmatch(r"\d{1,12}", draft.target_id.strip()):
                key = str(int(draft.target_id))
            elif op != "CREATE":
                ambiguous(f"Target id {draft.target_id!r} is not a valid {entity.display_name.lower()} id.")
        for fname, fval in (draft.target_match or {}).items():
            spec = _resolve_field(entity, fname)
            if spec is None:
                ambiguous(f"Unknown field '{fname}' used to identify the target.")
                continue
            coerced, problem, _ = coerce_value(fval, spec)
            if problem:
                ambiguous(problem)
            else:
                match[spec.name] = coerced
        if op == "CREATE":
            if key is not None:
                plan.warnings.append("An explicit id was given for a new record; ids are assigned by the system and will not be checked.")
            key, match = None, {}
        elif key is None and not match and not any("target" in a for a in plan.ambiguities):
            ambiguous(f"No target {entity.display_name.lower()} (id or identifying field) was identified.")
    if bulk:
        plan.target = Target(key_field=entity.key_field, scope="all", conditions=conditions)
    elif op != "CREATE" or entity.key_type == "path":
        plan.target = Target(key_field=entity.key_field, key=key, match=match)
    if not bulk and key is None and match and op in ("UPDATE", "DELETE"):
        plan.warnings.append("The target is identified by field values rather than an id; it must match exactly one record.")

    # -- expected values -------------------------------------------------
    expected: dict[str, Any] = {}
    for fname, fval in (draft.values or {}).items():
        spec = _resolve_field(entity, fname)
        if spec is None:
            ambiguous(f"Unknown field '{fname}' for {entity.display_name.lower()} records.")
            continue
        if spec.name in entity.managed_fields or spec.name == entity.key_field:
            ambiguous(f"'{spec.name}' is system-managed and cannot be set by a task.")
            continue
        coerced, problem, warning = coerce_value(fval, spec)
        if problem:
            ambiguous(problem)
            continue
        if warning:
            plan.warnings.append(warning)
        expected[spec.name] = coerced
    if op in ("DELETE", "READ") and expected:
        plan.warnings.append(f"Field values are ignored for {op} operations.")
        expected = {}
    if op in ("CREATE", "UPDATE") and not expected and not any("value" in a or "content" in a for a in plan.ambiguities):
        ambiguous("No expected field values were stated for this operation.")
    if op == "CREATE" and entity.key_type != "path":
        missing = [f.name for f in entity.fields
                   if f.required_on_create and f.name not in expected and f.name not in entity.managed_fields]
        if missing:
            plan.warnings.append(f"Required field(s) not stated: {', '.join(missing)}. The agent may be unable to create the record.")
    plan.expected_state = expected

    # -- constraints & requirements ----------------------------------------
    plan.constraints = PlanConstraints(
        exactly_one_affected=True,
        unexpected_changes_allowed=False,
        permitted_fields=sorted(set(expected) | set(entity.managed_fields)),
        managed_fields=list(entity.managed_fields),
        must_not_preexist=(entity.key_type == "path" and op == "CREATE" and not draft.allow_overwrite),
    )
    plan.requirements = derive_requirements(op, entity, expected, plan.constraints, bulk=bulk)
    if op == "DELETE":
        plan.requires_approval = True
        plan.approval_reason = ("Destructive bulk operation: every matching record will be permanently removed."
                                if bulk else "Destructive operation: the record will be permanently removed.")
    elif bulk and op == "UPDATE":
        plan.requires_approval = True
        plan.approval_reason = "Bulk change: every record matching the condition will be modified."
    return plan
