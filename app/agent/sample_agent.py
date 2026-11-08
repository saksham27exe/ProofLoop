"""Built-in sample agent with controllable fault injection.

The agent plans its action (deterministic rules or the local LLM), then calls
environment tools. Every tool call/response flows through the trace
collector. Fault modes perturb the agent's behaviour in realistic ways so the
verifier can be exercised against known failure patterns.
"""

from __future__ import annotations

import math
from typing import Any, Protocol

from app.agent.base_agent import AgentRunResult, BaseAgent
from app.agent.fault_injection import FaultMode
from app.collector.trace_collector import SuppressingCollector, TraceCollector
from app.environments.base import (
    EntitySpec,
    Environment,
    EnvironmentSchema,
    Toolbox,
    ToolResult,
)
from app.interpretation.llm_guard import normalize_llm_draft
from app.interpretation.plan_validator import build_plan
from app.interpretation.rule_parser import parse_instruction
from app.interpretation.verification_plan import InterpretationDraft, VerificationPlan
from app.llm.base import LLMError, LLMProvider
from app.llm.prompts import AGENT_SYSTEM, interpretation_prompt
from app.utils.security import normalize_relative_path


class AgentError(Exception):
    pass


# --------------------------------------------------------------------------
# Planners
# --------------------------------------------------------------------------
class Planner(Protocol):
    name: str

    def plan(self, instruction: str, schema: EnvironmentSchema) -> VerificationPlan: ...


class RulePlanner:
    name = "rules"

    def plan(self, instruction: str, schema: EnvironmentSchema) -> VerificationPlan:
        return build_plan(parse_instruction(instruction, schema), schema, instruction, "agent:rules")


class LLMPlanner:
    name = "llm"

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def plan(self, instruction: str, schema: EnvironmentSchema) -> VerificationPlan:
        result = self.provider.generate(
            interpretation_prompt(instruction, schema), system=AGENT_SYSTEM, response_schema=InterpretationDraft
        )
        # Structural clean-up only (e.g. new-record fields placed in target_match). The agent is *not* given
        # ProofLoop's grounding checks: its own mistakes are exactly what the verifier must catch.
        return build_plan(normalize_llm_draft(result.parsed, schema), schema, instruction, f"agent:{result.model}")


