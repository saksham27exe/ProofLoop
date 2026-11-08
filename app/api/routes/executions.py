"""Execution lifecycle endpoints: create (interpret), run, traces."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agent.fault_injection import FAULTS, FaultMode
from app.api.dependencies import (
    AppContext,
    current_user,
    get_ctx,
    get_session,
    require_role,
)
from app.api.schemas import (
    ExecutionCreate,
    ExecutionDetail,
    ExecutionList,
    ExecutionSummary,
    RunRequest,
)
from app.collector.trace_storage import TraceStorage
from app.database import repositories as repo
from app.database.models import Execution, User
from app.services.execution_service import ExecutionError, load_scenarios
from app.services.external_sessions import EventsRequest, SessionFinish, SessionStart, ToolCallRequest
from app.utils.timeutil import iso

router = APIRouter(prefix="/executions", tags=["executions"])


def http_error(err: ExecutionError) -> HTTPException:
    return HTTPException(status_code=err.status_code, detail={"code": err.code, "message": err.message})


def build_detail(ctx: AppContext, session: Session, ex: Execution, include_report: bool = False) -> ExecutionDetail:
    plan = repo.get_plan(session, ex.id)
    report = repo.get_report(session, ex.id)
    scenario = next((s for s in load_scenarios(ctx.settings.sample_data_dir) if s["id"] == ex.scenario_id), None)
    fault = FAULTS.get(FaultMode(ex.fault_mode)) if ex.fault_mode in FaultMode._value2member_map_ else None
    base = ExecutionSummary.of(ex).model_dump()
    real = not ctx.registry.is_builtin(ex.environment)
    backup = None
    bk = ctx.sources.backup_for(ex.id) if real else None
    if bk is not None:
        latest = ctx.sources.latest_backup(bk.environment)
        backup = {"created_at": iso(bk.created_at), "size_bytes": bk.size_bytes, "restored_at": iso(bk.restored_at),
                  "restored_by": bk.restored_by,
                  "can_undo": bk.restored_at is None and latest is not None and latest.execution_id == ex.id}
    return ExecutionDetail(
        real_data=real,
        backup=backup,
        **base,
        agent_framework=ex.agent_framework,
        agent_claim=ex.agent_claim,
        agent_error=ex.agent_error,
        approved_by=ex.approved_by,
        plan=plan.plan_data if plan else None,
        durations_ms={"interpretation": ex.interpretation_ms, "execution": ex.execution_ms, "verification": ex.verification_ms},
        started_at=iso(ex.started_at),
        completed_at=iso(ex.completed_at),
        report_id=report.id if report else None,
        harness={
            "note": ("Real run on connected data: no fault was injected; the agent ran exactly as it is." if real else
                     "Test-harness metadata. It is never passed to the verification engine."),
            "fault_mode": ex.fault_mode,
            "fault_label": fault.label if fault else ex.fault_mode,
            "fault_description": fault.description if fault else "",
            "scenario_id": ex.scenario_id,
            "scenario_title": scenario["title"] if scenario else None,
            # Real runs have no expected verdict - nobody knows in advance what a real agent will do.
            "expected_verdict": None if real else (
                scenario["expected_verdict"] if scenario else (fault.expected_verdict if fault else None)),
        },
        report=report.report_data if (include_report and report) else None,
    )


def _get(session: Session, exec_id: str) -> Execution:
    ex = session.get(Execution, exec_id)
    if ex is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Execution not found."})
    return ex


@router.post("", response_model=ExecutionDetail, status_code=201)
def create_execution(body: ExecutionCreate, user: User = Depends(require_role("analyst")), ctx: AppContext = Depends(get_ctx),
                     session: Session = Depends(get_session)) -> ExecutionDetail:
    """Create an execution and interpret the task into a verification plan (the agent does not run yet)."""
    try:
        exec_id = ctx.executions.create_execution(
            instruction=body.instruction, environment=body.environment, agent_config=body.agent_config,
            fault_mode=body.fault_mode, scenario_id=body.scenario_id, reset_sandbox=body.reset_sandbox, user=user,
        )
    except ExecutionError as err:
        raise http_error(err) from None
    session.expire_all()
    return build_detail(ctx, session, _get(session, exec_id))


@router.get("", response_model=ExecutionList)
def list_executions(
    verdict: list[str] | None = Query(None, description="Filter: VERIFIED, FAILED, INCONCLUSIVE, PENDING"),
    environment: str | None = None,
    q: str | None = Query(None, max_length=100, description="Search execution id or instruction text"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> ExecutionList:
    rows, total = repo.list_executions(session, verdicts=verdict, environment=environment, query=q, limit=limit, offset=offset)
    return ExecutionList(total=total, items=[ExecutionSummary.of(e) for e in rows])


@router.get("/{exec_id}", response_model=ExecutionDetail)
def get_execution(exec_id: str, include_report: bool = False, _: User = Depends(current_user),
                  ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> ExecutionDetail:
    return build_detail(ctx, session, _get(session, exec_id), include_report=include_report)


@router.get("/{exec_id}/traces")
def get_traces(exec_id: str, _: User = Depends(current_user), ctx: AppContext = Depends(get_ctx),
               session: Session = Depends(get_session)) -> dict[str, Any]:
    _get(session, exec_id)
    trace = TraceStorage(ctx.db).load(exec_id)
    return {
        "execution_id": exec_id,
        "integrity": trace.integrity.model_dump(),
        "events": [
            {**e.model_dump(mode="json"), "timestamp": iso(e.timestamp)} for e in trace.events
        ],
    }


@router.get("/{exec_id}/snapshots")
def get_snapshots(exec_id: str, include_state: bool = False, _: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    _get(session, exec_id)
    out: dict[str, Any] = {}
    for phase in ("before", "after"):
        snap = repo.get_snapshot(session, exec_id, phase)
        if snap is None:
            out[phase] = None
            continue
        out[phase] = {"available": snap.available, "error": snap.error, "state_hash": snap.state_hash,
                      "source": snap.source, "captured_at": iso(snap.captured_at),
                      "record_counts": {k: len(v) for k, v in (snap.state or {}).items()}}
        if include_state:
            out[phase]["state"] = snap.state
    return out


@router.post("/{exec_id}/run", response_model=ExecutionDetail)
def run_execution(exec_id: str, body: RunRequest, user: User = Depends(require_role("analyst")),
                  ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> ExecutionDetail:
    """Run the sample agent. Destructive plans require ``approved=true``; ambiguous plans require ``force=true``."""
    try:
        ctx.executions.run_execution(exec_id, approved=body.approved, force=body.force, user=user)
        if body.verify:
            ctx.executions.verify_execution(exec_id, user=user)
    except ExecutionError as err:
        raise http_error(err) from None
    session.expire_all()
    return build_detail(ctx, session, _get(session, exec_id), include_report=body.verify)


@router.post("/{exec_id}/undo", response_model=ExecutionDetail)
def undo_execution(exec_id: str, user: User = Depends(require_role("analyst")), ctx: AppContext = Depends(get_ctx),
                   session: Session = Depends(get_session)) -> ExecutionDetail:
    """Restore a connected data source to the backup taken right before this run (most recent run only)."""
    # Do not touch the request session before the service call: an open SQLite transaction here would block the
    # service's own writes ("database is locked"). The service reports unknown executions itself.
    try:
        ctx.executions.undo_execution(exec_id, user=user)
    except ExecutionError as err:
        raise http_error(err) from None
    session.expire_all()
    return build_detail(ctx, session, _get(session, exec_id))


@router.post("/{exec_id}/verify", tags=["verification"])
def verify_execution(exec_id: str, user: User = Depends(require_role("analyst")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Independently verify a completed execution and return the immutable report."""
    try:
        return ctx.executions.verify_execution(exec_id, user=user)
    except ExecutionError as err:
        raise http_error(err) from None


