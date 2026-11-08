"""Trace collector: the only channel through which an agent run reaches the
evidence store. It can append events - nothing else.
"""

from __future__ import annotations

import itertools
from typing import Any, Protocol

from app.collector.event_schema import EventType, TraceEventIn
from app.collector.trace_storage import TraceStorage


class EventSink(Protocol):
    def record(self, event: TraceEventIn) -> None: ...


class TraceCollector:
    """Append-only event recorder bound to one execution."""

    def __init__(self, storage: TraceStorage, execution_id: str) -> None:
        self._storage = storage
        self.execution_id = execution_id
        self._counter = itertools.count(1)

    # Generic --------------------------------------------------------------
    def record(self, event: TraceEventIn) -> None:
        self._storage.append(self.execution_id, event)

    def new_call_id(self) -> str:
        return f"call_{next(self._counter)}"

    # Convenience helpers ----------------------------------------------------
    def started(self, message: str, metadata: dict[str, Any] | None = None, source: str = "proofloop") -> None:
        self.record(TraceEventIn(event_type=EventType.EXECUTION_STARTED, source=source, message=message, input=metadata))

    def plan(self, steps: list[str], metadata: dict[str, Any] | None = None) -> None:
        self.record(
            TraceEventIn(event_type=EventType.AGENT_PLAN, message=" -> ".join(steps) if steps else "(no steps)",
                         input={"steps": steps, **(metadata or {})})
        )

    def tool_call(self, call_id: str, tool: str, args: dict[str, Any]) -> None:
        self.record(TraceEventIn(event_type=EventType.TOOL_CALL, tool_name=tool, call_id=call_id, input=args,
                                 message=f"Calling {tool}"))

    def tool_response(
        self, call_id: str, tool: str, status: str, output: Any, status_code: int | None = None, message: str = ""
    ) -> None:
        self.record(
            TraceEventIn(event_type=EventType.TOOL_RESPONSE, tool_name=tool, call_id=call_id, status=status,  # type: ignore[arg-type]
                         status_code=status_code, output=output, message=message or f"{tool} returned {status}")
        )

    def error(self, message: str, call_id: str | None = None, tool: str | None = None, details: Any = None) -> None:
        self.record(TraceEventIn(event_type=EventType.ERROR, status="error", call_id=call_id, tool_name=tool,
                                 message=message, output=details))

    def claim(self, text: str, claimed_status: str) -> None:
        self.record(
            TraceEventIn(event_type=EventType.AGENT_CLAIM, status="success" if claimed_status == "success" else "error",
                         message=text, output={"claimed_status": claimed_status})
        )

    def completed(self, message: str, metadata: dict[str, Any] | None = None, source: str = "proofloop") -> None:
        self.record(TraceEventIn(event_type=EventType.EXECUTION_COMPLETED, source=source, message=message,
                                 output=metadata))

    def annotate(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        self.record(TraceEventIn(event_type=EventType.ANNOTATION, source="proofloop", message=message, input=metadata))


class SuppressingCollector(TraceCollector):
    """Collector that silently drops tool events.

    Used by the fault-injection harness to simulate an agent/integration that
    does not emit execution telemetry ("missing execution evidence").
    """

    def __init__(self, inner: TraceCollector) -> None:
        self._inner = inner
        self.execution_id = inner.execution_id
        self._counter = inner._counter
        self._storage = inner._storage

    def record(self, event: TraceEventIn) -> None:
        if event.event_type in (EventType.TOOL_CALL, EventType.TOOL_RESPONSE, EventType.AGENT_PLAN, EventType.ERROR):
            return
        self._inner.record(event)
