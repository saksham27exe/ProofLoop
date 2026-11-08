"""Deterministic guard rails around the local LLM's proposed interpretation.

Small local models are useful for free-form wording but are not reliable on
their own. Testing ``qwen2.5:3b`` showed three failure patterns, each handled
here without trusting the model:

1. **Structural slips** - values of a *new* record put into ``target_match``
   (e.g. a ticket title), or schema fields echoed as ``null``
   (``{"name": null}``). These are repaired without adding information
   (:func:`normalize_llm_draft`).
2. **Hallucinated or injected values** - every identifier, name, e-mail, phone
   number and text the model proposes must literally appear in the
   instruction; otherwise the plan is not verifiable (:func:`grounding_problems`).
3. **Prompt injection** - text such as "ignore previous rules" or a fake
   ``</instruction>`` tag can steer a small model. Such instructions are flagged
   (:func:`injection_markers`) and an LLM-only plan for them needs human review.
"""

from __future__ import annotations

import html
import re
from typing import Any

from app.environments.base import EntitySpec, EnvironmentSchema
from app.interpretation.verification_plan import InterpretationDraft, VerificationPlan

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("a fake instruction delimiter", re.compile(r"<\s*/?\s*instruction\s*>", re.I)),
    ("'ignore previous rules/instructions'", re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your|the)\b[^.\n]{0,20}"
        r"\b(rules?|instructions?|prompts?|guidelines?)\b", re.I)),
    ("a role/system marker", re.compile(r"(^|[\s.;:])(system|assistant|developer)\s*:", re.I)),
    ("'you are now ...'", re.compile(r"\byou\s+are\s+now\b", re.I)),
    ("an output-format override", re.compile(
        r"\b(output|respond|reply|return)\b[^.\n]{0,30}\b(operation|target_id|target id)\b", re.I)),
]

_GROUNDED_TYPES = {"string", "email", "phone", "text"}
# ">900", "&gt;=5", "*gt;900" - an operator followed by a number (so "<html>" file content is never matched).
_OPERATOR_EXPR = re.compile(
    r"^[\s*&]*(?:(?:gte?|lte?|ge|le|ne|eq);\s*(?:>=|<=|!=|<>|==|>|<)?|>=|<=|!=|<>|==|>|<)\s*-?\d", re.I)


def injection_markers(instruction: str) -> list[str]:
    """Return human-readable descriptions of prompt-injection markers in ``instruction``."""
    return [label for label, pattern in _INJECTION_PATTERNS if pattern.search(instruction or "")]


def _field_names(entity: EntitySpec | None) -> set[str]:
    if entity is None:
        return set()
    names = set()
    for f in entity.fields:
        names.update({f.name.lower(), f.name.replace("_", " ").lower(), *(s.lower() for s in f.synonyms)})
    return names


