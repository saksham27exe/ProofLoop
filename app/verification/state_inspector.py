"""Independent state capture through an environment's read-only inspector."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.environments.base import Environment, InspectorUnavailable, Snapshot
from app.utils.hashing import hash_obj
from app.utils.timeutil import utcnow


class SnapshotResult(BaseModel):
    phase: str
    available: bool
    state: Snapshot | None = None
    state_hash: str | None = None
    error: str | None = None
    source: str
    resource_type: str
    resource_identifier: str
    captured_at: datetime

    def record_counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in (self.state or {}).items()}


def capture_snapshot(environment: Environment, phase: str) -> SnapshotResult:
    inspector = environment.inspector()
    try:
        state = inspector.snapshot()
        return SnapshotResult(
            phase=phase,
            available=True,
            state=state,
            state_hash=hash_obj(state),
            source=inspector.source,
            resource_type=environment.name,
            resource_identifier=environment.resource_identifier,
            captured_at=utcnow(),
        )
    except InspectorUnavailable as exc:
        return SnapshotResult(
            phase=phase,
            available=False,
            error=str(exc),
            source=inspector.source,
            resource_type=environment.name,
            resource_identifier=environment.resource_identifier,
            captured_at=utcnow(),
        )


def unavailable_snapshot(environment: Environment, phase: str, reason: str) -> SnapshotResult:
    return SnapshotResult(
        phase=phase,
        available=False,
        error=reason,
        source=environment.inspector().source,
        resource_type=environment.name,
        resource_identifier=environment.resource_identifier,
        captured_at=utcnow(),
    )
