"""Scenarios, options, dashboard statistics, sandbox inspection and settings."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.base_agent import AGENT_CONFIGS
from app.agent.fault_injection import FAULTS, applicable_faults
from app.api.dependencies import (
    AppContext,
    current_user,
    get_ctx,
    get_session,
    require_role,
)
from app.api.schemas import ExecutionSummary
from app.database.models import Execution, User
from app.environments.base import InspectorUnavailable
from app.llm.cloud_notice import CLOUD_NOTICE, CLOUD_NOTICE_VERSION
from app.llm.model_manager import RECOMMENDED_CLOUD_MODELS
from app.services.execution_service import load_scenarios
from app.services.runtime_settings import (
    ConsentRequired,
    RuntimeSettingsUpdate,
    cloud_consent,
    load_runtime,
    save_runtime,
)
from app.utils.timeutil import utcnow

router = APIRouter(tags=["platform"])


@router.get("/scenarios")
def scenarios(_: User = Depends(current_user), ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    out = []
    for s in load_scenarios(ctx.settings.sample_data_dir):
        fault = FAULTS.get(s["fault_mode"])  # type: ignore[call-overload]
        out.append({**s, "fault_label": fault.label if fault else s["fault_mode"]})
    return out


@router.get("/options")
def options(_: User = Depends(current_user), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    envs = []
    for env in ctx.registry.all():
        schema = env.schema
        real = not ctx.registry.is_builtin(env.name)
        envs.append({
            "name": env.name,
            "title": schema.title,
            "description": schema.description,
            "real_data": real,
            "entities": [{"name": e.name, "display_name": e.display_name, "fields": e.writable_fields,
                          "key_type": e.key_type} for e in schema.entities],
            "faults_by_operation": {op: (["none"] if real else [f.value for f in applicable_faults(env.name, op)])
                                    for op in ("CREATE", "UPDATE", "DELETE", "READ")},
        })
    return {
        "environments": envs,
        "agents": [a.model_dump() for a in AGENT_CONFIGS.values() if a.interactive],
        "faults": [f.model_dump(mode="json") for f in FAULTS.values()],
    }


@router.get("/stats")
def stats(_: User = Depends(current_user), session: Session = Depends(get_session)) -> dict[str, Any]:
    by_verdict = dict(session.execute(select(Execution.verdict, func.count()).group_by(Execution.verdict)).all())
    total = sum(by_verdict.values())
    verified = by_verdict.get("VERIFIED", 0)
    failed = by_verdict.get("FAILED", 0)
    inconclusive = by_verdict.get("INCONCLUSIVE", 0)
    completed = verified + failed + inconclusive
    false_claims = session.scalar(
        select(func.count()).select_from(Execution).where(Execution.verdict == "FAILED", Execution.agent_claimed_status == "success")
    ) or 0
    claimed_success = session.scalar(
        select(func.count()).select_from(Execution).where(Execution.verdict.is_not(None), Execution.agent_claimed_status == "success")
    ) or 0
    by_env = {env: dict(Counter()) for env in ("database", "api", "file")}
    for env, verdict, n in session.execute(
        select(Execution.environment, Execution.verdict, func.count()).group_by(Execution.environment, Execution.verdict)
    ).all():
        by_env.setdefault(env, {})[verdict or "PENDING"] = n
    since = utcnow() - timedelta(days=13)
    trend: dict[str, dict[str, int]] = {}
    for day_offset in range(14):
        day = (since + timedelta(days=day_offset)).date().isoformat()
        trend[day] = {"VERIFIED": 0, "FAILED": 0, "INCONCLUSIVE": 0}
    for created, verdict in session.execute(
        select(Execution.created_at, Execution.verdict).where(Execution.created_at >= since.replace(hour=0, minute=0, second=0),
                                                              Execution.verdict.is_not(None))
    ).all():
        day = created.date().isoformat()
        if day in trend and verdict in trend[day]:
            trend[day][verdict] += 1
    recent = session.scalars(select(Execution).order_by(Execution.created_at.desc()).limit(8))
    avg_verify = session.scalar(select(func.avg(Execution.verification_ms)).where(Execution.verification_ms.is_not(None)))
    return {
        "total_executions": total,
        "verified": verified,
        "failed": failed,
        "inconclusive": inconclusive,
        "pending": by_verdict.get(None, 0),
        "verification_success_rate": round(verified / completed, 4) if completed else None,
        "false_success_claims_caught": false_claims,
        "claimed_success_total": claimed_success,
        "avg_verification_ms": round(avg_verify, 1) if avg_verify is not None else None,
        "by_environment": by_env,
        "trend": [{"day": d, **v} for d, v in trend.items()],
        "recent": [ExecutionSummary.of(e).model_dump() for e in recent],
    }


@router.get("/sandbox/{env_name}")
def sandbox_state(env_name: str, _: User = Depends(current_user), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    try:
        env = ctx.registry.get(env_name)
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Unknown environment."}) from None
    try:
        state = env.inspector().snapshot()
    except InspectorUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "inspector_unavailable", "message": str(exc)}) from None
    return {"environment": env_name, "title": env.schema.title, "resource": env.resource_identifier,
            "collections": {k: list(v.values()) for k, v in state.items()}}


@router.post("/sandbox/{env_name}/reset")
def reset_sandbox(env_name: str, user: User = Depends(require_role("analyst")), ctx: AppContext = Depends(get_ctx)) -> dict:
    if env_name not in ctx.registry.names():
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Unknown environment."})
    ctx.executions.reset_sandbox(env_name, user.username)
    return {"status": "reset", "environment": env_name}


@router.get("/settings")
def get_settings_view(_: User = Depends(current_user), ctx: AppContext = Depends(get_ctx),
                      session: Session = Depends(get_session)) -> dict[str, Any]:
    rt = load_runtime(session, ctx.settings)
    cloud_on = rt.llm_backend == "cloud"
    return {
        "runtime": rt.model_dump(),
        "config": ctx.settings.public_view(),
        "cloud": {
            "active": cloud_on,
            "endpoint": ctx.settings.cloud_base_url,
            "api_key_configured": ctx.settings.public_view()["cloud_api_key_configured"],
            "notice": {"version": CLOUD_NOTICE_VERSION, "text": CLOUD_NOTICE},
            "consent": cloud_consent(session),
            "recommended_models": RECOMMENDED_CLOUD_MODELS,
        },
        "privacy": {
            "external_llm_apis": (f"ACTIVE - cloud model at {ctx.settings.cloud_base_url}; task data leaves this machine"
                                  if cloud_on else "off (local Ollama only); cloud model available as an opt-in"),
            "telemetry": "none implemented (Streamlit usage stats disabled in .streamlit/config.toml)",
            "network_bindings": "API and dashboard bind to 127.0.0.1 only",
            "ollama_endpoint_restricted_to_loopback": not ctx.settings.allow_remote_ollama,
            "proxy_environment_ignored_for_llm_calls": True,
            "data_location": str(ctx.settings.data_dir),
        },
    }


@router.put("/settings")
def update_settings(body: RuntimeSettingsUpdate, admin: User = Depends(require_role("admin")),
                    ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        changed = save_runtime(session, body, admin.username, ctx.settings)
    except ConsentRequired as err:
        raise HTTPException(status_code=409, detail={"code": err.code, "message": err.message}) from None
    session.commit()
    return {"changed": changed, "runtime": load_runtime(session, ctx.settings).model_dump()}