def normalize_llm_draft(draft: InterpretationDraft, schema: EnvironmentSchema, instruction: str | None = None) -> InterpretationDraft:
    """Repair structural slips that do not change meaning. Never adds information."""
    d = draft.model_copy(deep=True)
    entity = schema.entity(d.entity)
    if entity is not None and instruction is not None:
        # A field that does not exist, with a value the user never wrote, is pure invention (seen with qwen2.5:3b:
        # "status_change_date": "2026-..."); drop it. If the user did write the value, keep it so the validator
        # reports the unknown field instead of silently ignoring something they asked for.
        known = _field_names(entity)
        d.values = {k: v for k, v in (d.values or {}).items()
                    if k.lower() in known or k.lower().replace("_", " ") in known or _appears(v, instruction, "string")}
    # A selection condition echoed into the new values ("total": "*gt;900" next to condition total > 900, seen with
    # qwen2.5:3b). An operator expression is never a literal new value, so drop it.
    d.values = {k: v for k, v in (d.values or {}).items()
                if not (isinstance(v, str) and _OPERATOR_EXPR.match(html.unescape(v)))}
    # {"name": null, "email": "..."}: small models echo schema fields with null. A null identifies nothing and
    # was not stated in the instruction (clearing a field is outside the supported grammar), so drop it.
    d.target_match = {k: v for k, v in (d.target_match or {}).items() if v is not None and str(v).strip() != ""}
    d.values = {k: v for k, v in (d.values or {}).items() if v is not None}
    if d.operation == "CREATE" and entity is not None and entity.key_type != "path" and d.target_match:
        merged = dict(d.target_match)
        merged.update(d.values or {})  # explicit values win
        d.values, d.target_match = merged, {}
    if d.conditions and d.target_scope == "one" and not d.target_id and d.operation in ("UPDATE", "DELETE", "READ"):
        # Conditions without an id mean "every matching record"; the validator still demands a real condition.
        d.target_scope = "all"
    if d.target_scope == "all" and d.target_id and d.operation in ("UPDATE", "DELETE", "READ"):
        d.target_scope, d.conditions = "one", []  # an explicit id wins over a vague "all"
    if d.operation == "UPDATE" and d.target_scope == "one" and d.target_id and d.target_match and not d.values:
        # "Ticket 12 is done, please resolve it" -> target_id "12", target_match {"status": "resolved"}, values {}:
        # the record is already identified by id, so the matched fields can only be the requested new values.
        d.values, d.target_match = dict(d.target_match), {}
    # "ambiguities": ["name"] - a bare field name is not a description of a gap.
    fields = _field_names(entity)
    d.ambiguities = [a for a in d.ambiguities if a and a.strip().lower().strip(" .'\"") not in fields]
    return d


def _squash(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.lower().split())


def _appears(value: Any, instruction: str, field_type: str) -> bool:
    text = _squash(str(value)).strip(" '\"")
    if not text:
        return True
    hay = _squash(instruction)
    if text in hay:
        return True
    if field_type == "phone":
        digits = re.sub(r"\D", "", text)
        return len(digits) >= 4 and digits in re.sub(r"\D", "", hay)
    return False


def grounding_problems(draft: InterpretationDraft, instruction: str, schema: EnvironmentSchema) -> list[str]:
    """Values the model proposed that are not present in the instruction (possible hallucination/injection)."""
    entity = schema.entity(draft.entity)
    if entity is None:
        return []
    problems: list[str] = []
    if draft.target_id:
        tid = draft.target_id.strip()
        if entity.key_type == "path":
            found = _squash(tid.replace("\\", "/")) in _squash(instruction.replace("\\", "/"))
        else:
            found = re.search(rf"(?<!\d){re.escape(tid)}(?!\d)", instruction) is not None
        if not found and not (draft.operation == "CREATE" and entity.key_type != "path"):
            problems.append(f"The model proposed target {tid!r}, which does not appear in the instruction.")
    for cond in draft.conditions:
        spec = entity.field(cond.field)
        if spec is not None and spec.type in _GROUNDED_TYPES and cond.value is not None \
                and not _appears(cond.value, instruction, spec.type):
            problems.append(f"The model proposed the condition {cond.field} {cond.op} {_short(cond.value)}, "
                            "which does not appear in the instruction (possible hallucination).")
    for source, mapping in (("target", draft.target_match), ("value", draft.values)):
        for fname, fval in (mapping or {}).items():
            spec = entity.field(fname)
            if spec is None or spec.type not in _GROUNDED_TYPES or fval is None:
                continue  # unknown fields/enums/integers are validated by the plan validator
            if not _appears(fval, instruction, spec.type):
                problems.append(
                    f"The model proposed {source} {spec.name}={_short(fval)}, which does not appear in the "
                    "instruction (possible hallucination)."
                )
    return problems


def injected_value_problems(plan: VerificationPlan) -> list[str]:
    """Expected values or target fields that themselves contain instruction-like text (any interpreter)."""
    problems = []
    fields = dict(plan.target.match) if plan.target else {}
    fields.update(plan.expected_state)
    for name, value in fields.items():
        if isinstance(value, str) and injection_markers(value):
            problems.append(
                f"The expected {name} {_short(value)} contains instruction-like text; confirm the intended value."
            )
    return problems


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 4] + "...'"
