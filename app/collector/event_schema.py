"""Framework-independent execution event format.

Any agent framework (native Python, LangGraph, CrewAI, AutoGen, ...) can be
integrated by emitting these events through a :class:`TraceCollector`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class EventType(str, Enum):
    EXECUTION_STARTED = "execution_started"
    AGENT_PLAN = "agent_plan"
    TOOL_CALL = "tool_call"
    TOOL_RESPONSE = "tool_response"
    ERROR = "error"
    AGENT_CLAIM = "agent_claim"
    EXECUTION_COMPLETED = "execution_completed"
    ANNOTATION = "annotation"


TOOL_EVENT_TYPES = {EventType.TOOL_CALL.value, EventType.TOOL_RESPONSE.value}


class TraceEventIn(BaseModel):
    """An event as submitted by an agent adapter (before hashing/storage)."""

    event_type: EventType
    source: Literal["agent", "proofloop", "adapter"] = "agent"
    tool_name: str | None = None
    call_id: str | None = None
    status: Literal["success", "error", "info"] | None = None
    status_code: int | None = None
    input: Any = None
    output: Any = None
    message: str = ""


class TraceEvent(BaseModel):
    """An accepted, immutable, hash-chained trace event."""

    sequence: int
    event_type: str
    source: str
    tool_name: str | None = None
    call_id: str | None = None
    status: str | None = None
    status_code: int | None = None
    input: Any = None
    output: Any = None
    message: str = ""
    timestamp: datetime
    prev_hash: str
    event_hash: str

    def hash_payload(self, execution_id: str) -> dict[str, Any]:
        return event_hash_payload(
            execution_id,
            self.sequence,
            self.event_type,
            self.source,
            self.tool_name,
            self.call_id,
            self.status,
            self.status_code,
            self.input,
            self.output,
            self.message,
            self.timestamp,
        )


def event_hash_payload(
    execution_id: str,
    sequence: int,
    event_type: str,
    source: str,
    tool_name: str | None,
    call_id: str | None,
    status: str | None,
    status_code: int | None,
    input_: Any,
    output: Any,
    message: str,
    timestamp: datetime,
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "sequence": sequence,
        "event_type": event_type,
        "source": source,
        "tool_name": tool_name,
        "call_id": call_id,
        "status": status,
        "status_code": status_code,
        "input": input_,
        "output": output,
        "message": message,
        "timestamp": timestamp.isoformat(timespec="microseconds"),
    }


class TraceIntegrity(BaseModel):
    valid: bool
    checked_events: int
    first_invalid_sequence: int | None = None
    detail: str = ""


class ExecutionTrace(BaseModel):
    execution_id: str
    events: list[TraceEvent] = Field(default_factory=list)
    integrity: TraceIntegrity
