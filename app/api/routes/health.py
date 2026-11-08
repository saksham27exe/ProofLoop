"""Health, LLM status (local or cloud) and local model listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.api.dependencies import AppContext, current_user, get_ctx, get_session
from app.database.models import User
from app.llm.base import LLMUnavailable
from app.llm.model_manager import RECOMMENDED_MODELS
from app.services.runtime_settings import load_runtime
from app.utils.timeutil import iso, utcnow

router = APIRouter(tags=["health"])


@router.get("/health")
def health(ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> dict:
    """Public liveness/readiness probe (contains no sensitive data)."""
    try:
        session.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001
        database = "error"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "version": __version__,
        "time": iso(utcnow()),
        "database": database,
        "environments": ctx.registry.names(),
        "uptime_seconds": int((utcnow() - ctx.started_at).total_seconds()),
    }


@router.get("/health/llm")
def llm_health(ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session),
               _: User = Depends(current_user)) -> dict:
    rt = load_runtime(session, ctx.settings)
    health = ctx.models.health_for_runtime(rt).model_dump()
    health["backend"] = rt.llm_backend
    health["llm_enabled"] = rt.llm_enabled
    health["interpreter_mode"] = rt.interpreter_mode
    health["verification_only_mode"] = (not rt.llm_enabled) or rt.interpreter_mode == "rules" or not health["model_available"]
    return health


@router.get("/models")
def models(ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session),
           _: User = Depends(current_user)) -> dict:
    rt = load_runtime(session, ctx.settings)
    try:
        installed = [m.model_dump() for m in ctx.models.provider(rt.ollama_model).list_models()]
        reachable = True
    except LLMUnavailable:
        installed, reachable = [], False
    return {"reachable": reachable, "selected": rt.ollama_model, "installed": installed, "recommended": RECOMMENDED_MODELS}
