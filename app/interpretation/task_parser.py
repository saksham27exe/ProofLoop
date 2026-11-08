"""Task interpretation: natural-language instruction -> validated verification plan.

Modes:

* ``llm``   - always consult the local Ollama model (error if unavailable);
  the rule parser still cross-checks it.
* ``rules`` - deterministic rule parser only (verification-only mode).
* ``auto``  - rule parser first; the local model is consulted only for wording
  the strict grammar cannot read (instant for the documented grammar, slow
  CPU inference only when it can add something). Falls back to the rules when
  the model is unavailable. There is never a fallback to any remote service.

When the LLM is used, its draft passes deterministic guard rails
(:mod:`app.interpretation.llm_guard`: structural normalisation, grounding of
every proposed value in the instruction text, prompt-injection markers) and the
rule parser runs as an independent cross-check:

* rules confident and in agreement -> the LLM plan is used;
* rules confident but the LLM disagrees -> the deterministic plan is kept and
  the disagreement is shown as a warning (small models are the likelier error);
* rules cannot parse the wording -> the guarded LLM plan is used; if the
  instruction looks like a prompt injection it needs explicit confirmation.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from app.environments.base import EnvironmentSchema
from app.interpretation.llm_guard import (
    grounding_problems,
    injected_value_problems,
    injection_markers,
    normalize_llm_draft,
)
from app.interpretation.plan_validator import build_plan
from app.interpretation.rule_parser import parse_instruction
from app.interpretation.verification_plan import InterpretationDraft, VerificationPlan
from app.llm.base import LLMError, LLMProvider
from app.llm.prompts import INTERPRETER_SYSTEM, interpretation_prompt


class InterpretationResult(BaseModel):
    plan: VerificationPlan
    draft: InterpretationDraft
    interpreter: str
    llm_latency_ms: int | None = None
    llm_attempts: int | None = None
    fallback_reason: str | None = None


def _signature(plan: VerificationPlan) -> tuple:
    target = plan.target.key if plan.target else None
    match = tuple(sorted((plan.target.match or {}).items())) if plan.target else ()
    scope = plan.target.scope if plan.target else "one"
    conds = tuple(sorted((c.field, c.op, str(c.value).lower()) for c in plan.target.conditions)) if plan.target else ()
    return (plan.entity, plan.operation, target, match, scope, conds,
            tuple(sorted((k, str(v)) for k, v in plan.expected_state.items())))


class TaskInterpreter:
    def __init__(
        self,
        provider_factory: Callable[[], LLMProvider] | None,
        mode: str = "auto",
        llm_enabled: bool = True,
    ) -> None:
        self._provider_factory = provider_factory
        self.mode = mode
        self.llm_enabled = llm_enabled

    def interpret(self, instruction: str, schema: EnvironmentSchema) -> InterpretationResult:
        result = self._interpret(instruction, schema)
        # Applies to every interpreter: a value that itself reads like an instruction is never silently accepted.
        for problem in injected_value_problems(result.plan):
            if problem not in result.plan.ambiguities:
                result.plan.ambiguities.append(problem)
        return result

    def _interpret(self, instruction: str, schema: EnvironmentSchema) -> InterpretationResult:
        use_llm = self.mode != "rules" and self.llm_enabled and self._provider_factory is not None
        if self.mode == "llm" and not use_llm:
            raise LLMError("LLM interpretation was requested but LLM features are disabled in settings.")
        if not use_llm:
            draft = parse_instruction(instruction, schema)
            plan = build_plan(draft, schema, instruction, "rules")
            return InterpretationResult(plan=plan, draft=draft, interpreter="rules")

        if self.mode == "auto":
            # Rules first: when the strict grammar reads the instruction, reconciliation would keep that reading
            # anyway, so a (slow, CPU-bound) model call could not change the plan. Free-form wording goes to the LLM.
            rule_draft = parse_instruction(instruction, schema)
            rule_plan = build_plan(rule_draft, schema, instruction, "rules")
            if rule_plan.verifiable:
                markers = injection_markers(instruction)
                if markers:
                    rule_plan.warnings.append(
                        f"The instruction contains text that looks like a prompt-injection attempt ({', '.join(markers)}). "
                        "It was treated as data; review the plan carefully."
                    )
                return InterpretationResult(plan=rule_plan, draft=rule_draft, interpreter="rules")

        provider = self._provider_factory()  # type: ignore[misc]
        try:
            result = provider.generate(
                interpretation_prompt(instruction, schema), system=INTERPRETER_SYSTEM, response_schema=InterpretationDraft
            )
        except LLMError as exc:
            if self.mode == "llm":
                raise
            draft = parse_instruction(instruction, schema)
            reason = f"LLM unavailable ({exc}); used the deterministic rule-based interpreter."
            plan = build_plan(draft, schema, instruction, "rules (fallback)", warnings=[reason])
            return InterpretationResult(plan=plan, draft=draft, interpreter="rules (fallback)", fallback_reason=str(exc))

        model_label = f"ollama:{result.model}"
        draft = normalize_llm_draft(result.parsed, schema, instruction)
        llm_plan = build_plan(draft, schema, instruction, model_label)
        for problem in grounding_problems(draft, instruction, schema):
            if problem not in llm_plan.ambiguities:
                llm_plan.ambiguities.append(problem)
        markers = injection_markers(instruction)
        injection_note = (
            f"The instruction contains text that looks like a prompt-injection attempt ({', '.join(markers)}). "
            "It was treated as data; review the plan carefully." if markers else None
        )
        meta = {"llm_latency_ms": result.latency_ms, "llm_attempts": result.attempts}

        # Independent deterministic cross-check and reconciliation.
        rule_draft = parse_instruction(instruction, schema)
        rule_plan = build_plan(rule_draft, schema, instruction, "rules")
        if rule_plan.verifiable:
            if llm_plan.verifiable and _signature(rule_plan) == _signature(llm_plan):
                if injection_note:
                    llm_plan.warnings.append(injection_note)
                return InterpretationResult(plan=llm_plan, draft=draft, interpreter=model_label, **meta)
            # The strict grammar matched this instruction exactly; a disagreeing (or unsure) small model is far
            # more likely to be wrong - or manipulated - than the grammar. Keep the deterministic plan.
            label = f"rules ({model_label} disagreed)"
            rule_plan.interpreter = label
            llm_reading = (
                f"{llm_plan.headline()} {llm_plan.expected_state}" if llm_plan.verifiable
                else "unresolved - " + "; ".join(llm_plan.ambiguities[:2])
            )
            rule_plan.warnings.append(
                f"The LLM and the rule-based interpreter disagree on this instruction (the LLM read it as: "
                f"{llm_reading}). The deterministic reading was kept; review the plan before approving."
            )
            if injection_note:
                rule_plan.warnings.append(injection_note)
            return InterpretationResult(plan=rule_plan, draft=rule_draft, interpreter=label, **meta)

        # Free-form wording outside the grammar: this is where the local model adds value.
        if injection_note:
            llm_plan.ambiguities.append(
                injection_note.replace("review the plan carefully", "the plan needs explicit human confirmation")
            )
        return InterpretationResult(plan=llm_plan, draft=draft, interpreter=model_label, **meta)
