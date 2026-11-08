"""Small query helpers shared by services and API routes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database.models import (
    AppSetting,
    AuditLog,
    EvidenceRecord,
    Execution,
    ExecutionEvent,
    StateSnapshot,
    User,
    VerificationCheckRecord,
    VerificationPlanRecord,
    VerificationReportRecord,
)
from app.utils.timeutil import utcnow


# --------------------------------------------------------------------------
# Settings & audit
# --------------------------------------------------------------------------
def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(AppSetting, key)
    return default if row is None else row.value


def set_setting(session: Session, key: str, value: Any, actor: str = "system") -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_by=actor, updated_at=utcnow()))
    else:
        row.value = value
        row.updated_by = actor
        row.updated_at = utcnow()


def audit(session: Session, actor: str, action: str, detail: str = "") -> None:
    session.add(AuditLog(actor=actor, action=action, detail=detail[:2000]))


def list_audit(session: Session, limit: int = 100) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)))


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def get_user_by_username(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(func.lower(User.username) == username.lower()))


def count_users(session: Session) -> int:
    return session.scalar(select(func.count(User.id))) or 0


def count_active_admins(session: Session) -> int:
    return session.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0


# --------------------------------------------------------------------------
# Executions
# --------------------------------------------------------------------------
def list_executions(
    session: Session,
    *,
    verdicts: list[str] | None = None,
    environment: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Execution], int]:
    stmt = select(Execution)
    if verdicts:
        normalized = [v.upper() for v in verdicts]
        conditions = []
        concrete = [v for v in normalized if v != "PENDING"]
        if concrete:
            conditions.append(Execution.verdict.in_(concrete))
        if "PENDING" in normalized:
            conditions.append(Execution.verdict.is_(None))
        stmt = stmt.where(or_(*conditions))
    if environment:
        stmt = stmt.where(Execution.environment == environment)
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(or_(Execution.id.ilike(like), Execution.task_description.ilike(like)))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(Execution.created_at.desc()).limit(limit).offset(offset))
    return list(rows), total


def get_events(session: Session, execution_id: str) -> list[ExecutionEvent]:
    return list(
        session.scalars(
            select(ExecutionEvent).where(ExecutionEvent.execution_id == execution_id).order_by(ExecutionEvent.sequence)
        )
    )


def get_plan(session: Session, execution_id: str) -> VerificationPlanRecord | None:
    return session.scalar(select(VerificationPlanRecord).where(VerificationPlanRecord.execution_id == execution_id))


def get_snapshot(session: Session, execution_id: str, phase: str) -> StateSnapshot | None:
    return session.scalar(
        select(StateSnapshot).where(StateSnapshot.execution_id == execution_id, StateSnapshot.phase == phase)
    )


def get_report(session: Session, execution_id: str) -> VerificationReportRecord | None:
    return session.scalar(
        select(VerificationReportRecord).where(VerificationReportRecord.execution_id == execution_id)
    )


def get_report_by_id(session: Session, report_id: str) -> VerificationReportRecord | None:
    return session.get(VerificationReportRecord, report_id)


def get_checks(session: Session, execution_id: str) -> list[VerificationCheckRecord]:
    return list(
        session.scalars(
            select(VerificationCheckRecord)
            .where(VerificationCheckRecord.execution_id == execution_id)
            .order_by(VerificationCheckRecord.id)
        )
    )


def get_evidence(session: Session, execution_id: str) -> list[EvidenceRecord]:
    return list(
        session.scalars(
            select(EvidenceRecord).where(EvidenceRecord.execution_id == execution_id).order_by(EvidenceRecord.collected_at)
        )
    )
