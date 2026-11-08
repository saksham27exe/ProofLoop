"""SQLAlchemy ORM models for ProofLoop's own evidence store (``data/proofloop.db``).

This database is *separate* from the sandbox environments that agents act on.
The sample agent never receives a handle to it: it can only append trace
events through the :class:`~app.collector.trace_collector.TraceCollector`.

Evidence-bearing tables (events, snapshots, checks, evidence, reports) are
append-only: SQLite triggers installed by :mod:`app.database.connection`
reject UPDATE and DELETE statements against them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils.timeutil import utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------------------------
# Identity & access
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="analyst")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sessions: Mapped[list[AuthSession]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditLog(Base):
    """Security-relevant events (logins, user management, settings changes)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")


# --------------------------------------------------------------------------
# Connected data sources (real data) & run backups
# --------------------------------------------------------------------------
class DataSource(Base):
    """A user-connected data source (SQLite database or folder)."""

    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))
    location: Mapped[str] = mapped_column(Text)
    excluded: Mapped[list[Any]] = mapped_column(JSON, default=list)
    exclusive: Mapped[bool] = mapped_column(Boolean, default=True)
    max_rows_changed: Mapped[int] = mapped_column(Integer, default=200)
    max_snapshot_rows: Mapped[int] = mapped_column(Integer, default=50_000)
    is_practice: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunBackup(Base):
    """Copy of a connected source taken right before an agent run (enables 'Undo run')."""

    __tablename__ = "run_backups"

    execution_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), index=True)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restored_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --------------------------------------------------------------------------
# Executions & evidence
# --------------------------------------------------------------------------
class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_description: Mapped[str] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(String(32), index=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    agent_framework: Mapped[str] = mapped_column(String(64), default="native-python")
    agent_config: Mapped[str] = mapped_column(String(32), default="scripted")
    # Fault injection is a property of the *test harness*. The verification
    # engine never reads this column (enforced by tests).
    fault_mode: Mapped[str] = mapped_column(String(48), default="none")
    scenario_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    execution_status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    interpreter: Mapped[str] = mapped_column(String(64), default="rules")
    agent_claim: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_claimed_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    agent_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    env_version_after_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    interpretation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verification_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    events: Mapped[list[ExecutionEvent]] = relationship(
        back_populates="execution", order_by="ExecutionEvent.sequence", passive_deletes=True
    )
    plan: Mapped[VerificationPlanRecord | None] = relationship(back_populates="execution", uselist=False)
    report: Mapped[VerificationReportRecord | None] = relationship(back_populates="execution", uselist=False)
    creator: Mapped[User | None] = relationship()


class ExecutionEvent(Base):
    """One immutable, hash-chained entry of an execution trace."""

    __tablename__ = "execution_events"
    __table_args__ = (UniqueConstraint("execution_id", "sequence", name="uq_event_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32), default="agent")
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    call_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_metadata: Mapped[Any] = mapped_column(JSON, nullable=True)
    output_metadata: Mapped[Any] = mapped_column(JSON, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))

    execution: Mapped[Execution] = relationship(back_populates="events")


class VerificationPlanRecord(Base):
    __tablename__ = "verification_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), unique=True)
    environment: Mapped[str] = mapped_column(String(32))
    entity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_operation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target: Mapped[Any] = mapped_column(JSON, nullable=True)
    expected_state: Mapped[Any] = mapped_column(JSON, default=dict)
    verification_conditions: Mapped[Any] = mapped_column(JSON, default=list)
    constraints: Mapped[Any] = mapped_column(JSON, default=dict)
    ambiguities: Mapped[Any] = mapped_column(JSON, default=list)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    interpreter: Mapped[str] = mapped_column(String(64), default="rules")
    plan_data: Mapped[Any] = mapped_column(JSON)
    plan_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    execution: Mapped[Execution] = relationship(back_populates="plan")


class StateSnapshot(Base):
    __tablename__ = "state_snapshots"
    __table_args__ = (UniqueConstraint("execution_id", "phase", name="uq_snapshot_phase"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), index=True)
    phase: Mapped[str] = mapped_column(String(8))  # "before" | "after"
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_identifier: Mapped[str] = mapped_column(String(256))
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[Any] = mapped_column(JSON, nullable=True)
    state_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VerificationCheckRecord(Base):
    __tablename__ = "verification_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), index=True)
    check_id: Mapped[str] = mapped_column(String(80))
    check_name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(24))
    decisive: Mapped[bool] = mapped_column(Boolean, default=True)
    expected_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    actual_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    explanation: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[Any] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EvidenceRecord(Base):
    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_exec_type", "execution_id", "evidence_type"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), index=True)
    evidence_type: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(80))
    resource: Mapped[str] = mapped_column(String(256), default="")
    result: Mapped[str] = mapped_column(String(24), default="neutral")
    description: Mapped[str] = mapped_column(Text, default="")
    evidence_data: Mapped[Any] = mapped_column(JSON, nullable=True)
    integrity_hash: Mapped[str] = mapped_column(String(64))
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VerificationReportRecord(Base):
    __tablename__ = "verification_reports"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), unique=True)
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    summary: Mapped[str] = mapped_column(Text)
    report_data: Mapped[Any] = mapped_column(JSON)
    report_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    execution: Mapped[Execution] = relationship(back_populates="report")


IMMUTABLE_TABLES = (
    "execution_events",
    "state_snapshots",
    "verification_checks",
    "evidence",
    "verification_reports",
    "verification_plans",
)
