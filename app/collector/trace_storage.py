"""Append-only, hash-chained persistence of execution traces.

Each event's hash covers its content *and* the previous event's hash, so any
retroactive edit, deletion or re-ordering breaks the chain. On top of that,
SQLite triggers reject UPDATE/DELETE on ``execution_events`` entirely.
Corrections must be appended as new ``annotation`` events.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import func, select

from app.collector.event_schema import (
    ExecutionTrace,
    TraceEvent,
    TraceEventIn,
    TraceIntegrity,
    event_hash_payload,
)
from app.database.connection import Database
from app.database.models import ExecutionEvent
from app.utils.hashing import hash_obj, to_jsonable
from app.utils.security import redact
from app.utils.timeutil import utcnow

GENESIS_HASH = "0" * 64


class TraceStorage:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = threading.Lock()

    def append(self, execution_id: str, event: TraceEventIn) -> TraceEvent:
        """Redact, hash-chain and persist one event. Returns the stored event."""
        safe_input = to_jsonable(redact(event.input)) if event.input is not None else None
        safe_output = to_jsonable(redact(event.output)) if event.output is not None else None
        message = redact(event.message or "")
        with self._lock, self._db.session() as session:
            last = session.execute(
                select(ExecutionEvent.sequence, ExecutionEvent.event_hash)
                .where(ExecutionEvent.execution_id == execution_id)
                .order_by(ExecutionEvent.sequence.desc())
                .limit(1)
            ).first()
            sequence = (last[0] + 1) if last else 1
            prev_hash = last[1] if last else GENESIS_HASH
            timestamp = utcnow()
            payload = event_hash_payload(
                execution_id,
                sequence,
                event.event_type.value,
                event.source,
                event.tool_name,
                event.call_id,
                event.status,
                event.status_code,
                safe_input,
                safe_output,
                message,
                timestamp,
            )
            event_hash = hash_obj({"prev": prev_hash, "event": payload})
            row = ExecutionEvent(
                execution_id=execution_id,
                sequence=sequence,
                event_type=event.event_type.value,
                source=event.source,
                tool_name=event.tool_name,
                call_id=event.call_id,
                status=event.status,
                status_code=event.status_code,
                input_metadata=safe_input,
                output_metadata=safe_output,
                message=message,
                timestamp=timestamp,
                prev_hash=prev_hash,
                event_hash=event_hash,
            )
            session.add(row)
        return _to_model(row)

    def count(self, execution_id: str) -> int:
        with self._db.session() as session:
            return session.scalar(
                select(func.count(ExecutionEvent.id)).where(ExecutionEvent.execution_id == execution_id)
            ) or 0

    def load(self, execution_id: str) -> ExecutionTrace:
        with self._db.session() as session:
            rows = list(
                session.scalars(
                    select(ExecutionEvent)
                    .where(ExecutionEvent.execution_id == execution_id)
                    .order_by(ExecutionEvent.sequence)
                )
            )
            events = [_to_model(r) for r in rows]
        return ExecutionTrace(execution_id=execution_id, events=events, integrity=verify_chain(execution_id, events))


def _to_model(row: ExecutionEvent) -> TraceEvent:
    return TraceEvent(
        sequence=row.sequence,
        event_type=row.event_type,
        source=row.source,
        tool_name=row.tool_name,
        call_id=row.call_id,
        status=row.status,
        status_code=row.status_code,
        input=row.input_metadata,
        output=row.output_metadata,
        message=row.message or "",
        timestamp=row.timestamp,
        prev_hash=row.prev_hash,
        event_hash=row.event_hash,
    )


def verify_chain(execution_id: str, events: list[TraceEvent]) -> TraceIntegrity:
    """Recompute every hash and check sequence continuity."""
    prev = GENESIS_HASH
    for expected_seq, event in enumerate(events, start=1):
        if event.sequence != expected_seq:
            return TraceIntegrity(
                valid=False,
                checked_events=len(events),
                first_invalid_sequence=event.sequence,
                detail=f"Sequence gap: expected {expected_seq}, found {event.sequence}",
            )
        if event.prev_hash != prev:
            return TraceIntegrity(
                valid=False, checked_events=len(events), first_invalid_sequence=event.sequence,
                detail="prev_hash does not match the preceding event",
            )
        recomputed = hash_obj({"prev": prev, "event": event.hash_payload(execution_id)})
        if recomputed != event.event_hash:
            return TraceIntegrity(
                valid=False, checked_events=len(events), first_invalid_sequence=event.sequence,
                detail="Event content does not match its recorded hash",
            )
        prev = event.event_hash
    return TraceIntegrity(valid=True, checked_events=len(events), detail="Hash chain intact")


def summarize_event(event: TraceEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "type": event.event_type,
        "tool": event.tool_name,
        "call_id": event.call_id,
        "status": event.status,
    }
