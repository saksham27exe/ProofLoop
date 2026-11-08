"""A real tool-calling AI agent running on the selected model (local Ollama by default, or the opt-in cloud model).

Nothing is scripted: the model receives the task and the data source's tools,
decides which tools to call (exploring, then changing data), sees each result
and finally reports what it did. Every tool call goes through an
:class:`InstrumentedToolbox`, so ProofLoop records it in the hash-chained trace -
but the verifier still judges the outcome only from independent snapshots.

The agent is deliberately *not* given ProofLoop's verification plan: it works
from the user's words, exactly like any third-party agent would.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.agent_adapter import InstrumentedToolbox
from app.agent.base_agent import AgentRunResult, BaseAgent
from app.collector.trace_collector import TraceCollector
from app.environments.base import Environment, Toolbox
from app.llm.base import LLMError, LLMProvider

MAX_STEPS = 10
MAX_RESULT_CHARS = 3500

SYSTEM = """You are an operations agent that completes tasks on a real data source by calling tools.

Rules:
- Use the tools to look up the exact records before changing anything. Never guess ids or values.
- A number right after a record type ("customer 5", "order #12") is that record's key, e.g. where {"id": 5}.
- Change exactly what the task asks for - no other records and no other fields.
- If the task is ambiguous (for example several records match a name) or impossible, change nothing and explain.
- Call one tool at a time and read each result before deciding the next step.
- When you are finished, reply WITHOUT a tool call. Start that final reply with "DONE:" if you completed the task
  or "FAILED:" if you could not, followed by one or two sentences saying exactly what you changed.

