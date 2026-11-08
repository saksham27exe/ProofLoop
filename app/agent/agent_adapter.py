"""Adapters that let any agent be verified by ProofLoop.

ProofLoop does not depend on an agent framework. An adapter only has to:

1. run the agent on an instruction, and
2. emit framework-independent events (tool calls, tool responses, errors,
   the final claim) through a :class:`TraceCollector`.

:class:`InstrumentedToolbox` makes step 2 automatic for tool calls: wrap the
environment toolbox and every invocation is recorded. Future adapters for
LangGraph / CrewAI / AutoGen would map those frameworks' callback hooks onto
the same collector methods.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agent.base_agent import AgentRunResult, BaseAgent
from app.collector.trace_collector import TraceCollector
from app.environments.base import Toolbox, ToolResult, ToolSpec


class InstrumentedToolbox(Toolbox):
    """Toolbox wrapper that records every call/response in the trace."""

    def __init__(self, inner: Toolbox, collector: TraceCollector) -> None:
        self._inner = inner
        self._collector = collector

    @property
    def specs(self) -> list[ToolSpec]:
        return self._inner.specs

    def call(self, name: str, args: dict[str, Any], *, inject_failure: bool = False) -> ToolResult:
        call_id = self._collector.new_call_id()
        self._collector.tool_call(call_id, name, args)
        result = self._inner.call(name, args, inject_failure=inject_failure)
        output = dict(result.output)
        if result.error:
            output["error"] = result.error
        self._collector.tool_response(call_id, name, result.status, output, result.status_code)
        return result


AgentFunction = Callable[[str, Toolbox], tuple[str, str]]


class NativePythonAdapter(BaseAgent):
    """Wrap a plain Python function ``fn(instruction, toolbox) -> (status, claim)``.

    The toolbox handed to ``fn`` is instrumented, so tool usage is traced
    without any changes to the agent code.
    """

    framework = "native-python"

    def __init__(self, fn: AgentFunction, toolbox: Toolbox, name: str = "custom-python-agent") -> None:
        self._fn = fn
        self._toolbox = toolbox
        self.name = name

    def run(self, instruction: str, collector: TraceCollector) -> AgentRunResult:
        tools = InstrumentedToolbox(self._toolbox, collector)
        try:
            status, claim = self._fn(instruction, tools)
        except Exception as exc:  # noqa: BLE001 - agent code is untrusted
            collector.error(f"Agent raised {type(exc).__name__}: {exc}")
            collector.claim(f"Agent crashed: {exc}", "failure")
            return AgentRunResult(claimed_status="failure", claim=f"Agent crashed: {exc}", error=str(exc))
        status = "success" if status == "success" else "failure"
        collector.claim(claim, status)
        return AgentRunResult(claimed_status=status, claim=claim)  # type: ignore[arg-type]
