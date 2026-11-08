"""Structured task interpretation and verification-plan schemas.

Two stages, deliberately separated:

1. :class:`InterpretationDraft` - what an interpreter (local LLM or the
   deterministic rule parser) *proposes*. It is untrusted.
2. :class:`VerificationPlan` - the validated, normalised plan produced by
   :mod:`app.interpretation.plan_validator`. Requirements are derived
   deterministically from the operation; the LLM never writes them.
"""

from __future__ import annotations

import html
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Operation = Literal["CREATE", "UPDATE", "DELETE", "READ"]
ScalarValue = str | int | float | bool | None
ConditionOp = Literal["=", "!=", "<", "<=", ">", ">=", "contains"]
_OP_ALIASES = {"==": "=", "eq": "=", "equals": "=", "is": "=", "<>": "!=", "ne": "!=", "not": "!=", "lt": "<", "lte": "<=",
               "gt": ">", "gte": ">=", "like": "contains", "includes": "contains"}


def _number_or_text(text: str) -> Any:
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() and "." not in text else number


class Condition(BaseModel):
    """One selection condition, e.g. ``city = 'Berlin'`` or ``total > 100``."""

    field: str
    op: ConditionOp = "="
    value: ScalarValue = None

    @field_validator("op", mode="before")
    @classmethod
    def _norm_op(cls, value: Any) -> Any:
        if isinstance(value, str):
            v = html.unescape(value).strip().lower()
            return _OP_ALIASES.get(v, v)
        return value

    @model_validator(mode="after")
    def _split_prefixed_value(self) -> Condition:
        """``op "=" value ">900"`` (or HTML-escaped ``"&gt;900"``) -> ``op ">" value 900``."""
        if self.op == "=" and isinstance(self.value, str):
            m = re.match(r"^\s*(>=|<=|!=|>|<)\s*(.+)$", html.unescape(self.value))
            if m:
                self.op = m.group(1)  # type: ignore[assignment]
                self.value = _number_or_text(m.group(2).strip())
        elif self.op in ("<", "<=", ">", ">=") and isinstance(self.value, str):
            # "&gt;900", "*gt;900", ">= 900": the operator is already known, keep only the number.
            m = re.fullmatch(r"[\s*&]*(?:gte?|lte?|ge|le)?;?\s*[<>=]*\s*(-?\d+(?:\.\d+)?)\s*", html.unescape(self.value), re.I)
            if m:
                self.value = _number_or_text(m.group(1))
        return self

    def describe(self) -> str:
        return f"{self.field} {self.op} {self.value!r}"


class InterpretationDraft(BaseModel):
    """Proposed interpretation of a natural-language instruction."""

    operation: Operation | None = Field(
        None, description="CREATE (insert/new), UPDATE (modify), DELETE (remove) or READ (look up)."
    )
    entity: str | None = Field(None, description="Entity name from the catalog, e.g. employee, customer, ticket, file.")
    target_id: str | None = Field(
        None, description="Identifier of the existing record for UPDATE/DELETE/READ (record id or file path)."
    )
    target_match: dict[str, ScalarValue] = Field(
        default_factory=dict, description="Field values identifying the target when no id is given, e.g. {'name': 'Alex Kim'}."
    )
    values: dict[str, ScalarValue] = Field(
        default_factory=dict, description="Field values the record must have after the operation (only values stated in the instruction)."
    )
    allow_overwrite: bool = Field(
        False, description="Files only: true when the instruction permits replacing an existing file (write/save/overwrite)."
    )
    target_scope: Literal["one", "all"] = Field(
        "one", description="'all' when the task applies to every record matching the conditions (e.g. 'all customers in Berlin')."
    )
    conditions: list[Condition] = Field(
        default_factory=list, description="Selection conditions for target_scope 'all', e.g. [{'field': 'city', 'op': '=', 'value': 'Berlin'}]."
    )
    ambiguities: list[str] = Field(
        default_factory=list, description="Anything unclear or missing. Never invent values; list the gap here instead."
    )

    @field_validator("operation", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        if isinstance(value, str):
            v = value.strip().upper()
            aliases = {"INSERT": "CREATE", "ADD": "CREATE", "MODIFY": "UPDATE", "SET": "UPDATE", "REMOVE": "DELETE",
                       "GET": "READ", "FETCH": "READ", "NONE": None, "": None}
            return aliases.get(v, v)
        return value

    @field_validator("target_id", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value.strip() if isinstance(value, str) and value.strip() else None
        return str(value)


class Target(BaseModel):
    key_field: str
    key: str | None = None
    match: dict[str, Any] = Field(default_factory=dict)
    # scope "all": every record satisfying ``conditions`` in the BEFORE state is a target (bulk task).
    scope: Literal["one", "all"] = "one"
    conditions: list[Condition] = Field(default_factory=list)

    @property
    def bulk(self) -> bool:
        return self.scope == "all"

    def describe(self, display_name: str) -> str:
        if self.bulk:
            cond = " and ".join(c.describe() for c in self.conditions) or "(no condition)"
            return f"every {display_name} where {cond}"
        if self.key is not None:
            return f"{display_name} {self.key}" if self.key_field != "path" else f"{display_name} '{self.key}'"
        if self.match:
            cond = ", ".join(f"{k}={v!r}" for k, v in self.match.items())
            return f"{display_name} where {cond}"
        return f"{display_name} (unspecified)"


class PlanConstraints(BaseModel):
    exactly_one_affected: bool = True
    unexpected_changes_allowed: bool = False
    permitted_fields: list[str] = Field(default_factory=list, description="Fields allowed to change on the target.")
    managed_fields: list[str] = Field(default_factory=list, description="System-maintained fields (legitimate dependencies).")
    must_not_preexist: bool = False


class VerificationRequirement(BaseModel):
    id: str
    description: str


class VerificationPlan(BaseModel):
    plan_version: str = "1.0"
    instruction: str
    environment: str
    entity: str | None = None
    entity_display: str | None = None
    collection: str | None = None
    operation: Operation | None = None
    target: Target | None = None
    expected_state: dict[str, Any] = Field(default_factory=dict)
    constraints: PlanConstraints = Field(default_factory=PlanConstraints)
    requirements: list[VerificationRequirement] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    approval_reason: str | None = None
    interpreter: str = "rules"

    @property
    def verifiable(self) -> bool:
        return not self.ambiguities and self.operation is not None and self.collection is not None

    def headline(self) -> str:
        if not self.operation or not self.entity_display:
            return "Unresolved task"
        if self.operation == "CREATE":
            if self.target and self.target.key:
                return f"CREATE {self.entity_display} '{self.target.key}'"
            return f"CREATE {self.entity_display}"
        target = self.target.describe(self.entity_display) if self.target else self.entity_display
        return f"{self.operation} {target}"
