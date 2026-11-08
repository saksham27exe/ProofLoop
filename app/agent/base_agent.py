"""Agent abstractions shared by the sample agent and external adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel

from app.collector.trace_collector import TraceCollector


class AgentRunResult(BaseModel):
    claimed_status: Literal["success", "failure"]
    claim: str
    error: str | None = None
    tool_calls: int = 0
    planner: str = ""


class AgentConfig(BaseModel):
    id: str
    name: str
    description: str
    planner: Literal["rules", "llm", "tool_calling"]
    requires_llm: bool = False
    # "sandbox": built-in demo environments only; "sources": connected data sources only.
    environments: Literal["sandbox", "sources"] = "sandbox"


AGENT_CONFIGS: dict[str, AgentConfig] = {
    "scripted": AgentConfig(
        id="scripted",
        name="Scripted agent",
        description="Deterministic planner (rule-based). Works offline; ideal for reproducible fault-injection demos.",
        planner="rules",
    ),
    "llm": AgentConfig(
        id="llm",
        name="Local-LLM agent",
        description="Plans its action with the local Ollama model, then calls sandbox tools.",
        planner="llm",
        requires_llm=True,
    ),
    "ai_agent": AgentConfig(
        id="ai_agent",
        name="AI agent (real tool use)",
        description="A genuine tool-calling agent on the selected model (local by default): it explores the data, decides its own steps, "
                    "makes the changes and reports back. Nothing is scripted or injected.",
        planner="tool_calling",
        requires_llm=True,
        environments="sources",
    ),
}


class BaseAgent(ABC):
    name: str = "agent"
    framework: str = "native-python"

    @abstractmethod
    def run(self, instruction: str, collector: TraceCollector) -> AgentRunResult:
        """Execute ``instruction``, emitting trace events through ``collector``."""