{context}"""


def _context(env: Environment) -> str:
    schema = env.schema
    lines = [f"Data source: {schema.title}"]
    for e in schema.entities:
        if e.key_type == "path":
            lines.append("It is a folder of files; use list_files / read_file / write_file / delete_file with relative paths.")
            continue
        cols = []
        for f in e.fields:
            desc = f.name
            if f.enum_values:
                desc += " (" + "|".join(f.enum_values) + ")"
            cols.append(desc)
        lines.append(f"- table {e.collection} (key {e.key_field}): {', '.join(cols)}")
    return "\n".join(lines)


def _tool_defs(env: Environment) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in env.toolbox().specs]


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}
    return {}


_INLINE_CALL = re.compile(r"\{\s*\"name\"\s*:\s*\"(\w+)\"\s*,\s*\"(?:arguments|parameters)\"\s*:\s*(\{.*\})\s*\}", re.S)


def _inline_tool_call(content: str, names: set[str]) -> tuple[str, dict[str, Any]] | None:
    """Small models sometimes print the tool call as JSON text instead of using the tool-call channel."""
    m = _INLINE_CALL.search(content or "")
    if not m or m.group(1) not in names:
        return None
    return m.group(1), _parse_args(m.group(2))


def _text_mode_nudge(env: Environment) -> dict[str, str]:
    sigs = "; ".join(f"{t.name}({', '.join(t.parameters.get('properties', {}))})" for t in env.toolbox().specs)
    return {"role": "user", "content": (
        "Write your next tool call as one JSON object, exactly like "
        '{"name": "find_rows", "arguments": {"table": "customers", "where": {"id": 5}}} '
        f"- or give your final DONE:/FAILED: report. Tools: {sigs}")}


def claimed_status(text: str) -> str:
    t = (text or "").strip()
    if re.match(r"^\W*done\b", t, re.I):
        return "success"
    if re.match(r"^\W*failed\b", t, re.I):
        return "failure"
    if re.search(r"\b(could not|couldn't|unable|cannot|can't|failed|no (?:record|row|customer|order)s? (?:found|match))\b", t, re.I):
        return "failure"
    return "success"


class ToolCallingAgent(BaseAgent):
    name = "proofloop-ai-agent"
    framework = "ollama-tool-calling"

    def __init__(self, env: Environment, provider: LLMProvider, max_steps: int = MAX_STEPS,
                 toolbox: Toolbox | None = None) -> None:
        self.env = env
        self._toolbox = toolbox  # e.g. a PolicyToolbox; defaults to the environment's own toolbox
        self.provider = provider
        self.max_steps = max_steps
        self.backend = getattr(provider, "name", "ollama")
        self.framework = "cloud-tool-calling" if self.backend == "cloud" else "ollama-tool-calling"

    def run(self, instruction: str, collector: TraceCollector) -> AgentRunResult:
        tools = InstrumentedToolbox(self._toolbox or self.env.toolbox(), collector)
        tool_defs = _tool_defs(self.env)
        names = {d["function"]["name"] for d in tool_defs}
        messages: list[dict[str, Any]] = [
            # str.replace, not str.format: the prompt contains literal JSON braces.
            {"role": "system", "content": SYSTEM.replace("{context}", _context(self.env))},
            {"role": "user", "content": instruction},
        ]
        collector.plan([f"Tool-calling agent on {self.provider.model} ({self.backend})", f"Up to {self.max_steps} steps",
                        "Tools: " + ", ".join(sorted(names))], {"model": self.provider.model, "backend": self.backend})
        calls = 0
        empty_replies = 0
        seen: dict[str, int] = {}
        for step in range(1, self.max_steps + 1):
            try:
                message, _model = self.provider.chat_with_tools(messages, tool_defs)
            except LLMError as exc:
                collector.error(f"Model error at step {step}: {exc}")
                text = f"FAILED: the model stopped responding ({exc})."
                collector.claim(text, "failure")
                return AgentRunResult(claimed_status="failure", claim=text, error=str(exc), tool_calls=calls,
                                      planner="tool_calling")
            content = (message.get("content") or "").strip()
            requested = [(c.get("function") or {}) for c in (message.get("tool_calls") or [])]
            pending = [(r.get("name"), _parse_args(r.get("arguments"))) for r in requested if r.get("name")]
            if not pending:
                inline = _inline_tool_call(content, names)
                if inline:
                    pending = [inline]
                    content = ""
            if not pending and not content and empty_replies < 2:
                # Observed with qwen2.5:3b: the model *did* generate a tool call, but the runtime could not parse it
                # and returned an empty message - and the identical request repeats that. Ask once without the native
                # tool channel: the model then writes the call as JSON text, which _inline_tool_call parses.
                empty_replies += 1
                collector.annotate(f"Empty model reply at step {step} (unparsed tool call); asking for a text-mode call.")
                try:
                    fallback, _model = self.provider.chat_with_tools(messages + [_text_mode_nudge(self.env)], [])
                except LLMError as exc:
                    collector.error(f"Model error at step {step}: {exc}")
                    continue
                content = (fallback.get("content") or "").strip()
                inline = _inline_tool_call(content, names)
                if inline:
                    pending, content = [inline], ""
                elif not content:
                    continue
            if not pending:
                text = content or "FAILED: the agent ended without a final report."
                status = claimed_status(text)
                collector.claim(text, status)
                return AgentRunResult(claimed_status=status, claim=text, tool_calls=calls, planner="tool_calling")  # type: ignore[arg-type]
            if content:
                collector.annotate(f"Agent reasoning (step {step}): {content[:600]}")
            messages.append({"role": "assistant", "content": content,
                             "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in pending]})
            for tool_name, args in pending:
                signature = f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"
                seen[signature] = seen.get(signature, 0) + 1
                if tool_name not in names:
                    result_text = json.dumps({"error": f"Unknown tool {tool_name!r}. Available: {sorted(names)}"})
                elif seen[signature] > 2:
                    result_text = json.dumps({"error": "You already made this exact call twice. Use the earlier result "
                                                       "or give your final DONE:/FAILED: report."})
                else:
                    calls += 1
                    result = tools.call(tool_name, args)
                    payload = result.output if result.ok else {"error": result.error, **result.output}
                    result_text = json.dumps(payload, default=str)
                    if len(result_text) > MAX_RESULT_CHARS:
                        result_text = result_text[:MAX_RESULT_CHARS] + '... (truncated; narrow your query)"'
                messages.append({"role": "tool", "content": result_text, "tool_name": tool_name})
        text = f"FAILED: stopped after {self.max_steps} steps without finishing."
        collector.error(text)
        collector.claim(text, "failure")
        return AgentRunResult(claimed_status="failure", claim=text, tool_calls=calls, planner="tool_calling")