# --------------------------------------------------------------------------
# Tool invocation with tracing and fault behaviour
# --------------------------------------------------------------------------
class ToolInvoker:
    def __init__(self, toolbox: Toolbox, tracer: TraceCollector, fault: FaultMode, mutating: set[str]) -> None:
        self.toolbox = toolbox
        self.tracer = tracer
        self.fault = fault
        self.mutating = mutating
        self.calls = 0
        self._fail_next_mutation = fault == FaultMode.TOOL_FAILURE_FALSE_CLAIM

    def __call__(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.calls += 1
        call_id = self.tracer.new_call_id()
        self.tracer.tool_call(call_id, tool, args)
        is_mutation = tool in self.mutating
        if is_mutation and self.fault == FaultMode.FABRICATED_SUCCESS:
            # Hallucinated tool output: nothing is executed.
            fake: dict[str, Any] = {"rows_affected": 1}
            if "record_id" in args:
                fake["record_id"] = args["record_id"]
            if "path" in args:
                fake["path"] = args["path"]
            result = ToolResult.success(fake, status_code=200 if tool.startswith("api_") else None)
        elif is_mutation and self._fail_next_mutation:
            self._fail_next_mutation = False
            result = self.toolbox.call(tool, args, inject_failure=True)
        else:
            result = self.toolbox.call(tool, args)
        output = dict(result.output)
        if result.error:
            output["error"] = result.error
        self.tracer.tool_response(call_id, tool, result.status, output, result.status_code)
        if is_mutation and result.ok and self.fault == FaultMode.CONTRADICTORY_LOGS:
            self.tracer.error(
                "Transaction rolled back after commit timeout - changes were NOT persisted",
                call_id=call_id, tool=tool, details={"rollback": True},
            )
        return result


# --------------------------------------------------------------------------
# Environment-specific skills
# --------------------------------------------------------------------------
class _Skills:
    def __init__(self, env: Environment, invoke: ToolInvoker, entity: EntitySpec) -> None:
        self.env = env
        self.invoke = invoke
        self.entity = entity

    def resolve(self, plan: VerificationPlan) -> str | None: ...
    def create(self, values: dict[str, Any], key: str | None) -> ToolResult: ...
    def update(self, key: str, values: dict[str, Any]) -> ToolResult: ...
    def delete(self, key: str) -> ToolResult: ...
    def read(self, key: str) -> ToolResult: ...
    def keys(self) -> list[str]: ...
    def duplicate(self, values: dict[str, Any], key: str | None) -> ToolResult: ...
    def collateral(self, exclude: set[str]) -> ToolResult | None: ...

    def created_key(self, result: ToolResult, key: str | None) -> str | None:
        return key

    def neighbor(self, key: str) -> str | None:
        keys = [k for k in self.keys() if k != key]
        if not keys:
            return None
        after = [k for k in keys if _sort_key(k) > _sort_key(key)]
        return after[0] if after else keys[0]


def _sort_key(k: str) -> tuple:
    return (0, int(k), "") if k.isdigit() else (1, 0, k)


class _DatabaseSkills(_Skills):
    @property
    def table(self) -> str:
        return self.entity.collection

    def resolve(self, plan: VerificationPlan) -> str | None:
        assert plan.target is not None
        if plan.target.key is not None:
            return plan.target.key
        res = self.invoke("db_find", {"table": self.table, "where": plan.target.match})
        records = res.output.get("records") or []
        return str(records[0]["id"]) if res.ok and records else None  # takes the first match

    def create(self, values: dict[str, Any], key: str | None) -> ToolResult:
        return self.invoke("db_insert", {"table": self.table, "values": values})

    def created_key(self, result: ToolResult, key: str | None) -> str | None:
        rid = result.output.get("record_id")
        return str(rid) if rid is not None else None

    def update(self, key: str, values: dict[str, Any]) -> ToolResult:
        self.invoke("db_get", {"table": self.table, "record_id": int(key)})
        return self.invoke("db_update", {"table": self.table, "record_id": int(key), "values": values})

    def delete(self, key: str) -> ToolResult:
        return self.invoke("db_delete", {"table": self.table, "record_id": int(key)})

    def read(self, key: str) -> ToolResult:
        return self.invoke("db_get", {"table": self.table, "record_id": int(key)})

    def keys(self) -> list[str]:
        res = self.invoke("db_list_ids", {"table": self.table})
        return [str(i) for i in res.output.get("ids", [])]

    def duplicate(self, values: dict[str, Any], key: str | None) -> ToolResult:
        return self.create(values, key)

    def collateral(self, exclude: set[str]) -> ToolResult | None:
        candidates = [k for k in self.keys() if k not in exclude]
        if not candidates:
            return None
        other = candidates[len(candidates) // 3]
        current = self.invoke("db_get", {"table": self.table, "record_id": int(other)}).output.get("record", {})
        new_phone = "+1-555-0100" if current.get("phone") != "+1-555-0100" else "+1-555-0101"
        return self.invoke("db_update", {"table": self.table, "record_id": int(other), "values": {"phone": new_phone}})


class _ApiSkills(_Skills):
    def _list(self) -> list[dict[str, Any]]:
        res = self.invoke("api_list_tickets", {})
        body = res.output.get("body") or {}
        return body.get("tickets", []) if isinstance(body, dict) else []

    def resolve(self, plan: VerificationPlan) -> str | None:
        assert plan.target is not None
        if plan.target.key is not None:
            return plan.target.key
        for t in self._list():
            if all(str(t.get(k)) == str(v) for k, v in plan.target.match.items()):
                return str(t["id"])
        return None

    def create(self, values: dict[str, Any], key: str | None) -> ToolResult:
        return self.invoke("api_create_ticket", {"fields": values})

    def created_key(self, result: ToolResult, key: str | None) -> str | None:
        body = result.output.get("body")
        return str(body["id"]) if isinstance(body, dict) and body.get("id") is not None else None

    def update(self, key: str, values: dict[str, Any]) -> ToolResult:
        self.invoke("api_get_ticket", {"ticket_id": int(key)})
        return self.invoke("api_update_ticket", {"ticket_id": int(key), "fields": values})

    def delete(self, key: str) -> ToolResult:
        return self.invoke("api_delete_ticket", {"ticket_id": int(key)})

    def read(self, key: str) -> ToolResult:
        return self.invoke("api_get_ticket", {"ticket_id": int(key)})

    def keys(self) -> list[str]:
        return [str(t["id"]) for t in self._list()]

    def duplicate(self, values: dict[str, Any], key: str | None) -> ToolResult:
        return self.create(values, key)

    def collateral(self, exclude: set[str]) -> ToolResult | None:
        tickets = [t for t in self._list() if str(t["id"]) not in exclude]
        if not tickets:
            return None
        other = tickets[len(tickets) // 3]
        new_priority = "low" if other.get("priority") != "low" else "medium"
        return self.invoke("api_update_ticket", {"ticket_id": int(other["id"]), "fields": {"priority": new_priority}})


class _FileSkills(_Skills):
    def resolve(self, plan: VerificationPlan) -> str | None:
        return plan.target.key if plan.target else None

    def create(self, values: dict[str, Any], key: str | None) -> ToolResult:
        if key is None:
            raise AgentError("No file path to write")
        return self.invoke("file_write", {"path": key, "content": values.get("content", "")})

    def update(self, key: str, values: dict[str, Any]) -> ToolResult:
        self.invoke("file_read", {"path": key})
        return self.invoke("file_write", {"path": key, "content": values.get("content", "")})

    def delete(self, key: str) -> ToolResult:
        return self.invoke("file_delete", {"path": key})

    def read(self, key: str) -> ToolResult:
        return self.invoke("file_read", {"path": key})

    def keys(self) -> list[str]:
        return list(self.invoke("file_list", {}).output.get("files", []))

    @staticmethod
    def _variant(path: str, suffix: str) -> str:
        head, _, name = path.rpartition("/")
        stem, dot, ext = name.rpartition(".")
        new_name = f"{stem}{suffix}.{ext}" if dot and stem else f"{name}{suffix}"
        return normalize_relative_path(f"{head}/{new_name}" if head else new_name)

    def wrong_path(self, key: str) -> str:
        return self._variant(key, "_v2")

    def duplicate(self, values: dict[str, Any], key: str | None) -> ToolResult:
        assert key is not None
        return self.invoke("file_write", {"path": self._variant(key, " (1)"), "content": values.get("content", "")})

    def collateral(self, exclude: set[str]) -> ToolResult | None:
        others = [k for k in self.keys() if k not in exclude]
        if not others:
            return None
        other = others[0]
        current = self.invoke("file_read", {"path": other}).output.get("content") or ""
        return self.invoke("file_write", {"path": other, "content": current + "\n# touched by agent\n"})


_SKILLS: dict[str, type[_Skills]] = {"database": _DatabaseSkills, "api": _ApiSkills, "file": _FileSkills}


def _partial(values: dict[str, Any]) -> dict[str, Any]:
    if len(values) >= 2:
        keep = list(values)[: max(1, math.floor(len(values) / 2))]
        return {k: values[k] for k in keep}
    key, value = next(iter(values.items()))
    if isinstance(value, str) and len(value) > 1:
        return {key: value[: max(1, len(value) // 2)]}
    return values


def _fmt_values(values: dict[str, Any]) -> str:
    parts = []
    for k, v in values.items():
        text = repr(v) if not isinstance(v, str) or len(v) <= 40 else repr(v[:37] + "...")
        parts.append(f"{k} = {text}")
    return ", ".join(parts)


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------
class SampleAgent(BaseAgent):
    name = "proofloop-sample-agent"
    framework = "native-python"

    def __init__(self, environment: Environment, planner: Planner, fault: FaultMode | str = FaultMode.NONE) -> None:
        self.environment = environment
        self.planner = planner
        self.fault = FaultMode(fault)

    def run(self, instruction: str, collector: TraceCollector) -> AgentRunResult:
        tracer: TraceCollector = SuppressingCollector(collector) if self.fault == FaultMode.MISSING_EVIDENCE else collector
        schema = self.environment.schema
        invoker = ToolInvoker(self.environment.toolbox(), tracer, self.fault, schema.mutating_tools)

        def finish(status: str, text: str, error: str | None = None) -> AgentRunResult:
            tracer.claim(text, status)
            return AgentRunResult(claimed_status=status, claim=text, error=error,  # type: ignore[arg-type]
                                  tool_calls=invoker.calls, planner=self.planner.name)

        try:
            plan = self.planner.plan(instruction, schema)
        except LLMError as exc:
            tracer.error(f"Planner failed: {exc}")
            return finish("failure", f"I could not plan this task because my planner failed: {exc}", str(exc))
        if not plan.verifiable or plan.entity is None:
            reason = "; ".join(plan.ambiguities) or "unrecognised instruction"
            tracer.plan([f"Unable to interpret instruction: {reason}"])
            return finish("failure", f"I could not understand the task: {reason}", reason)

        entity = schema.entity(plan.entity)
        assert entity is not None
        skills = _SKILLS[self.environment.name](self.environment, invoker, entity)
        op = plan.operation
        values = dict(plan.expected_state)
        noun = entity.display_name.lower()
        tracer.plan([plan.headline()] + ([f"set {_fmt_values(values)}"] if values else []),
                    {"planner": self.planner.name, "operation": op})

        try:
            if self.fault == FaultMode.PARTIAL_EXECUTION and op in ("CREATE", "UPDATE") and values:
                values = _partial(values)

            if op == "CREATE":
                key = plan.target.key if plan.target else None
                if self.fault == FaultMode.WRONG_RECORD and isinstance(skills, _FileSkills) and key:
                    key = skills.wrong_path(key)
                result = skills.create(values, key)
                new_key = skills.created_key(result, key)
                if self.fault == FaultMode.DUPLICATE_OPERATION and result.ok:
                    skills.duplicate(values, key)
                touched = {new_key} if new_key else set()
                described = f"the {noun} '{key}'" if entity.key_type == "path" else f"a new {noun} record ({_fmt_values(plan.expected_state)})"
                success_text = f"Successfully created {described}."
            else:
                key = skills.resolve(plan)
                if key is None:
                    tracer.error(f"Target {noun} not found")
                    if self.fault == FaultMode.NONE:
                        return finish("failure", f"I could not find the {noun} to {op.lower()}.", "target not found")
                    return finish("success", f"Successfully completed the {op.lower()} of the {noun}.")
                if self.fault == FaultMode.WRONG_RECORD:
                    key = skills.neighbor(key) or key
                if op == "UPDATE":
                    result = skills.update(key, values)
                    shown = plan.target.key if plan.target and plan.target.key else key
                    success_text = f"Successfully updated {noun} {shown}: {_fmt_values(plan.expected_state)}."
                elif op == "DELETE":
                    result = skills.delete(key)
                    shown = plan.target.key if plan.target and plan.target.key else key
                    success_text = f"Successfully deleted {noun} {shown}."
                else:
                    result = skills.read(key)
                    success_text = f"Retrieved {noun} {key}."
                touched = {key}

            if self.fault == FaultMode.SIDE_EFFECT and result.ok:
                skills.collateral(touched)
        except AgentError as exc:
            tracer.error(str(exc))
            return finish("failure", f"I could not complete the task: {exc}", str(exc))

        if result.ok or self.fault != FaultMode.NONE:
            # Faulty agents claim success regardless of what actually happened.
            return finish("success", success_text)
        return finish("failure", f"I could not complete the task: {result.error}", result.error)