@router.get("/{exec_id}/report", tags=["reports"])
def get_report(exec_id: str, _: User = Depends(current_user), session: Session = Depends(get_session)) -> dict[str, Any]:
    _get(session, exec_id)
    report = repo.get_report(session, exec_id)
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "not_verified", "message": "This execution has not been verified yet."})
    return report.report_data


# ------------------------------------------------------------------ your own agent (verification sessions)
@router.post("/{exec_id}/session/start", tags=["external agents"])
def start_session(exec_id: str, body: SessionStart, user: User = Depends(require_role("analyst")),
                  ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Start verifying your own agent: backup + independent before-snapshot; returns the tools it may call."""
    try:
        return ctx.sessions.start(exec_id, body, user=user)
    except ExecutionError as err:
        raise http_error(err) from None


@router.get("/{exec_id}/session/tools", tags=["external agents"])
def session_tools(exec_id: str, _: User = Depends(current_user), ctx: AppContext = Depends(get_ctx),
                  session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """OpenAI-style function definitions of the tools available in this session."""
    return ctx.sessions.tool_specs(_get(session, exec_id).environment)


@router.post("/{exec_id}/session/tools/{tool}", tags=["external agents"])
def session_call_tool(exec_id: str, tool: str, body: ToolCallRequest, _: User = Depends(require_role("analyst")),
                      ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Run one tool for the agent. Checked against the write policy and recorded in the hash-chained trace."""
    try:
        return ctx.sessions.call_tool(exec_id, tool, body.arguments)
    except ExecutionError as err:
        raise http_error(err) from None


@router.post("/{exec_id}/session/events", tags=["external agents"])
def session_events(exec_id: str, body: EventsRequest, _: User = Depends(require_role("analyst")),
                   ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Report tool calls the agent made itself (outside ProofLoop's tools), or notes."""
    try:
        return ctx.sessions.record_events(exec_id, body.events)
    except ExecutionError as err:
        raise http_error(err) from None


@router.post("/{exec_id}/session/finish", tags=["external agents"])
def finish_session(exec_id: str, body: SessionFinish, user: User = Depends(require_role("analyst")),
                   ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Record the agent's claim, take the after-snapshot and verify. Returns the immutable report."""
    try:
        return ctx.sessions.finish(exec_id, body, user=user)
    except ExecutionError as err:
        raise http_error(err) from None


@router.post("/{exec_id}/session/abort", tags=["external agents"])
def abort_session(exec_id: str, user: User = Depends(require_role("analyst")), ctx: AppContext = Depends(get_ctx),
                  reason: str = Query("", max_length=300)) -> dict[str, Any]:
    """Abandon the session without verifying (the backup stays available for 'Undo run')."""
    try:
        return ctx.sessions.abort(exec_id, user=user, reason=reason)
    except ExecutionError as err:
        raise http_error(err) from None
